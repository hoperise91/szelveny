"""Point objects on the network (szelveny.point_object).

OSM nodes already imported into raw.osm_road_points, placed on the nearest Szelvény road and its km:
  - level_crossing  railway=level_crossing (road × railway)
  - junction        highway=motorway_junction (exit number `ref`, name)
  - border          barrier=border_control (border crossing points)
  - toll            barrier=toll_booth
  - ferry           both ends of an OSM route=ferry way (raw.osm_ferries) within FERRY_NEAR_M of a
                    road: a terminal on that road, named after the crossing

A node is kept when a road's centreline passes within ON_ROAD_M; when several roads do (a level
crossing at a junction), the most important category wins. Every object keeps source, date and
licence. Run after `make lrs`; re-running replaces the OSM objects.

NAP packages give point objects on NAP roads with their own road number and szelvény (nap_objects):
  - km_post    Szelvényjelek (the km stones' surveyed positions)
  - bridge     Hídhasználati feltételek, at the bridge's middle, with its load limits
  - rest_area  Pihenőparkolók, at the middle, with side and parking places
"""

import json
import sys

import psycopg
from shapely import wkb

from szelveny import sources
from szelveny.attributes import load_roads
from szelveny.config import CRS_METRIC, PG_DSN

ON_ROAD_M = 30
FERRY_NEAR_M = 300  # a ferry's landing to the road it serves (ramps and quays are mapped apart)
# Only crossings that carry vehicles belong to the road network: tagged for motor vehicles, or
# named as a car ferry ("Szob–Nagymaros komp", "… ferry"); passenger boats (BKV hajó, tourist
# boats on Balaton) are tagged motor_vehicle/motorcar=no and stay out.
CAR_FERRY_SQL = """
    coalesce(f.tags->>'motor_vehicle', '') <> 'no' AND coalesce(f.tags->>'motorcar', '') <> 'no'
    AND ('yes' IN (f.tags->>'motor_vehicle', f.tags->>'motorcar', f.tags->>'hgv')
         OR f.name ~* '(^|\\s)(komp|kompátkelő|rév|ferry)(\\s|$)')"""
KINDS = {  # raw.osm_road_points.kind -> point_object.type
    "level_crossing": "level_crossing",
    "motorway_junction": "junction",
    "border_control": "border",
    "toll_booth": "toll",
}
RANK = {c: i for i, c in enumerate(["M", "F", "OK", "BK", "local"])}


def main() -> int:
    manifest = sources.read_manifest("osm")
    if not manifest:
        print("objects: no OSM download", file=sys.stderr)
        return 1
    with psycopg.connect(PG_DSN) as conn:
        roads, _ = load_roads(conn)
        category = dict(conn.execute("SELECT road_id, category FROM szelveny.road").fetchall())
        rows, skipped = [], 0
        for osm_id, kind, ref, tags, g, cands in conn.execute(f"""
                SELECT p.osm_id, p.kind, p.ref, p.tags, ST_AsBinary(ST_Transform(p.geom, {CRS_METRIC})),
                       ARRAY(SELECT DISTINCT s.road_id FROM szelveny.road_segment s
                             -- index prefilter (0.0006° ≥ 44 m in Hungary), then the exact metric distance
                             WHERE s.geom && ST_Expand(p.geom, 0.0006)
                               AND ST_DWithin(ST_Transform(s.geom, {CRS_METRIC}),
                                              ST_Transform(p.geom, {CRS_METRIC}), {ON_ROAD_M}))
                FROM raw.osm_road_points p WHERE p.kind = ANY(%s)""", (list(KINDS),)):
            cands = [c for c in cands if c in roads]  # local roads have no km to place on
            if not cands:
                skipped += 1
                continue
            pt = wkb.loads(bytes(g))
            best = min(cands, key=lambda rid: (RANK.get(category[rid], 9),
                                               roads[rid].measure(pt)[1]))
            km, _ = roads[best].measure(pt)
            props = {k: v for k, v in {
                "name": tags.get("name"), "ref": ref or tags.get("ref"), "osm_id": osm_id,
                "barrier": tags.get("crossing:barrier"), "operator": tags.get("operator"),
            }.items() if v}
            rows.append((best, round(km, 3), KINDS[kind], json.dumps(props, ensure_ascii=False),
                         osm_id))
        # Ferry terminals: each end of a ferry route, on the nearest numbered road.
        has_ferries = conn.execute("SELECT to_regclass('raw.osm_ferries') IS NOT NULL").fetchone()[0]
        for osm_id, name, operator, end, g, cands in (conn.execute(f"""
                SELECT f.osm_id, f.name, f.tags->>'operator', e.n,
                       ST_AsBinary(ST_Transform(e.p, {CRS_METRIC})),
                       ARRAY(SELECT DISTINCT s.road_id FROM szelveny.road_segment s
                             WHERE s.geom && ST_Expand(e.p, 0.005)
                               AND ST_DWithin(ST_Transform(s.geom, {CRS_METRIC}),
                                              ST_Transform(e.p, {CRS_METRIC}), {FERRY_NEAR_M}))
                FROM raw.osm_ferries f
                CROSS JOIN LATERAL (VALUES (0, ST_StartPoint(f.geom)), (1, ST_EndPoint(f.geom))) e(n, p)
                WHERE {CAR_FERRY_SQL}
                """) if has_ferries else []):
            cands = [c for c in cands if c in roads]  # local roads have no km to place on
            if not cands:
                continue
            pt = wkb.loads(bytes(g))
            best = min(cands, key=lambda rid: (RANK.get(category[rid], 9), roads[rid].measure(pt)[1]))
            km, _ = roads[best].measure(pt)
            props = {k: v for k, v in {"name": name, "operator": operator, "osm_id": osm_id}.items() if v}
            rows.append((best, round(km, 3), "ferry", json.dumps(props, ensure_ascii=False),
                         (osm_id, end)))
        conn.execute("DELETE FROM szelveny.point_object WHERE source_id = 'osm'")
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO szelveny.point_object (road_id, km, type, props, geom, source_id, source_date, licence)
                SELECT %s, %s, %s, %s::jsonb, p.geom, 'osm', %s, %s
                FROM raw.osm_road_points p WHERE p.osm_id = %s""",
                [(rid, km, typ, props, manifest["version"], manifest["licence"], ref)
                 for rid, km, typ, props, ref in rows if typ != "ferry"])
            # Ferry terminals sit at the route's end, not at an OSM node.
            cur.executemany("""
                INSERT INTO szelveny.point_object (road_id, km, type, props, geom, source_id, source_date, licence)
                SELECT %s, %s, 'ferry', %s::jsonb,
                       CASE WHEN %s = 0 THEN ST_StartPoint(f.geom) ELSE ST_EndPoint(f.geom) END,
                       'osm', %s, %s
                FROM raw.osm_ferries f WHERE f.osm_id = %s""",
                [(rid, km, props, ref[1], manifest["version"], manifest["licence"], ref[0])
                 for rid, km, typ, props, ref in rows if typ == "ferry"])
        conn.commit()
        summary = conn.execute("""
            SELECT type, count(*), count(DISTINCT road_id) FROM szelveny.point_object
            WHERE source_id = 'osm' GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    for typ, n, nroads in summary:
        print(f"objects: {typ:<15}{n:>7,} on {nroads:,} roads")
    print(f"objects: {skipped:,} nodes not within {ON_ROAD_M} m of a Szelvény road (left out)")
    nap_objects()
    return 0


