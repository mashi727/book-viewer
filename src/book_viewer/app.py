"""自炊本PDFリーダー。

  book-viewer [フォルダ | PDF]

画面は左から フォルダツリー | ページサムネール | ページ。フォルダツリーは Windows
エクスプローラー風、それ以外の操作・用語・ショートカットは Adobe Acrobat に揃える。

  ファイル:
    ⌘O 開く…（PDF）   ⇧⌘O フォルダを開く…   ⌘S 保存   ⌘W 閉じる   ⌘D 文書のプロパティ…
  編集:
    ⇧⌘F ファイル名を検索…（起動フォルダ以下、サブフォルダも含む。Esc で消す）
  文書:
    ⇧⌘T 空白ページを挿入…（基準ページと同じ大きさ。サムネールの右クリックからも）
    ページの移動 … サムネールを ⇧ / ⌘ で複数選択してドラッグ
    挿入・移動は未保存の変更になり、［保存］でまとめて PDF に書き込む
  表示:
    ページナビゲーション … 最初 / 前 / 次 / 最後のページ、⇧⌘N ページへ移動…
    ページ表示 …… 単一ページ表示 / 見開きページ表示 / 見開きページ表示で表紙を表示
    表示切り替え … F4 ページサムネール、フォルダツリー
  ツールバーの「見開きページ表示 ▾」… 本体で見開きに切替、▾ で表紙あり / なしを選ぶ
    ズーム ……… ⌘+ / ⌘- ズームイン / アウト、⌘0 ページレベルにズーム、⌘1 実際のサイズ、⌘2 幅に合わせる
    ⌘L 全画面モード（Esc で解除）
  読書:
    → ← Space ホイール クリック … ページ送り（詳細は spread_view.py）
    ツールバーの ↑ ↓ とページ番号欄 … 前 / 次のページ、番号を入れて Enter で移動
  本棚（フォルダツリー）:
    フォルダをクリック … 開閉   ⌘↑ … 親フォルダを選択して閉じる
    .. … 起動フォルダを 1 つ上へ付け替える（起動フォルダを選択中の ⌘↑ も同じ）
  開き方の保存:
    ⌘D の「開き方 › ページレイアウト」と「詳細設定 › 綴じ方」を OK で PDF に書き込む。
    ツールバー（単一 / 見開き▾ / 左綴じ / 右綴じ）と表示メニューでも同じ項目を切り替えられ、
    ［保存］か ⌘S でいまの表示を PDF に書き込む。PDF と違う表示のときだけ［保存 •］になる
  自動再読込:
    表示中の PDF が書き換えられたら（TeX の再コンパイル等）、書き込みが落ち着くのを
    待って読み込み直す。ページ位置と、その場の表示は保つ。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QFont, QIntValidator, QKeySequence, QShortcut
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QToolButton,
    QToolTip,
)

from . import direction, pdfprefs
from .file_browser import FileBrowserPanel
from .layout import Layout
from .insert_dialog import InsertBlankDialog
from .properties import DocumentPropertiesDialog
from .spread_view import SpreadView
from .state import Store, cache_dir, state_dir
from .thumbnails import ThumbnailPane

_DEFAULT_SIZE = QSize(1920, 1080)   # 既定のウィンドウサイズ（FHD）。画面が小さければ収まる大きさに縮める
# アプリ内の文字はすべてこの大きさ（macOS の既定は 13pt、ツールバー 10pt、ツールチップ 11pt）。
# macOS は 1pt = 1 論理 px、Windows は 1pt = 96/72 px なので、Windows の 12pt が macOS の 16pt と同じ 16px になる
_UI_PT = 12 if sys.platform == "win32" else 16
_RELOAD_SETTLE_MS = 400      # 書き込みが止んだと見なすまでの待ち
_RELOAD_MAX_TRIES = 50       # 400ms × 50 ≒ 20 秒待って読めなければ諦める



def _work_path(book: str) -> Path:
    """未保存の挿入を入れておく作業用コピー（プロセスごと・本ごと）。"""
    key = hashlib.sha1(book.encode()).hexdigest()[:12]
    return cache_dir() / "work" / f"{os.getpid()}-{key}.pdf"


def _clean_old_work_files(max_age: float = 24 * 3600) -> None:
    """落ちたり Windows で消せなかったりして残った作業用コピーを掃除する（1 日以上前のもの）。"""
    now = time.time()
    for f in (cache_dir() / "work").glob("*.pdf"):
        try:
            if now - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


def _log_path() -> Path:
    """PDF への書き込み履歴（開き方の変更前の値、空白ページの挿入位置）。"""
    return state_dir() / "prefs-log.jsonl"


class BookViewer(QMainWindow):
    def __init__(self, directory: str, open_pdf: str | None = None):
        super().__init__()
        self._apply_ui_font()
        self.setWindowTitle("Book Viewer")
        self._store = Store()
        self._root = str(Path(directory).resolve())
        self._book: str | None = None
        self._saved_layout = Layout()       # PDF に書かれている開き方
        # 未保存のページの編集（空白ページの挿入・ページの移動）。元の PDF には手を付けず、
        # 作業用コピー（_work）に入れて表示し、［保存］で元の PDF にまとめて書き込む（Acrobat と同じ）。
        # 要素は pdfprefs.apply_edits の操作 ("insert", index, count, ref) / ("move", rows, dest)
        self._work: str | None = None
        self._pending: list[tuple] = []
        _clean_old_work_files()

        # 左: 本棚（フォルダツリー）
        self._browser = FileBrowserPanel(Path(self._root), self._store.position, font_size=_UI_PT)
        self._browser.close_requested.connect(lambda: self._set_tree_visible(False))
        self._browser.pdf_clicked.connect(lambda p: self._open_book(str(p)))
        self._browser.start_dir_changed.connect(lambda p: setattr(self, "_root", str(p)))
        self._browser.reveal(self._root)

        # 中: ページサムネール
        self._thumbs = ThumbnailPane()
        self._thumbs.page_clicked.connect(lambda page: self._view.go_to_page(page))
        self._thumbs.close_requested.connect(lambda: self._set_thumbs_visible(False))
        self._thumbs.insert_blank_requested.connect(self._show_insert_blank)
        self._thumbs.move_requested.connect(self._move_pages)

        # 右: ページ
        self._doc = QPdfDocument(self)
        self._view = SpreadView()
        self._view.pageChanged.connect(self._on_page_changed)
        self._view.zoomChanged.connect(self._on_zoom_changed)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._browser)
        self._splitter.addWidget(self._thumbs)
        self._splitter.addWidget(self._view)
        for i, stretch in enumerate((0, 0, 1)):
            self._splitter.setStretchFactor(i, stretch)
        # フラットな 1px の分割線。1px でも Qt は掴める幅を 5px に広げる（見た目は 1px のまま）
        self._splitter.setHandleWidth(1)
        self._splitter.setStyleSheet("QSplitter::handle { background-color: palette(mid); }")
        self.setCentralWidget(self._splitter)
        self.setStatusBar(QStatusBar())

        self._build_actions()
        self._build_menus()
        self._build_toolbar()

        QShortcut(QKeySequence("Ctrl+Up"), self).activated.connect(self._browser.select_parent)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(
            lambda: self._set_fullscreen(False)
        )

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

        self._apply_default_size()
        self._set_tree_visible(self._store.ui("show_tree", True))
        self._set_thumbs_visible(self._store.ui("show_thumbnails", True))
        self._view.set_zoom(self._store.ui("zoom_mode", "page"), self._store.ui("zoom_percent", 100.0))
        self._on_page_changed(0)
        self._update_save_state()               # 本を開くまでは保存できない

        if open_pdf:
            self._open_book(open_pdf)
            self._browser.reveal(open_pdf)

    @staticmethod
    def _apply_ui_font() -> None:
        """アプリ内の文字を _UI_PT に揃える。

        QApplication.setFont は既定の文字を変えるが、macOS がクラスごとに割り当てた
        小さい文字（ツールバーのボタン、ポップアップメニュー）とツールチップには
        効かないので、それらは個別に設定する（_build_toolbar と下の QToolTip）。
        """
        font = QFont(QApplication.font())
        font.setPointSize(_UI_PT)
        QApplication.setFont(font)
        QToolTip.setFont(font)

    def _apply_default_size(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        w = min(_DEFAULT_SIZE.width(), avail.width())
        h = min(_DEFAULT_SIZE.height(), avail.height())
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)
        self._splitter.setSizes([420, 170, max(400, w - 590)])

    # ---- アクション（メニューとツールバーで共有） ----

    def _action(self, text: str, shortcut: str | QKeySequence.StandardKey | None = None,
                slot=None, checkable: bool = False, tip: str | None = None) -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        seq = a.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
        a.setToolTip(f"{tip or text} ({seq})" if seq else (tip or text))
        if slot is not None:
            if checkable:
                a.triggered.connect(slot)                         # checked を受け取る
            else:
                a.triggered.connect(lambda _checked=False, s=slot: s())
        return a

    def _build_actions(self) -> None:
        A = self._action
        # ファイル
        self._act_open = A("開く…", QKeySequence.StandardKey.Open, self._open_file, tip="PDF を開く")
        self._act_open_folder = A("フォルダを開く…", "Ctrl+Shift+O", self._open_folder)
        self._act_save = A("保存", QKeySequence.StandardKey.Save, self._save_current,
                           tip="いまの表示（ページレイアウト・綴じ方）を PDF に保存")
        self._act_close = A("閉じる", QKeySequence.StandardKey.Close, self._close_book)
        self._act_props = A("文書のプロパティ…", "Ctrl+D", self._show_properties)
        self._act_find = A("ファイル名を検索…", "Ctrl+Shift+F", self._focus_search,
                            tip="起動フォルダ以下（サブフォルダも含む）の PDF をファイル名で探す")
        self._act_insert_blank = A("空白ページを挿入…", "Ctrl+Shift+T", self._show_insert_blank,
                                   tip="基準ページと同じ大きさの空白ページを挿入")
        # ページナビゲーション（←→ Home End はページビューが受け持つので、ここでは割り当てない。
        # 割り当てるとフォルダツリーやページ番号欄で矢印キーが使えなくなる）
        self._act_first = A("最初のページ", None, self._view.first)
        self._act_prev = A("前のページ", None, lambda: self._view.go(-1), tip="前のページを表示")
        self._act_next = A("次のページ", None, lambda: self._view.go(+1), tip="次のページを表示")
        self._act_last = A("最後のページ", None, self._view.last)
        self._act_goto = A("ページへ移動…", "Ctrl+Shift+N", self._go_to_page_dialog)
        # ページ表示
        self._act_single = A("単一ページ表示", None, self._on_view_layout_changed, checkable=True)
        self._act_spread = A("見開きページ表示", None, self._on_view_layout_changed, checkable=True)
        group = QActionGroup(self)
        group.setExclusive(True)
        group.addAction(self._act_single)
        group.addAction(self._act_spread)
        self._act_single.setChecked(True)
        self._act_cover = A("見開きページ表示で表紙を表示", None, self._on_view_layout_changed, checkable=True)
        self._act_cover.setChecked(True)
        # ツールバーの「見開きページ表示 ▾」のプルダウン。表示メニューのチェックと同じ状態を持つ
        self._act_cover_on = A("見開きページ（表紙あり）", None, lambda on: self._set_cover(True),
                               checkable=True, tip="1 ページ目（表紙）を単独で表示する")
        self._act_cover_off = A("見開きページ（表紙なし）", None, lambda on: self._set_cover(False),
                                checkable=True, tip="1 ページ目から 2 ページずつ並べる")
        cover_group = QActionGroup(self)
        cover_group.setExclusive(True)
        cover_group.addAction(self._act_cover_on)
        cover_group.addAction(self._act_cover_off)
        self._act_cover_on.setChecked(True)
        # 綴じ方（文書のプロパティ › 詳細設定 と同じ項目。ボタンで切り替えて表示に即反映、保存は ⌘S）
        self._act_ltr = A("左綴じ", None, self._on_view_layout_changed, checkable=True,
                          tip="左綴じ（横書き。→ で次のページ）")
        self._act_rtl = A("右綴じ", None, self._on_view_layout_changed, checkable=True,
                          tip="右綴じ（縦書き。← で次のページ）")
        binding = QActionGroup(self)
        binding.setExclusive(True)
        binding.addAction(self._act_ltr)
        binding.addAction(self._act_rtl)
        self._act_ltr.setChecked(True)
        # 表示切り替え
        self._act_thumbs = A("ページサムネール", "F4", self._set_thumbs_visible, checkable=True,
                             tip="ページサムネールを表示 / 非表示")
        self._act_tree = A("フォルダツリー", None, self._set_tree_visible, checkable=True,
                           tip="フォルダツリーを表示 / 非表示")
        self._act_full = A("全画面モード", "Ctrl+L", self._set_fullscreen, checkable=True)
        # ズーム（Acrobat と同じキー）
        self._act_zoom_in = A("ズームイン", QKeySequence.StandardKey.ZoomIn, self._view.zoom_in)
        self._act_zoom_out = A("ズームアウト", QKeySequence.StandardKey.ZoomOut, self._view.zoom_out)
        self._act_zoom_page = A("ページレベルにズーム", "Ctrl+0", lambda: self._set_zoom("page"))
        self._act_zoom_actual = A("実際のサイズ", "Ctrl+1", lambda: self._set_zoom("actual"))
        self._act_zoom_width = A("幅に合わせる", "Ctrl+2", lambda: self._set_zoom("width"))
        self._act_zoom_in.setIconText("+")
        self._act_zoom_out.setIconText("−")

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("ファイル")
        m.addActions([self._act_open, self._act_open_folder])
        m.addSeparator()
        m.addActions([self._act_save, self._act_close])
        m.addSeparator()
        m.addAction(self._act_props)

        m = mb.addMenu("編集")
        m.addAction(self._act_find)

        m = mb.addMenu("文書")
        m.addAction(self._act_insert_blank)

        m = mb.addMenu("表示")
        nav = m.addMenu("ページナビゲーション")
        nav.addActions([self._act_first, self._act_prev, self._act_next, self._act_last])
        nav.addSeparator()
        nav.addAction(self._act_goto)
        disp = m.addMenu("ページ表示")
        disp.addActions([self._act_single, self._act_spread])
        disp.addSeparator()
        disp.addAction(self._act_cover)
        disp.addSeparator()
        disp.addActions([self._act_ltr, self._act_rtl])
        zoom = m.addMenu("ズーム")
        zoom.addActions([self._act_zoom_in, self._act_zoom_out])
        zoom.addSeparator()
        zoom.addActions([self._act_zoom_page, self._act_zoom_actual, self._act_zoom_width])
        toggle = m.addMenu("表示切り替え")
        panes = toggle.addMenu("ナビゲーションパネル")
        panes.addAction(self._act_thumbs)
        toggle.addAction(self._act_tree)
        m.addSeparator()
        m.addAction(self._act_full)

    def _build_toolbar(self) -> None:
        tb = QToolBar("main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self._toolbar = tb

        self._act_prev.setIconText("↑")
        self._act_next.setIconText("↓")
        tb.addAction(self._act_open)
        tb.addAction(self._act_save)
        tb.addSeparator()
        tb.addAction(self._act_thumbs)
        tb.addSeparator()
        tb.addAction(self._act_prev)
        tb.addAction(self._act_next)
        self._page_box = QLineEdit()
        self._page_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_box.setFixedWidth(70)
        self._page_box.setToolTip("ページ番号を入れて Enter で移動")
        self._page_box.returnPressed.connect(self._on_page_box)
        tb.addWidget(self._page_box)
        self._page_total = QLabel()
        self._page_total.setContentsMargins(4, 0, 8, 0)
        tb.addWidget(self._page_total)
        tb.addSeparator()
        tb.addAction(self._act_zoom_out)
        self._zoom_box = QComboBox()
        self._zoom_box.setEditable(True)
        self._zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._zoom_box.setMinimumContentsLength(6)
        self._zoom_box.setToolTip("倍率（数字を入れて Enter）")
        for label, data in (("ページレベルにズーム", "page"), ("幅に合わせる", "width"), ("実際のサイズ", "actual")):
            self._zoom_box.addItem(label, data)
        self._zoom_box.insertSeparator(3)
        for z in (50, 75, 100, 125, 150, 200, 300, 400):
            self._zoom_box.addItem(f"{z}%", float(z))
        self._zoom_box.activated.connect(self._on_zoom_box_activated)
        self._zoom_box.lineEdit().returnPressed.connect(self._on_zoom_box_entered)
        tb.addWidget(self._zoom_box)
        tb.addAction(self._act_zoom_in)
        tb.addSeparator()
        tb.addAction(self._act_single)
        spread_btn = QToolButton()
        spread_btn.setDefaultAction(self._act_spread)
        spread_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)   # 本体=切替、▾=表紙の有無
        cover_menu = QMenu(spread_btn)
        cover_menu.setFont(QApplication.font())       # macOS はメニューに小さい文字を割り当てる
        cover_menu.addActions([self._act_cover_on, self._act_cover_off])
        spread_btn.setMenu(cover_menu)
        tb.addWidget(spread_btn)
        tb.addSeparator()
        tb.addAction(self._act_ltr)
        tb.addAction(self._act_rtl)
        tb.addSeparator()
        tb.addAction(self._act_full)

        # macOS の既定では「選択中」のボタンの文字が薄く、押せない（無効）ボタンと見分けにくい。
        # 選択中は灰色の背景に通常の文字色（アクセント色はウィンドウが非アクティブだと淡くなり
        # 白い文字が読みにくい）。無効は薄い文字。▾ 付きのボタンは ▾ の分の余白を取る
        tb.setStyleSheet(
            "QToolButton { padding: 2px 6px; }"
            "QToolButton:checked { background: palette(mid); color: palette(text); border-radius: 4px; }"
            "QToolButton:disabled { color: palette(mid); }"
            'QToolButton[popupMode="1"] { padding-right: 18px; }')
        # ボタンはアクション追加時に作られるので、最後にまとめて文字を大きくする
        font = QApplication.font()
        for w in (tb, self._page_box, self._page_total, self._zoom_box, *tb.findChildren(QToolButton)):
            w.setFont(font)
        self._zoom_box.view().setFont(font)

    # ---- 開き方（表示）と文書のプロパティ ----

    def _current_layout(self) -> Layout:
        return Layout(
            spread=self._act_spread.isChecked(),
            cover_single=self._act_cover.isChecked(),
            rtl=self._act_rtl.isChecked(),
        )

    def _apply_layout(self, layout: Layout) -> None:
        """開き方をボタン・表示メニューに反映して、ページビューに適用する。"""
        # setChecked は toggled しか出さず、スロットは triggered（ユーザー操作）に繋いでいるので
        # シグナルを止める必要はない。止めると QActionGroup が選択の切り替わりを知らず、
        # 「単一」と「見開き」が両方チェックされたままになる
        self._act_spread.setChecked(layout.spread)
        self._act_single.setChecked(not layout.spread)
        self._act_cover.setChecked(layout.cover_single)
        self._act_cover.setEnabled(layout.spread)
        self._sync_cover_choice(layout.cover_single)
        self._act_rtl.setChecked(layout.rtl)
        self._act_ltr.setChecked(not layout.rtl)
        self._view.set_layout(layout)
        self._update_save_state()

    def _on_view_layout_changed(self, _checked: bool = False) -> None:
        self._act_cover.setEnabled(self._act_spread.isChecked())
        self._sync_cover_choice(self._act_cover.isChecked())
        self._view.set_layout(self._current_layout())
        self._update_save_state()

    def _update_save_state(self) -> None:
        """保存するものがあるときだけ［保存 •］にする。

        保存するもの = 未保存の空白ページの挿入、または PDF に書かれた開き方と違う表示。
        ウィンドウの未保存の印（閉じるボタンの点）は、文書の変更（挿入）があるときだけ付ける。
        """
        dirty = self._book is not None and (bool(self._pending) or self._current_layout() != self._saved_layout)
        self._act_save.setEnabled(dirty)
        self._act_save.setText("保存 •" if dirty else "保存")
        self.setWindowModified(bool(self._pending))

    def _save_current(self) -> bool:
        """［保存］・⌘S: 未保存の挿入と、いまの表示（ページレイアウト・綴じ方）を PDF に書き込む。"""
        if not self._book:
            return True
        layout = self._current_layout()
        if layout == self._saved_layout and not self._pending:
            return True
        try:
            self._write_layout(layout)
        except Exception as e:  # 暗号化 PDF・壊れた PDF・書き込み不可など
            QMessageBox.warning(self, "PDF に保存できませんでした", f"{Path(self._book).name}\n\n{e}")
            return False
        self.statusBar().showMessage("PDF に保存しました", 3000)
        return True

    def _set_cover(self, cover: bool) -> None:
        """プルダウンで表紙の有無を選んだ。見開きでなければ見開きにする。"""
        self._act_cover.setChecked(cover)
        self._act_spread.setChecked(True)
        self._on_view_layout_changed()

    def _sync_cover_choice(self, cover: bool) -> None:
        (self._act_cover_on if cover else self._act_cover_off).setChecked(True)

    def _show_properties(self) -> None:
        if not self._book:
            return
        # 初期値は「いま見えている表示」。表示を整えてから ⌘D → OK で、見たとおりに保存できる
        dlg = DocumentPropertiesDialog(self._book, self._view.page_count(), self._current_layout(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # OK はその場で保存する（未保存の空白ページの挿入も一緒に書き込む）
        self._apply_layout(dlg.result_layout())
        self._save_current()

    def _write_layout(self, layout: Layout) -> None:
        """未保存の挿入と開き方を元の PDF に 1 回で書き込み、元の PDF を開き直して表示に反映する。"""
        book = self._book
        if not book:
            return
        ops = list(self._pending)
        new_layout = layout if layout != self._saved_layout else None   # 変えていない開き方は書かない
        self._modify_pdf(
            lambda path: pdfprefs.apply_edits(path, ops, new_layout, log_path=_log_path()),
            target=book, show=book)
        self._discard_work()
        self._saved_layout = layout
        self._apply_layout(layout)

    def _edit(self, op: tuple, page_after) -> None:
        """ページの編集を作業用コピーに適用して表示する（未保存。［保存］で元の PDF に書き込む）。

        page_after: 編集前に表示していたページ → 編集後の同じ内容のページ。
        """
        book = self._book
        if not book:
            return
        work = self._work
        if work is None:
            work = str(_work_path(book))
            Path(work).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(book, work)
        try:
            self._modify_pdf(lambda path: pdfprefs.apply_edits(path, [op]),
                             target=work, show=work, page_after=page_after)
        except Exception:
            if self._work is None:
                Path(work).unlink(missing_ok=True)
            raise
        self._work = work
        self._pending.append(op)
        self._update_save_state()

    def _insert_blank(self, index: int, count: int, ref: int) -> None:
        """空白ページを挿入する。挿入位置より後ろを読んでいたら、同じ内容のページを見せ続ける。"""
        self._edit(("insert", index, count, ref),
                   page_after=lambda page: page + count if index <= page else page)
        self.statusBar().showMessage(
            f"空白ページを {count} 枚挿入しました（{index + 1} ページ目から。［保存］で PDF に書き込みます）", 8000)

    def _move_pages(self, rows: list, dest: int) -> None:
        """サムネイルのドラッグ: rows のページを dest の位置へ移す。移したページを選んだままにする。"""
        n = self._view.page_count()
        order = pdfprefs.move_order(n, rows, dest)
        if order == list(range(n)):
            return                                  # 自分の位置に落とした
        new_pos = {old: new for new, old in enumerate(order)}
        try:
            self._edit(("move", sorted(rows), dest), page_after=lambda page: new_pos.get(page, page))
        except Exception as e:
            QMessageBox.warning(self, "ページを移動できませんでした", str(e))
            return
        self._thumbs.select_pages(sorted(new_pos[r] for r in rows))
        first = min(new_pos[r] for r in rows) + 1
        self.statusBar().showMessage(
            f"{len(rows)} ページを移動しました（{first} ページ目へ。［保存］で PDF に書き込みます）", 8000)

    def _discard_work(self) -> None:
        """未保存の挿入を捨てる（作業用コピーを消す）。"""
        if self._work:
            try:
                Path(self._work).unlink(missing_ok=True)
            except OSError:
                pass                      # Windows で開いたまま等。起動時の掃除で消える
        self._work = None
        self._pending = []
        self._update_save_state()

    def _confirm_discard(self) -> bool:
        """未保存の挿入があれば「保存しますか？」と聞く。続けてよければ True。"""
        if not self._pending or not self._book:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"「{Path(self._book).name}」への変更を保存しますか？")
        box.setInformativeText("ページの挿入・移動が保存されていません。保存しないと失われます。")
        save = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("保存しない", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save)
        box.exec()
        if box.clickedButton() is save:
            return self._save_current()
        if box.clickedButton() is discard:
            self._discard_work()
            return True
        return False

    def _show_insert_blank(self, page: int | None = None) -> None:
        if not self._book:
            return
        book = self._book
        dlg = InsertBlankDialog(self._view.page_count(),
                                self._view.current_page() if page is None else page,
                                lambda p: pdfprefs.page_size_mm(self._work or book, p), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        index, count, ref = dlg.insertion()
        try:
            self._insert_blank(index, count, ref)
        except Exception as e:  # 暗号化 PDF・壊れた PDF・書き込み不可など
            QMessageBox.warning(self, "空白ページを挿入できませんでした", f"{Path(book).name}\n\n{e}")

    def _modify_pdf(self, write, target: str, show: str, page_after=lambda page: page) -> None:
        """target を書き換え、show を開き直して表示する（保存・空白ページの挿入で共通）。

        書き込みに失敗したら、それまで表示していたもの（作業用コピーか元の PDF）を開き直す。

        Windows では開いているファイルを置き換えられない（os.replace が WinError 5）。
        ページビューとサムネイルのスレッドが PDF を開いたままなので、書き込みの間だけ
        両方を閉じ、終わったら（失敗しても）開き直す。書き込みは同期処理で、その間に
        再描画は走らないので画面はちらつかない。
        """
        page = self._view.current_page()
        before = self._work or self._book
        self._thumbs.release_file()
        self._doc.close()
        ok = False
        try:
            write(target)
            ok = True
        finally:
            path = show if ok else before
            if ok:
                page = page_after(page)
            doc = QPdfDocument(self)
            doc.load(path)
            old, self._doc = self._doc, doc
            self._thumbs.set_document(path, doc.pageCount())
            self._view.set_document(doc, page)
            old.deleteLater()
            if self._book:
                self._loaded_sig = self._file_sig(self._book)   # 自分の書き込みで再読込が走らないように
                if self._book not in self._file_watcher.files():
                    self._file_watcher.addPath(self._book)

    # ---- ズーム ----

    def _set_zoom(self, mode: str, percent: float | None = None) -> None:
        self._view.set_zoom(mode, percent)
        self._store.set_ui("zoom_mode", mode)
        if mode == "custom":
            self._store.set_ui("zoom_percent", self._view.zoom_percent())
        self._store.save()

    def _on_zoom_changed(self, percent: float) -> None:
        # ズームイン / アウト・ピンチ・⌘ホイールで変わった倍率も記録する
        if self._view.zoom_mode() == "custom":
            self._store.set_ui("zoom_mode", "custom")
            self._store.set_ui("zoom_percent", percent)
        self._zoom_box.setEditText(f"{percent:.0f}%")

    def _on_zoom_box_activated(self, index: int) -> None:
        data = self._zoom_box.itemData(index)
        if isinstance(data, str):
            self._set_zoom(data)
        elif isinstance(data, float):
            self._set_zoom("custom", data)
        self._zoom_box.setEditText(f"{self._view.zoom_percent():.0f}%")
        self._view.setFocus()

    def _on_zoom_box_entered(self) -> None:
        text = self._zoom_box.currentText().strip().rstrip("%").strip()
        try:
            self._set_zoom("custom", float(text))
        except ValueError:
            pass
        self._zoom_box.setEditText(f"{self._view.zoom_percent():.0f}%")
        self._view.setFocus()

    # ---- 表示切り替え ----

    def _set_thumbs_visible(self, on: bool) -> None:
        on = bool(on)
        self._act_thumbs.setChecked(on)
        self._thumbs.setVisible(on)
        self._store.set_ui("show_thumbnails", on)
        self._store.save()
        if on:
            self._thumbs.set_current_pages(self._view.current_group())

    def _focus_search(self) -> None:
        if not self._act_tree.isChecked():
            self._set_tree_visible(True)
        self._browser.focus_search()

    def _set_tree_visible(self, on: bool) -> None:
        on = bool(on)
        self._act_tree.setChecked(on)
        self._browser.setVisible(on)
        self._store.set_ui("show_tree", on)
        self._store.save()

    def _set_fullscreen(self, on: bool) -> None:
        on = bool(on)
        if on == self.isFullScreen():
            self._act_full.setChecked(on)
            return
        self._act_full.setChecked(on)
        # 全画面ではページだけにする。解除したら表示切り替えの状態に戻す
        self._browser.setVisible(not on and self._act_tree.isChecked())
        self._thumbs.setVisible(not on and self._act_thumbs.isChecked())
        self.statusBar().setVisible(not on)
        self._toolbar.setVisible(not on)
        if on:
            self.showFullScreen()
        else:
            self.showNormal()
        self._view.setFocus()

    # ---- 開く・閉じる ----

    def _open_file(self) -> None:
        start = str(Path(self._book).parent) if self._book else self._root
        chosen, _ = QFileDialog.getOpenFileName(self, "開く", start, "PDF (*.pdf *.PDF)")
        if chosen:
            self._open_book(chosen)
            self._browser.reveal(chosen)

    def _open_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "フォルダを開く", self._root)
        if chosen:
            self._root = chosen
            self._browser.reveal(chosen)

    def _open_book(self, path: str) -> None:
        path = str(Path(path).resolve())
        if path == self._book:
            self._view.setFocus()
            return
        if not self._confirm_discard():
            return
        self._save_position()
        self._discard_work()
        doc = QPdfDocument(self)
        if doc.load(path) != QPdfDocument.Error.None_:
            self.statusBar().showMessage(f"開けませんでした: {Path(path).name}", 5000)
            doc.deleteLater()
            return
        try:
            layout = pdfprefs.read_layout(path)
            direction_set = pdfprefs.direction_is_set(path)
        except Exception:
            layout, direction_set = Layout(), False
        saved = layout
        # 綴じ方が書かれていない本は、テキストレイヤーの文字の並びから縦書きかを推定する
        # （画像だけの本は推定しない。⌘D で綴じ方を保存すれば次からはそれを使う）
        guessed = None if direction_set else direction.detect(doc)
        if guessed == "rtl":
            layout = Layout(spread=layout.spread, cover_single=layout.cover_single, rtl=True)
        if self._book:
            self._file_watcher.removePath(self._book)
        self._book = path
        self._saved_layout = saved
        self._store.last_dir = str(Path(path).parent)
        self._apply_layout(layout)
        pos = self._store.position(path)
        old, self._doc = self._doc, doc
        self._thumbs.set_document(path, doc.pageCount())
        self._view.set_document(doc, pos[0] if pos else 0)
        old.deleteLater()
        self._file_watcher.addPath(path)
        self._loaded_sig = self._file_sig(path)
        self.setWindowTitle(f"{Path(path).stem}[*] — Book Viewer")
        self._update_save_state()
        if guessed == "rtl":
            self.statusBar().showMessage(
                "本文が縦書きなので右綴じで表示しています（⌘D の綴じ方で PDF に保存できます）", 8000)
        self._view.setFocus()

    def _close_book(self) -> None:
        if not self._book or not self._confirm_discard():
            return
        self._save_position()
        self._file_watcher.removePath(self._book)
        self._book = None
        self._thumbs.set_document(None, 0)
        self._view.set_document(None)
        self._doc.deleteLater()
        self._doc = QPdfDocument(self)
        self._discard_work()
        self.setWindowTitle("Book Viewer")
        self._update_save_state()

    # ---- ページ ----

    def _on_page_changed(self, _page: int) -> None:
        n = self._view.page_count()
        g = self._view.current_group()
        has = bool(n and g)
        for a in (self._act_first, self._act_prev, self._act_next, self._act_last,
                  self._act_goto, self._act_props, self._act_close, self._act_insert_blank):
            a.setEnabled(has)
        self._page_box.setEnabled(has)
        if not has:
            self._page_box.clear()
            self._page_total.setText("")
            return
        self._page_box.setValidator(QIntValidator(1, n, self._page_box))
        self._page_box.setText(str(g[0] + 1))
        self._page_total.setText(f"/ {n}")
        self._act_prev.setEnabled(g[0] > 0)
        self._act_first.setEnabled(g[0] > 0)
        self._act_next.setEnabled(g[-1] < n - 1)
        self._act_last.setEnabled(g[-1] < n - 1)
        if self._thumbs.isVisible():
            self._thumbs.set_current_pages(g)
        self._save_timer.start()

    def _on_page_box(self) -> None:
        text = self._page_box.text()
        if text.isdigit():
            self._view.go_to_page(int(text) - 1)
        self._on_page_changed(0)          # 範囲外などは現在のページ番号に戻す
        self._view.setFocus()

    def _go_to_page_dialog(self) -> None:
        n = self._view.page_count()
        if not n:
            return
        page, ok = QInputDialog.getInt(self, "ページへ移動", f"ページ番号（1〜{n}）:",
                                       self._view.current_page() + 1, 1, n)
        if ok:
            self._view.go_to_page(page - 1)

    def _save_position(self) -> None:
        if not self._book or not self._view.page_count():
            return
        self._store.set_position(self._book, self._view.current_page(), self._view.page_count())
        self._store.save()
        self._browser.refresh_progress(self._book)

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
        self._update_save_state()               # 外で書き換えられた開き方と、いまの表示を比べ直す
        page = self._view.current_page()
        old, self._doc = self._doc, doc
        self._thumbs.set_document(path, doc.pageCount())
        self._view.set_document(doc, page)      # その場の表示は保ったまま
        old.deleteLater()
        self._loaded_sig = sig
        self._browser.forget_thumbnail(path)
        if self._work:
            # 未保存の挿入は古い版に対するものなので捨てる
            self._discard_work()
            self.statusBar().showMessage(
                "元の PDF が更新されたので読み込み直しました（未保存のページの挿入・移動は取り消しました）", 8000)
        else:
            self.statusBar().showMessage(f"再読み込みしました（{doc.pageCount()} ページ）", 3000)

    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        self._save_position()
        self._store.save()                      # ズームの倍率など
        self._thumbs.shutdown()
        super().closeEvent(event)


def main() -> int:
    # 配布用バイナリの自己診断（CI 用。packaging/build.py と .github/workflows/build.yml を参照）
    selftest_result = os.environ.get("BOOK_VIEWER_SELFTEST")
    if selftest_result:
        from .selftest import run
        return run(selftest_result)
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
