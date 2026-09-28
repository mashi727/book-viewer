"""ページサムネールのナビゲーションパネル（Acrobat の「ページサムネール」に相当）。

  - 表示中の本の全ページを縮小表示し、クリックでそのページへ移動する
  - 表示中の組（見開きなら 2 ページ）を選択状態で示し、ページ送りに追従してスクロールする
  - パネル幅を広げると複数列に並ぶ（Acrobat と同じ）
  - ⇧ / ⌘（Windows は Ctrl）+ クリックで複数選択し、ドラッグしてページを移動する（落とす位置は青い縦線）。
    移動そのものはアプリ側（未保存の変更 → ［保存］で PDF に書き込む）

レンダリングは専用スレッドで行い、画面に見えている項目の分だけ要求する
（QListView は見えている項目にしか DecorationRole を問い合わせない）。
数百ページの自炊本でも開いた瞬間に全ページを描かないため、UI が止まらない。
本を切り替えたり再読込したりしたら世代番号を進め、古い要求の結果は捨てる。
"""
from __future__ import annotations

import json
from collections import OrderedDict

from PySide6.QtCore import (
    QAbstractListModel,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QMetaObject,
    QModelIndex,
    QObject,
    QSize,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

THUMB = QSize(96, 128)       # サムネイルの枠（論理 px）。縦横比を保ってこの中に収める
_MIME = "application/x-book-viewer-pages"   # ドラッグで運ぶもの（ページ番号の JSON）
_CACHE_MAX = 400             # 保持するサムネイル数（超えたら古いものから捨て、見えたら描き直す）


class _RenderWorker(QObject):
    """専用スレッドで動く。自分用の QPdfDocument を持ち、要求されたページを描く。"""

    rendered = Signal(int, int, QImage)      # 世代, ページ, 画像

    def __init__(self) -> None:
        super().__init__()
        self.latest = 0                      # 最新の世代（メインスレッドが書き換える）
        self._doc: QPdfDocument | None = None
        self._path: str | None = None

    @Slot()
    def release(self) -> None:
        """開いている PDF を閉じる（Windows で PDF を書き換える前に呼ぶ）。"""
        if self._doc is not None:
            self._doc.close()
        self._doc = None
        self._path = None

    @Slot(int, str, int, int, int)
    def render(self, gen: int, path: str, page: int, w: int, h: int) -> None:
        if gen != self.latest:
            return                           # 本が切り替わった後の古い要求
        if path != self._path:
            if self._doc is not None:
                self._doc.close()
            self._doc = QPdfDocument()
            self._path = path
            if self._doc.load(path) != QPdfDocument.Error.None_:
                self._path = None
                return
        doc = self._doc
        if doc is None or not (0 <= page < doc.pageCount()):
            return
        s = doc.pagePointSize(page)
        if s.width() <= 0 or s.height() <= 0:
            return
        scale = min(w / s.width(), h / s.height())
        size = QSize(max(1, round(s.width() * scale)), max(1, round(s.height() * scale)))
        rendered = doc.render(page, size)
        # 透明な背景（TeX の出力など）に紙の白を敷く
        img = QImage(size, QImage.Format.Format_RGB32)
        img.fill(Qt.GlobalColor.white)
        painter = QPainter(img)
        painter.drawImage(0, 0, rendered)
        painter.end()
        self.rendered.emit(gen, page, img)


class ThumbnailModel(QAbstractListModel):
    requested = Signal(int, str, int, int, int)

    def __init__(self, worker: _RenderWorker, dpr: float, parent=None):
        super().__init__(parent)
        self._worker = worker
        self._dpr = dpr
        self._path: str | None = None
        self._count = 0
        self._gen = 0
        self._cache: OrderedDict[int, QPixmap] = OrderedDict()
        self._pending: set[int] = set()
        self._placeholder = QPixmap(THUMB)
        self._placeholder.fill(Qt.GlobalColor.transparent)
        p = QPainter(self._placeholder)
        p.fillRect(8, 0, THUMB.width() - 16, THUMB.height(), QColor(225, 225, 225))
        p.end()

    def set_document(self, path: str | None, count: int) -> None:
        self.beginResetModel()
        self._gen += 1
        self._worker.latest = self._gen
        self._path = path
        self._count = count if path else 0
        self._cache.clear()
        self._pending.clear()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else self._count

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self) -> list[str]:
        return [_MIME]

    def mimeData(self, indexes) -> QMimeData:
        data = QMimeData()
        data.setData(_MIME, json.dumps(sorted({i.row() for i in indexes})).encode())
        return data

    def supportedDragActions(self):
        return Qt.DropAction.MoveAction

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        page = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            return str(page + 1)
        if role == Qt.ItemDataRole.DecorationRole:
            pm = self._cache.get(page)
            if pm is not None:
                self._cache.move_to_end(page)
                return pm
            if page not in self._pending and self._path:
                self._pending.add(page)
                self.requested.emit(self._gen, self._path, page,
                                    round(THUMB.width() * self._dpr), round(THUMB.height() * self._dpr))
            return self._placeholder
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        return None

    @Slot(int, int, QImage)
    def on_rendered(self, gen: int, page: int, img: QImage) -> None:
        if gen != self._gen:
            return
        self._pending.discard(page)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(self._dpr)
        self._cache[page] = pm
        while len(self._cache) > _CACHE_MAX:
            self._cache.popitem(last=False)
        idx = self.index(page)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


