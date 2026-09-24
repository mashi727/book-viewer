"""見開きの組み方（Qt 非依存の純粋ロジック）。

ページ番号は 0 始まり。「組」は読む順に並んだ 1〜2 ページ、「スロット」は
画面上の左→右の並び（空き側は None）。

  見開き・表紙単独:  (0,) (1,2) (3,4) ...   表紙は「読む順で後ろ側」の半分に置く
  見開き・表紙なし:  (0,1) (2,3) ...
  右綴じ:            スロットを左右反転する（右ページから読む）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    spread: bool = False        # 見開き
    cover_single: bool = True   # 1 ページ目（表紙）を単独にする（見開き時のみ意味を持つ）
    rtl: bool = False           # 右綴じ（右→左に読む）


def groups(page_count: int, layout: Layout) -> list[tuple[int, ...]]:
    """読む順のページの組の一覧。"""
    if page_count <= 0:
        return []
    if not layout.spread:
        return [(i,) for i in range(page_count)]
    out: list[tuple[int, ...]] = []
    i = 0
    if layout.cover_single:
        out.append((0,))
        i = 1
    while i < page_count:
        out.append(tuple(range(i, min(i + 2, page_count))))
        i += 2
    return out


def slots(group: tuple[int, ...], index: int, layout: Layout) -> tuple[int | None, ...]:
    """組を画面上の左→右の並びにする。見開きで 1 枚だけの組は片側を空ける。

    表紙（先頭の単独組）は読む順で後ろ側、末尾の余り 1 枚は前側に置く。
    こうすると単独ページでも本の中での左右位置が変わらない。
    """
    if not layout.spread:
        return group
    if len(group) == 2:
        first, second = group
    elif index == 0 and layout.cover_single:
        first, second = None, group[0]
    else:
        first, second = group[0], None
    return (second, first) if layout.rtl else (first, second)


def index_of_page(page: int, grouped: list[tuple[int, ...]]) -> int:
    """page を含む組の番号。範囲外は端に丸める。"""
    if not grouped:
        return 0
    for k, g in enumerate(grouped):
        if page in g:
            return k
    return 0 if page < 0 else len(grouped) - 1
