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
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pikepdf

from .layout import Layout

# 直近の書き込みで元ファイルをどう差し替えたか（"replace" / "replace-retry" / "copy"）。自己診断で記録する
last_replace_method = ""


class TmpKeptError(PermissionError):
    """差し替えに失敗し、正しい内容の一時ファイルを残した。"""


def _replace(tmp: Path, path: Path) -> None:
    """検証済みの一時ファイルで元ファイルを差し替える。

    通常は os.replace（アトミック）。Windows では開いているファイルを置き換えられず
    PermissionError（WinError 5）になる。ウイルス対策ソフトの走査などの一時的なものは
    待てば通るので少し再試行し、それでも駄目なら中身を上書きコピーする（書き込み共有を
    許して開いている相手がいても通る）。コピーの途中で失敗したら一時ファイルを残して知らせる。
    """
    global last_replace_method
    tries = 15 if sys.platform == "win32" else 1
    for i in range(tries):
        try:
            os.replace(tmp, path)
            last_replace_method = "replace" if i == 0 else f"replace-retry({i})"
            return
        except PermissionError:
            if i == tries - 1:
                break
            time.sleep(0.2)
    if sys.platform != "win32":
        raise PermissionError(f"置き換えられませんでした: {path}")
    try:
        shutil.copyfile(tmp, path)
    except OSError as e:
        raise TmpKeptError(
            f"PDF を書き換えられませんでした（他のアプリが開いている可能性があります）。"
            f"書き込むはずだった内容は {tmp} に残しています: {e}") from e
    last_replace_method = "copy"
    tmp.unlink(missing_ok=True)

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


def direction_is_set(path: str | os.PathLike) -> bool:
    """綴じ方（/ViewerPreferences /Direction）が PDF に書かれているか。"""
    with pikepdf.open(Path(path)) as pdf:
        return _raw(pdf)["Direction"] is not None


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
    keep_tmp = False
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
        _replace(tmp, path)
    except TmpKeptError:
        keep_tmp = True                 # 中身は正しいので残す（エラーに場所を書いてある）
        raise
    finally:
        if not keep_tmp:
            tmp.unlink(missing_ok=True)
