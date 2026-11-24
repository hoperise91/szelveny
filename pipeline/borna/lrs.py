"""Phase 2: build the linear reference network (road number + km -> place on the map).

1. Normalise OSM road numbers to HG 540 form (borna.osm_way_ref) and note the counties each
   numbered way touches (borna.osm_way_county).
2. For each HG 540 road, build one centreline through its OSM ways: node the lines, bridge small
   gaps at dead ends, keep the longest path (which also picks one side of dual carriageways).
   County roads (DJ/DC/DP) are clipped to their county, since their numbers repeat elsewhere.
3. Calibrate km along the path, in Stereo 70, from control points:
     - county borders: HG 540's per-county DN tables give the km at every border crossing
     - OSM milestones carrying the road number and a km value
     - the official start/end km, when the path agrees with them
   km is piecewise linear between control points.
4. Write borna.road_segment (one segment between consecutive control points, with km_quality)
   and borna.lrs_road; check tests/lrs_checkpoints.csv with borna.locate().

km_quality: 'calibrated' between two independent control points, 'interpolated' between an
official end and a control point, 'estimated' when extrapolated or the path disagrees with
the official length.
"""

import csv
import itertools
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass

import networkx as nx
import psycopg
import shapely
from shapely import wkb
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point
from shapely.ops import nearest_points, substring

from borna.config import CRS_METRIC, DATA_OUT, PG_DSN, ROOT
from borna.romania import county_code, fold

GAP_M = 300            # bridge gaps up to this size between dead ends of a road's ways
MIN_PIECE_M = 500      # national roads: separate pieces shorter than this are ignored
TWIN_M = 80            # a whole piece running this close alongside a longer piece ...
TWIN_SHARE = 0.8       # ... for at least this share of its length is the other carriageway
FOLD_NEAR_M = 70       # the path passing this close to itself, ...
FOLD_MIN_M = 1500      # ... at least this far apart along it, is running back on itself
OSM_ONLY_MIN_M = 2000  # OSM-only national roads shorter than this are not added (review list)
OSM_ONLY_CATEGORIES = ("A", "DX", "DN", "V")
OSM_ONLY_HIGHWAYS = ("motorway", "trunk", "primary")  # national roads are mapped as these
# Bypasses ('Centura Beclean', 'Varianta Ocolitoare Brănești') are short and sometimes mapped as
# secondary roads: named ones need only 1 km, mapped as trunk/primary/secondary.
OSM_ONLY_BYPASS_RE = re.compile(r"\b(centura|varianta|ocolitoare|ocolire)\b", re.IGNORECASE)
OSM_ONLY_BYPASS_MIN_M = 1000
OSM_ONLY_MAX_SHARED = 0.5  # a candidate mostly on ways of a register road is an alias ('DNVO1Z' on CORA)
MILESTONE_SNAP_M = 50  # a milestone further from the path than this is ignored
JUNCTION_SNAP_M = 150  # the road named at a route end must come this close to the path
PLACE_NEAR_M = 5000    # a place named at a route end must be this close to the path
STOP_PREFIX_RE = re.compile(r"^(MUN\.?|MUNICIPIUL|ORAS(UL)?|COMUNA|SAT(UL)?|GARA|STATIA CF)\s+")
LENGTH_TOLERANCE = 0.10
CHECKPOINT_TARGET_M = 50

OSM_REF_RE = re.compile(r"^(A|DN|DJ|DC|DP|DEX|DX)(\d+)([A-ZĂÂÎȘȚ]{0,3}\d?)$")
# Bypasses opened after the last HG 540 amendment carry informal codes in OSM: 'DNVO1Z'
# (Centura Oradea), 'DNVOF' (Făgăraș), 'VO3G'. Kept as written, without 'DN'; never official.
OSM_BYPASS_RE = re.compile(r"^(?:DN)?(VO[0-9A-Z]{1,4})$")
ROUTE_REF_RE = re.compile(r"\b(A|DN|DJ|DC)\s?(\d+\s?[A-Z]?)\b")


# --- 1. road numbers ---------------------------------------------------------------------

def normalise_osm_ref(token: str, bypass_codes: set[str]) -> tuple[str, str] | None:
    """One OSM ref token -> (ref, category) in HG 540 form, or None.

    'DN 6' -> ('DN6', 'DN'), 'DEx12' -> ('DX12', 'DX'), 'DNCB' -> ('CB', 'V') when CB is an
    HG 540 bypass code, 'DNVOF' -> ('VOF', 'V') for a bypass HG 540 does not list yet.
    Bare numbers ('34') and foreign refs are left out.
    """
    t = re.sub(r"[\s.]", "", token.upper())
    if t in bypass_codes:  # before the pattern: 'DN7CC' is the 7CC bypass, not DN7 + 'CC'
        return t, "V"
    if t.startswith("DN") and t[2:] in bypass_codes:
        return t[2:], "V"
    if m := OSM_BYPASS_RE.match(t):
        return m.group(1), "V"
    if m := OSM_REF_RE.match(t):
        prefix = "DX" if m.group(1) == "DEX" else m.group(1)
        return f"{prefix}{m.group(2)}{m.group(3)}", prefix
    return None