class _ThumbView(QListView):
    """サムネイル一覧。選んだページをドラッグして、サムネイルの間に落とすと移動を依頼する。

    QListView の組み込みの移動（アイコン表示では項目を自由な位置へ動かす）は使わず、
    落とした位置（項目の左半分なら前、右半分なら後ろ）から挿入位置を求めて move_requested を出す。
    """

    move_requested = Signal(list, int)      # 移すページ（元の位置）, 挿入位置（元の並びで数える）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._drop: int | None = None

    def _drop_row(self, pos) -> int | None:
        model = self.model()
        n = model.rowCount() if model else 0
        if n == 0:
            return None
        idx = self.indexAt(pos)
        if idx.isValid():
            r = self.visualRect(idx)
            return idx.row() + (1 if pos.x() > r.center().x() else 0)
        # 項目の隙間: 画面に見えている項目のうち、いちばん近いものの前後
        best, best_d = None, None
        for row in range(n):
            r = self.visualRect(model.index(row))
            if not r.intersects(self.viewport().rect()):
                continue
            d = (r.center() - pos).manhattanLength()
            if best_d is None or d < best_d:
                best, best_d = (row, r), d
        if best is None:
            return n
        row, r = best
        return row + (1 if pos.x() > r.center().x() else 0)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(_MIME):         # この形式はこのアプリのサムネイルだけが作る
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)               # 端での自動スクロール
        if not event.mimeData().hasFormat(_MIME):
            event.ignore()
            return
        self._drop = self._drop_row(event.position().toPoint())
        self.viewport().update()
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:
        self._drop = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        dest = self._drop_row(event.position().toPoint())
        self._drop = None
        self.viewport().update()
        if not event.mimeData().hasFormat(_MIME) or dest is None:
            event.ignore()
            return
        rows = json.loads(bytes(event.mimeData().data(_MIME)).decode())
        # Copy として受け取る（Move にするとドラッグ元のビューがモデルから行を消そうとする）
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.move_requested.emit(rows, dest)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._drop is None or not self.model() or self.model().rowCount() == 0:
            return
        n = self.model().rowCount()
        if self._drop < n:
            r = self.visualRect(self.model().index(self._drop))
            x = r.left() - 3
        else:
            r = self.visualRect(self.model().index(n - 1))
            x = r.right() + 3
        painter = QPainter(self.viewport())
        painter.setPen(QPen(self.palette().highlight().color(), 4))
        painter.drawLine(x, r.top() + 4, x, r.bottom() - 4)
        painter.end()


