"""自炊本PDFリーダー。

  book-viewer [フォルダ | PDF]

左は本棚（Windows エクスプローラー風のフォルダツリー。起動フォルダ・ホーム・この Mac の各ドライブ）。
PDF をクリックで右に表示し、前回読んでいた位置から再開する。読書列は進捗、
PDF にマウスを乗せると表紙のサムネイルが出る。

  開き方（PDF 本体に書き込める。⌘S）:
    B  … 右綴じ / 左綴じ      D … 見開き / 単ページ      C … 表紙を単独にする
  読書:
    → ← Space ホイール クリック … ページ送り（詳細は spread_view.py）
    F / Esc … 全画面 / 解除      ⌘O … フォルダをツリーで開く
    フォルダをクリック … 開閉      ⌘↑ … 親フォルダを選択して閉じる
    .. … 起動フォルダを 1 つ上へ付け替える（起動フォルダを選択中の ⌘↑ も同じ）
  自動再読込:
    表示中の PDF が書き換えられたら（TeX の再コンパイル等）、書き込みが落ち着くのを
    待って読み込み直す。ページ位置と、ビューア上の開き方は保つ。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QToolBar,
    QWidget,
)

from . import pdfprefs
from .file_browser import FileBrowserPanel
from .layout import Layout
from .spread_view import SpreadView
from .state import Store, state_dir

_RELOAD_SETTLE_MS = 400      # 書き込みが止んだと見なすまでの待ち
_RELOAD_MAX_TRIES = 50       # 400ms × 50 ≒ 20 秒待って読めなければ諦める


class BookViewer(QMainWindow):
    def __init__(self, directory: str, open_pdf: str | None = None):
        super().__init__()
        self.setWindowTitle("Book Viewer")
        self.resize(1400, 900)
        self._store = Store()
        self._root = str(Path(directory).resolve())
        self._book: str | None = None
        self._saved_layout = Layout()       # PDF に書かれている開き方

        # 左: 本棚（フォルダツリー）
        self._browser = FileBrowserPanel(Path(self._root), self._store.position)
        self._browser.pdf_clicked.connect(lambda p: self._open_book(str(p)))
        self._browser.start_dir_changed.connect(lambda p: setattr(self, "_root", str(p)))
        self._browser.reveal(self._root)

        # 右: 見開きビュー
        self._doc = QPdfDocument(self)
        self._view = SpreadView()
        self._view.pageChanged.connect(self._on_page_changed)

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._browser)
        self._splitter.addWidget(self._view)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([420, 980])
        self.setCentralWidget(self._splitter)
        self.setStatusBar(QStatusBar())

        self._build_toolbar()

        QShortcut(QKeySequence(QKeySequence.StandardKey.Open), self).activated.connect(self._open_folder)
        QShortcut(QKeySequence("Ctrl+Up"), self).activated.connect(self._go_up)
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

        if open_pdf:
            self._open_book(open_pdf)
            self._browser.reveal(open_pdf)

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
        self._browser.setVisible(not on)
        self.statusBar().setVisible(not on)
        self._toolbar.setVisible(not on)
        if on:
            self.showFullScreen()
        else:
            self.showNormal()
        self._view.setFocus()

    # ---- 本棚 ----

    def _go_up(self) -> None:
        self._browser.select_parent()

    def _open_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "フォルダを開く", self._root)
        if chosen:
            self._root = chosen
            self._browser.reveal(chosen)

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
        self._store.last_dir = str(Path(path).parent)
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
        page = self._view.current_page()
        old, self._doc = self._doc, doc
        self._view.set_document(doc, page)      # ビューア上の開き方は保ったまま
        old.deleteLater()
        self._loaded_sig = sig
        self._update_action_state()
        self._browser.forget_thumbnail(path)
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
