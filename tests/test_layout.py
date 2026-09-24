from book_viewer.layout import Layout, groups, index_of_page, slots

SINGLE = Layout(spread=False)
COVER = Layout(spread=True, cover_single=True)
NOCOVER = Layout(spread=True, cover_single=False)
COVER_RTL = Layout(spread=True, cover_single=True, rtl=True)


def test_groups():
    assert groups(3, SINGLE) == [(0,), (1,), (2,)]
    assert groups(5, COVER) == [(0,), (1, 2), (3, 4)]
    assert groups(4, COVER) == [(0,), (1, 2), (3,)]
    assert groups(5, NOCOVER) == [(0, 1), (2, 3), (4,)]
    assert groups(0, COVER) == []


def test_slots_ltr():
    g = groups(4, COVER)
    assert slots(g[0], 0, COVER) == (None, 0)       # 表紙は右
    assert slots(g[1], 1, COVER) == (1, 2)
    assert slots(g[2], 2, COVER) == (3, None)       # 末尾の余りは左


def test_slots_rtl():
    g = groups(4, COVER_RTL)
    assert slots(g[0], 0, COVER_RTL) == (0, None)   # 右綴じの表紙は左
    assert slots(g[1], 1, COVER_RTL) == (2, 1)      # 右から読む
    assert slots(g[2], 2, COVER_RTL) == (None, 3)


def test_index_of_page():
    g = groups(5, COVER)
    assert index_of_page(2, g) == 1
    assert index_of_page(99, g) == 2
    assert index_of_page(-1, g) == 0
