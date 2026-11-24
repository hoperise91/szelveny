"""Road attributes along the km (szelveny.attribute_event).

Every attribute value is placed on a road's km range and keeps its source, date and licence:
  - OSM, per way on the road: surface, lanes, maxspeed, width, bridge, tunnel (values as tagged)
  - European routes (OSM route relations network=e-road): eroad = 'E75' on the roads they follow
  - horizontal curves from Szelvény's own (OSM-derived) centrelines: curve (see curves.py)

A way or section is placed by measuring its two ends on the road's calibrated segments (as
szelveny.measure() does, but in memory), kept only if its midpoint lies on the road (within
ON_ROAD_M; dual carriageways are ~10-30 m apart).
Adjacent or overlapping ranges with the same value are merged.

Run after `make lrs`. Re-running replaces the events from these sources.
"""

import sys
from collections import defaultdict

import psycopg
from shapely import wkb
from shapely.geometry import LineString, Point

from szelveny import sources
from szelveny.config import CRS_METRIC, PG_DSN
from szelveny.curves import curve_events

ON_ROAD_M = 30         # an OSM way's midpoint must be this close to the road's centreline
MERGE_GAP_KM = 0.02    # ranges this close together with the same value become one

OSM_TAGS = {           # attribute_event.type -> OSM column / tag expression
    "surface": "w.surface",
    "lanes": "w.lanes",
    "maxspeed": "w.maxspeed",
    "width": "w.tags->>'width'",
    "bridge": "w.bridge",
    "tunnel": "w.tunnel",
}

MERGE_SQL = f"""
    WITH ev AS (
        SELECT road_id, type, value, least(a, b) AS f, greatest(a, b) AS t
        FROM ev_in WHERE value IS NOT NULL AND value <> 'no' AND abs(b - a) > 0.001
    ), o AS (
        SELECT *, max(t) OVER (PARTITION BY road_id, type, value ORDER BY f, t
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_t
        FROM ev
    ), g AS (
        SELECT *, sum(CASE WHEN prev_t IS NULL OR f > prev_t + {MERGE_GAP_KM} THEN 1 ELSE 0 END)
                  OVER (PARTITION BY road_id, type, value ORDER BY f, t) AS grp
        FROM o
    )
    INSERT INTO szelveny.attribute_event
        (road_id, km_from, km_to, type, value, source_id, source_date, licence)
    SELECT road_id, round(min(f), 3), round(max(t), 3), type, value,
           %(source_id)s, %(source_date)s, %(licence)s
    FROM g GROUP BY road_id, type, value, grp
"""


class RoadLine:
    """A road's calibrated segments in the metric CRS, for point -> km placement."""

    def __init__(self):
        self.segs: list[tuple[LineString, float, float]] = []

    def measure(self, pt: Point) -> tuple[float, float]:
        """(km, offset in metres) of the nearest point on the road."""
        line, f, t = min(self.segs, key=lambda s: s[0].distance(pt))
        frac = line.project(pt) / line.length if line.length else 0.0
        return f + (t - f) * frac, line.distance(pt)


def load_roads(conn) -> tuple[dict[int, RoadLine], dict[tuple, int]]:
    roads: dict[int, RoadLine] = defaultdict(RoadLine)
    for road_id, f, t, g in conn.execute(f"""
            SELECT road_id, km_from, km_to, ST_AsBinary(ST_Transform(geom, {CRS_METRIC}))
            FROM szelveny.road_segment ORDER BY road_id, sequence"""):
        roads[road_id].segs.append((wkb.loads(bytes(g)), float(f), float(t)))
    by_key = {(ref, county): rid for rid, ref, county in conn.execute(
        "SELECT road_id, ref, county FROM szelveny.road") if rid in roads}
    return roads, by_key


def place(road: RoadLine, line: LineString, on_road_m: float) -> tuple[float, float] | None:
    """km range a way or section covers on the road, or None if it is not on the road."""
    _, offset = road.measure(line.interpolate(0.5, normalized=True))
    if offset > on_road_m:
        return None
    a, _ = road.measure(Point(line.coords[0]))
    b, _ = road.measure(Point(line.coords[-1]))
    return a, b


