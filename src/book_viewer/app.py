"""自炊本PDFリーダー。

  book-viewer [フォルダ | PDF]

左は本棚（現在フォルダの一覧。`..`、サブフォルダ、PDF をサムネイルと読書進捗つきで表示）。
PDF をクリックで右に表示し、前回読んでいた位置から再開する。

  開き方（PDF 本体に書き込める。⌘S）:
    B  … 右綴じ / 左綴じ      D … 見開き / 単ページ      C … 表紙を単独にする
  読書:
    → ← Space ホイール クリック … ページ送り（詳細は spread_view.py）
    F / Esc … 全画面 / 解除      ⌘O … フォルダを開く      ⌘↑ … 親フォルダへ
  自動再読込:
    表示中の PDF が書き換えられたら（TeX の再コンパイル等）、書き込みが落ち着くのを
    待って読み込み直す。ページ位置と、ビューア上の開き方は保つ。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from PySide6.QtCore import (
    QFileSystemWatcher,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QIcon, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QToolBar,
    QWidget,
)

from . import pdfprefs
from .layout import Layout
from .spread_view import SpreadView
from .state import Store, state_dir

_THUMB = QSize(48, 64)
_THUMB_DIR = Path.home() / ".cache" / "book-viewer" / "thumbs"
_RELOAD_SETTLE_MS = 400      # 書き込みが止んだと見なすまでの待ち
_RELOAD_MAX_TRIES = 50       # 400ms × 50 ≒ 20 秒待って読めなければ諦める


# ---- サムネイル（別スレッドでレンダリングし、ディスクにキャッシュ） ----

class _ThumbSignals(QObject):
    done = Signal(str, QImage)


class _ThumbJob(QRunnable):
    def __init__(self, path: str, signals: _ThumbSignals):
        super().__init__()
        self._path = path
        self._signals = signals

    def run(self) -> None:
        img = QImage()
        try:
            st = Path(self._path).stat()
            key = hashlib.sha1(f"{self._path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
            cache = _THUMB_DIR / f"{key}.png"
            if cache.exists():
                img.load(str(cache))
            if img.isNull():
                doc = QPdfDocument()
                if doc.load(self._path) == QPdfDocument.Error.None_ and doc.pageCount() > 0:
                    s = doc.pagePointSize(0)
                    h = _THUMB.height() * 2
                    w = max(1, round(h * s.width() / s.height())) if s.height() > 0 else h
                    page = doc.render(0, QSize(w, h))
                    # 透明な背景（TeX の出力など）に紙の白を敷く
                    img = QImage(page.size(), QImage.Format.Format_RGB32)
                    img.fill(Qt.GlobalColor.white)
                    painter = QPainter(img)
                    painter.drawImage(0, 0, page)
                    painter.end()
                    _THUMB_DIR.mkdir(parents=True, exist_ok=True)
                    img.save(str(cache))
                doc.close()
        except OSError:
            pass
        self._signals.done.emit(self._path, img)


class BookViewer(QMainWindow):
    def __init__(self, directory: str, open_pdf: str | None = None):
        super().__init__()
        self.setWindowTitle("Book Viewer")
        self.resize(1400, 900)
        self._store = Store()
        self._root = str(Path(directory).resolve())
        self._book: str | None = None
        self._saved_layout = Layout()       # PDF に書かれている開き方
        self._items: dict[str, QListWidgetItem] = {}
        self._thumbs: dict[str, QIcon] = {}

        # 左: 本棚
        self._list = QListWidget()
        self._list.setIconSize(_THUMB)
        self._list.setSpacing(1)
        self._list.itemClicked.connect(self._on_item_clicked)
        self._list.itemDoubleClicked.connect(self._on_item_double)

        # 右: 見開きビュー
        self._doc = QPdfDocument(self)
        self._view = SpreadView()
        self._view.pageChanged.connect(self._on_page_changed)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._list)
        self._splitter.addWidget(self._view)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([320, 1080])
        self.setCentralWidget(self._splitter)
        self.setStatusBar(QStatusBar())

        self._build_toolbar()

        QShortcut(QKeySequence(QKeySequence.StandardKey.Open), self).activated.connect(self._open_folder)
        QShortcut(QKeySequence("Ctrl+Up"), self).activated.connect(self._go_up)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(
            lambda: self._set_fullscreen(False)
        )

        # フォルダ監視（本棚の自動更新）
        self._dir_watcher = QFileSystemWatcher(self)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(200)
        self._refresh_timer.timeout.connect(self._populate)
        self._dir_watcher.directoryChanged.connect(lambda _p: self._refresh_timer.start())

        # 表示中 PDF の監視（自動再読込）
        self._file_watcher = QFileSystemWatcher(self)
        self._file_watcher.fileChanged.connect(self._on_file_changed)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(_RELOAD_SETTLE_MS)
        self._reload_timer.timeout.connect(self._try_reload)
        self._pending_sig: tuple[int, int] | None = None
        self._loaded_sig: tuple[int, int] | None = None
        self._reload_tries = 0

        # 読書位置の保存（ページ送りのたびに書かない）
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1000)
        self._save_timer.timeout.connect(self._save_position)

        # サムネイル生成は 1 スレッドに絞る（pdfium は内部でグローバルロックを取る）
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.done.connect(self._on_thumb)

        self._set_root(self._root)
        if open_pdf:
            self._open_book(open_pdf)
            item = self._items.get(open_pdf)
            if item:
                self._list.setCurrentItem(item)

    # ---- ツールバー ----

    def _build_toolbar(self) -> None:
        tb = QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self._toolbar = tb

        def action(text: str, shortcut: str, tip: str, checkable=False, slot=None) -> QAction:
            a = QAction(text, self)
            a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(f"{tip} ({QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)})")
            a.setCheckable(checkable)
            if slot == self._set_fullscreen:
                a.triggered.connect(slot)                   # checked を受け取る
            elif slot:
                a.triggered.connect(lambda _checked=False, s=slot: s())
            tb.addAction(a)
            return a

        action("📂", "Ctrl+O", "フォルダを開く", slot=self._open_folder).setShortcut(QKeySequence())
        tb.addSeparator()
        self._act_rtl = action("右綴じ", "B", "右綴じ / 左綴じ", True, self._on_layout_toggled)
        self._act_spread = action("見開き", "D", "見開き / 単ページ", True, self._on_layout_toggled)
        self._act_cover = action("表紙単独", "C", "見開きで表紙を単独ページにする", True, self._on_layout_toggled)
        tb.addSeparator()
        self._act_save = action("PDFに保存", "Ctrl+S", "開き方を PDF 本体に書き込む", slot=self._save_layout)
        tb.addSeparator()
        self._act_full = action("全画面", "F", "全画面", True, self._set_fullscreen)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self._page_label = QLabel()
        self._page_label.setContentsMargins(0, 0, 8, 0)
        tb.addWidget(self._page_label)
        self._sync_actions(Layout())

    def _current_layout(self) -> Layout:
        return Layout(
            spread=self._act_spread.isChecked(),
            cover_single=self._act_cover.isChecked(),
            rtl=self._act_rtl.isChecked(),
        )

    def _sync_actions(self, layout: Layout) -> None:
        for a, v in ((self._act_rtl, layout.rtl), (self._act_spread, layout.spread),
                     (self._act_cover, layout.cover_single)):
            a.blockSignals(True)
            a.setChecked(v)
            a.blockSignals(False)
        self._update_action_state()

    def _update_action_state(self) -> None:
        layout = self._current_layout()
        self._act_cover.setEnabled(layout.spread)
        dirty = self._book is not None and layout != self._saved_layout
        self._act_save.setEnabled(dirty)
        self._act_save.setText("PDFに保存 •" if dirty else "PDFに保存")

    def _on_layout_toggled(self) -> None:
        self._view.set_layout(self._current_layout())
        self._update_action_state()

    def _save_layout(self) -> None:
        if not self._book:
            return
        layout = self._current_layout()
        try:
            pdfprefs.write_layout(self._book, layout, log_path=state_dir() / "prefs-log.jsonl")
        except Exception as e:  # 暗号化 PDF・壊れた PDF・書き込み不可など
            QMessageBox.warning(self, "PDF に保存できませんでした", f"{Path(self._book).name}\n\n{e}")
            return
        self._saved_layout = layout
        self._update_action_state()
        self.statusBar().showMessage("開き方を PDF に書き込みました", 3000)
        # 置き換えたファイルは直後に fileChanged が来て再読込される

    def _set_fullscreen(self, on: bool) -> None:
        on = bool(on)
        if on == self.isFullScreen():
            return
        self._act_full.setChecked(on)
        self._list.setVisible(not on)
        self.statusBar().setVisible(not on)
        self._toolbar.setVisible(not on)
        if on:
            self.showFullScreen()
        else:
            self.showNormal()
        self._view.setFocus()

    # ---- 本棚 ----

    def _set_root(self, path: str) -> None:
        path = str(Path(path).resolve())
        if not Path(path).is_dir():
            return
        self._root = path
        self._store.last_dir = path
        self._store.save()
        old = self._dir_watcher.directories()
        if old:
            self._dir_watcher.removePaths(old)
        self._dir_watcher.addPath(path)
        if not self._book:
            self.setWindowTitle(f"{Path(path).name} — Book Viewer")
        self._populate()

    def _progress_text(self, pdf: str) -> str:
        pos = self._store.position(pdf)
        if not pos:
            return "未読"
        page, pages = pos
        pct = round(100 * (page + 1) / pages) if pages else 0
        return f"{pct}%  ({page + 1}/{pages})"

    def _populate(self) -> None:
        cur = self._list.currentItem()
        selected = cur.data(Qt.ItemDataRole.UserRole) if cur else None
        self._list.clear()
        self._items.clear()
        style = self.style()
        path = self._root
        parent = str(Path(path).parent)
        if parent != path:
            up = QListWidgetItem(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogToParent), "..")
            up.setData(Qt.ItemDataRole.UserRole, parent)
            self._list.addItem(up)
        try:
            entries = list(Path(path).iterdir())
        except OSError:
            entries = []
        dirs = sorted((p for p in entries if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.name.lower())
        pdfs = sorted((p for p in entries if p.is_file() and p.suffix.lower() == ".pdf"
                       and not p.name.startswith(".")), key=lambda p: p.name.lower())
        for p in dirs:
            it = QListWidgetItem(style.standardIcon(QStyle.StandardPixmap.SP_DirIcon), p.name)
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            self._list.addItem(it)
        placeholder = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        for p in pdfs:
            sp = str(p)
            it = QListWidgetItem(self._thumbs.get(sp, placeholder), f"{p.stem}\n{self._progress_text(sp)}")
            it.setData(Qt.ItemDataRole.UserRole, sp)
            it.setToolTip(p.name)
            self._list.addItem(it)
            self._items[sp] = it
            if sp not in self._thumbs:
                self._pool.start(_ThumbJob(sp, self._thumb_signals))
        if selected:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.ItemDataRole.UserRole) == selected:
                    self._list.setCurrentRow(i)
                    break

    def _on_thumb(self, path: str, img: QImage) -> None:
        if img.isNull():
            return
        icon = QIcon(QPixmap.fromImage(img))
        self._thumbs[path] = icon
        item = self._items.get(path)
        if item:
            item.setIcon(icon)

    def _go_up(self) -> None:
        parent = str(Path(self._root).parent)
        if parent != self._root:
            self._set_root(parent)

    def _open_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "フォルダを開く", self._root)
        if chosen:
            self._set_root(chosen)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and path.lower().endswith(".pdf") and Path(path).is_file():
            self._open_book(path)

    def _on_item_double(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).is_dir():
            self._set_root(path)

    # ---- 本を開く ----

    def _open_book(self, path: str) -> None:
        path = str(Path(path).resolve())
        if path == self._book:
            self._view.setFocus()
            return
        self._save_position()
        doc = QPdfDocument(self)
        if doc.load(path) != QPdfDocument.Error.None_:
            self.statusBar().showMessage(f"開けませんでした: {Path(path).name}", 5000)
            doc.deleteLater()
            return
        try:
            layout = pdfprefs.read_layout(path)
        except Exception:
            layout = Layout()
        if self._book:
            self._file_watcher.removePath(self._book)
        self._book = path
        self._saved_layout = layout
        self._sync_actions(layout)
        self._view.set_layout(layout)
        pos = self._store.position(path)
        old, self._doc = self._doc, doc
        self._view.set_document(doc, pos[0] if pos else 0)
        old.deleteLater()
        self._file_watcher.addPath(path)
        self._loaded_sig = self._file_sig(path)
        self.setWindowTitle(f"{Path(path).stem} — Book Viewer")
        self._view.setFocus()

    def _on_page_changed(self, _page: int) -> None:
        n = self._view.page_count()
        g = self._view.current_group()
        if not n or not g:
            self._page_label.setText("")
            return
        pages = "–".join(str(p + 1) for p in g)
        self._page_label.setText(f"{pages} / {n}")
        self._save_timer.start()

    def _save_position(self) -> None:
        if not self._book or not self._view.page_count():
            return
        self._store.set_position(self._book, self._view.current_page(), self._view.page_count())
        self._store.save()
        item = self._items.get(self._book)
        if item:
            item.setText(f"{Path(self._book).stem}\n{self._progress_text(self._book)}")

    # ---- 自動再読込 ----

    @staticmethod
    def _file_sig(path: str) -> tuple[int, int] | None:
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    @staticmethod
    def _looks_complete(path: str) -> bool:
        """末尾 1 KB に %%EOF があるか。

        scp（luatex-pdf の取得）は既存ファイルを先頭から上書きするので、転送が一瞬
        止まると書きかけのファイルが「安定」して見える。pdfium は壊れた PDF も
        xref を再構築して開いてしまい、ページ数の少ない版を読むことになるため、
        サイズの安定だけでなく末尾の %%EOF も完成の条件にする。
        """
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 1024))
                return b"%%EOF" in f.read()
        except OSError:
            return False

    def _on_file_changed(self, path: str) -> None:
        if path != self._book:
            return
        # 書き込み中のファイルを pdfium に読ませないよう、新規レンダリングを止める
        self._view.set_frozen(True)
        self._pending_sig = None
        self._reload_tries = 0
        self._reload_timer.start()

    def _try_reload(self) -> None:
        path = self._book
        if not path:
            return
        self._reload_tries += 1
        # 置き換え（削除→作成）で監視が外れるので張り直す
        if Path(path).exists() and path not in self._file_watcher.files():
            self._file_watcher.addPath(path)
        sig = self._file_sig(path)
        # 存在しない、または前回の確認からサイズ・時刻が動いている → まだ書き込み中
        if sig is None or sig != self._pending_sig:
            self._pending_sig = sig
            if self._reload_tries < _RELOAD_MAX_TRIES:
                self._reload_timer.start()
            else:
                self._view.set_frozen(False)
                self.statusBar().showMessage("再読込を中止しました（ファイルが安定しません）", 5000)
            return
        if sig == self._loaded_sig:
            self._view.set_frozen(False)
            return
        doc = QPdfDocument(self)
        if (not self._looks_complete(path)
                or doc.load(path) != QPdfDocument.Error.None_ or doc.pageCount() == 0):
            # 転送途中で止まっている等。少し待って再試行する
            doc.deleteLater()
            self._pending_sig = None
            if self._reload_tries < _RELOAD_MAX_TRIES:
                self._reload_timer.start()
            else:
                self._view.set_frozen(False)
                self.statusBar().showMessage("再読込に失敗しました", 5000)
            return
        try:
            self._saved_layout = pdfprefs.read_layout(path)
        except Exception:
            pass
        page = self._view.current_page()
        old, self._doc = self._doc, doc
        self._view.set_document(doc, page)      # ビューア上の開き方は保ったまま
        old.deleteLater()
        self._loaded_sig = sig
        self._update_action_state()
        self._thumbs.pop(path, None)
        self._refresh_timer.start()
        self.statusBar().showMessage(f"再読み込みしました（{doc.pageCount()} ページ）", 3000)

    def closeEvent(self, event) -> None:
        self._save_position()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("book-viewer")
    positional = [a for a in app.arguments()[1:] if not a.startswith("-")]
    open_pdf = None
    if positional:
        target = Path(positional[0]).expanduser().resolve()
        if target.is_file() and target.suffix.lower() == ".pdf":
            open_pdf, directory = str(target), str(target.parent)
        else:
            directory = str(target)
    else:
        directory = Store().last_dir or QFileDialog.getExistingDirectory(None, "本のフォルダを選択")
    if not directory:
        print("フォルダが指定されていません", file=sys.stderr)
        return 1
    if not Path(directory).is_dir():
        print(f"フォルダではありません: {directory}", file=sys.stderr)
        return 1

    win = BookViewer(directory, open_pdf)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
