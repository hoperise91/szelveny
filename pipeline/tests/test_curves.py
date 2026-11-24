import math

import numpy as np
from shapely.geometry import LineString

from szelveny.curves import _curves_on_piece, _pieces, radius_class


def arc(r: float, deg: float, straight: float = 200.0) -> np.ndarray:
    """A straight, a left arc of radius r turning deg, then a straight (metric coordinates)."""
    pts = [(-straight, 0.0)]
    for a in np.linspace(0, math.radians(deg), max(int(r * math.radians(deg) / 2), 4)):
        pts.append((r * math.sin(a), r - r * math.cos(a)))
    a = math.radians(deg)
    end = pts[-1]
    pts.append((end[0] + straight * math.cos(a), end[1] + straight * math.sin(a)))
    return np.array(pts)


def curves(xy: np.ndarray):
    d = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    return _curves_on_piece(xy, d / 1000)


def test_straight_has_no_curves():
    assert curves(np.array([(0.0, 0.0), (500.0, 3.0), (1000.0, 0.0)])) == []


def test_hairpin():
    (c,) = curves(arc(20, 180))
    assert c[3] == "L" and 150 <= c[4] <= 210
    assert radius_class(c[2], c[4]) == "hairpin"


def test_tight_corner_is_not_a_hairpin():
    (c,) = curves(arc(15, 90))
    assert radius_class(c[2], c[4]) == "very_sharp"


def test_moderate_radius_measured():
    (c,) = curves(arc(300, 60))
    assert 250 <= c[2] <= 350 and radius_class(c[2], c[4]) == "moderate"


def test_pieces_join_only_touching_segments():
    a = LineString([(0, 0), (100, 0)])
    b = LineString([(100, 0), (200, 0)])
    c = LineString([(500, 0), (600, 0)])  # a gap: never joined
    assert len(_pieces([(a, 0.0, 0.1), (b, 0.1, 0.2), (c, 0.5, 0.6)])) == 2
