"""読書位置などビューア側の状態（JSON）。

PDF 本体は読むたびに書き換えたくないので、読書位置はこちらに持つ。
キーは PDF の絶対パス（ファイル名を変えると位置は引き継がれない）。

  既定の置き場: $XDG_STATE_HOME/book-viewer/  （未設定なら ~/.local/state/book-viewer/）
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "book-viewer"


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "state.json"
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
        self._data.setdefault("books", {})

    def position(self, pdf: str) -> tuple[int, int] | None:
        """(ページ 0 始まり, 総ページ数)。記録が無ければ None。"""
        b = self._data["books"].get(str(Path(pdf).resolve()))
        return (b["page"], b["pages"]) if b else None

    def set_position(self, pdf: str, page: int, pages: int) -> None:
        self._data["books"][str(Path(pdf).resolve())] = {"page": page, "pages": pages}

    @property
    def last_dir(self) -> str | None:
        return self._data.get("last_dir")

    @last_dir.setter
    def last_dir(self, value: str) -> None:
        self._data["last_dir"] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
