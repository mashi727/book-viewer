"""PDF 本体への書き込み（pikepdf）: 「開き方」の設定と、空白ページの挿入。

PDF 標準（ISO 32000）のカタログ項目を使うので、プレビュー・Acrobat 等の
他のリーダーにも同じ設定が伝わる（どこまで尊重するかはリーダー次第）。

  /PageLayout
      /SinglePage /OneColumn        … 単ページ
      /TwoPageLeft /TwoColumnLeft   … 見開き・奇数ページが左（表紙なし）
      /TwoPageRight /TwoColumnRight … 見開き・奇数ページが右（表紙単独）
  /ViewerPreferences /Direction /L2R | /R2L   … 綴じ方向

空白ページは基準ページと同じ MediaBox / CropBox / Rotate / UserUnit で作る
（Acrobat の「ページを挿入 › 空白ページ」と同じく、見た目の大きさと向きが揃う）。

書き込みは「同じフォルダの一時ファイルへ保存 → 読み戻して検証 → 差し替え」（_rewrite）。
途中で失敗しても元ファイルは無傷。何をしたか（変更前の値・挿入位置）は履歴 (JSONL) に残す。
"""
from __future__ import annotations

import hashlib
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


def _rewrite(path: str | os.PathLike, mutate, verify, log_path: Path | None) -> dict:
    """mutate(pdf) で書き換えて一時ファイルに保存し、verify(chk) で読み戻して確かめてから差し替える。

    mutate は履歴に残す内容（dict）を返す。
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.book-viewer-tmp")
    keep_tmp = False
    try:
        with pikepdf.open(path) as pdf:
            info = mutate(pdf)
            pdf.save(tmp)
        with pikepdf.open(tmp) as chk:
            verify(chk)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "path": str(path.resolve()),
                    **info,
                }, ensure_ascii=False) + "\n")
        _replace(tmp, path)
        return info
    except TmpKeptError:
        keep_tmp = True                 # 中身は正しいので残す（エラーに場所を書いてある）
        raise
    finally:
        if not keep_tmp:
            tmp.unlink(missing_ok=True)


def _inherited(page: pikepdf.Dictionary, key: str):
    """ページ属性を親の /Pages から継承して引く（/Rotate などはページ木の上に書かれることがある）。"""
    node = page
    for _ in range(64):                 # 壊れた循環参照への保険
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def page_size_mm(path: str | os.PathLike, index: int) -> tuple[float, float]:
    """ページの見た目の大きさ (幅, 高さ) mm。CropBox・回転・UserUnit を反映する。"""
    with pikepdf.open(Path(path)) as pdf:
        page = pdf.pages[index]
        box = [float(v) for v in page.cropbox]
        unit = float(_inherited(page.obj, "/UserUnit") or 1)
        w, h = abs(box[2] - box[0]) * unit, abs(box[3] - box[1]) * unit
        if int(_inherited(page.obj, "/Rotate") or 0) % 180:
            w, h = h, w
        return w * 25.4 / 72, h * 25.4 / 72


def _layout_values(layout: Layout) -> dict[str, str]:
    page_layout = (
        ("/TwoPageRight" if layout.cover_single else "/TwoPageLeft")
        if layout.spread
        else "/SinglePage"
    )
    return {"PageLayout": page_layout, "Direction": "/R2L" if layout.rtl else "/L2R"}


def _set_layout(pdf: pikepdf.Pdf, layout: Layout) -> dict:
    old = _raw(pdf)
    new = _layout_values(layout)
    pdf.Root.PageLayout = pikepdf.Name(new["PageLayout"])
    vp = pdf.Root.get("/ViewerPreferences")
    if not isinstance(vp, pikepdf.Dictionary):
        pdf.Root.ViewerPreferences = pikepdf.Dictionary()
    pdf.Root.ViewerPreferences.Direction = pikepdf.Name(new["Direction"])
    return {"action": "layout", "old": old, "new": new}


def _insert_blank(pdf: pikepdf.Pdf, index: int, count: int, ref: int | None) -> dict:
    """index の位置に空白ページを count 枚。大きさ・向きは ref ページ（省略時は直前、先頭なら 1 ページ目）に揃える。"""
    if count < 1:
        raise ValueError("count must be >= 1")
    n_pages = len(pdf.pages)
    if not 0 <= index <= n_pages:
        raise ValueError(f"挿入位置が範囲外です: {index}（0〜{n_pages}）")
    r = ref if ref is not None else max(0, index - 1)
    src = pdf.pages[r]
    media = pikepdf.Array([float(v) for v in src.mediabox])
    crop = pikepdf.Array([float(v) for v in src.cropbox])
    rotate = _inherited(src.obj, "/Rotate")
    unit = _inherited(src.obj, "/UserUnit")
    for _ in range(count):
        d = pikepdf.Dictionary(
            Type=pikepdf.Name.Page,
            MediaBox=media,
            Resources=pikepdf.Dictionary(),
            Contents=pdf.make_stream(b""),
        )
        if list(crop) != list(media):
            d.CropBox = crop
        if rotate is not None:
            d.Rotate = rotate
        if unit is not None:
            d.UserUnit = unit
        pdf.pages.insert(index, pikepdf.Page(pdf.make_indirect(d)))
    return {"action": "insert_blank", "index": index, "count": count, "ref": r, "pages_before": n_pages}


def move_order(n: int, rows: list[int], dest: int) -> list[int]:
    """rows（元の位置、順不同）を dest（元の並びで数えた挿入位置 0〜n）へ移した後の並び（元の位置の列）。

    離れたページをまとめて動かしても、選んだページどうしの順序は元のまま保つ。
    """
    moving = sorted(set(rows))
    rest = [i for i in range(n) if i not in set(moving)]
    k = dest - sum(1 for r in moving if r < dest)
    return rest[:k] + moving + rest[k:]


def _page_key(page: pikepdf.Page) -> str:
    """ページの同一性の目印（内容ストリームと MediaBox のハッシュ）。並べ替えの検証に使う。"""
    h = hashlib.sha1()
    contents = page.obj.get("/Contents")
    streams = contents if isinstance(contents, pikepdf.Array) else [contents] if contents is not None else []
    for st in streams:
        h.update(st.read_raw_bytes())
    h.update(repr([float(v) for v in page.mediabox]).encode())
    return h.hexdigest()


def apply_edits(path: str | os.PathLike, ops: list[tuple] = (),
                layout: Layout | None = None, log_path: Path | None = None) -> None:
    """ページの編集を順に適用し、開き方も含めて 1 回の書き込みで行う（［保存］でたまった変更を書く）。

      ("insert", index, count, ref) … index の位置に空白ページを count 枚（大きさは ref ページ）
      ("move", rows, dest)          … rows のページを dest（その時点の並びで数えた挿入位置）へ

    検証: 各ページの中身（_page_key）が、意図した順序どおりに並んでいること。
    """
    ops = [tuple(op) for op in ops]
    expected: list[str] = []

    def mutate(pdf: pikepdf.Pdf) -> dict:
        keys = [_page_key(pg) for pg in pdf.pages]
        steps = []
        for op in ops:
            if op[0] == "insert":
                _kind, index, count, ref = op
                steps.append(_insert_blank(pdf, index, count, ref))
                keys[index:index] = [_page_key(pdf.pages[i]) for i in range(index, index + count)]
            elif op[0] == "move":
                _kind, rows, dest = op
                n = len(pdf.pages)
                if not rows or any(not 0 <= r < n for r in rows) or not 0 <= dest <= n:
                    raise ValueError(f"移動の指定が範囲外です: {rows} → {dest}（0〜{n}）")
                order = move_order(n, list(rows), dest)
                pdf.pages[:] = [pdf.pages[i] for i in order]
                keys = [keys[i] for i in order]
                steps.append({"action": "move", "rows": sorted(set(rows)), "dest": dest, "pages": n})
            else:
                raise ValueError(f"unknown op: {op[0]}")
        if layout is not None:
            steps.append(_set_layout(pdf, layout))
        expected[:] = keys
        return steps[0] if len(steps) == 1 else {"action": "edits", "steps": steps}

    def verify(chk: pikepdf.Pdf) -> None:
        got = [_page_key(pg) for pg in chk.pages]
        if got != expected:
            bad = next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), min(len(got), len(expected)))
            raise RuntimeError(f"書き込み検証に失敗: {bad + 1} ページ目が意図した内容ではありません"
                               f"（ページ数 {len(got)} / 期待 {len(expected)}）")
        if layout is not None and _raw(chk) != _layout_values(layout):
            raise RuntimeError(f"書き込み検証に失敗: {_raw(chk)}")

    _rewrite(path, mutate, verify, log_path)


def write_layout(path: str | os.PathLike, layout: Layout, log_path: Path | None = None) -> None:
    """開き方を PDF に書き込む。"""
    apply_edits(path, layout=layout, log_path=log_path)


def insert_blank_pages(path: str | os.PathLike, index: int, count: int = 1,
                       ref: int | None = None, log_path: Path | None = None) -> None:
    """index（0 始まり。0 なら先頭、ページ数なら末尾）の位置に空白ページを count 枚挿入する。"""
    apply_edits(path, [("insert", index, count, ref)], log_path=log_path)
