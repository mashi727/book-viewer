import json

import pikepdf

from book_viewer.layout import Layout
from book_viewer.pdfprefs import read_layout, write_layout


def _make_pdf(path, pages=5):
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page()
    pdf.save(path)


def test_default_is_single_ltr(tmp_path):
    p = tmp_path / "a.pdf"
    _make_pdf(p)
    assert read_layout(p) == Layout(spread=False, cover_single=True, rtl=False)


def test_roundtrip_and_log(tmp_path):
    p = tmp_path / "a.pdf"
    log = tmp_path / "log.jsonl"
    _make_pdf(p)
    for layout in (Layout(True, True, True), Layout(True, False, False), Layout(False, True, True)):
        write_layout(p, layout, log_path=log)
        got = read_layout(p)
        assert (got.spread, got.rtl) == (layout.spread, layout.rtl)
        if layout.spread:
            assert got.cover_single == layout.cover_single
    with pikepdf.open(p) as pdf:
        assert len(pdf.pages) == 5
        assert pdf.Root.PageLayout == "/SinglePage"
        assert pdf.Root.ViewerPreferences.Direction == "/R2L"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert entries[0]["old"] == {"PageLayout": None, "Direction": None}
    assert entries[1]["old"] == {"PageLayout": "/TwoPageRight", "Direction": "/R2L"}
    assert not list(tmp_path.glob(".*tmp"))           # 一時ファイルが残らない


def test_existing_viewer_preferences_are_kept(tmp_path):
    p = tmp_path / "a.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.Root.ViewerPreferences = pikepdf.Dictionary(HideToolbar=True)
    pdf.save(p)
    write_layout(p, Layout(True, True, True))
    with pikepdf.open(p) as pdf:
        assert pdf.Root.ViewerPreferences.HideToolbar is True
        assert pdf.Root.ViewerPreferences.Direction == "/R2L"


def test_direction_is_set(tmp_path):
    from book_viewer.pdfprefs import direction_is_set
    p = tmp_path / "a.pdf"
    _make_pdf(p)
    assert direction_is_set(p) is False
    write_layout(p, Layout(False, True, False))
    assert direction_is_set(p) is True


def test_temp_file_removed_on_failure(tmp_path):
    import pytest
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not a pdf")
    with pytest.raises(Exception):
        write_layout(p, Layout(True, True, True))
    assert not list(tmp_path.glob(".*tmp"))


def _pdf_with_sizes(path, sizes, rotate_parent=None):
    pdf = pikepdf.new()
    for w, h in sizes:
        pdf.add_blank_page(page_size=(w, h))
    for page in pdf.pages:                           # 中身のあるページにする
        page.obj.Contents = pdf.make_stream(b"0 0 m 10 10 l S")
    if rotate_parent is not None:
        pdf.Root.Pages.Rotate = rotate_parent        # 親の /Pages に書かれた回転（継承される）
    pdf.save(path)


def test_insert_blank_after_page_matches_size(tmp_path):
    from book_viewer.pdfprefs import insert_blank_pages
    p = tmp_path / "a.pdf"
    log = tmp_path / "log.jsonl"
    _pdf_with_sizes(p, [(420, 595), (500, 700), (420, 595)])
    insert_blank_pages(p, index=2, count=2, log_path=log)      # 2 ページ目の後に 2 枚
    with pikepdf.open(p) as pdf:
        assert len(pdf.pages) == 5
        for i in (2, 3):
            assert [float(v) for v in pdf.pages[i].mediabox] == [0, 0, 500, 700]   # 直前のページに揃う
            assert pdf.pages[i].obj.Contents.read_bytes() == b""
        assert pdf.pages[1].obj.Contents.read_bytes() != b""  # 元のページは無傷
        assert pdf.pages[4].obj.Contents.read_bytes() != b""
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["action"] == "insert_blank" and entry["index"] == 2 and entry["count"] == 2
    assert not list(tmp_path.glob(".*tmp"))


def test_insert_blank_at_front_and_inherited_rotation(tmp_path):
    from book_viewer.pdfprefs import insert_blank_pages, page_size_mm
    p = tmp_path / "a.pdf"
    _pdf_with_sizes(p, [(420, 595), (420, 595)], rotate_parent=90)
    insert_blank_pages(p, index=0)                             # 先頭（1 ページ目に揃う）
    with pikepdf.open(p) as pdf:
        assert len(pdf.pages) == 3
        assert int(pdf.pages[0].obj.Rotate) == 90             # 親から継承した回転を写す
    w, h = page_size_mm(p, 0)
    assert round(w) == 210 and round(h) == 148                 # 90° 回転なので横長


def test_insert_blank_rejects_out_of_range(tmp_path):
    import pytest
    from book_viewer.pdfprefs import insert_blank_pages
    p = tmp_path / "a.pdf"
    _pdf_with_sizes(p, [(420, 595)])
    with pytest.raises(ValueError):
        insert_blank_pages(p, index=5)
    with pikepdf.open(p) as pdf:
        assert len(pdf.pages) == 1
    assert not list(tmp_path.glob(".*tmp"))


def test_apply_edits_multiple_inserts_and_layout_in_one_write(tmp_path):
    from book_viewer.pdfprefs import apply_edits
    p = tmp_path / "a.pdf"
    log = tmp_path / "log.jsonl"
    _pdf_with_sizes(p, [(420, 595)] * 4)
    # 2 ページ目の後に 1 枚 → その結果の先頭に 2 枚（あとの挿入は前の挿入後のページ番号で数える）
    apply_edits(p, inserts=[(2, 1, 1), (0, 2, 0)], layout=Layout(True, True, True), log_path=log)
    with pikepdf.open(p) as pdf:
        assert len(pdf.pages) == 7
        blank = [i for i, pg in enumerate(pdf.pages) if pg.obj.Contents.read_bytes() == b""]
        assert blank == [0, 1, 4]
    assert read_layout(p) == Layout(True, True, True)
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["action"] == "edits"   # 1 回の書き込みで 1 行
