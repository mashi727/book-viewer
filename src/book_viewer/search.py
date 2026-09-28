"""ファイル名の検索（起動フォルダ以下、サブフォルダも含む）。Qt 非依存。

  scan_pdfs(root)      … root 以下の PDF を集める（隠しフォルダ・隠しファイルは飛ばす）
  normalize(text)      … 比較用に揃える（NFKC + casefold）
  search(index, query) … 空白区切りの語をすべて含むファイル名を返す（AND）

normalize で揃えるもの:
  - macOS のファイル名は「ガ」が「カ」+ 濁点の 2 文字（NFD）で記録されていることがあり、
    入力した「ガ」（NFC）と一致しない → NFKC で合成済みの形に揃える
  - 全角 / 半角（ＡＢＣ / ABC、ｶﾞ / ガ）→ NFKC
  - 大文字 / 小文字 → casefold
"""
from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from .fsutil import is_hidden

MAX_RESULTS = 500


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def scan_pdfs(root: str | os.PathLike) -> list[str]:
    """root 以下の PDF のパス（絶対パス）。シンボリックリンクのフォルダは辿らない（循環の防止）。"""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in dirnames:
            if d.startswith("."):
                continue
            try:
                if is_hidden(os.stat(os.path.join(dirpath, d), follow_symlinks=False)):
                    continue
            except OSError:
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f.lower().endswith(".pdf") and not f.startswith("."):
                out.append(os.path.join(dirpath, f))
    return out


def search(index: list[str], query: str, limit: int = MAX_RESULTS) -> tuple[list[str], int]:
    """(一致したパス（最大 limit 件）, 一致した総数)。語が無ければ ([], 0)。"""
    terms = normalize(query).split()
    if not terms:
        return [], 0
    hits = [p for p in index if all(t in normalize(Path(p).name) for t in terms)]
    return hits[:limit], len(hits)
