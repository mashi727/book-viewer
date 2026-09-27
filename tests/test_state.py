from book_viewer.state import Store


def test_position_roundtrip(tmp_path):
    s = Store(tmp_path / "state.json")
    assert s.position(str(tmp_path / "b.pdf")) is None
    s.set_position(str(tmp_path / "b.pdf"), 41, 300)
    s.last_dir = str(tmp_path)
    s.save()
    s2 = Store(tmp_path / "state.json")
    assert s2.position(str(tmp_path / "b.pdf")) == (41, 300)
    assert s2.last_dir == str(tmp_path)


def test_broken_file_is_ignored(tmp_path):
    (tmp_path / "state.json").write_text("{broken")
    assert Store(tmp_path / "state.json").position("x.pdf") is None


def test_ui_prefs(tmp_path):
    s = Store(tmp_path / "state.json")
    assert s.ui("show_thumbnails", True) is True
    s.set_ui("show_thumbnails", False)
    s.save()
    assert Store(tmp_path / "state.json").ui("show_thumbnails", True) is False


def test_is_hidden_by_platform_attributes():
    import stat
    from types import SimpleNamespace
    from book_viewer.file_browser import is_hidden
    assert is_hidden(SimpleNamespace(st_flags=stat.UF_HIDDEN)) is True                        # macOS
    assert is_hidden(SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_HIDDEN)) is True  # Windows
    assert is_hidden(SimpleNamespace(st_flags=0)) is False
    assert is_hidden(SimpleNamespace()) is False                                              # どちらも無い OS
