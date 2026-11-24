"""Phase 4.8: horizontal curves (NVDB Kurvatur) from Bornă's own centrelines.

Each road's centreline (borna.road_segment, OSM-derived, so ODbL) is cleaned lightly, resampled
every STEP_M along the line in the metric CRS, and its heading is followed. The curvature at a
point is the heading change over a window of ±HALF_WINDOW_M (±LONG_HALF_WINDOW_M for radii over
SHORT_MAX_RADIUS_M); its inverse is the radius.
A curve is a stretch that keeps turning the same way with R < MAX_RADIUS_M, at least
MIN_LENGTH_M long and turning at least MIN_TURN_DEG in total (drops junction kinks and mapping
noise) and nothing within ROUNDABOUT_M of a roundabout; same-way stretches up to MERGE_M apart are one compound curve. Pieces of a road that do not touch (the unmapped links across real gaps) are never joined.

Each curve becomes an attribute_event of type 'curve' with value '<class>:<R>:<L|R>:<turn>':
class from the smallest radius (CLASSES; a hairpin must also turn ≥ HAIRPIN_TURN_DEG), R in metres (rounded to 5), direction in the km
direction, total turn in degrees. Called from `make attributes`.
"""

import math
from itertools import pairwise

import numpy as np
import shapely
from shapely import wkb
from shapely.geometry import LineString

from borna.config import CRS_METRIC

STEP_M = 10.0          # resampling step along the centreline
HALF_WINDOW_M = 30.0   # curvature = heading change over ±30 m (a 60 m window) ...
SHORT_MAX_RADIUS_M = 200.0  # ... for radii below this; above it over ±60 m:
LONG_HALF_WINDOW_M = 60.0
CLEAN_M = 2.0          # simplify tolerance: removes node jitter, keeps real bends
MAX_RADIUS_M = 500.0   # above this the road counts as straight
MIN_LENGTH_M = 20.0
MIN_TURN_DEG = 10.0
HAIRPIN_TURN_DEG = 135.0   # a hairpin also turns back on itself (else it is 'very_sharp')
ROUNDABOUT_M = 40.0    # no curves on or this near a roundabout (OSM junction=roundabout|circular)
MERGE_M = 30.0         # same-way turning stretches this close together are one (compound) curve
JOIN_M = 1.0           # consecutive segments closer than this are one continuous line

# (upper radius bound in m, class). Loosely after STAS 863 minimum radii per design speed.
CLASSES = [(30, "hairpin"), (80, "very_sharp"), (200, "sharp"), (MAX_RADIUS_M, "moderate")]


def radius_class(r: float, turn: float) -> str:
    c = next(c for bound, c in CLASSES if r < bound)
    return "very_sharp" if c == "hairpin" and turn < HAIRPIN_TURN_DEG else c


def _pieces(segs):
    """Continuous lines of a road: [(coords array, km at each vertex)], segments in km order."""
    pieces, cur_xy, cur_km = [], [], []
    for line, f, t in segs:
        xy = np.asarray(line.coords)
        d = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
        km = f + (t - f) * (d / d[-1] if d[-1] else d)
        if cur_xy and np.hypot(*(cur_xy[-1][-1] - xy[0])) < JOIN_M:
            cur_xy.append(xy[1:]); cur_km.append(km[1:])
        else:
            if cur_xy:
                pieces.append((np.concatenate(cur_xy), np.concatenate(cur_km)))
            cur_xy, cur_km = [xy], [km]
    if cur_xy:
        pieces.append((np.concatenate(cur_xy), np.concatenate(cur_km)))
    return pieces