KM = "(split_part({c}, '+', 1)::numeric + trim(split_part({c}, '+', 2))::numeric / 1000)"
# Middle of a section; a few NAP bridges are multi-part or points: the point nearest the centre.
MID = ("CASE WHEN ST_GeometryType(ST_LineMerge(x.geom)) = 'ST_LineString'"
       " THEN ST_LineInterpolatePoint(ST_LineMerge(x.geom), 0.5)"
       " ELSE ST_ClosestPoint(x.geom, ST_Centroid(x.geom)) END")
# point_object.type -> (NAP table, km expression, point geometry, props)
NAP_OBJECTS = {
    "km_post": ("raw.nap_szelvenyjelek", "x.kmjel::numeric", "ST_GeometryN(x.geom, 1)",
                "jsonb_build_object('ref', x.kmjel::text, 'carriageway', x.pkod)"),
    "bridge": ("raw.nap_hidak", f"({KM.format(c='x.kszelv')} + {KM.format(c='x.vszelv')}) / 2", MID,
               ("jsonb_strip_nulls(jsonb_build_object('name', nullif(trim(x.hnev), ''), 'ref', x.hszam::text,"
               " 'kind', x.hnem, 'position', x.hhely, 'capacity', x.huzemtb,"
               " 'limits', (SELECT jsonb_agg(l) FROM (VALUES (x.hkorlt1, x.hkorle1), (x.hkorlt2, x.hkorle2),"
               " (x.hkorlt3, x.hkorle3), (x.hkorlt4, x.hkorle4), (x.hkorlt5, x.hkorle5), (x.hkorlt6, x.hkorle6))"
               " AS v(t, e), LATERAL (SELECT concat_ws(': ', v.t, v.e::text) AS l) z"
               " WHERE v.t IS NOT NULL AND v.t <> 'Nincs korlátozás a hídon')))")),
    "rest_area": ("raw.nap_pihenoparkolok", f"({KM.format(c='x.kszelv')} + {KM.format(c='x.vszelv')}) / 2", MID,
                  ("jsonb_strip_nulls(jsonb_build_object('name', x.ppnev, 'side', x.ppold,"
                  " 'bus', x.ppa, 'truck', x.ppk, 'car', x.ppszgk))")),
}


def nap_objects() -> None:
    manifest = sources.read_manifest("nap")
    if not manifest:
        print("objects: no NAP manifest, NAP objects skipped", file=sys.stderr)
        return
    dates = {p["table"]: p["download"]["retrieved_at"][:10]
             for p in manifest["packages"] if p.get("download")}
    with psycopg.connect(PG_DSN) as conn:
        conn.execute("DELETE FROM szelveny.point_object WHERE source_id = 'nap'")
        for typ, (table, km, geom, props) in NAP_OBJECTS.items():
            if table not in dates:
                continue
            n = conn.execute(f"""
                INSERT INTO szelveny.point_object (road_id, km, type, props, geom, source_id, source_date, licence)
                SELECT r.road_id, round({km}, 3), %(type)s, {props}, {geom}, 'nap', %(date)s, %(licence)s
                FROM {table} x JOIN szelveny.road r ON r.ref = x.kszam AND r.source_id = 'nap'
                WHERE x.geom IS NOT NULL
            """, {"type": typ, "date": dates[table], "licence": manifest["licence"]}).rowcount
            print(f"objects: nap {typ:<11}{n:>7,}")
        conn.commit()


if __name__ == "__main__":
    sys.exit(main())
