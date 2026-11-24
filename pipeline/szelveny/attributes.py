"""Road attributes along the km (szelveny.attribute_event).

Every attribute value is placed on a road's km range and keeps its source, date and licence:
  - OSM, per way on the road: surface, lanes, maxspeed, width, bridge, tunnel (values as tagged)
  - European routes (OSM route relations network=e-road): eroad = 'E75' on the roads they follow
  - horizontal curves from Szelvény's own (OSM-derived) centrelines: curve (see curves.py)
  - NAP packages on NAP roads (nap_events): lanes, maxspeed, surface, width, aadt, built_up,
    oneway, truck_restriction, bridge. These carry the road number, carriageway and szelvény
    themselves, so they need no placement: one event per NAP record. Values as published.
  - general speed limits (default_speed_events, source 'kresz'): NAP lists only posted limits;
    elsewhere KRESZ §26 applies, derived from NAP's built-up sections and category.

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
            SELECT s.road_id, s.km_from, s.km_to, ST_AsBinary(ST_Transform(s.geom, {CRS_METRIC}))
            FROM szelveny.road_segment s JOIN szelveny.road r USING (road_id)
            WHERE r.category <> 'local'   -- local roads have no km to place values on
            ORDER BY s.road_id, s.sequence"""):
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


KM = "(split_part({c}, '+', 1)::numeric + trim(split_part({c}, '+', 2))::numeric / 1000)"
# attribute_event.type -> (NAP table, value expression). Speed limits: the posted value, with
# its direction when it applies to one side only and its supplementary plate.
NAP_EVENTS = {
    "lanes": ("raw.nap_savok", "savossz::text"),
    "maxspeed": ("raw.nap_sebesseg",
                 ("sebkorl::text || CASE WHEN sebkir IS NOT NULL AND sebkir <> 'teljes keresztmetszetre'"
                 " THEN ' (' || sebkir || ')' ELSE '' END"
                 " || CASE WHEN nullif(trim(sebkieg), '') IS NOT NULL THEN ' · ' || sebkieg ELSE '' END")),
    "surface": ("raw.nap_burkolat", "nullif(trim(rbtip), '')"),
    "width": ("raw.nap_burkolat", "rbtsz::text"),
    "aadt": ("raw.nap_forgalom", "oj::text"),          # all vehicles per day (anf is in PCU)
    "built_up": ("raw.nap_lakott_terulet", "szakjel"),
    "oneway": ("raw.nap_egyiranyu", "irany"),
    "truck_restriction": ("raw.nap_teher_korlatozas",
                          ("concat_ws(' ', sulyk, CASE WHEN sulykorl IS NOT NULL THEN sulykorl::text || ' t' END)"
                          " || CASE WHEN sulyk2 IS NOT NULL THEN '; ' || concat_ws(' ', sulyk2,"
                          " CASE WHEN sulykorl2 IS NOT NULL THEN sulykorl2::text || ' t' END) ELSE '' END")),
    "bridge": ("raw.nap_hidak", "coalesce(nullif(trim(hnev), ''), hnem, 'híd')"),
}


def nap_events(conn, manifest: dict, roads=None, by_key=None) -> None:
    """One event per NAP record, on the NAP road with that number (no placement needed)."""
    dates = {p["table"]: p["download"]["retrieved_at"][:10]
             for p in manifest["packages"] if p.get("download")}
    types = list(NAP_EVENTS)
    conn.execute("DELETE FROM szelveny.attribute_event WHERE source_id = 'nap' AND type = ANY(%s)",
                 (types,))
    for typ, (table, value) in NAP_EVENTS.items():
        if table not in dates:
            print(f"attributes: nap: {table} not downloaded, {typ} skipped", file=sys.stderr)
            continue
        f, t = KM.format(c="x.kszelv"), KM.format(c="x.vszelv")
        n = conn.execute(f"""
            INSERT INTO szelveny.attribute_event (road_id, km_from, km_to, type, value, carriageway,
                                                  source_id, source_date, licence)
            SELECT r.road_id, round(least({f}, {t}), 3), round(greatest({f}, {t}), 3), %(type)s, v,
                   x.pkod::smallint, 'nap', %(date)s, %(licence)s
            FROM {table} x
            JOIN szelveny.road r ON r.ref = x.kszam AND r.source_id = 'nap'
            CROSS JOIN LATERAL (SELECT {value} AS v) val
            WHERE v IS NOT NULL AND x.kszelv LIKE '%%+%%' AND x.vszelv LIKE '%%+%%'
        """, {"type": typ, "date": dates[table], "licence": manifest["licence"]}).rowcount
        print(f"attributes: nap: {typ}: {n:,} events")