def _write_events(conn, rows: list[tuple], manifest: dict, source_id: str) -> None:
    conn.execute("CREATE TEMP TABLE ev_in (road_id int, type text, value text, a numeric, b numeric)")
    with conn.cursor().copy("COPY ev_in FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    conn.execute("DELETE FROM szelveny.attribute_event WHERE source_id = %s", (source_id,))
    conn.execute(MERGE_SQL, {"source_id": source_id, "source_date": manifest["version"],
                             "licence": manifest["licence"]})
    conn.execute("DROP TABLE ev_in")


def osm_events(conn, manifest: dict, roads, by_key) -> None:
    cols = ", ".join(f"{expr} AS {t}" for t, expr in OSM_TAGS.items())
    rows, placed, seen = [], 0, 0
    for ref, counties, g, *values in conn.execute(f"""
            SELECT x.ref,
                   ARRAY(SELECT c.county FROM szelveny.osm_way_county c WHERE c.osm_id = w.osm_id),
                   ST_AsBinary(ST_Transform(w.geom, {CRS_METRIC})), {cols}
            FROM szelveny.osm_way_ref x
            JOIN raw.osm_roads w USING (osm_id)
            WHERE right(w.highway, 5) <> '_link'"""):
        if not any(values):
            continue
        seen += 1
        line = wkb.loads(bytes(g))
        keys = [(ref, None)] + [(ref, c) for c in counties]
        for key in keys:
            rid = by_key.get(key)
            if rid is None or (km := place(roads[rid], line, ON_ROAD_M)) is None:
                continue
            placed += 1
            for t, v in zip(OSM_TAGS, values):
                if v and v.strip():
                    rows.append((rid, t, v.strip(), *km))
    print(f"attributes: osm: {placed:,} way placements from {seen:,} tagged ways")
    _write_events(conn, rows, manifest, "osm")




def eroad_events(conn, manifest: dict, roads, by_key) -> None:
    """European routes (E60, E75, ...): each relation way placed on the Szelvény roads it carries."""
    rows, placed = [], 0
    for eref, ref, counties, g in conn.execute(f"""
            SELECT regexp_replace(upper(r.ref), '\\s', '', 'g'), x.ref,
                   ARRAY(SELECT c.county FROM szelveny.osm_way_county c WHERE c.osm_id = w.osm_id),
                   ST_AsBinary(ST_Transform(w.geom, {CRS_METRIC}))
            FROM raw.osm_road_routes r
            CROSS JOIN LATERAL jsonb_array_elements_text(r.way_ids) AS way_id
            JOIN szelveny.osm_way_ref x ON x.osm_id = way_id::bigint
            JOIN raw.osm_roads w ON w.osm_id = x.osm_id
            WHERE r.network = 'e-road' AND r.ref ~* '^E\\s?[0-9]+$'
              AND x.category IN ('M', 'F')  -- E-routes follow motorways and main roads only
              AND right(w.highway, 5) <> '_link'"""):
        line = wkb.loads(bytes(g))
        for key in [(ref, None)] + [(ref, c) for c in counties]:
            rid = by_key.get(key)
            if rid is not None and (km := place(roads[rid], line, ON_ROAD_M)) is not None:
                rows.append((rid, "eroad", eref, *km))
                placed += 1
    print(f"attributes: eroad: {placed:,} way placements")
    # Same source as the other OSM values: replace only the eroad events.
    conn.execute("CREATE TEMP TABLE ev_in (road_id int, type text, value text, a numeric, b numeric)")
    with conn.cursor().copy("COPY ev_in FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    conn.execute("DELETE FROM szelveny.attribute_event WHERE source_id = 'osm' AND type = 'eroad'")
    conn.execute(MERGE_SQL, {"source_id": "osm", "source_date": manifest["version"],
                             "licence": manifest["licence"]})
    conn.execute("DROP TABLE ev_in")


def main() -> int:
    with psycopg.connect(PG_DSN) as conn:
        roads, by_key = load_roads(conn)
        for source_id, fn in (("osm", osm_events), ("osm", eroad_events), ("osm", curve_events)):
            manifest = sources.read_manifest(source_id)
            if not manifest:
                print(f"attributes: {source_id}: no download, skipped", file=sys.stderr)
                continue
            print(f"attributes: placing {source_id} values on the network…", flush=True)
            fn(conn, manifest, roads, by_key)
            conn.commit()

        rows = conn.execute("""
            WITH road_km AS (
                SELECT r.category, sum(s.km_to - s.km_from) AS km
                FROM szelveny.road_segment s JOIN szelveny.road r USING (road_id) GROUP BY 1
            ), cov AS (
                SELECT r.category, e.type, sum(e.km_to - e.km_from) AS km, count(*) AS n
                FROM szelveny.attribute_event e JOIN szelveny.road r USING (road_id) GROUP BY 1, 2
            )
            SELECT c.category, c.type, c.n, round(100 * least(c.km / k.km, 1))
            FROM cov c JOIN road_km k USING (category)
            ORDER BY c.category, c.type""").fetchall()
    print(f"\n{'cat':<4}{'attribute':<10}{'events':>8}{'% of km':>9}")
    for cat, typ, n, pct in rows:
        print(f"{cat:<4}{typ:<10}{n:>8}{pct:>8}%")
    print("(% of km is capped at 100: both carriageways of a dual road can carry a value)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
