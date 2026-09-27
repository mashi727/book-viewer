"""本棚パネル: Windows エクスプローラーのナビゲーションウィンドウ風のフォルダツリー。

  ..                 （起動フォルダを 1 つ上へ付け替える）
  📁 <起動フォルダ>
  🏠 ホーム
  💻 この Mac（Windows では「PC」）
      Macintosh HD / 外付けドライブ / ネットワークドライブ（/Volumes。Windows では C: などのドライブ）

最上位が複数あるので QFileSystemModel（根は 1 つ）ではなく QTreeWidget で組み、
フォルダは展開された時点で中身を読む（遅延読み込み）。アイコンは
QFileIconProvider から取るので Finder と同じ（ドライブの種類ごとに違う）。

  - PDF 以外のファイルと隠しファイル（名前が . 始まり、または Finder と同じ
    UF_HIDDEN フラグ付き。/bin・/usr・~/Library など）は出さない
  - 展開済みのフォルダは QFileSystemWatcher で見張り、追加・削除を反映する
  - /Volumes も見張り、ドライブの抜き差しを「この Mac」に反映する
  - 2 列目は読書の進捗（%）。PDF にマウスを乗せると表紙のサムネイルを出す
"""
from __future__ import annotations

import hashlib
import html
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QCollator,
    QEvent,
    QFileInfo,
    QFileSystemWatcher,
    QObject,
    QRunnable,
    QSize,
    QStorageInfo,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont, QHelpEvent, QIcon, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .state import cache_dir

_THUMB_HEIGHT = 240          # ツールチップのサムネイル高さ (px)
_THUMB_DIR = cache_dir() / "thumbs"
_COMPUTER_LABEL = "PC" if sys.platform == "win32" else "この Mac"
_PATH_ROLE = Qt.ItemDataRole.UserRole          # 項目のパス（str）。「この Mac」は None
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1    # フォルダの中身を読み込み済みか
_PLACEHOLDER = "…"                             # 未読込フォルダに ▸ を出すための仮の子

# macOS keeps system snapshots / helper volumes under /Volumes as well.
_HIDDEN_VOLUME_NAMES = {"Recovery", "Preboot", "VM", "Update", "xarts", "iSCPreboot", "Hardware"}
_NETWORK_FS = {"smbfs", "afpfs", "nfs", "webdav", "cifs", "smb3", "fuse.sshfs"}


def is_hidden(st: os.stat_result) -> bool:
    """Finder / エクスプローラーと同じく隠しファイルか（名前の . 始まりは呼び出し側で見る）。

    macOS は UF_HIDDEN フラグ（/bin・/usr・~/Library など）、Windows は隠し属性。
    どちらの属性も、無い OS の stat_result には存在しない。
    """
    if getattr(st, "st_flags", 0) & stat.UF_HIDDEN:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)


def mounted_volumes() -> list[tuple[str, Path]]:
    """「この Mac」に並べるボリューム ``[(ラベル, ルート)]``。起動ディスクが先頭。"""
    boot: list[tuple[str, Path]] = []
    others: list[tuple[str, Path]] = []
    for info in QStorageInfo.mountedVolumes():
        if not (info.isValid() and info.isReady()):
            continue
        root = Path(info.rootPath())
        if sys.platform == "darwin":
            if root != Path("/") and (root.parent != Path("/Volumes") or root.name in _HIDDEN_VOLUME_NAMES):
                continue
        elif sys.platform != "win32":
            if root != Path("/") and not str(root).startswith(("/media/", "/mnt/", "/run/media/")):
                continue
        name = info.displayName() or root.name or str(root)
        fs = bytes(info.fileSystemType().data()).decode(errors="ignore").lower()
        if fs in _NETWORK_FS:
            # Windows の「Public (\\Drobo5N-02)」と同じく、接続先を添える
            device = bytes(info.device().data()).decode(errors="ignore")
            name += f" ({device.lstrip('/')})"
        (boot if root == Path("/") else others).append((name, root))
    return boot + sorted(others, key=lambda v: v[0].lower())


# ---- サムネイル（別スレッドでレンダリングし、ディスクにキャッシュ） ----

def thumb_cache_path(pdf: str) -> Path | None:
    try:
        st = Path(pdf).stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{pdf}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
    return _THUMB_DIR / f"{key}.png"


class _ThumbSignals(QObject):
    done = Signal(str)


