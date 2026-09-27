"""見開き対応のページ表示ウィジェット（拡大・縮小とスクロールつき）。

QPdfView には見開きモードが無いので、QPdfDocument.render() で各ページを
ラスタライズして自前で並べる。QAbstractScrollArea の上に描くので、拡大して
はみ出した分はスクロールバーとスクロールで見る（Acrobat の単一ページ表示と同じ）。

  ズーム（Acrobat と同じ）:
    ページレベル（組全体を収める・既定） / 幅に合わせる / 実際のサイズ（100%） / 任意の倍率
    ⌘+ホイール・ピンチ … 拡大・縮小

  ページ送り（右綴じでは ←/→ の向きが反転する）:
    → / ←                 … 綴じ方向に沿って次 / 前
    ↓ ↑ ・ホイール        … はみ出していればスクロール。端まで来てさらに送ると次 / 前の組
                            （前の組へ戻ったときはその組の下端を見せる）
    Space / ⇧Space        … 1 画面分スクロール。端なら次 / 前の組
    PageDown / PageUp     … 次 / 前の組
    Home / End            … 先頭 / 末尾
    横スワイプ            … はみ出していれば横スクロール。収まっていれば綴じ方向に沿ってページ送り
                            （左綴じは左へスワイプで次、右綴じは右へスワイプで次）
    クリック              … 左半分 = ←、右半分 = →   ドラッグ … 表示位置を動かす
"""
from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QNativeGestureEvent,
    QPainter,
    QWheelEvent,
)
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QAbstractScrollArea, QFrame

from .layout import Layout, groups, index_of_page, slots

_MARGIN = 6
_CACHE_BYTES = 400 * 1024 * 1024   # レンダリング済みページ画像の上限（拡大時は 1 枚が大きい）
_MAX_PIXELS = 40_000_000           # 1 ページを描く画素数の上限。超える倍率では拡大して貼る（少しぼける）
_PREFETCH_PIXELS = 8_000_000       # これより大きい画像は先読みしない
_WHEEL_STEP = 120                  # angleDelta の 1 ノッチ
_LINE_STEP = 60                    # ↑↓ 1 回のスクロール量 (px)
_CLICK_SLOP = 4                    # これ以上動いたらクリックではなくドラッグ (px)
ZOOM_STEPS = [25, 33.3, 50, 66.7, 75, 100, 125, 150, 200, 300, 400]   # ズームイン / アウトの刻み (%)
ZOOM_MIN, ZOOM_MAX = 10.0, 400.0


