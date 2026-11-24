import pytest
from shapely.geometry import LineString, Point

from szelveny.lrs import (
    Control,
    _end_refs,
    anchors_for,
    attach_junctions,
    build_path,
    decide_orientation,
    normalise_osm_ref,
    segment_quality,
)


@pytest.mark.parametrize("token,expected", [
    ("M 1", ("M1", "M")),
    ("M0", ("M0", "M")),
    ("86", ("86", "F")),
    ("4512", ("4512", "OK")),
    ("E60", None),
    ("D2", None),                  # a Slovak road number on a border way
])
def test_normalise_osm_ref(token, expected):
    assert normalise_osm_ref(token) == expected


def test_end_refs():
    assert _end_refs("Lenti (M70) - Zalaegerszeg - Győr (1)") == ("M70", "1")
    assert _end_refs("Zalaegerszeg (74. sz. főút) - Pápa") == ("74", None)
    assert _end_refs("Budapest - Hegyeshalom") == (None, None)


def test_build_path_bridges_gap_and_picks_one_carriageway():
    lines = [
        LineString([(0, 0), (1000, 0)]),
        LineString([(1100, 0), (2000, 0)]),                        # 100 m mapping gap
        LineString([(2000, 0), (2500, 0), (3000, 0)]),             # carriageway A
        LineString([(2000, 0), (2500, 30), (3000, 0)]),            # carriageway B (longer)
    ]
    path, gaps = build_path(lines)
    assert path.length == pytest.approx(3000 + 100 * 0, abs=1)   # gap bridged straight
    assert gaps == []                                            # small gaps are road, not gaps
    assert Point(0, 0).distance(Point(path.coords[0])) < 1 or \
        Point(0, 0).distance(Point(path.coords[-1])) < 1


def test_build_path_leaves_far_pieces_out():
    lines = [LineString([(0, 0), (1000, 0)]), LineString([(5000, 0), (5200, 0)])]
    assert build_path(lines)[0].length == pytest.approx(1000)


def test_build_path_links_pieces_of_national_roads():
    # M0-like: two open sections 20 km apart; km must run on, the gap must not be drawn
    lines = [LineString([(0, 0), (10000, 0)]), LineString([(30000, 0), (45000, 0)])]
    path, gaps = build_path(lines, connect_pieces=True)
    assert path.length == pytest.approx(45000)
    [(f, to)] = gaps                                              # either direction
    assert (f, to) in (pytest.approx((10000, 30000)), pytest.approx((15000, 35000)))


def test_without_gaps():
    from szelveny.lrs import without_gaps
    assert without_gaps(0, 100, [(20, 40), (60, 70)]) == [(0, 20), (40, 60), (70, 100)]
    assert without_gaps(0, 100, [(-10, 5), (95, 120)]) == [(5, 95)]


def test_orientation_from_controls_and_route_ends():
    rev, how = decide_orientation(10000, 0, 10, [Control(1000, 9, "junction"),
                                                 Control(9000, 1, "milestone")], None)
    assert (rev, how) == (True, "controls")
    assert decide_orientation(10000, 0, 10, [], 500) == (False, "route_ends")
    assert decide_orientation(10000, 0, 10, [], -500) == (True, "route_ends")
    assert decide_orientation(10000, 0, 10, [], None) == (False, "none")


def test_anchors_official_ends_without_controls():
    a = anchors_for(10000, 0, 10, [])
    assert [(c.pos, c.km, c.kind) for c in a] == [(0, 0, "start"), (10000, 10, "end")]


def test_anchors_true_scale_when_path_disagrees_with_official_span():
    # official km 0-68.8, the mapped road is 400 km: don't squeeze, count at true scale
    a = anchors_for(400000, 0, 68.8, [])
    assert [(c.km, c.kind) for c in a] == [(0, "start"), (400, "extrapolated")]


def test_anchors_drop_outlier_and_extrapolate():
    controls = [Control(2000, 12, "milestone"), Control(5000, 15, "milestone"),
                Control(6000, 3, "milestone"),            # wrong km: dropped
                Control(8000, 18, "milestone")]
    a = anchors_for(10000, 0, 30, controls)
    assert [c.km for c in a] == [10, 12, 15, 18, 20]       # extended at scale 1 both ways
    assert a[0].kind == a[-1].kind == "extrapolated"


def test_anchors_end_junctions_become_ends():
    a = anchors_for(5000, 0, 5.2, [Control(0, 0, "junction"), Control(5000, 5.2, "junction")])
    assert [(c.pos, c.kind) for c in a] == [(0, "junction"), (5000, "junction")]
    assert segment_quality(a[0], a[1], True) == "calibrated"


def test_attach_junctions_extends_path_to_the_other_road():
    path = LineString([(0, 0), (1000, 0)])
    other = LineString([(-80, -500), (-80, 500)])          # crossing road 80 m before the start
    new, controls = attach_junctions(path, other, None, 0.0, 1.0)
    assert new.length == pytest.approx(1080)
    assert [(c.pos, c.km) for c in controls] == [(0, 0.0)]


def test_stop_names():
    from szelveny.lrs import stop_names
    assert stop_names("Győr (1) - Pápa") == ("GYOR", "PAPA")
    assert stop_names("Megyehatár - Nagykanizsa (7) - Zákány") == (None, "ZAKANY")
    assert stop_names("Hódmezővásárhely - Makó") == ("HODMEZOVASARHELY", "MAKO")
    assert stop_names("M7 csomópont - Siófok") == (None, "SIOFOK")


def test_dual_carriageway_joined_at_one_end_is_not_doubled():
    # two one-way carriageways 30 m apart, joined only at the north end (a U): the path must
    # run end to end along one side (50 km), not up one side and back down the other (100 km)
    north = LineString([(0, 0), (0, 50000)])
    south = LineString([(30, 50000), (30, 0)])
    link = LineString([(0, 50000), (30, 50000)])
    path, gaps = build_path([north, south, link], connect_pieces=True)
    assert path.length == pytest.approx(50000, abs=100)
    assert gaps == []


def test_fold_at_one_end_is_removed():
    # carriageways joined at the east end only, far apart at the west end (no bridge):
    # the path must not run east on one side and back west on the other
    from szelveny.lrs import build_path
    north = LineString([(0, 0), (40000, 0)])
    south = LineString([(40000, 30), (500, 30)])            # stops 500 m short: > GAP_M from (0, 0)
    link = LineString([(40000, 0), (40000, 30)])
    path, _ = build_path([north, south, link], connect_pieces=True)
    assert path.length == pytest.approx(40000, rel=0.03)


def test_twin_carriageway_pieces_are_not_linked_into_a_u():
    # two sections, each mapped as two separate carriageways (no slip roads): keep one side
    # of each and link the sections end to end
    from szelveny.lrs import build_path
    a1 = LineString([(0, 0), (20000, 0)])
    a2 = LineString([(20000, 30), (0, 30)])
    b1 = LineString([(30000, 0), (50000, 0)])
    b2 = LineString([(50000, 30), (30000, 30)])
    path, gaps = build_path([a1, a2, b1, b2], connect_pieces=True)
    assert path.length == pytest.approx(50000, abs=100)
    assert len(gaps) == 1 and gaps[0][1] - gaps[0][0] == pytest.approx(10000, abs=100)
