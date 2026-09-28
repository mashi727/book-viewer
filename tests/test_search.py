import os
import unicodedata

from book_viewer.search import normalize, scan_pdfs, search


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").close()


def test_scan_pdfs_recurses_and_skips_hidden(tmp_path):
    _touch(tmp_path / "a.pdf")
    _touch(tmp_path / "sub" / "deep" / "b.PDF")
    _touch(tmp_path / "sub" / "notes.txt")
    _touch(tmp_path / ".hidden" / "c.pdf")          # 隠しフォルダの中は探さない
    _touch(tmp_path / "sub" / ".d.pdf")              # 隠しファイルも出さない
    found = sorted(os.path.relpath(p, tmp_path) for p in scan_pdfs(tmp_path))
    assert found == ["a.pdf", os.path.join("sub", "deep", "b.PDF")]


def test_search_and_terms_width_case_and_nfd():
    nfd = unicodedata.normalize("NFD", "ガボール・アイトレーニング.pdf")   # macOS で見かける分解形
    index = [f"/x/{nfd}", "/x/Python実践 機械学習.pdf", "/y/ＰＭＰ完全攻略テキスト.pdf"]
    assert search(index, "ガボール")[1] == 1                          # 入力（NFC）と NFD の名前が一致
    assert search(index, "python 機械")[0] == ["/x/Python実践 機械学習.pdf"]   # AND・大文字小文字
    assert search(index, "pmp")[1] == 1                                # 全角英字
    assert search(index, "python 存在しない")[1] == 0
    assert search(index, "   ") == ([], 0)


def test_search_limit():
    index = [f"/b/book{i}.pdf" for i in range(10)]
    hits, total = search(index, "book", limit=3)
    assert len(hits) == 3 and total == 10


def test_normalize():
    assert normalize("ＡＢＣ ｶﾞ") == normalize("abc ガ")
