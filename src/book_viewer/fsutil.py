"""ファイルシステムの小物（Qt 非依存）。"""
from __future__ import annotations

import os
import stat


def is_hidden(st: os.stat_result) -> bool:
    """Finder / エクスプローラーと同じく隠しファイルか（名前の . 始まりは呼び出し側で見る）。

    macOS は UF_HIDDEN フラグ（/bin・/usr・~/Library など）、Windows は隠し属性。
    どちらの属性も、無い OS の stat_result には存在しない。
    """
    if getattr(st, "st_flags", 0) & stat.UF_HIDDEN:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