class _ThumbJob(QRunnable):
    def __init__(self, path: str, signals: _ThumbSignals):
        super().__init__()
        self._path = path
        self._signals = signals

    def run(self) -> None:
        cache = thumb_cache_path(self._path)
        if cache is None or cache.exists():
            return
        doc = QPdfDocument()
        if doc.load(self._path) == QPdfDocument.Error.None_ and doc.pageCount() > 0:
            s = doc.pagePointSize(0)
            h = _THUMB_HEIGHT
            w = max(1, round(h * s.width() / s.height())) if s.height() > 0 else h
            page = doc.render(0, QSize(w, h))
            # 透明な背景（TeX の出力など）に紙の白を敷く
            img = QImage(page.size(), QImage.Format.Format_RGB32)
            img.fill(Qt.GlobalColor.white)
            painter = QPainter(img)
            painter.drawImage(0, 0, page)
            painter.end()
            _THUMB_DIR.mkdir(parents=True, exist_ok=True)
            # 一時ファイルに書き終えてから名前を付け替える。直接書くと、書き込み中に
            # ツールチップが書きかけの PNG を読み「libpng error: Read Error」になる
            tmp = cache.with_name(f".{cache.stem}.{os.getpid()}.tmp.png")
            if img.save(str(tmp)):
                os.replace(tmp, cache)
            else:
                tmp.unlink(missing_ok=True)
            self._signals.done.emit(self._path)
        doc.close()


# ---- ツリー ----

class _Tree(QTreeWidget):
    """PDF の上ではツールチップを動的に作る（サムネイルは生成済みのときだけ出す）。"""

    def __init__(self, tooltip: Callable[[str], str], parent=None):
        super().__init__(parent)
        self._tooltip = tooltip

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            item = self.itemAt(event.pos())
            path = item.data(0, _PATH_ROLE) if item else None
            if path and path.lower().endswith(".pdf"):
                QToolTip.showText(event.globalPos(), self._tooltip(path), self)
                return True
        return super().viewportEvent(event)


