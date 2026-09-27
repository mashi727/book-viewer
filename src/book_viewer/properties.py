"""文書のプロパティ（⌘D）。Acrobat の同名ダイアログの「開き方」「詳細設定 › 綴じ方」に相当。

OK で PDF 本体に書き込む（pdfprefs.write_layout）。項目名は Acrobat に揃える。

  開き方 › ページレイアウト: 単一ページ / 見開きページ / 見開きページ（表紙）
  詳細設定 › 綴じ方:         左 / 右
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .layout import Layout

_PAGE_LAYOUTS = [
    ("単一ページ", False, True),
    ("見開きページ", True, False),
    ("見開きページ（表紙）", True, True),
]


_TEXT_WIDTH = 360        # ファイル名・場所の表示幅 (px)。これを超える分は中ほどを「…」で省略する


def _elided_label(text: str) -> QLabel:
    """長いパスでダイアログが横に伸びないよう、中ほどを省略して表示する（全体はツールチップ）。

    パスには空白が無く、折り返し（wordWrap）では折り返されない。
    """
    label = QLabel()
    label.setText(label.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, _TEXT_WIDTH))
    label.setToolTip(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class DocumentPropertiesDialog(QDialog):
    def __init__(self, path: str, pages: int, layout: Layout, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文書のプロパティ")
        p = Path(path)

        summary = QWidget()
        form = QFormLayout(summary)
        form.addRow("ファイル:", _elided_label(p.name))
        form.addRow("場所:", _elided_label(str(p.parent)))
        try:
            size_mb = p.stat().st_size / 1e6
            form.addRow("ファイルサイズ:", QLabel(f"{size_mb:,.1f} MB"))
        except OSError:
            pass
        form.addRow("ページ数:", QLabel(str(pages)))

        opening = QWidget()
        form = QFormLayout(opening)
        self._page_layout = QComboBox()
        for label, _spread, _cover in _PAGE_LAYOUTS:
            self._page_layout.addItem(label)
        current = 0 if not layout.spread else (2 if layout.cover_single else 1)
        self._page_layout.setCurrentIndex(current)
        form.addRow("ページレイアウト:", self._page_layout)

        advanced = QWidget()
        form = QFormLayout(advanced)
        self._binding = QComboBox()
        self._binding.addItems(["左", "右"])
        self._binding.setCurrentIndex(1 if layout.rtl else 0)
        form.addRow("綴じ方:", self._binding)

        tabs = QTabWidget()
        tabs.addTab(summary, "概要")
        tabs.addTab(opening, "開き方")
        tabs.addTab(advanced, "詳細設定")
        tabs.setCurrentWidget(opening)

        note = QLabel("OK を押すと PDF ファイルに書き込みます（変更前の値は履歴に残ります）。")
        note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        # Qt の翻訳を読み込んでいないので、標準ボタンの文言は明示する
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout_ = QVBoxLayout(self)
        layout_.addWidget(tabs)
        layout_.addWidget(note)
        layout_.addWidget(buttons)

    def result_layout(self) -> Layout:
        _label, spread, cover = _PAGE_LAYOUTS[self._page_layout.currentIndex()]
        return Layout(spread=spread, cover_single=cover, rtl=self._binding.currentIndex() == 1)