# KRESZ (1/1975. KPM-BM) §26 general limits for cars where no sign says otherwise, by NAP kutka
# and built-up section. Ramps (8, 9) and rest-area roads (P) get none: their limit is signed.
KRESZ_SOURCE = {
    "source_id": "kresz",
    "name": "Általános sebességhatárok (KRESZ 26. §) a NAP útkategóriái és lakott területi szakaszai alapján",
    "url": None,
    "licence": "derived by Szelvény: KRESZ (1/1975. KPM-BM együttes rendelet, public law) "
               "applied to NAP data (ÁSZF 2024-03-01 §32)",
    "attribution": "Szelvény, a NAP adataiból (Forrás: Magyar Közút Nonprofit Zrt. – NAP)",
}
DEFAULT_SPEED_SQL = """
    CASE x.kutka WHEN '1' THEN 130 WHEN '2' THEN 110
         ELSE CASE x.szakjel WHEN 'átkelési' THEN 50 WHEN 'külsőségi' THEN 90 END END"""


def default_speed_events(conn, manifest: dict, roads=None, by_key=None) -> None:
    """The general limit on every NAP km without a posted limit (on the same carriageway)."""
    pkg = next((p for p in manifest["packages"] if p["table"] == "raw.nap_lakott_terulet"), None)
    if not pkg or not pkg.get("download"):
        print("attributes: kresz: no built-up sections from NAP, skipped", file=sys.stderr)
        return
    date = pkg["download"]["retrieved_at"][:10]
    conn.execute("""
        INSERT INTO szelveny.source (source_id, name, url, licence, attribution, retrieved_at, notes)
        VALUES (%(source_id)s, %(name)s, %(url)s, %(licence)s, %(attribution)s, %(date)s::timestamptz,
                %(notes)s)
        ON CONFLICT (source_id) DO UPDATE SET name = EXCLUDED.name, licence = EXCLUDED.licence,
            attribution = EXCLUDED.attribution, retrieved_at = EXCLUDED.retrieved_at, notes = EXCLUDED.notes
    """, {**KRESZ_SOURCE, "date": date, "notes": f"version {date}"})
    conn.execute("DELETE FROM szelveny.attribute_event WHERE source_id = 'kresz'")
    f, t = KM.format(c="x.kszelv"), KM.format(c="x.vszelv")
    n = conn.execute(f"""
        WITH base AS (
            SELECT r.road_id, x.pkod::smallint AS cw, {DEFAULT_SPEED_SQL} AS v,
                   numrange(least({f}, {t}), greatest({f}, {t})) AS rng
            FROM raw.nap_lakott_terulet x
            JOIN szelveny.road r ON r.ref = x.kszam AND r.source_id = 'nap'
            WHERE x.kutka NOT IN ('8', '9', 'P') AND x.kszelv LIKE '%%+%%' AND x.vszelv LIKE '%%+%%'
        ), posted AS (
            SELECT road_id, carriageway AS cw, range_agg(numrange(km_from, km_to)) AS mr
            FROM szelveny.attribute_event WHERE source_id = 'nap' AND type = 'maxspeed'
            GROUP BY 1, 2
        ), free AS (
            SELECT b.road_id, b.cw, b.v,
                   unnest(nummultirange(b.rng) - coalesce(p.mr, '{{}}'::nummultirange)) AS rng
            FROM base b LEFT JOIN posted p ON p.road_id = b.road_id AND p.cw = b.cw
            WHERE b.v IS NOT NULL
        )
        INSERT INTO szelveny.attribute_event (road_id, km_from, km_to, type, value, carriageway,
                                              source_id, source_date, licence)
        SELECT road_id, round(lower(rng), 3), round(upper(rng), 3), 'maxspeed', v::text, cw,
               'kresz', %(date)s, %(licence)s
        FROM free WHERE upper(rng) - lower(rng) >= 0.001
    """, {"date": date, "licence": KRESZ_SOURCE["licence"]}).rowcount
    print(f"attributes: kresz: {n:,} general speed-limit events")


def main() -> int:
    with psycopg.connect(PG_DSN) as conn:
        roads, by_key = load_roads(conn)
        for source_id, fn in (("osm", osm_events), ("osm", eroad_events), ("osm", curve_events),
                              ("nap", nap_events), ("nap", default_speed_events)):
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
