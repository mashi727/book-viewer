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