class FileBrowserPanel(QWidget):
    pdf_clicked = Signal(Path)
    close_requested = Signal()            # 見出しの ✕
    start_dir_changed = Signal(Path)      # .. で起動フォルダを付け替えたとき

    def __init__(
        self,
        start_dir: Path,
        progress: Callable[[str], tuple[int, int] | None],
        *,
        font_size: int = 16,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._start_dir = Path(start_dir)
        self._progress = progress
        self._icons = QFileIconProvider()
        self._collator = QCollator()
        self._collator.setNumericMode(True)          # 「2巻」<「10巻」
        self._collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        # 展開済みフォルダの監視。変化したフォルダはまとめて読み直す
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._dirty: set[str] = set()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(200)
        self._refresh_timer.timeout.connect(self._refresh_dirty)

        # サムネイル生成は 1 スレッドに絞る（pdfium は内部でグローバルロックを取る）
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._requested: set[str] = set()
        self._thumb_signals = _ThumbSignals()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # 見出しは「ページサムネール」と同じ形（太字 + ✕）。QGroupBox の見出しは
        # macOS が文字設定に関係なく小さく描くので使わない（枠だけ使う）
        title = QLabel("ファイルブラウザ")
        title.setStyleSheet("font-weight: bold;")
        close = QToolButton()
        close.setText("✕")
        close.setAutoRaise(True)
        close.setStyleSheet("QToolButton { border: none; background: transparent; padding: 2px 6px; }"
                            "QToolButton:hover { background: palette(midlight); border-radius: 4px; }")
        close.setToolTip("フォルダツリーを閉じる")
        close.clicked.connect(self.close_requested.emit)
        head = QHBoxLayout()
        head.setContentsMargins(6, 4, 2, 0)
        head.addWidget(title, 1)
        head.addWidget(close)
        outer.addLayout(head)
        # フラット: 枠（QGroupBox）もツリーの縁取りも無し。パネルの境目は分割線 1px だけ
        layout = outer

        self.tree = _Tree(self._tooltip)
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setExpandsOnDoubleClick(False)      # 開閉はシングルクリックで行う
        self.tree.setIconSize(QSize(font_size + 4, font_size + 4))
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemExpanded.connect(self._ensure_loaded)
        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        layout.addWidget(self.tree)

        self._build_roots()
        if Path("/Volumes").is_dir():
            self._watcher.addPath("/Volumes")          # ドライブの抜き差し

    # ---- API ----

    def reveal(self, path: str | Path, under: QTreeWidgetItem | None = None) -> bool:
        """path までツリーを開いて選択する。

        under を省くと、path を含む最上位項目のうち最も深いものの下を辿る。
        """
        target = Path(path)
        best: tuple[int, QTreeWidgetItem] | None = None
        for item in [under] if under is not None else self._all_roots():
            p = item.data(0, _PATH_ROLE)
            if p is None:
                continue
            base = Path(p)
            if target == base or base in target.parents:
                depth = len(base.parts)
                if best is None or depth > best[0]:
                    best = (depth, item)
        if best is None:
            return False
        item = best[1]
        base = Path(item.data(0, _PATH_ROLE))
        for part in target.relative_to(base).parts:
            self._ensure_loaded(item)
            item.setExpanded(True)
            nxt = self._child_by_name(item, part)
            if nxt is None:
                break
            item = nxt
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)
        return True

    def select_parent(self) -> None:
        """⌘↑: 親フォルダを選択して閉じる。起動フォルダ自身なら .. と同じ。"""
        item = self.tree.currentItem()
        if item and item.parent():
            self.tree.setCurrentItem(item.parent())
            item.parent().setExpanded(False)
        elif item is self._start_item:
            self.go_up()

    def go_up(self) -> None:
        """起動フォルダを 1 つ上に付け替え、元のフォルダを開いて選択する。"""
        old = self._start_dir
        parent = old.parent
        if parent == old:
            return
        index = self.tree.indexOfTopLevelItem(self._start_item)
        self.tree.takeTopLevelItem(index)
        self._start_dir = parent
        self._start_item = self._make_start_item()
        self.tree.insertTopLevelItem(index, self._start_item)
        self._update_up_item()
        self.reveal(old, under=self._start_item)
        self.start_dir_changed.emit(parent)

    def refresh_progress(self, path: str) -> None:
        for item in self._items_for(path):
            item.setText(1, self._progress_text(path))

    def forget_thumbnail(self, path: str) -> None:
        self._requested.discard(path)

    # ---- 構築 ----

    def _build_roots(self) -> None:
        up = QTreeWidgetItem(["..", ""])
        up.setIcon(0, self._up_arrow_icon())
        up.setData(0, _PATH_ROLE, None)
        self._up_item = up
        start = self._start_item = self._make_start_item()
        home = self._dir_item(Path.home(), "ホーム")
        mac = QTreeWidgetItem([_COMPUTER_LABEL, ""])
        mac.setIcon(0, self._icons.icon(QFileIconProvider.IconType.Computer))
        mac.setData(0, _PATH_ROLE, None)
        self._mac = mac
        self.tree.addTopLevelItems([up, start, home, mac])
        self._update_up_item()
        self._fill_volumes()
        # 起動フォルダは畳んでおく。展開すると中身で「ホーム」「この Mac」が
        # 画面外へ押し出され、ただのフォルダ一覧に見えてしまう（Windows と同じく
        # 最上位の構造が一目で見える状態から始める）
        mac.setExpanded(True)

    def _up_arrow_icon(self) -> QIcon:
        """Windows 11 エクスプローラーの「上へ」と同じ、細い線の ↑。

        macOS 標準の「親フォルダへ」(▲) は取り出しボタンに見えるので自前で描く。
        Windows のグリフ（Segoe Fluent Icons）は使わず、同じ形を線で描く。
        色はツリーの文字色に合わせる（暗い配色でも見える）。
        """
        size = self.tree.iconSize().height()
        dpr = 2.0
        pm = QPixmap(round(size * dpr), round(size * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.tree.palette().color(QPalette.ColorRole.Text), max(1.5, size / 14))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        cx, top, bottom, wing = size / 2, size * 0.18, size * 0.84, size * 0.30
        path = QPainterPath()
        path.moveTo(cx, bottom)
        path.lineTo(cx, top)                      # 軸
        path.moveTo(cx - wing, top + wing)
        path.lineTo(cx, top)                      # 矢じり
        path.lineTo(cx + wing, top + wing)
        painter.drawPath(path)
        painter.end()
        return QIcon(pm)

    def _make_start_item(self) -> QTreeWidgetItem:
        item = self._dir_item(self._start_dir, self._start_dir.name or str(self._start_dir))
        item.setToolTip(0, str(self._start_dir))
        return item

    def _update_up_item(self) -> None:
        parent = self._start_dir.parent
        self._up_item.setHidden(parent == self._start_dir)      # ルートでは出さない
        self._up_item.setToolTip(0, f"1 つ上へ: {parent}")

    def _fill_volumes(self) -> None:
        expanded = self._expanded_paths(self._mac)
        self._mac.takeChildren()
        for name, root in mounted_volumes():
            self._mac.addChild(self._dir_item(root, name))
        self._restore_expanded(self._mac, expanded)

    def _dir_item(self, path: Path, label: str | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label or path.name, ""])
        item.setIcon(0, self._icons.icon(QFileInfo(str(path))))
        item.setData(0, _PATH_ROLE, str(path))
        item.setData(0, _LOADED_ROLE, False)
        item.addChild(QTreeWidgetItem([_PLACEHOLDER, ""]))    # ▸ を出す
        return item

    def _pdf_item(self, path: Path) -> QTreeWidgetItem:
        item = QTreeWidgetItem([path.name, self._progress_text(str(path))])
        item.setIcon(0, self._icons.icon(QFileInfo(str(path))))
        item.setData(0, _PATH_ROLE, str(path))
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return item

    def _ensure_loaded(self, item: QTreeWidgetItem) -> None:
        if item is self._mac or item.data(0, _LOADED_ROLE):
            return
        self._fill(item)

    def _fill(self, item: QTreeWidgetItem) -> None:
        """フォルダ項目の子を作り直す。展開状態と選択は保つ。"""
        path = item.data(0, _PATH_ROLE)
        expanded = self._expanded_paths(item)
        current = self.tree.currentItem()
        selected = current.data(0, _PATH_ROLE) if current else None
        dirs: list[Path] = []
        pdfs: list[Path] = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        # Finder / エクスプローラーと同じく隠し属性のもの（/bin・/usr・~/Library 等）も隠す
                        if is_hidden(e.stat(follow_symlinks=False)):
                            continue
                        if e.is_dir():
                            dirs.append(Path(e.path))
                        elif e.name.lower().endswith(".pdf") and e.is_file():
                            pdfs.append(Path(e.path))
                    except OSError:
                        continue
        except OSError:
            pass
        key = lambda p: self._collator.sortKey(p.name)    # noqa: E731
        item.takeChildren()
        for d in sorted(dirs, key=key):
            item.addChild(self._dir_item(d))
        for f in sorted(pdfs, key=key):
            item.addChild(self._pdf_item(f))
        item.setData(0, _LOADED_ROLE, True)
        if path not in self._watcher.directories():
            self._watcher.addPath(path)
        self._restore_expanded(item, expanded)
        if selected:
            for it in self._items_for(selected):
                self.tree.setCurrentItem(it)
                break

    # ---- 監視 ----

    def _on_dir_changed(self, path: str) -> None:
        self._dirty.add(path)
        self._refresh_timer.start()

    def _refresh_dirty(self) -> None:
        dirty, self._dirty = self._dirty, set()
        if "/Volumes" in dirty:
            self._fill_volumes()
        for path in dirty:
            for item in self._items_for(path):
                if item.data(0, _LOADED_ROLE):
                    self._fill(item)

    # ---- 補助 ----

    def _all_roots(self) -> list[QTreeWidgetItem]:
        tops = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        return tops + [self._mac.child(i) for i in range(self._mac.childCount())]

    def _items_for(self, path: str) -> list[QTreeWidgetItem]:
        """path を表す項目（同じフォルダが複数の最上位の下に出ることがある）。"""
        out = []
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, _PATH_ROLE) == path:
                out.append(item)
            if item is self._mac or item.data(0, _LOADED_ROLE):
                stack.extend(item.child(i) for i in range(item.childCount()))
        return out

    @staticmethod
    def _child_by_name(item: QTreeWidgetItem, name: str) -> QTreeWidgetItem | None:
        for i in range(item.childCount()):
            child = item.child(i)
            p = child.data(0, _PATH_ROLE)
            if p and Path(p).name == name:
                return child
        return None

    def _expanded_paths(self, item: QTreeWidgetItem) -> set[str]:
        out: set[str] = set()
        stack = [item.child(i) for i in range(item.childCount())]
        while stack:
            it = stack.pop()
            if it.isExpanded() and it.data(0, _PATH_ROLE):
                out.add(it.data(0, _PATH_ROLE))
                stack.extend(it.child(i) for i in range(it.childCount()))
        return out

    def _restore_expanded(self, item: QTreeWidgetItem, expanded: set[str]) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            if child.data(0, _PATH_ROLE) in expanded:
                self._ensure_loaded(child)
                child.setExpanded(True)
                self._restore_expanded(child, expanded)

    def _progress_text(self, path: str) -> str:
        pos = self._progress(path)
        if not pos or not pos[1]:
            return ""
        return f"{round(100 * (pos[0] + 1) / pos[1])}%"

    def _tooltip(self, path: str) -> str:
        name = html.escape(Path(path).name)
        pos = self._progress(path)
        progress = f"{pos[0] + 1} / {pos[1]} ページ" if pos else "未読"
        cache = thumb_cache_path(path)
        if cache is not None and cache.exists():
            return f'<img src="{cache.as_uri()}"><br>{name}<br>{progress}'
        if path not in self._requested:
            self._requested.add(path)
            self._pool.start(_ThumbJob(path, self._thumb_signals))
        return f"{name}<br>{progress}"

    def _on_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        if item is self._up_item:
            self.go_up()
            return
        path = item.data(0, _PATH_ROLE)
        if path and path.lower().endswith(".pdf") and Path(path).is_file():
            self.pdf_clicked.emit(Path(path))
        elif path and item.childCount():
            # Windows と同じく、フォルダ名のクリックでも開閉できる
            self._ensure_loaded(item)
            item.setExpanded(not item.isExpanded())
