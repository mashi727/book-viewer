"""PDF 本体の「開き方」設定の読み書き（pikepdf）。

PDF 標準（ISO 32000）のカタログ項目を使うので、プレビュー・Acrobat 等の
他のリーダーにも同じ設定が伝わる（どこまで尊重するかはリーダー次第）。

  /PageLayout
      /SinglePage /OneColumn        … 単ページ
      /TwoPageLeft /TwoColumnLeft   … 見開き・奇数ページが左（表紙なし）
      /TwoPageRight /TwoColumnRight … 見開き・奇数ページが右（表紙単独）
  /ViewerPreferences /Direction /L2R | /R2L   … 綴じ方向

書き込みは「同じフォルダの一時ファイルへ保存 → 読み戻して検証 → os.replace」。
途中で失敗しても元ファイルは無傷。変更前の値は undo ログ (JSONL) に残す。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pikepdf

from .layout import Layout

_SPREAD_COVER = {"/TwoPageRight", "/TwoColumnRight"}
_SPREAD_NO_COVER = {"/TwoPageLeft", "/TwoColumnLeft"}


def _raw(pdf: pikepdf.Pdf) -> dict[str, str | None]:
    root = pdf.Root
    layout = root.get("/PageLayout")
    vp = root.get("/ViewerPreferences")
    direction = vp.get("/Direction") if isinstance(vp, pikepdf.Dictionary) else None
    return {
        "PageLayout": str(layout) if layout is not None else None,
        "Direction": str(direction) if direction is not None else None,
    }


def _to_layout(raw: dict[str, str | None]) -> Layout:
    pl = raw.get("PageLayout") or ""
    spread = pl in _SPREAD_COVER or pl in _SPREAD_NO_COVER
    return Layout(
        spread=spread,
        cover_single=pl not in _SPREAD_NO_COVER,
        rtl=raw.get("Direction") == "/R2L",
    )


def read_layout(path: str | os.PathLike) -> Layout:
    """PDF に書かれた開き方。項目が無ければ既定値（単ページ・左綴じ）。"""
    with pikepdf.open(Path(path)) as pdf:
        return _to_layout(_raw(pdf))


def write_layout(path: str | os.PathLike, layout: Layout, log_path: Path | None = None) -> None:
    """開き方を PDF に書き込む（アトミック置換）。"""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.book-viewer-tmp")
    page_layout = (
        ("/TwoPageRight" if layout.cover_single else "/TwoPageLeft")
        if layout.spread
        else "/SinglePage"
    )
    direction = "/R2L" if layout.rtl else "/L2R"
    try:
        with pikepdf.open(path) as pdf:
            old = _raw(pdf)
            n_pages = len(pdf.pages)
            pdf.Root.PageLayout = pikepdf.Name(page_layout)
            vp = pdf.Root.get("/ViewerPreferences")
            if not isinstance(vp, pikepdf.Dictionary):
                pdf.Root.ViewerPreferences = pikepdf.Dictionary()
            pdf.Root.ViewerPreferences.Direction = pikepdf.Name(direction)
            pdf.save(tmp)
        # 読み戻して検証してから差し替える
        with pikepdf.open(tmp) as chk:
            got = _raw(chk)
            if len(chk.pages) != n_pages or got != {"PageLayout": page_layout, "Direction": direction}:
                raise RuntimeError(f"書き込み検証に失敗: pages={len(chk.pages)}/{n_pages} {got}")
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "path": str(path.resolve()),
                    "old": old,
                    "new": {"PageLayout": page_layout, "Direction": direction},
                }, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
