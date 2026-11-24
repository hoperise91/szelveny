import pytest
from shapely.geometry import LineString, Point

from borna.attributes import RoadLine, place


def road() -> RoadLine:
    r = RoadLine()
    # 0-1000 m calibrated as km 5.0-6.0, 1000-3000 m as km 6.0-8.2 (stretched)
    r.segs = [(LineString([(0, 0), (1000, 0)]), 5.0, 6.0),
              (LineString([(1000, 0), (3000, 0)]), 6.0, 8.2)]
    return r


def test_measure_uses_segment_calibration():
    km, offset = road().measure(Point(2000, 12))
    assert km == pytest.approx(7.1)
    assert offset == pytest.approx(12)


def test_place_way_on_road():
    assert place(road(), LineString([(500, 15), (1500, 15)]), 30) == pytest.approx((5.5, 6.55))


def test_place_rejects_way_off_the_road():
    assert place(road(), LineString([(500, 200), (1500, 200)]), 30) is None
