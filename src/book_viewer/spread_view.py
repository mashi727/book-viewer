"""見開き対応のページ表示ウィジェット。

QPdfView には見開きモードが無いので、QPdfDocument.render() で各ページを
ラスタライズして自前で並べる。常に「組全体をウィンドウに収める」表示。

  ページ送り（右綴じでは ←/→ の向きが反転する）:
    → / ←                 … 綴じ方向に沿って次 / 前
    Space ↓ PageDown      … 次          ⇧Space ↑ PageUp … 前
    Home / End            … 先頭 / 末尾
    ホイール              … 積算して 1 ノッチ相当ごとに 1 組（慣性スクロールは無視）
    クリック              … 左半分 = ←、右半分 = →
"""
from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QRectF, QSize, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QWheelEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QWidget

from .layout import Layout, groups, index_of_page, slots

_MARGIN = 6
_CACHE_MAX = 12          # レンダリング済みページ画像の保持数（現在の組 + 先読み分）
_WHEEL_STEP = 120        # angleDelta の 1 ノッチ


class SpreadView(QWidget):
    pageChanged = Signal(int)    # 現在の組の先頭ページ（読む順、0 始まり）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 200)
        self._doc: QPdfDocument | None = None
        self._layout = Layout()
        self._groups: list[tuple[int, ...]] = []
        self._k = 0
        self._cache: OrderedDict[tuple[int, int, int], QImage] = OrderedDict()
        self._frozen = False
        self._wheel_acc = 0
        self._bg = QColor(40, 40, 40)

    # ---- 外部 API ----

    def set_document(self, doc: QPdfDocument | None, page: int = 0) -> None:
        self._doc = doc
        self._cache.clear()
        self._frozen = False
        self._regroup(page)

    def set_layout(self, layout: Layout) -> None:
        page = self.current_page()
        self._layout = layout
        self._regroup(page)

    def current_page(self) -> int:
        return self._groups[self._k][0] if self._groups else 0

    def current_group(self) -> tuple[int, ...]:
        return self._groups[self._k] if self._groups else ()

    def page_count(self) -> int:
        return self._doc.pageCount() if self._doc else 0

    def set_frozen(self, frozen: bool) -> None:
        """True の間は新規レンダリングをせずキャッシュだけで描く（ファイル書き換え中の保護）。"""
        self._frozen = frozen

    def go(self, delta: int) -> None:
        self._go_to_index(self._k + delta)

    def go_to_page(self, page: int) -> None:
        self._go_to_index(index_of_page(page, self._groups))

    def first(self) -> None:
        self._go_to_index(0)

    def last(self) -> None:
        self._go_to_index(len(self._groups) - 1)

    # ---- 内部 ----

    def _regroup(self, page: int) -> None:
        self._groups = groups(self.page_count(), self._layout)
        self._k = index_of_page(page, self._groups)
        self.update()
        self.pageChanged.emit(self.current_page())

    def _go_to_index(self, k: int) -> None:
        k = max(0, min(k, len(self._groups) - 1))
        if k != self._k:
            self._k = k
            self.update()
            self.pageChanged.emit(self.current_page())

    def _placements(self, k: int) -> list[tuple[int, QRectF]]:
        """組 k の各ページの描画矩形。空きスロットは相方と同じ大きさで場所だけ取る。"""
        if not self._doc or not (0 <= k < len(self._groups)):
            return []
        sl = slots(self._groups[k], k, self._layout)
        real = [p for p in sl if p is not None]
        ref = self._doc.pagePointSize(real[0])
        sizes = [self._doc.pagePointSize(p) if p is not None else ref for p in sl]
        sizes = [s if s.width() > 0 and s.height() > 0 else QSizeF(595, 842) for s in sizes]
        total_w = sum(s.width() for s in sizes)
        max_h = max(s.height() for s in sizes)
        avail_w = max(1, self.width() - 2 * _MARGIN)
        avail_h = max(1, self.height() - 2 * _MARGIN)
        scale = min(avail_w / total_w, avail_h / max_h)
        x = (self.width() - total_w * scale) / 2
        out = []
        for p, s in zip(sl, sizes):
            w, h = s.width() * scale, s.height() * scale
            if p is not None:
                out.append((p, QRectF(x, (self.height() - h) / 2, w, h)))
            x += w
        return out

    def _image(self, page: int, rect: QRectF, render: bool) -> QImage | None:
        dpr = self.devicePixelRatioF()
        px = QSize(max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr)))
        key = (page, px.width(), px.height())
        img = self._cache.get(key)
        if img is not None:
            self._cache.move_to_end(key)
            return img
        if not render or self._frozen or not self._doc:
            # サイズ違いでも同じページがあれば暫定表示に使う
            for (p, _w, _h), cached in reversed(self._cache.items()):
                if p == page:
                    return cached
            return None
        img = self._doc.render(page, px)
        img.setDevicePixelRatio(dpr)
        self._cache[key] = img
        while len(self._cache) > _CACHE_MAX:
            self._cache.popitem(last=False)
        return img

    def _prefetch(self) -> None:
        for k in (self._k + 1, self._k - 1):
            for page, rect in self._placements(k):
                self._image(page, rect, render=True)

    # ---- Qt イベント ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._bg)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for page, rect in self._placements(self._k):
            # pdfium は背景を塗らないページを透明で返す（TeX の出力など）。紙の白を敷く
            painter.fillRect(rect, Qt.GlobalColor.white)
            img = self._image(page, rect, render=True)
            if img is not None:
                painter.drawImage(rect, img)
        painter.end()
        # 次・前の組を先読み（描画が終わってから）
        QTimer.singleShot(0, self._prefetch)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        shift = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        forward_arrow = Qt.Key.Key_Left if self._layout.rtl else Qt.Key.Key_Right
        backward_arrow = Qt.Key.Key_Right if self._layout.rtl else Qt.Key.Key_Left
        if key == forward_arrow:
            self.go(+1)
        elif key == backward_arrow:
            self.go(-1)
        elif key == Qt.Key.Key_Space:
            self.go(-1 if shift else +1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self.go(+1)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.go(-1)
        elif key == Qt.Key.Key_Home:
            self.first()
        elif key == Qt.Key.Key_End:
            self.last()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        # macOS のトラックパッドは細かいイベントを大量に出すので積算して刻む。
        # 指を離した後の慣性スクロールは数ページ飛ぶ原因になるので捨てる。
        if event.phase() == Qt.ScrollPhase.ScrollMomentum:
            return
        if event.phase() in (Qt.ScrollPhase.ScrollBegin, Qt.ScrollPhase.ScrollEnd):
            self._wheel_acc = 0
        self._wheel_acc += event.angleDelta().y()
        while self._wheel_acc <= -_WHEEL_STEP:
            self._wheel_acc += _WHEEL_STEP
            self.go(+1)
        while self._wheel_acc >= _WHEEL_STEP:
            self._wheel_acc -= _WHEEL_STEP
            self.go(-1)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        left = event.position().x() < self.width() / 2
        # 左半分は「←」と同じ（右綴じなら次、左綴じなら前）
        forward = left == self._layout.rtl
        self.go(+1 if forward else -1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update()
