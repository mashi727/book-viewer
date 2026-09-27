"""空白ページを挿入（⇧⌘T / サムネールの右クリック）。Acrobat の「ページを挿入」に相当。

  位置: 後 / 前
  ページ: 最初 / 最後 / ページ番号
  枚数: 1〜

空白ページの大きさは基準ページ（挿入位置の隣のページ）と同じ。基準ページの大きさを mm で示す。
"""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class InsertBlankDialog(QDialog):
    def __init__(self, pages: int, current: int, size_mm: Callable[[int], tuple[float, float]], parent=None):
        """pages: 総ページ数、current: 初期のページ（0 始まり）、size_mm: ページ番号 → (幅, 高さ) mm。"""
        super().__init__(parent)
        self.setWindowTitle("空白ページを挿入")
        self._pages = pages
        self._size_mm = size_mm

        self._where = QComboBox()
        self._where.addItems(["後", "前"])

        self._first = QRadioButton("最初")
        self._last = QRadioButton("最後")
        self._at = QRadioButton("ページ:")
        self._at.setChecked(True)
        group = QButtonGroup(self)
        for b in (self._first, self._last, self._at):
            group.addButton(b)
        self._page = QSpinBox()
        self._page.setRange(1, max(1, pages))
        self._page.setValue(current + 1)
        of = QLabel(f"/ {pages}")
        page_row = QHBoxLayout()
        page_row.setContentsMargins(0, 0, 0, 0)
        for w in (self._first, self._last, self._at, self._page, of):
            page_row.addWidget(w)
        page_row.addStretch(1)
        page_box = QWidget()
        page_box.setLayout(page_row)

        self._count = QSpinBox()
        self._count.setRange(1, 100)
        self._count.setValue(1)

        self._size = QLabel()

        form = QFormLayout()
        form.addRow("位置:", self._where)
        form.addRow("ページ:", page_box)
        form.addRow("枚数:", self._count)
        form.addRow("大きさ:", self._size)

        note = QLabel("OK を押すと PDF ファイルに書き込みます（挿入した位置は履歴に残ります）。")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        # Qt の翻訳を読み込んでいないので、標準ボタンの文言は明示する
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

        for sig in (self._where.currentIndexChanged, self._page.valueChanged, group.buttonToggled):
            sig.connect(lambda *_: self._update())
        self._update()

    def _target_page(self) -> int:
        """基準のページ（0 始まり）。"""
        if self._first.isChecked():
            return 0
        if self._last.isChecked():
            return self._pages - 1
        return self._page.value() - 1

    def _update(self) -> None:
        self._page.setEnabled(self._at.isChecked())
        page = self._target_page()
        try:
            w, h = self._size_mm(page)
            self._size.setText(f"{page + 1} ページと同じ（{w:.0f} × {h:.0f} mm）")
        except Exception:
            self._size.setText(f"{page + 1} ページと同じ")

    def insertion(self) -> tuple[int, int, int]:
        """(挿入位置 index, 枚数, 基準ページ)。index は 0 なら先頭、総ページ数なら末尾。"""
        ref = self._target_page()
        index = ref + 1 if self._where.currentIndex() == 0 else ref
        return index, self._count.value(), ref