def build_way_refs(conn) -> None:
    bypass = {r for (r,) in conn.execute("SELECT ref FROM borna.road WHERE category = 'V'")}
    tokens = {t.strip() for (raw,) in conn.execute(
        "SELECT DISTINCT ref FROM raw.osm_roads WHERE ref IS NOT NULL "
        "UNION SELECT DISTINCT ref FROM raw.osm_road_routes WHERE ref IS NOT NULL")
        for t in re.split(r"[;,]", raw)}
    conn.execute("CREATE TEMP TABLE ref_map (raw text PRIMARY KEY, ref text, category text)")
    with conn.cursor().copy("COPY ref_map FROM STDIN") as copy:
        for t in tokens:
            if t and (n := normalise_osm_ref(t, bypass)):
                copy.write_row((t, *n))
    conn.execute("""
        TRUNCATE borna.osm_way_ref, borna.osm_way_county;
        INSERT INTO borna.osm_way_ref
        SELECT osm_id, ref, min(category), bool_or(via_tag), bool_or(via_route)
        FROM (
            SELECT w.osm_id, m.ref, m.category, true AS via_tag, false AS via_route
            FROM raw.osm_roads w
            CROSS JOIN LATERAL regexp_split_to_table(w.ref, '[;,]') AS tok
            JOIN ref_map m ON m.raw = trim(tok)
            UNION ALL
            SELECT (way_id)::bigint, m.ref, m.category, false, true
            FROM raw.osm_road_routes r
            CROSS JOIN LATERAL jsonb_array_elements_text(r.way_ids) AS way_id
            CROSS JOIN LATERAL regexp_split_to_table(r.ref, '[;,]') AS tok
            JOIN ref_map m ON m.raw = trim(tok)
            WHERE r.network IS NULL OR r.network ILIKE 'RO:%'
        ) x
        WHERE osm_id IN (SELECT osm_id FROM raw.osm_roads)
        GROUP BY osm_id, ref;

        CREATE TEMP TABLE county_part AS
        SELECT name, ST_Subdivide(geom, 256) AS geom FROM raw.osm_counties;
        CREATE INDEX ON county_part USING gist (geom);
        INSERT INTO borna.osm_way_county
        SELECT DISTINCT w.osm_id, c.name
        FROM raw.osm_roads w JOIN county_part c ON ST_Intersects(w.geom, c.geom)
        WHERE w.osm_id IN (SELECT osm_id FROM borna.osm_way_ref);
        DROP TABLE county_part, ref_map;
    """)
    # OSM county names ('Covasna', 'București') -> ISO codes
    for (name,) in conn.execute("SELECT DISTINCT county FROM borna.osm_way_county").fetchall():
        conn.execute("UPDATE borna.osm_way_county SET county = %s WHERE county = %s",
                     (county_code(name), name))
    conn.execute("""
        -- HG 540 still lists some county roads that were transferred to the state and renumbered
        -- (DJ113 Covasna = DN11C); OSM route relations may still bundle them on the national road.
        -- Such a county road gives its national-tagged ways up when they add up to at least half its
        -- own official length: a duplicate (DJ113 = DN11C) or a relation that wandered onto the
        -- national road (DJ156 Prahova, officially 6.4 km, claimed 9 km of DN72). The road page's
        -- "now DN11C" note needs a national road of about the same length (tiles.py).
        -- Ordinary shared stretches (DJ391 along DN38) stay: they hold county routes together.
        CREATE TEMP TABLE route_claims AS
        SELECT x.osm_id, x.ref, c.county, min(n.ref) AS national,
               ST_Length(w.geom::geography) AS m
        FROM borna.osm_way_ref x
        JOIN borna.osm_way_ref n ON n.osm_id = x.osm_id AND n.via_tag
                                AND n.category IN ('A', 'DX', 'DN', 'V')
        JOIN raw.osm_roads w ON w.osm_id = x.osm_id
        JOIN borna.osm_way_county c ON c.osm_id = x.osm_id
        WHERE NOT x.via_tag AND x.category IN ('DJ', 'DC')
        GROUP BY x.osm_id, x.ref, c.county, w.geom;
        CREATE TEMP TABLE route_claims_dropped AS
        SELECT rc.* FROM route_claims rc
        JOIN (SELECT rc2.ref, rc2.county, rc2.national
              FROM route_claims rc2
              JOIN borna.road cr ON cr.ref = rc2.ref AND cr.county = rc2.county
              JOIN borna.road nr ON nr.ref = rc2.national AND nr.county IS NULL
                                AND nr.source_id <> 'osm'
              GROUP BY rc2.ref, rc2.county, rc2.national, cr.length_official
              HAVING sum(rc2.m) >= 500 * cr.length_official
             ) dup USING (ref, county, national);
        DELETE FROM borna.osm_way_ref x USING route_claims_dropped d
        WHERE x.osm_id = d.osm_id AND x.ref = d.ref;
    """)


# --- 2. centreline ---------------------------------------------------------------------

def _node(c) -> tuple[float, float]:
    return round(c[0], 1), round(c[1], 1)


