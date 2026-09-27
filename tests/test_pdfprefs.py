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
