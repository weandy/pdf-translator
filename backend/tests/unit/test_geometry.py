from pdftr.domain.geometry import Rect, from_normalized


def test_rect_basic():
    r = Rect.of([10, 20, 110, 70])
    assert (r.width, r.height, r.area) == (100, 50, 5000)
    assert r.center == (60, 45)
    assert r.contains_point(10, 20)
    assert not r.contains_point(9, 20)
    assert r.contains_point(9, 20, tol=1.5)


def test_rect_intersection_and_shrink():
    a, b = Rect.of([0, 0, 10, 10]), Rect.of([5, 5, 20, 20])
    assert a.intersection(b) == Rect.of([5, 5, 10, 10])
    assert a.intersection(Rect.of([10, 10, 20, 20])) is None
    assert a.shrink(0.5) == Rect.of([0.5, 0.5, 9.5, 9.5])


def test_from_normalized_matches_page_size():
    # MinerU content_list 的 0–1000 坐标换算到 595×842 pt 的页面
    r = from_normalized([0, 0, 1000, 500], 595.0, 842.0)
    assert r == Rect.of([0, 0, 595.0, 421.0])