def build_path(lines: list[LineString], gap_m: float = GAP_M,
               prefer: list[Point] | None = None,
               connect_pieces: bool = False) -> tuple[LineString, list[tuple[float, float]]] | None:
    """Longest path through a road's lines (metric CRS), bridging small gaps at dead ends.
    Returns the path and its unmapped gaps as (from, to) positions in metres.

    When the lines fall into separate pieces:
      - connect_pieces (national roads, which really are built in pieces, e.g. A1): all pieces
        of at least MIN_PIECE_M are linked through their nearest ends, so km runs on across
        the gaps; the links are returned as gaps and are not drawn;
      - else the longest piece wins, unless `prefer` points (the places a route is officially
        named after) are given: then the piece nearest them wins among pieces at least a
        quarter as long as the longest.
    """
    lines = [ln for ln in lines if ln.length > 0]
    if not lines:
        return None
    noded = shapely.unary_union(lines)
    parts = [noded] if isinstance(noded, LineString) else list(getattr(noded, "geoms", []))
    g = nx.Graph()
    for ln in parts:
        if not isinstance(ln, LineString) or ln.length == 0:
            continue
        a, b = _node(ln.coords[0]), _node(ln.coords[-1])
        if a == b:
            continue  # closed loop (a full roundabout): the road passes through, not along it
        if not g.has_edge(a, b) or g[a][b]["weight"] > ln.length:
            g.add_edge(a, b, weight=ln.length, line=ln)
    if g.number_of_nodes() == 0:
        return None

    # Dual carriageways mapped as two separate pieces: keep one, before anything links them
    # (linked, the twins form a U or a loop, and paths run up one side and back the other).
    pieces = list(nx.connected_components(g))
    keep = _drop_twin_carriageways(g, pieces)
    for c in pieces:
        if c not in keep:
            g.remove_nodes_from(c)

    # bridge small mapping gaps: from each dead end to the nearest node of another piece
    comp_of = {n: i for i, comp in enumerate(nx.connected_components(g)) for n in comp}
    if len(set(comp_of.values())) > 1:
        nodes = list(g.nodes)
        tree = shapely.STRtree([Point(n) for n in nodes])
        for n in [n for n in nodes if g.degree(n) == 1]:
            best = None
            for j in tree.query(Point(n), predicate="dwithin", distance=gap_m):
                m = nodes[j]
                if comp_of[m] != comp_of[n]:
                    d = Point(n).distance(Point(m))
                    if best is None or d < best[0]:
                        best = (d, m)
            if best and not g.has_edge(n, best[1]):
                g.add_edge(n, best[1], weight=best[0] * 1.5, line=None, gap=False)

    comps = list(nx.connected_components(g))

    def comp_length(c):
        return sum(d["weight"] for _, _, d in g.subgraph(c).edges(data=True))

    if connect_pieces and len(comps) > 1:
        comps = [c for c in comps if comp_length(c) >= MIN_PIECE_M] or [max(comps, key=comp_length)]
        # Kruskal over the pieces: link each pair through their nearest dead ends
        ends = [MultiPoint([n for n in c if g.degree(n) == 1] or list(c)) for c in comps]
        links = []
        for i, j in itertools.combinations(range(len(comps)), 2):
            pi, pj = nearest_points(ends[i], ends[j])
            links.append((pi.distance(pj), i, j, _node(pi.coords[0]), _node(pj.coords[0])))
        parent = list(range(len(comps)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for d, i, j, ni, nj in sorted(links):
            if find(i) != find(j):
                parent[find(i)] = find(j)
                g.add_edge(ni, nj, weight=d, line=None, gap=True)
        comp = set().union(*comps)
    elif prefer and len(comps) > 1:
        longest = max(comp_length(c) for c in comps)
        comps = [c for c in comps if comp_length(c) >= 0.25 * longest]
        def remoteness(c):
            geom = MultiLineString([d["line"] for _, _, d in g.subgraph(c).edges(data=True)
                                    if d["line"] is not None] or [LineString(list(c)[:2])])
            return sum(min(geom.distance(p), PLACE_NEAR_M) for p in prefer)
        comp = min(comps, key=remoteness)
    else:
        comp = max(comps, key=comp_length)
    h = nx.Graph(g.subgraph(comp))
    for _ in range(6):  # drop legs where the path runs back on itself, then try again
        route = _longest_route(h)
        if route is None:
            return None
        path, gaps, spans = _assemble(h, route)
        fold = _fold(path)
        if fold is None:
            return path, gaps
        f, t = fold
        h.remove_edges_from([e for e, (a, b) in spans.items() if f <= (a + b) / 2 <= t])
        h = nx.Graph(h.subgraph(max(nx.connected_components(h), key=lambda c: sum(
            d["weight"] for _, _, d in h.subgraph(c).edges(data=True)))))
    return path, gaps


def _drop_twin_carriageways(g: nx.Graph, comps: list[set]) -> list[set]:
    """Motorways are mapped as two one-way carriageways; without their slip roads each is a
    separate piece. Keep one: a piece lying mostly alongside a longer kept piece is its twin.
    Linked to each other at one end, the twins would form a U that paths run up and back."""
    def geom(c):
        return MultiLineString([d["line"] for _, _, d in g.subgraph(c).edges(data=True)
                                if d["line"] is not None] or [LineString(list(c)[:2])])
    kept, zone = [], None
    for c in sorted(comps, key=lambda c: -geom(c).length):
        gc = geom(c)
        if zone is not None and gc.intersection(zone).length >= TWIN_SHARE * gc.length:
            continue
        kept.append(c)
        zone = gc.buffer(TWIN_M) if zone is None else zone.union(gc.buffer(TWIN_M))
    return kept


def _longest_route(h: nx.Graph) -> list | None:
    """Double sweep: the farthest node from anywhere, then the farthest from that."""
    first = next(iter(h.nodes))
    dist = nx.single_source_dijkstra_path_length(h, first)
    a = max(dist, key=dist.get)
    dist, paths = nx.single_source_dijkstra(h, a)
    route = paths[max(dist, key=dist.get)]
    return route if len(route) >= 2 else None


def _assemble(h: nx.Graph, route: list):
    """Route nodes -> (path, gaps, {edge: (from, to) position along the path})."""
    coords: list[tuple[float, float]] = []
    gaps: list[tuple[float, float]] = []
    spans = {}
    run = 0.0
    for u, v in itertools.pairwise(route):
        edge = h[u][v]
        line = edge["line"]
        seg = [u, v] if line is None else list(line.coords)
        if line is not None and _node(seg[0]) != u:
            seg.reverse()
        coords.extend(seg if not coords else seg[1:])
        step = LineString(seg).length
        if edge.get("gap"):
            gaps.append((run, run + step))
        spans[(u, v)] = (run, run + step)
        run += step
    return LineString(coords), gaps, spans


def _fold(path: LineString, step: float = 100.0) -> tuple[float, float] | None:
    """Where the path runs back alongside itself (e.g. up one carriageway of a dual road and
    back down the other): the (from, to) positions of the leg to drop, or None.
    A fold at the start drops the outward leg; elsewhere the returning leg."""
    n = int(path.length // step)
    if n < 3:
        return None
    pos = [i * step for i in range(n + 1)]
    pts = [path.interpolate(p) for p in pos]
    tree = shapely.STRtree(pts)
    back = {}  # sample index -> earliest earlier position it runs alongside
    for i, p in enumerate(pts):
        near = [j for j in tree.query(p, predicate="dwithin", distance=FOLD_NEAR_M)
                if pos[j] < pos[i] - FOLD_MIN_M]
        if near:
            back[i] = min(pos[j] for j in near)
    if len(back) * step < FOLD_MIN_M:
        return None
    # the longest run of consecutive "running back" samples
    runs, cur = [], []
    for i in sorted(back):
        if cur and i != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(i)
    runs.append(cur)
    longest = max(runs, key=len)
    if len(longest) * step < FOLD_MIN_M:
        return None
    late = (pos[longest[0]], pos[longest[-1]])
    early = (min(back[i] for i in longest), max(back[i] for i in longest))
    return (0.0, late[0]) if early[0] < FOLD_MIN_M else late


# --- 3. calibration --------------------------------------------------------------------

@dataclass
class Control:
    pos: float   # metres along the path
    km: float
    kind: str    # 'border' | 'junction' | 'milestone' | 'start' | 'end' | 'extrapolated'


def _increasing(controls: list[Control]) -> list[Control]:
    """Longest subsequence with km strictly increasing along the path (drops outliers)."""
    pts = sorted(controls, key=lambda c: c.pos)
    if not pts:
        return []
    best = [[c] for c in pts]
    for i, c in enumerate(pts):
        for j in range(i):
            if pts[j].km < c.km and pts[j].pos < c.pos and len(best[j]) + 1 > len(best[i]):
                best[i] = best[j] + [c]
    return max(best, key=len)


def decide_orientation(length: float, km_from: float, km_to: float, interior: list[Control],
                       route_evidence: float | None,
                       evidence_label: str = "route_ends") -> tuple[bool, str]:
    """(reverse?, method). route_evidence (metres): how much closer the path's first point is
    to the road named at the route's start (and its last point to the road at the route's
    end) than the other way round; positive means the path already runs start -> end."""
    if len(interior) >= 2:
        pts = sorted(interior, key=lambda c: c.pos)
        n = len(pts)
        mp, mk = sum(c.pos for c in pts) / n, sum(c.km for c in pts) / n
        slope = sum((c.pos - mp) * (c.km - mk) for c in pts)
        return slope < 0, "controls"
    if route_evidence is not None and abs(route_evidence) > 100:
        return route_evidence < 0, evidence_label
    if len(interior) == 1 and length > 0:
        c = interior[0]
        span = km_to - km_from
        fwd = abs(km_from + span * c.pos / length - c.km)
        rev = abs(km_from + span * (length - c.pos) / length - c.km)
        return rev < fwd, "single_control"
    return False, "none"


def anchors_for(length: float, km_from: float, km_to: float,
                interior: list[Control]) -> list[Control]:
    """Control points (pos ascending, km ascending) that define km along the path."""
    pts = _increasing([c for c in interior if 0 <= c.pos <= length])
    if not pts:
        span = km_to - km_from
        if span <= 0 or abs(length / 1000 - span) / span > LENGTH_TOLERANCE:
            # the path doesn't match the official span (a road longer than the register says,
            # like A3, or only partly mapped): count km at true scale from the start, estimated
            return [Control(0, km_from, "start"), Control(length, km_from + length / 1000, "extrapolated")]
        return [Control(0, km_from, "start"), Control(length, km_to, "end")]
    if pts[0].pos < 1:
        pts[0] = Control(0, pts[0].km, pts[0].kind)
    if length - pts[-1].pos < 1:
        pts[-1] = Control(length, pts[-1].km, pts[-1].kind)
    first, last = pts[0], pts[-1]
    # Before the first control: interpolate to the official start if the control is clearly
    # past it (and, with several controls, the path agrees with it); otherwise extend at
    # scale 1. Same after the last control.
    start_km = first.km - first.pos / 1000
    if first.pos == 0:
        start = None
    elif first.km - km_from > 0.05 and (len(pts) == 1 or abs(start_km - km_from) < 0.5):
        start = Control(0, km_from, "start")
    else:
        start = Control(0, start_km, "extrapolated")
    end_km = last.km + (length - last.pos) / 1000
    if last.pos == length:
        end = None
    elif km_to - last.km > 0.05 and (len(pts) == 1 or abs(end_km - km_to) < 0.5):
        end = Control(length, km_to, "end")
    else:
        end = Control(length, end_km, "extrapolated")
    out = [c for c in (start, *pts, end) if c is not None]
    if len(out) == 1:  # a single control at one end: the official km fixes the other
        out = ([out[0], Control(length, km_to, "end")] if out[0].pos == 0
               else [Control(0, km_from, "start"), out[0]])
    # drop anchors that collapse onto their neighbour
    clean = [out[0]]
    for c in out[1:]:
        if c.pos - clean[-1].pos > 1 and c.km > clean[-1].km:
            clean.append(c)
    return clean


def without_gaps(f: float, t: float, gaps: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The parts of [f, t] outside the gaps (pieces shorter than 1 m dropped)."""
    out, cur = [], f
    for gf, gt in sorted(gaps):
        if gt <= cur or gf >= t:
            continue
        if gf - cur > 1:
            out.append((cur, gf))
        cur = max(cur, gt)
    if t - cur > 1:
        out.append((cur, t))
    return out


def segment_quality(a: Control, b: Control, length_ok: bool) -> str:
    kinds = {a.kind, b.kind}
    if "extrapolated" in kinds or not length_ok:
        return "estimated"
    if kinds <= {"border", "milestone", "junction"}:
        return "calibrated"
    return "interpolated"


# --- 4. build ---------------------------------------------------------------------------

def _end_refs(route_desc: str | None) -> tuple[str | None, str | None]:
    """Roads named at the first and last stop of an official route:
    'Chichiș (DN 11)- Sfântu Gheorghe- Toplița (DN 15)' -> ('DN11', 'DN15')."""
    if not route_desc:
        return None, None
    stops = re.split(r"\s[-–]\s?|[-–]\s", route_desc.split(" | ")[0])

    def ref_in(stop: str) -> str | None:
        m = ROUTE_REF_RE.search(stop)
        return f"{m.group(1)}{m.group(2).replace(' ', '')}" if m else None

    return ref_in(stops[0]), (ref_in(stops[-1]) if len(stops) > 1 else None)


def stop_names(route_desc: str | None) -> tuple[str | None, str | None]:
    """Place names at a route's first and last stop, folded for matching:
    'Ghimbav (DN 1) - Brașov' -> ('GHIMBAV', 'BRASOV'). Stops that are a county limit, a
    junction or a road number give None."""
    if not route_desc:
        return None, None
    stops = re.split(r"\s[-–]\s?|[-–]\s", route_desc.split(" | ")[0])

    def clean(stop: str) -> str | None:
        name = STOP_PREFIX_RE.sub("", fold(re.sub(r"\(.*?\)", "", stop)).strip())
        if not name or re.search(r"LIMIT|INTERSECT|^(A|DN|DJ|DC)\s?\d", name):
            return None
        return name

    return clean(stops[0]), (clean(stops[-1]) if len(stops) > 1 else None)


def attach_junctions(path: LineString, start_hint, end_hint, km_from: float,
                     km_to: float) -> tuple[LineString, list[Control]]:
    """Official km at the junctions with the roads named at the route's ends.

    Where the path stops short of that road (an interchange whose ramps were dropped, a small
    mapping gap), it is extended straight to it, so km 0 sits on the other road's axis.
    """
    controls = []
    for hint, at_start, km in ((start_hint, True, km_from), (end_hint, False, km_to)):
        reach = min(3000.0, path.length * 0.25)
        if hint is None or reach <= 0:
            continue
        part = (substring(path, 0, reach) if at_start
                else substring(path, path.length - reach, path.length))
        on_path, on_hint = nearest_points(part, hint)
        gap = on_path.distance(on_hint)
        if gap > JUNCTION_SNAP_M:
            continue
        end_pt = Point(path.coords[0] if at_start else path.coords[-1])
        if gap > 1 and on_path.distance(end_pt) < 1:  # stops short: extend to the other road
            coords = list(path.coords)
            path = LineString([on_hint.coords[0], *coords] if at_start
                              else [*coords, on_hint.coords[0]])
            on_path = on_hint
        controls.append((on_path, km))
    return path, [Control(path.project(pt), km, "junction") for pt, km in controls]


def _border_controls(path: LineString, sections: list[tuple], county_geom: dict) -> list[Control]:
    """km at county-border crossings from the per-county DN sections (county, km_from, km_to)."""
    out = []
    secs = sorted(sections, key=lambda s: s[1])
    for (c1, _f1, t1), (c2, f2, _t2) in itertools.pairwise(secs):
        if c1 == c2 or abs(t1 - f2) > 0.5 or c1 not in county_geom or c2 not in county_geom:
            continue
        border = county_geom[c1].boundary.intersection(county_geom[c2].boundary)
        if border.is_empty:
            continue
        hits = path.intersection(border)
        pts = [hits] if isinstance(hits, Point) else [
            g for g in getattr(hits, "geoms", []) if isinstance(g, Point)]
        if len(pts) == 1:  # several crossings: ambiguous, skip
            out.append(Control(path.project(pts[0]), float(t1), "border"))
    return out


HOLDOUT_EVERY = 5  # --holdout: every 5th milestone (by OSM id) is kept out of calibration


def build(conn, holdout: bool = False) -> tuple[list[dict], list[tuple]]:
    srid = CRS_METRIC
    print("lrs: loading ways, counties, milestones…", flush=True)
    ways_by_ref: dict[str, list] = defaultdict(list)
    # Roads under construction (highway=construction) are not part of the network: they are
    # exported separately for the map. Ramps (*_link) are kept only as junction hints.
    for _osm_id, ref, counties, g, is_link in conn.execute(f"""
            SELECT w.osm_id, r.ref, array_remove(array_agg(DISTINCT c.county), NULL),
                   ST_AsBinary(ST_Transform(w.geom, {srid})), right(w.highway, 5) = '_link'
            FROM borna.osm_way_ref r
            JOIN raw.osm_roads w USING (osm_id)
            LEFT JOIN borna.osm_way_county c USING (osm_id)
            WHERE w.highway <> 'construction'
            GROUP BY w.osm_id, r.ref, w.geom, w.highway"""):
        ways_by_ref[ref].append((set(counties), wkb.loads(bytes(g)), is_link))

    county_geom = {county_code(name): wkb.loads(bytes(g)) for name, g in conn.execute(
        f"SELECT name, ST_AsBinary(ST_Transform(geom, {srid})) FROM raw.osm_counties")}

    bypass = {r for (r,) in conn.execute("SELECT ref FROM borna.road WHERE category = 'V'")}
    milestones = defaultdict(list)
    held_out = []  # (ref, km, osm_id) of milestones kept out to test against
    for osm_id, ref, dist, g in conn.execute(f"""
            SELECT osm_id, ref, distance, ST_AsBinary(ST_Transform(geom, {srid}))
            FROM raw.osm_road_points WHERE kind = 'milestone' AND ref IS NOT NULL
              AND distance ~ '^\\s*\\d+([.,]\\d+)?\\s*$'"""):
        if n := normalise_osm_ref(ref.split(";")[0], bypass):
            km = float(dist.replace(",", "."))
            if holdout and osm_id % HOLDOUT_EVERY == 0:
                held_out.append((n[0], km, osm_id))
            else:
                milestones[n[0]].append((km, wkb.loads(bytes(g))))

    places = defaultdict(list)
    for name, g in conn.execute(
            f"SELECT name, ST_AsBinary(ST_Transform(geom, {srid})) FROM raw.osm_places"):
        places[fold(name)].append(wkb.loads(bytes(g)))

    sections = defaultdict(list)
    for ref, county, f, t in conn.execute(
            "SELECT ref, county, km_from, km_to FROM borna.register_entry "
            "WHERE list = 'county_dn' AND km_from IS NOT NULL AND km_to IS NOT NULL"):
        sections[ref].append((county, float(f), float(t)))

    # National roads open in OSM but missing from HG 540 (A7, DX12, Centura Brașov DN1K, the
    # Făgăraș bypass VOF, ...): added as roads with source 'osm', no official km, the OSM name
    # as route. Candidates too short or not mapped as main roads go to the review list instead.
    candidates = conn.execute(f"""
        SELECT x.ref, min(x.category),
               sum(ST_Length(w.geom::geography)) FILTER (WHERE w.highway IN {OSM_ONLY_HIGHWAYS}),
               sum(ST_Length(w.geom::geography)) FILTER (WHERE w.highway IN {OSM_ONLY_HIGHWAYS + ("secondary",)}),
               sum(ST_Length(w.geom::geography)),
               mode() WITHIN GROUP (ORDER BY w.highway),
               mode() WITHIN GROUP (ORDER BY w.name),
               sum(ST_Length(w.geom::geography)) FILTER (WHERE s.refs IS NOT NULL),
               string_agg(DISTINCT s.refs, ',')
        FROM borna.osm_way_ref x JOIN raw.osm_roads w USING (osm_id)
        LEFT JOIN LATERAL (
            SELECT string_agg(y.ref, ',') AS refs FROM borna.osm_way_ref y
            JOIN borna.road r ON r.ref = y.ref AND r.source_id <> 'osm'
            WHERE y.osm_id = x.osm_id AND y.ref <> x.ref) s ON true
        WHERE x.category IN {OSM_ONLY_CATEGORIES}
          AND NOT EXISTS (SELECT 1 FROM borna.road r WHERE r.ref = x.ref AND r.source_id <> 'osm')
        GROUP BY x.ref ORDER BY x.ref""").fetchall()
    conn.execute("DELETE FROM borna.road WHERE source_id = 'osm'")
    osm_only_report = []
    for ref, category, main_m, sec_m, all_m, highway, name, shared_m, shared_refs in candidates:
        alias = (shared_m or 0) > OSM_ONLY_MAX_SHARED * all_m
        bypass = category in ("DN", "V") and bool(name and OSM_ONLY_BYPASS_RE.search(name))
        added = not alias and ((main_m or 0) > OSM_ONLY_MIN_M
                               or (bypass and (sec_m or 0) > OSM_ONLY_BYPASS_MIN_M))
        if added:
            # OSM names fit bypasses ('Centura Brașov') but not motorways ('Lot 1').
            route = name if category in ("DN", "V") else None
            conn.execute("INSERT INTO borna.road (ref, category, county, route_desc, source_id) "
                         "VALUES (%s, %s, NULL, %s, 'osm')", (ref, category, route))
        osm_only_report.append({
            "ref": ref, "category": category, "km": round(all_m / 1000, 1),
            "main_road_km": round((main_m or 0) / 1000, 1), "highway": highway, "name": name or "",
            "added": "yes" if added else
                     f"no: overlaps {','.join(sorted(set(shared_refs.split(','))))}" if alias else
                     "no: review"})
    (DATA_OUT / "lrs").mkdir(parents=True, exist_ok=True)
    with open(DATA_OUT / "lrs" / "osm_only.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(osm_only_report[0]) if osm_only_report else ["ref"])
        w.writeheader()
        w.writerows(osm_only_report)
    print(f"lrs: {sum(r['added'] == 'yes' for r in osm_only_report)} national roads added from OSM, "
          f"{sum(r['added'] != 'yes' for r in osm_only_report)} to review (osm_only.csv)")
    roads = conn.execute("""
        SELECT road_id, ref, category, county, route_desc,
               km_from_official, km_to_official, length_official, source_id = 'osm'
        FROM borna.road ORDER BY category, ref""").fetchall()

    hints: dict[tuple, object] = {}

    def hint_geom(hint_ref: str | None, county: str | None):
        """Geometry of the road a route starts/ends at (this county's part for county roads)."""
        if not hint_ref or not ways_by_ref.get(hint_ref):
            return None
        key = (hint_ref, county)
        if key not in hints:
            geoms = [g for cs, g, _ in ways_by_ref[hint_ref] if not county or county in cs]
            hints[key] = MultiLineString(
                [ln for g in geoms for ln in getattr(g, "geoms", [g])]) if geoms else None
        return hints[key]

    print(f"lrs: building {len(roads):,} roads…", flush=True)
    results, segments = [], []
    for i, (road_id, ref, category, county, route_desc, km_from, km_to, length,
            osm_only) in enumerate(roads):
        if i and i % 1000 == 0:
            print(f"lrs:   {i:,}/{len(roads):,}", flush=True)
        cands = ways_by_ref.get(ref, [])
        if any(not link for _, _, link in cands):  # ramps are not the road's centreline
            cands = [c for c in cands if not c[2]]
        if county:  # county roads: only this county's part
            cands = [g for cs, g, _ in cands if county in cs]
            clip = county_geom.get(county)
            lines = []
            for g in cands:
                part = g.intersection(clip) if clip is not None else g
                lines += [p for p in getattr(part, "geoms", [part]) if isinstance(p, LineString)]
        else:
            lines = [g for _, g, _ in cands]
        # places the official route names at its ends, to choose between separate pieces
        prefer = []
        if lines and county:  # national roads are long and gappy: keep their longest piece
            near = MultiLineString(lines)
            for name in stop_names(route_desc):
                if name and places.get(name):
                    pt = min(places[name], key=near.distance)
                    if near.distance(pt) <= PLACE_NEAR_M:
                        prefer.append(pt)
        built = build_path(lines, prefer=prefer, connect_pieces=county is None)
        official = float(length) if length is not None else None
        path, gaps = built if built else (None, [])
        if osm_only and path is not None:  # no official km: count from 0 along the path
            km_from, km_to = 0.0, (path.length - sum(t - f for f, t in gaps)) / 1000
        if path is None or km_from is None or km_to is None:
            results.append({"road_id": road_id, "status": "no_geometry", "official_km": official,
                                "path_km": None, "n_border": 0, "n_milestone": 0, "orientation": "none",
                                "km_quality": "estimated", "controls_km": None})
            continue
        km_from, km_to, plen = float(km_from), float(km_to), path.length

        interior = _border_controls(path, sections.get(ref, []), county_geom) if not county else []
        for km, pt in milestones.get(ref, []):
            if path.distance(pt) <= MILESTONE_SNAP_M and (not county or county_geom.get(county) is None
                                                          or county_geom[county].covers(pt)):
                interior.append(Control(path.project(pt), km, "milestone"))

        start_ref, end_ref = _end_refs(route_desc)
        start_hint = hint_geom(start_ref, county) if start_ref != ref else None
        end_hint = hint_geom(end_ref, county) if end_ref != ref else None
        # Evidence (metres) that the path already runs from the route's start to its end:
        # first from the places named at the ends, else from the roads named there.
        p0, p1 = Point(path.coords[0]), Point(path.coords[-1])
        evidence, label = 0.0, "route_places"
        for name, at_start in zip(stop_names(route_desc), (True, False)):
            cands = places.get(name) if name else None
            if cands:
                pt = min(cands, key=path.distance)
                if path.distance(pt) <= PLACE_NEAR_M:
                    d0, d1 = p0.distance(pt), p1.distance(pt)
                    evidence += (d1 - d0) if at_start else (d0 - d1)
        if abs(evidence) <= 100:
            evidence, label = 0.0, "route_ends"
            if start_hint is not None:
                evidence += p1.distance(start_hint) - p0.distance(start_hint)
            if end_hint is not None:
                evidence += p0.distance(end_hint) - p1.distance(end_hint)
        reverse, how = decide_orientation(plen, km_from, km_to, interior, evidence or None, label)
        if reverse:
            path = path.reverse()
            for c in interior:
                c.pos = plen - c.pos
            gaps = [(plen - t, plen - f) for f, t in reversed(gaps)]
        path, junctions = attach_junctions(path, start_hint, end_hint, km_from, km_to)
        if path.length != plen:  # extended: shift existing controls if the start moved
            shift = path.project(Point(p0.coords[0] if not reverse else p1.coords[0]))
            for c in interior:
                c.pos += shift
            gaps = [(f + shift, t + shift) for f, t in gaps]
            plen = path.length
        interior += junctions

        anchors = anchors_for(plen, km_from, km_to, interior)
        used = {(round(a.pos), a.kind) for a in anchors}
        n_ms = sum(1 for c in interior if c.kind == "milestone" and (round(c.pos), c.kind) in used)
        n_bd = sum(1 for c in interior if c.kind in ("border", "junction")
                   and (round(c.pos), c.kind) in used)
        span = km_to - km_from
        length_ok = how != "none" and (
            len(anchors) > 2 or (span > 0 and abs(plen / 1000 - span) / span <= LENGTH_TOLERANCE))
        worst = "calibrated"
        seq = 0
        for a, b in itertools.pairwise(anchors):
            q = segment_quality(a, b, length_ok)
            worst = max(worst, q, key=["calibrated", "interpolated", "estimated"].index)
            rate = (b.km - a.km) / (b.pos - a.pos)  # km per metre within this anchor pair
            for f, t in without_gaps(a.pos, b.pos, gaps):
                segments.append((road_id, seq, round(a.km + (f - a.pos) * rate, 3),
                                 round(a.km + (t - a.pos) * rate, 3), substring(path, f, t).wkb, q))
                seq += 1
        mapped = plen - sum(t - f for f, t in gaps)
        if osm_only:
            status = "osm_only"
        else:
            status = "ok" if official and abs(mapped / 1000 - official) / official <= LENGTH_TOLERANCE \
                else "partial"
        results.append({"road_id": road_id, "status": status, "official_km": official,
                            "path_km": round(mapped / 1000, 3), "n_border": n_bd, "n_milestone": n_ms,
                            "orientation": how, "km_quality": worst,
                            "controls_km": sorted({round(a.km, 3) for a in anchors
                                                   if a.kind in ("border", "milestone", "junction")})})

    print(f"lrs: writing {len(segments):,} segments…", flush=True)
    conn.execute("TRUNCATE borna.road_segment, borna.lrs_road RESTART IDENTITY")
    conn.execute("CREATE TEMP TABLE seg_in (road_id int, sequence int, km_from numeric, "
                 "km_to numeric, g bytea, km_quality text)")
    with conn.cursor().copy("COPY seg_in FROM STDIN") as copy:
        for row in segments:
            copy.write_row(row)
    conn.execute(f"""
        INSERT INTO borna.road_segment (road_id, sequence, km_from, km_to, geom, km_quality, source_id)
        SELECT road_id, sequence, km_from, km_to,
               ST_Transform(ST_SetSRID(ST_GeomFromWKB(g), {srid}), 4326), km_quality, 'osm'
        FROM seg_in WHERE ST_GeometryType(ST_GeomFromWKB(g)) = 'ST_LineString';
        DROP TABLE seg_in;
    """)
    with conn.cursor().copy(
            "COPY borna.lrs_road (road_id, status, official_km, path_km, n_border, n_milestone, "
            "orientation, km_quality, controls_km) FROM STDIN") as copy:
        for r in results:
            copy.write_row([r[k] for k in ("road_id", "status", "official_km", "path_km",
                                           "n_border", "n_milestone", "orientation", "km_quality",
                                           "controls_km")])
    return results, held_out


CHECKPOINT_FILES = ("lrs_checkpoints.csv",    # public, in git
                    "local_checkpoints.csv")  # private QA points, git-ignored, optional


def check_checkpoints(conn) -> list[dict]:
    out = []
    for name in CHECKPOINT_FILES:
        path = ROOT / "tests" / name
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            out += _check_file(conn, csv.DictReader(f), name)
    return out


def _check_file(conn, checkpoints, file_name: str) -> list[dict]:
    out = []
    if True:
        for cp in checkpoints:
            row = conn.execute("""
                SELECT l.km_quality,
                       round(ST_Distance(l.geom::geography,
                             ST_SetSRID(ST_Point(%s, %s), 4326)::geography)::numeric, 1)
                FROM borna.locate(%s, %s, %s) l""",
                (float(cp["lon"]), float(cp["lat"]), cp["road"], cp["km"],
                 cp.get("county") or None)).fetchone()
            out.append({"file": file_name, "road": cp["road"], "km": cp["km"],
                        "county": cp.get("county") or "", "note": cp["note"],
                        "km_quality": row[0] if row else None,
                        "error_m": float(row[1]) if row else None})
    return out


def check_holdout(conn, held_out: list[tuple]) -> list[float]:
    """Distance (m) from each held-out milestone to where locate() puts its km.
    National roads only: milestones carry no county."""
    errors = []
    for ref, km, osm_id in held_out:
        row = conn.execute("""
            SELECT ST_Distance(l.geom::geography, p.geom::geography)
            FROM borna.locate(%s, %s::numeric) l, raw.osm_road_points p
            WHERE p.osm_id = %s AND ST_DWithin(l.geom::geography, p.geom::geography, 5000)""",
            (ref, km, osm_id)).fetchone()
        if row:
            errors.append(row[0])
    return errors


def main() -> int:
    holdout = "--holdout" in sys.argv
    with psycopg.connect(PG_DSN) as conn:
        conn.execute((ROOT / "pipeline" / "sql" / "10_lrs.sql").read_text())
        print("lrs: normalising OSM road numbers…", flush=True)
        build_way_refs(conn)
        n_ways, n_refs = conn.execute(
            "SELECT count(DISTINCT osm_id), count(DISTINCT ref) FROM borna.osm_way_ref").fetchone()
        print(f"lrs: {n_ways:,} ways carry {n_refs:,} distinct road numbers")
        dropped = conn.execute("""
            SELECT ref, county, national, count(*), round((sum(m) / 1000)::numeric, 1)
            FROM route_claims_dropped GROUP BY 1, 2, 3 ORDER BY 5 DESC""").fetchall()
        print(f"lrs: {sum(r[3] for r in dropped):,} ways tagged as national roads were not given "
              f"to county-road relations ({len(dropped)} roads, route_claims_dropped.csv)")
        results, held_out = build(conn, holdout)
        conn.commit()
        checkpoints = check_checkpoints(conn)

        report = conn.execute("""
            SELECT r.category, count(*),
                   count(*) FILTER (WHERE l.status = 'ok'),
                   count(*) FILTER (WHERE l.status = 'partial'),
                   count(*) FILTER (WHERE l.status = 'no_geometry'),
                   round(100 * (SELECT sum(s.length_m) FILTER (WHERE s.km_quality = 'calibrated')
                                / nullif(sum(s.length_m), 0)
                         FROM borna.road_segment s JOIN borna.road r2 USING (road_id)
                         WHERE r2.category = r.category)),
                   round(sum(l.path_km)), round(sum(l.official_km))
            FROM borna.lrs_road l JOIN borna.road r USING (road_id)
            GROUP BY 1 ORDER BY 2 DESC""").fetchall()
        gaps = conn.execute("""
            SELECT r.ref, r.county, r.category, l.status, l.official_km, l.path_km, r.route_desc
            FROM borna.lrs_road l JOIN borna.road r USING (road_id)
            WHERE l.status <> 'ok' ORDER BY r.category, r.county, r.ref""").fetchall()
        unknown = conn.execute("""
            SELECT w.ref, w.category, count(*) AS ways
            FROM borna.osm_way_ref w
            WHERE NOT EXISTS (SELECT 1 FROM borna.road r WHERE r.ref = w.ref)
            GROUP BY 1, 2 ORDER BY 3 DESC""").fetchall()

    out = DATA_OUT / "lrs"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "gaps.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ref", "county", "category", "status", "official_km", "path_km", "route_desc"])
        w.writerows(gaps)
    with open(out / "osm_refs_not_in_register.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ref", "category", "ways"])
        w.writerows(unknown)
    with open(out / "route_claims_dropped.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ref", "county", "national", "ways", "km"])
        w.writerows(dropped)
    with open(out / "checkpoints.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(checkpoints[0]) if checkpoints else ["road"])
        w.writeheader()
        w.writerows(checkpoints)

    print(f"\n{'cat':<4}{'roads':>7}{'ok':>7}{'partial':>9}{'no geom':>9}{'calib%':>9}"
          f"{'path km':>10}{'official km':>13}")
    for cat, n, ok, part, none, cal, pkm, okm in report:
        print(f"{cat:<4}{n:>7}{ok:>7}{part:>9}{none:>9}{cal or 0:>9}{pkm or 0:>10}{okm or 0:>13}")
    print(f"\n{len(unknown)} OSM road numbers not in the HG 540 register "
          f"(see {out / 'osm_refs_not_in_register.csv'})")
    print("checkpoints:")
    fails = 0
    for cp in checkpoints:
        ok = cp["error_m"] is not None and cp["error_m"] <= CHECKPOINT_TARGET_M
        fails += not ok
        err = "not found" if cp["error_m"] is None else f"{cp['error_m']:.0f} m"
        print(f"  {'✓' if ok else '✗'} {cp['road']} km {cp['km']}: {err} "
              f"({cp['km_quality']})  {cp['note']}")
    print(f"{len(checkpoints) - fails}/{len(checkpoints)} within {CHECKPOINT_TARGET_M} m")
    if holdout:
        with psycopg.connect(PG_DSN) as conn:
            errs = sorted(check_holdout(conn, held_out))
        if errs:
            def pct(p):
                return errs[min(len(errs) - 1, int(p * len(errs)))]
            within = sum(e <= CHECKPOINT_TARGET_M for e in errs)
            (out / "holdout.json").write_text(json.dumps({
                "milestones_held_out": len(held_out), "located": len(errs),
                "median_m": round(pct(0.5), 1), "p80_m": round(pct(0.8), 1),
                "p95_m": round(pct(0.95), 1), "within_50m_share": round(within / len(errs), 3),
            }, indent=2) + "\n")
            print(f"held-out milestones: {len(errs)} of {len(held_out)} located; median "
                  f"{pct(0.5):.0f} m, 80% within {pct(0.8):.0f} m, 95% within {pct(0.95):.0f} m; "
                  f"{within} ({100 * within / len(errs):.0f}%) within {CHECKPOINT_TARGET_M} m")
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