class SpreadView(QAbstractScrollArea):
    pageChanged = Signal(int)      # 現在の組の先頭ページ（読む順、0 始まり）
    zoomChanged = Signal(float)    # 実効倍率 (%)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(200, 200)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._doc: QPdfDocument | None = None
        self._layout = Layout()
        self._groups: list[tuple[int, ...]] = []
        self._k = 0
        self._cache: OrderedDict[tuple[int, int, int], QImage] = OrderedDict()
        self._cache_bytes = 0
        self._frozen = False
        self._wheel_acc = 0
        self._bg = QColor(40, 40, 40)
        self._zoom_mode = "page"        # page / width / actual / custom
        self._zoom_factor = 1.0         # custom のときの倍率（1.0 = 100%）
        self._last_percent = 0.0
        self._press: tuple[QPointF, int, int] | None = None
        self._dragging = False

    # ---- 外部 API ----

    def set_document(self, doc: QPdfDocument | None, page: int = 0) -> None:
        self._doc = doc
        self._clear_cache()
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

    # ズーム

    def zoom_mode(self) -> str:
        return self._zoom_mode

    def zoom_percent(self) -> float:
        return self._scale(self._k) / self._unit() * 100 if self._groups else 100.0

    def set_zoom(self, mode: str, percent: float | None = None) -> None:
        """mode: page（ページレベル）/ width（幅に合わせる）/ actual（100%）/ custom（percent を使う）。"""
        if mode == "custom" and percent is not None:
            self._zoom_factor = max(ZOOM_MIN, min(ZOOM_MAX, percent)) / 100
        self._zoom_mode = mode
        self._relayout(keep_center=True)

    def zoom_in(self) -> None:
        cur = self.zoom_percent()
        self.set_zoom("custom", next((z for z in ZOOM_STEPS if z > cur + 0.5), ZOOM_MAX))

    def zoom_out(self) -> None:
        cur = self.zoom_percent()
        self.set_zoom("custom", next((z for z in reversed(ZOOM_STEPS) if z < cur - 0.5), ZOOM_MIN))

    # ---- 配置 ----

    def _unit(self) -> float:
        """100% の拡大率。1pt を画面上の 1/72 インチにする（macOS では 1pt = 1 論理 px）。"""
        return self.logicalDpiX() / 72.0

    def _sizes(self, k: int) -> tuple[tuple[int | None, ...], list[QSizeF]]:
        sl = slots(self._groups[k], k, self._layout)
        real = [p for p in sl if p is not None]
        ref = self._doc.pagePointSize(real[0])
        sizes = [self._doc.pagePointSize(p) if p is not None else ref for p in sl]
        return sl, [s if s.width() > 0 and s.height() > 0 else QSizeF(595, 842) for s in sizes]

    def _scale(self, k: int) -> float:
        if not self._doc or not (0 <= k < len(self._groups)):
            return self._unit()
        _sl, sizes = self._sizes(k)
        total_w = sum(s.width() for s in sizes)
        max_h = max(s.height() for s in sizes)
        vw = max(1, self.viewport().width() - 2 * _MARGIN)
        vh = max(1, self.viewport().height() - 2 * _MARGIN)
        if self._zoom_mode == "page":
            return min(vw / total_w, vh / max_h)
        if self._zoom_mode == "width":
            return vw / total_w
        if self._zoom_mode == "actual":
            return self._unit()
        return self._zoom_factor * self._unit()

    def _content_size(self, k: int) -> tuple[float, float]:
        if not self._doc or not (0 <= k < len(self._groups)):
            return 0.0, 0.0
        _sl, sizes = self._sizes(k)
        s = self._scale(k)
        return (sum(z.width() for z in sizes) * s + 2 * _MARGIN,
                max(z.height() for z in sizes) * s + 2 * _MARGIN)

    def _placements(self, k: int, for_current: bool = True) -> list[tuple[int, QRectF]]:
        """組 k の各ページの描画矩形（ビューポート座標）。空きスロットは相方と同じ大きさで場所だけ取る。"""
        if not self._doc or not (0 <= k < len(self._groups)):
            return []
        sl, sizes = self._sizes(k)
        s = self._scale(k)
        cw, ch = self._content_size(k)
        vw, vh = self.viewport().width(), self.viewport().height()
        hval = self.horizontalScrollBar().value() if for_current else 0
        vval = self.verticalScrollBar().value() if for_current else 0
        ox = (vw - cw) / 2 if cw <= vw else -hval
        oy = (vh - ch) / 2 if ch <= vh else -vval
        max_h = max(z.height() for z in sizes) * s
        x = ox + _MARGIN
        out = []
        for p, z in zip(sl, sizes):
            w, h = z.width() * s, z.height() * s
            if p is not None:
                out.append((p, QRectF(x, oy + _MARGIN + (max_h - h) / 2, w, h)))
            x += w
        return out

    def _relayout(self, keep_center: bool = False) -> None:
        """倍率・組・ウィンドウの大きさが変わったらスクロール範囲を作り直す。"""
        hs, vs = self.horizontalScrollBar(), self.verticalScrollBar()
        # 表示の中心（内容に対する割合）を覚えておき、倍率を変えても同じ場所を見せる
        fx = (hs.value() + hs.pageStep() / 2) / max(1, hs.maximum() + hs.pageStep())
        fy = (vs.value() + vs.pageStep() / 2) / max(1, vs.maximum() + vs.pageStep())
        if vs.maximum() == 0:
            fy = 0.0        # 収まっていた表示から拡大したら、ページの上端から見せる（読み始めの位置）
        cw, ch = self._content_size(self._k)
        vw, vh = self.viewport().width(), self.viewport().height()
        hs.setRange(0, max(0, round(cw - vw)))
        vs.setRange(0, max(0, round(ch - vh)))
        hs.setPageStep(vw)
        vs.setPageStep(vh)
        hs.setSingleStep(_LINE_STEP)
        vs.setSingleStep(_LINE_STEP)
        if keep_center:
            hs.setValue(round(fx * (hs.maximum() + vw) - vw / 2))
            vs.setValue(round(fy * (vs.maximum() + vh) - vh / 2))
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._overflows()
                                  else Qt.CursorShape.ArrowCursor)
        percent = self.zoom_percent()
        if abs(percent - self._last_percent) > 0.05:
            self._last_percent = percent
            self.zoomChanged.emit(percent)
        self.viewport().update()

    def _overflows(self) -> bool:
        return self.horizontalScrollBar().maximum() > 0 or self.verticalScrollBar().maximum() > 0

    # ---- ページの切り替え ----

    def _regroup(self, page: int) -> None:
        self._groups = groups(self.page_count(), self._layout)
        self._k = index_of_page(page, self._groups)
        self._relayout()
        self.verticalScrollBar().setValue(0)
        self.pageChanged.emit(self.current_page())

    def _go_to_index(self, k: int, show_end: bool = False) -> None:
        k = max(0, min(k, len(self._groups) - 1))
        if k != self._k:
            self._k = k
            self._relayout()
            vs = self.verticalScrollBar()
            vs.setValue(vs.maximum() if show_end else 0)
            self.pageChanged.emit(self.current_page())

    def _scroll_v(self, dy: int) -> bool:
        """縦にスクロールする。動けたら True（端に張り付いていたら False）。"""
        vs = self.verticalScrollBar()
        before = vs.value()
        vs.setValue(before + dy)
        return vs.value() != before

    def _step(self, forward: bool, amount: int) -> None:
        """スクロールできればスクロール、端なら次 / 前の組へ（前へ戻ったら下端を見せる）。"""
        if not self._scroll_v(amount if forward else -amount):
            self._go_to_index(self._k + (1 if forward else -1), show_end=not forward)

    # ---- 画像 ----

    def _clear_cache(self) -> None:
        self._cache.clear()
        self._cache_bytes = 0

    def _image(self, page: int, rect: QRectF, render: bool) -> QImage | None:
        dpr = self.devicePixelRatioF()
        w, h = rect.width() * dpr, rect.height() * dpr
        if w * h > _MAX_PIXELS:                     # 高倍率では上限の解像度で描いて拡大して貼る
            f = (_MAX_PIXELS / (w * h)) ** 0.5
            w, h = w * f, h * f
        px = QSize(max(1, round(w)), max(1, round(h)))
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
        self._cache[key] = img
        self._cache_bytes += img.sizeInBytes()
        while self._cache_bytes > _CACHE_BYTES and len(self._cache) > 1:
            _k, old = self._cache.popitem(last=False)
            self._cache_bytes -= old.sizeInBytes()
        return img

    def _prefetch(self) -> None:
        dpr = self.devicePixelRatioF()
        for k in (self._k + 1, self._k - 1):
            for page, rect in self._placements(k, for_current=False):
                if rect.width() * rect.height() * dpr * dpr <= _PREFETCH_PIXELS:
                    self._image(page, rect, render=True)

    # ---- Qt イベント ----

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), self._bg)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        visible = QRectF(self.viewport().rect())
        for page, rect in self._placements(self._k):
            if not rect.intersects(visible):
                continue
            # pdfium は背景を塗らないページを透明で返す（TeX の出力など）。紙の白を敷く
            painter.fillRect(rect, Qt.GlobalColor.white)
            img = self._image(page, rect, render=True)
            if img is not None:
                painter.drawImage(rect, img)
        painter.end()
        # 次・前の組を先読み（描画が終わってから）
        QTimer.singleShot(0, self._prefetch)

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout(keep_center=True)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        forward_arrow = Qt.Key.Key_Left if self._layout.rtl else Qt.Key.Key_Right
        backward_arrow = Qt.Key.Key_Right if self._layout.rtl else Qt.Key.Key_Left
        page_step = max(_LINE_STEP, int(self.viewport().height() * 0.9))
        if key == forward_arrow:
            self.go(+1)
        elif key == backward_arrow:
            self.go(-1)
        elif key == Qt.Key.Key_Space:
            self._step(not shift, page_step)
        elif key == Qt.Key.Key_Down:
            self._step(True, _LINE_STEP)
        elif key == Qt.Key.Key_Up:
            self._step(False, _LINE_STEP)
        elif key == Qt.Key.Key_PageDown:
            self.go(+1)
        elif key == Qt.Key.Key_PageUp:
            self.go(-1)
        elif key == Qt.Key.Key_Home:
            self.first()
        elif key == Qt.Key.Key_End:
            self.last()
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        # ⌘+ホイールは拡大・縮小（Acrobat と同じ）
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            elif event.angleDelta().y() < 0:
                self.zoom_out()
            event.accept()
            return
        delta = event.angleDelta()
        pixel = event.pixelDelta()
        horizontal = abs(delta.x()) > abs(delta.y())
        hs, vs = self.horizontalScrollBar(), self.verticalScrollBar()
        # はみ出している向きなら、まずスクロール（慣性スクロールもそのまま使う）
        if horizontal and hs.maximum() > 0:
            hs.setValue(hs.value() - (pixel.x() if not pixel.isNull() else delta.x() // 3))
            event.accept()
            return
        if not horizontal and vs.maximum() > 0:
            dy = pixel.y() if not pixel.isNull() else delta.y() // 3
            before = vs.value()
            vs.setValue(before - dy)
            if vs.value() != before:
                self._wheel_acc = 0
                event.accept()
                return
        # ここからはページ送り。macOS のトラックパッドは細かいイベントを大量に出すので積算して刻む。
        # 指を離した後の慣性スクロールは数ページ飛ぶ原因になるので捨てる。
        if event.phase() == Qt.ScrollPhase.ScrollMomentum:
            event.accept()
            return
        if event.phase() in (Qt.ScrollPhase.ScrollBegin, Qt.ScrollPhase.ScrollEnd):
            self._wheel_acc = 0
        if horizontal:
            # 横スワイプ。x < 0 は「右へ進む」（指を左へ払う）。右綴じの本は逆向きに進む
            self._wheel_acc += -delta.x() if self._layout.rtl else delta.x()
        else:
            self._wheel_acc += delta.y()
        while self._wheel_acc <= -_WHEEL_STEP:
            self._wheel_acc += _WHEEL_STEP
            self._go_to_index(self._k + 1)
        while self._wheel_acc >= _WHEEL_STEP:
            self._wheel_acc -= _WHEEL_STEP
            self._go_to_index(self._k - 1, show_end=not horizontal)
        event.accept()

    def viewportEvent(self, event: QEvent) -> bool:
        # トラックパッドのピンチで拡大・縮小（ジェスチャーはビューポートに届く）
        if event.type() == QEvent.Type.NativeGesture and isinstance(event, QNativeGestureEvent):
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.set_zoom("custom", self.zoom_percent() * (1 + event.value()))
                return True
        return super().viewportEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus()
        self._press = (event.position(), self.horizontalScrollBar().value(), self.verticalScrollBar().value())
        self._dragging = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None or not self._overflows():
            return
        start, h0, v0 = self._press
        d = event.position() - start
        if not self._dragging and abs(d.x()) + abs(d.y()) < _CLICK_SLOP:
            return
        self._dragging = True
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self.horizontalScrollBar().setValue(round(h0 - d.x()))
        self.verticalScrollBar().setValue(round(v0 - d.y()))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return super().mouseReleaseEvent(event)
        dragged = self._dragging
        self._press = None
        self._dragging = False
        if dragged:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            return
        left = event.position().x() < self.viewport().width() / 2
        # 左半分は「←」と同じ（右綴じなら次、左綴じなら前）
        forward = left == self._layout.rtl
        self.go(+1 if forward else -1)
