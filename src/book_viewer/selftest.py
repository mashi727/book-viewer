"""配布用バイナリの自己診断（BOOK_VIEWER_SELFTEST=<結果ファイル> で起動）。

画面の無い CI（GitHub Actions の Windows / macOS）で、固めたバイナリが
  1. 起動してウィンドウを組み立てられるか（PySide6 / QtPdf が同梱されているか）
  2. PDF を開いて描けるか
  3. 開き方を PDF に書き込めるか（pikepdf が同梱されているか）
  4. 縦書き判定が動くか
を確かめる。結果は終了コード（0 = 成功）と、指定したファイルへの 1 行で返す
（--windowed のバイナリは標準出力を持たないので、ファイルに書く）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def run(result_path: str) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="book-viewer-selftest-"))
    os.environ["XDG_STATE_HOME"] = str(tmp / "state")       # 利用者の状態ファイルを汚さない
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    steps: list[str] = []
    try:
        import pikepdf
        from PySide6.QtWidgets import QApplication

        from . import direction, pdfprefs
        from .app import BookViewer
        from .layout import Layout

        app = QApplication.instance() or QApplication(sys.argv[:1])
        pdf_path = tmp / "sample.pdf"
        pdf = pikepdf.new()
        for _ in range(3):
            pdf.add_blank_page()
        pdf.save(pdf_path)
        steps.append("pdf created")

        win = BookViewer(str(tmp), str(pdf_path))
        win.show()
        end = time.time() + 1.0
        while time.time() < end:
            app.processEvents()
            time.sleep(0.01)
        assert win._view.page_count() == 3, f"page_count={win._view.page_count()}"
        win._view.viewport().grab()                          # 実際に描く
        steps.append("opened and rendered")

        direction.detect(win._doc)
        steps.append("direction detect")

        pdfprefs.write_layout(pdf_path, Layout(spread=True, cover_single=True, rtl=True))
        assert pdfprefs.read_layout(pdf_path).rtl
        steps.append("layout written")

        win.close()
        Path(result_path).write_text("OK " + " / ".join(steps) + "\n", encoding="utf-8")
        return 0
    except Exception:
        Path(result_path).write_text(
            "FAIL after: " + " / ".join(steps) + "\n" + traceback.format_exc(), encoding="utf-8")
        return 1
