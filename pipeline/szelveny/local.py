"""Local and municipal roads from OpenStreetMap: roads without a national number.

Usage: python -m szelveny.local    (after `make reference lrs`)

NAP holds only the national network, so streets and other local roads come from OSM. They have
no szelvény and are identified by name + settlement (CLAUDE.md): one szelveny.road per
(name, settlement), category 'local', with its OSM ways merged into segments of km_quality
'none'. On 'none' segments km_from/km_to are the mapped length counted from the road's first
piece, not a chainage; the site doesn't show them as km.

Left out: unnamed ways, ways carrying a national road number (lrs.py), and named ways that run
on a NAP road whose ref is missing in OSM (main streets of villages often are national roads):
a way whose midpoint and both quarter points lie within ON_NAP_M of a NAP section.
Settlement and county: the GISCO LAU unit (KSH settlement) containing the way's midpoint.

Re-running replaces the local roads.
"""

import sys

import psycopg

from szelveny import sources
from szelveny.config import CRS_METRIC, PG_DSN

HIGHWAYS = ("residential", "living_street", "unclassified", "tertiary", "secondary", "primary", "trunk")
ON_NAP_M = 15


def build(conn) -> None:
    conn.execute(f"""
        CREATE TEMP TABLE nap AS
        SELECT ST_Transform(geom, {CRS_METRIC}) AS geom FROM szelveny.road_segment
        WHERE source_id = 'nap';
        CREATE INDEX ON nap USING gist (geom);

        CREATE TEMP TABLE w AS
        SELECT r.osm_id, trim(r.name) AS name, r.geom, ST_Transform(r.geom, {CRS_METRIC}) AS g
        FROM raw.osm_roads r
        WHERE r.name IS NOT NULL AND trim(r.name) <> '' AND r.highway IN {HIGHWAYS}
          AND NOT EXISTS (SELECT 1 FROM szelveny.osm_way_ref x WHERE x.osm_id = r.osm_id);
        CREATE INDEX ON w USING gist (g);

        DELETE FROM w USING (
            SELECT w.osm_id FROM w
            WHERE (SELECT bool_and(EXISTS (SELECT 1 FROM nap
                                           WHERE ST_DWithin(nap.geom, ST_LineInterpolatePoint(w.g, f), {ON_NAP_M})))
                   FROM unnest(ARRAY[0.25, 0.5, 0.75]) AS f)
        ) on_nap WHERE w.osm_id = on_nap.osm_id;

        CREATE TEMP TABLE lw AS
        SELECT w.*, l.lau_name AS settlement, l.county
        FROM w JOIN raw.gisco_lau l ON ST_Intersects(l.geom, ST_LineInterpolatePoint(w.geom, 0.5));

        DELETE FROM szelveny.road WHERE category = 'local' AND source_id = 'osm';

        INSERT INTO szelveny.road (ref, category, county, settlement, source_id)
        SELECT DISTINCT name, 'local', county, settlement, 'osm' FROM lw;

        -- Each road's ways merged into continuous pieces, longest first; km = mapped length
        -- running on from piece to piece (not a szelvény).
        CREATE TEMP TABLE merged AS
        SELECT r.road_id, ST_LineMerge(ST_Collect(lw.geom)) AS geom
        FROM szelveny.road r JOIN lw ON lw.name = r.ref AND lw.settlement = r.settlement
        WHERE r.category = 'local' AND r.source_id = 'osm'
        GROUP BY r.road_id;
        CREATE TEMP TABLE piece AS
        SELECT m.road_id, d.geom, ST_Length(ST_Transform(d.geom, {CRS_METRIC})) / 1000 AS km
        FROM merged m CROSS JOIN LATERAL ST_Dump(m.geom) d;
    """)
    conn.execute("""
        INSERT INTO szelveny.road_segment (road_id, sequence, km_from, km_to, geom, km_quality, source_id)
        SELECT road_id, seq, round(start::numeric, 3), round((start + km)::numeric, 3), geom, 'none', 'osm'
        FROM (SELECT road_id, geom, km,
                     row_number() OVER w AS seq,
                     coalesce(sum(km) OVER (w ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS start
              FROM piece WHERE ST_GeometryType(geom) = 'ST_LineString'
              WINDOW w AS (PARTITION BY road_id ORDER BY km DESC, ST_XMin(geom))) x;
        UPDATE szelveny.road r SET length_official = NULL, route_desc = NULL
        WHERE r.category = 'local' AND r.source_id = 'osm';
    """)


def main() -> int:
    manifest = sources.read_manifest("osm")
    if not manifest:
        print("local: no OSM download", file=sys.stderr)
        return 1
    with psycopg.connect(PG_DSN) as conn:
        build(conn)
        conn.commit()
        n, segs, km, settlements = conn.execute(f"""
            SELECT count(DISTINCT r.road_id), count(*),
                   round((sum(ST_Length(ST_Transform(s.geom, {CRS_METRIC}))) / 1000)::numeric),
                   count(DISTINCT r.settlement)
            FROM szelveny.road r JOIN szelveny.road_segment s USING (road_id)
            WHERE r.category = 'local'""").fetchone()
    print(f"local: {n:,} roads in {settlements:,} settlements, {segs:,} pieces, {km:,} km")
    return 0


if __name__ == "__main__":
    sys.exit(main())
