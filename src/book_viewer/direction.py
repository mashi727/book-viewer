"""縦書き / 横書きの推定（PDF に綴じ方が書かれていない本のため）。

OCR などのテキストレイヤーがあれば、続く文字どうしの位置の動きで判定する。
縦書きなら次の文字は下へ（y が進み x はほぼ一定）、横書きなら右へ進む。

  主任設計者が明かす F-2戦闘機開発（縦書き）: 平均 |dx| 0.3pt, |dy| 9.9pt
  日本人の起源（横書き）:                     平均 |dx| 12.9pt, |dy| 0.6pt

テキストレイヤーの無い画像だけの自炊本は判定しない（None）。ページ画像の
行間の帯から推定する方法も試したが、zz_Books 950 冊で 1 冊の中でもページごとに
判定が割れ（DARPA秘史は 7 ページ中 2 ページしか縦書きと出ない）、誤判定すると
矢印キーの向きが逆になって「推定しない」より悪いので採らない。
画像だけの本は ⌘D の綴じ方を一度設定すれば PDF に残る。
"""
from __future__ import annotations

from PySide6.QtPdf import QPdfDocument

_MAX_STEP = 40.0          # これより大きい移動は改行・段の切り替えとみなして数えない (pt)
_MIN_STEPS = 20           # 1 ページで判定に使う最小の文字間の数
_DOMINANCE = 3.0          # 片方の平均移動量がもう片方のこれ倍以上なら、そのページを判定する


def sample_pages(count: int, n: int = 5) -> list[int]:
    """本の中ほど（20%〜80%）から n ページ。表紙・目次・索引を避ける。"""
    if count <= 0:
        return []
    lo, hi = int(count * 0.2), max(int(count * 0.8), int(count * 0.2) + 1)
    step = max(1, (hi - lo) // n)
    return list(range(lo, hi, step))[:n]


def classify_steps(dx: float, dy: float, steps: int) -> str | None:
    """文字間の平均移動量からページを判定する。'rtl'（縦書き）/ 'ltr'（横書き）/ None。"""
    if steps < _MIN_STEPS:
        return None
    if dy >= dx * _DOMINANCE:
        return "rtl"
    if dx >= dy * _DOMINANCE:
        return "ltr"
    return None


def page_steps(doc: QPdfDocument, page: int, limit: int = 60) -> tuple[float, float, int]:
    """(平均 |dx|, 平均 |dy|, 数えた文字間の数)。テキストが無ければ (0, 0, 0)。"""
    text = doc.getAllText(page).text()
    if len(text) < _MIN_STEPS:
        return 0.0, 0.0, 0
    dx = dy = 0.0
    n = 0
    prev = None
    for i in range(min(len(text), limit)):
        if text[i].isspace():
            prev = None
            continue
        r = doc.getSelectionAtIndex(page, i, 1).boundingRectangle()
        if r.isEmpty():
            prev = None
            continue
        c = r.center()
        if prev is not None:
            ddx, ddy = abs(c.x() - prev.x()), abs(c.y() - prev.y())
            if ddx < _MAX_STEP and ddy < _MAX_STEP:
                dx += ddx
                dy += ddy
                n += 1
        prev = c
    return (dx / n, dy / n, n) if n else (0.0, 0.0, 0)


def detect(doc: QPdfDocument) -> str | None:
    """本全体の判定（多数決）。'rtl' / 'ltr' / None（テキストが無い・割れた）。"""
    votes = {"ltr": 0, "rtl": 0}
    for page in sample_pages(doc.pageCount()):
        verdict = classify_steps(*page_steps(doc, page))
        if verdict:
            votes[verdict] += 1
    if votes["rtl"] > votes["ltr"]:
        return "rtl"
    if votes["ltr"] > votes["rtl"]:
        return "ltr"
    return None