class ThumbnailPane(QWidget):
    """見出し「ページサムネール」+ ✕ + サムネイル一覧。"""

    page_clicked = Signal(int)
    close_requested = Signal()
    insert_blank_requested = Signal(int)     # 右クリックしたページ（0 始まり）
    move_requested = Signal(list, int)       # ドラッグで移すページ, 挿入位置

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = _RenderWorker()
        self._worker.moveToThread(self._thread)
        self._thread.start()

        self.model = ThumbnailModel(self._worker, self.devicePixelRatioF(), self)
        self.model.requested.connect(self._worker.render)          # 別スレッドへ（キュー接続）
        self._worker.rendered.connect(self.model.on_rendered)      # メインスレッドへ

        title = QLabel("ページサムネール")
        title.setStyleSheet("font-weight: bold;")
        close = QToolButton()
        close.setText("✕")
        close.setAutoRaise(True)
        close.setStyleSheet("QToolButton { border: none; background: transparent; padding: 2px 6px; }"
                            "QToolButton:hover { background: palette(midlight); border-radius: 4px; }")
        close.setToolTip("ページサムネールを閉じる (F4)")
        close.clicked.connect(self.close_requested.emit)
        head = QHBoxLayout()
        head.setContentsMargins(6, 4, 2, 0)
        head.addWidget(title, 1)
        head.addWidget(close)

        self.view = _ThumbView()
        self.view.move_requested.connect(self.move_requested.emit)
        self._auto_selection: set[int] = set()   # ページ送りで自動的に選んだ組（利用者の複数選択と区別する）
        self.view.setModel(self.model)
        self.view.setViewMode(QListView.ViewMode.IconMode)
        self.view.setMovement(QListView.Movement.Static)
        self.view.setResizeMode(QListView.ResizeMode.Adjust)        # 幅に応じて列数が変わる
        self.view.setWrapping(True)
        self.view.setUniformItemSizes(True)
        self.view.setIconSize(THUMB)
        self.view.setGridSize(QSize(THUMB.width() + 24, THUMB.height() + 34))
        self.view.setSpacing(4)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)   # ⇧ / ⌘ で複数選択
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setFrameShape(QListView.Shape.NoFrame)     # フラット（境目は分割線 1px）
        self.view.clicked.connect(self._on_clicked)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self.view)

    def set_document(self, path: str | None, count: int) -> None:
        self.model.set_document(path, count)
        self._auto_selection = set()

    def _on_clicked(self, idx) -> None:
        # ⇧ / ⌘（Ctrl）を押しながらのクリックは選択だけ。修飾キーなしならそのページへ移動する
        mods = QApplication.keyboardModifiers()
        if mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
                   | Qt.KeyboardModifier.MetaModifier):
            return
        self.page_clicked.emit(idx.row())

    def selected_pages(self) -> list[int]:
        return sorted(i.row() for i in self.view.selectionModel().selectedIndexes())

    def select_pages(self, pages: list[int]) -> None:
        """移動した後のページを選択状態にする（続けて動かせるように）。"""
        sel = QItemSelection()
        for p in pages:
            idx = self.model.index(p)
            if idx.isValid():
                sel.select(idx, idx)
        self.view.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self._auto_selection = set()
        if pages:
            self.view.scrollTo(self.model.index(pages[0]), QAbstractItemView.ScrollHint.EnsureVisible)

    def _context_menu(self, pos) -> None:
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        menu = QMenu(self)
        menu.setFont(self.font())
        menu.addAction(f"空白ページを挿入…（{idx.row() + 1} ページ）",
                       lambda: self.insert_blank_requested.emit(idx.row()))
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def set_current_pages(self, pages: tuple[int, ...]) -> None:
        """表示中の組を選択状態にして見える位置までスクロールする。

        利用者が複数のページを選んでいる間（移動の準備中）は、その選択を崩さない。
        """
        current = set(self.selected_pages())
        if len(current) > 1 and current != self._auto_selection:
            if pages:
                self.view.scrollTo(self.model.index(pages[0]), QAbstractItemView.ScrollHint.EnsureVisible)
            return
        self._auto_selection = set(pages)
        sel = QItemSelection()
        for p in pages:
            idx = self.model.index(p)
            if idx.isValid():
                sel.select(idx, idx)
        sm = self.view.selectionModel()
        sm.select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if pages:
            idx = self.model.index(pages[0])
            if idx.isValid():
                self.view.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)

    def release_file(self) -> None:
        """描画スレッドが開いている PDF を閉じる。スレッド側で閉じ終わるまで待つ。"""
        QMetaObject.invokeMethod(self._worker, "release", Qt.ConnectionType.BlockingQueuedConnection)

    def shutdown(self) -> None:
        self._thread.quit()
        self._thread.wait(2000)