def _curves_on_piece(xy: np.ndarray, km: np.ndarray, roundabouts=None):
    """[(km_from, km_to, min radius, 'L'|'R', turn degrees)] along one continuous line."""
    line = LineString(xy).simplify(CLEAN_M)
    if line.length < 2 * HALF_WINDOW_M + STEP_M:
        return []
    # km at each resampled point: project back onto the original line's vertex km
    d_orig = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    s = np.arange(0.0, line.length, STEP_M)
    pts = np.array([line.interpolate(v).coords[0] for v in s])
    km_s = np.interp(s * d_orig[-1] / line.length, d_orig, km)  # simplified ≈ original length
    heading = np.unwrap(np.arctan2(np.diff(pts[:, 1]), np.diff(pts[:, 0])))  # per step
    n = len(heading)

    def curvature(half_m: float) -> np.ndarray:
        # at point i (between steps i-1 and i): heading change from step i-h to step i+h-1,
        # whose midpoints are (2h-1) steps apart
        h = round(half_m / STEP_M)
        k = np.zeros(len(pts))
        if n > 2 * h:
            idx = np.arange(h, n - h + 1)
            k[idx] = (heading[idx + h - 1] - heading[idx - h]) / ((2 * h - 1) * STEP_M)
        return k

    # Tight bends need a short window; big radii need a long one (a short window turns OSM node
    # jitter on a straight motorway into R ≈ 400 m).
    k_short, k_long = curvature(HALF_WINDOW_M), curvature(LONG_HALF_WINDOW_M)
    tight = np.abs(k_short) > 1.0 / SHORT_MAX_RADIUS_M
    k = np.where(tight, k_short, k_long)
    turning = tight | (np.abs(k_long) > 1.0 / MAX_RADIUS_M)
    if roundabouts is not None:  # going round a roundabout is not a curve of the road
        turning &= ~shapely.contains_xy(roundabouts, pts[:, 0], pts[:, 1])
    sign = np.sign(k)
    # bridge short dips inside one bend (two arcs joined by a near-straight bit, same direction)
    gap = int(MERGE_M / STEP_M)
    on = np.flatnonzero(turning)
    for p, q in pairwise(on):
        if 1 < q - p <= gap + 1 and sign[p] == sign[q]:
            turning[p + 1:q] = True
            sign[p + 1:q] = sign[p]
    out = []
    i = 0
    while i < len(pts):
        if not turning[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(pts) and turning[j + 1] and sign[j + 1] == sign[i]:
            j += 1
        a, b = i, j
        length = s[b] - s[a]
        turn = abs(math.degrees(heading[min(b, n - 1)] - heading[max(a - 1, 0)]))
        if length >= MIN_LENGTH_M and turn >= MIN_TURN_DEG:
            r = 1.0 / np.max(np.abs(k[a:b + 1]))
            out.append((float(km_s[a]), float(km_s[b]), float(r),
                        "L" if sign[i] > 0 else "R", turn))
        i = j + 1
    return out


def load_roundabouts(conn):
    """Area within ROUNDABOUT_M of any OSM roundabout, in the metric CRS (prepared)."""
    (g,) = conn.execute(f"""
        SELECT ST_AsBinary(ST_Union(ST_Buffer(ST_Transform(geom, {CRS_METRIC}), {ROUNDABOUT_M})))
        FROM raw.osm_roads WHERE tags->>'junction' IN ('roundabout', 'circular')""").fetchone()
    if g is None:
        return None
    area = wkb.loads(bytes(g))
    shapely.prepare(area)
    return area


def curve_rows(roads, roundabouts=None) -> list[tuple]:
    """(road_id, 'curve', value, km_from, km_to) for every curve on every road."""
    rows = []
    for rid, road in roads.items():
        for xy, km in _pieces(road.segs):
            for f, t, r, side, turn in _curves_on_piece(xy, km, roundabouts):
                r5 = max(5, round(r / 5.0) * 5)
                rows.append((rid, "curve", f"{radius_class(r, turn)}:{r5}:{side}:{round(turn)}",
                             round(f, 3), round(t, 3)))
    return rows


def curve_events(conn, manifest: dict, roads, by_key) -> None:
    rows = curve_rows(roads, load_roundabouts(conn))
    print(f"attributes: curve: {len(rows):,} curves")
    conn.execute("DELETE FROM borna.attribute_event WHERE source_id = 'osm' AND type = 'curve'")
    with conn.cursor().copy(
            "COPY borna.attribute_event (road_id, type, value, km_from, km_to, source_id, "
            "source_date, licence) FROM STDIN") as copy:
        for rid, typ, value, f, t in rows:
            if t > f:
                copy.write_row((rid, typ, value, f, t, "osm", manifest["version"],
                                manifest["licence"]))
