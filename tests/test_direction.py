from book_viewer.direction import classify_steps, sample_pages


def test_classify_steps():
    assert classify_steps(0.3, 9.9, 157) == "rtl"      # 主任設計者が明かす F-2戦闘機開発
    assert classify_steps(12.9, 0.6, 161) == "ltr"     # 日本人の起源
    assert classify_steps(5.0, 4.0, 100) is None       # 割れている
    assert classify_steps(0.1, 9.0, 5) is None         # 文字が少なすぎる


def test_sample_pages_middle():
    pages = sample_pages(100)
    assert pages and min(pages) >= 20 and max(pages) < 80
    assert sample_pages(0) == []
