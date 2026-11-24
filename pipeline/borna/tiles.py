"""Phase 3: export the network for the static website (web/public/data/).

  borna.pmtiles        vector tiles: roads (by category, km quality) and attribute layers
                       (surface class, maxspeed, aadt, width in m, lanes) for the map
  roads/<key>.json     one file per road: official data, calibrated segments for the km lookup,
                       attributes along the km, per-county km for national roads.
                       key: 'DN1' for national roads, 'CV-DJ103' for county roads.
  index.json           every road, for search and county browsing:
                       [key, ref, county, category, route, bbox, status, counties passed through]
  sources.json         every source with licence, date and attribution
  downloads/           the HG 540 register (public domain) and the QA tables

Run after `make lrs attributes qa`. The files are generated, not committed.
"""

import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg

from borna.config import DATA_OUT, PG_DSN, ROOT
from borna.romania import county_code

WEB_DATA = ROOT / "web" / "public" / "data"
MIN_ZOOM = {"A": 5, "DX": 5, "DN": 5, "V": 7, "DJ": 7, "DC": 9, "DP": 10}
OBJECT_MIN_ZOOM = {"border": 6, "ferry": 6, "junction": 8, "toll": 8, "level_crossing": 11}
COORD_DECIMALS = 6          # ~0.1 m
SIMPLIFY_M = 2              # per-road JSON geometry, in Stereo 70

SURFACE_CLASS_SQL = """
    CASE WHEN value IN ('asphalt', 'concrete', 'concrete:plates', 'concrete:lanes', 'paved',
                        'paving_stones', 'sett', 'cobblestone', 'unhewn_cobblestone', 'chipseal')
              THEN 'paved'
         WHEN value IN ('gravel', 'fine_gravel', 'compacted', 'pebblestone', 'rock') THEN 'gravel'
         WHEN value IN ('dirt', 'ground', 'earth', 'grass', 'mud', 'sand', 'soil') THEN 'earth'
         WHEN value = 'unpaved' THEN 'unpaved' ELSE 'other' END"""

# OSM width as tagged ('7', '7 m', '6,5') -> metres; implausible road widths dropped.
WIDTH_M_SQL = """
    CASE WHEN replace(substring(value from '^[0-9]+(?:[.,][0-9]+)?'), ',', '.')::numeric
              BETWEEN 2.5 AND 40
         THEN replace(substring(value from '^[0-9]+(?:[.,][0-9]+)?'), ',', '.')::numeric END"""

ROAD_KEY_SQL = "CASE WHEN r.county IS NULL THEN r.ref ELSE r.county || '-' || r.ref END"

# Geometry of each attribute event: the part of each overlapping segment between its km.
EVENT_GEOM_SQL = """
    SELECT e.road_id, e.type, e.value, e.km_from, e.km_to, r.category,
           ST_LineSubstring(s.geom,
               ((greatest(e.km_from, s.km_from) - s.km_from) / (s.km_to - s.km_from))::float8,
               ((least(e.km_to, s.km_to) - s.km_from) / (s.km_to - s.km_from))::float8) AS geom
    FROM borna.attribute_event e
    JOIN borna.road r USING (road_id)
    JOIN borna.road_segment s ON s.road_id = e.road_id
         AND s.km_to > e.km_from AND s.km_from < e.km_to AND s.km_to > s.km_from
    WHERE e.type = %s"""


def _geojsonseq(conn, path: Path, sql: str, params=()) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for (feature,) in conn.execute(sql, params):
            f.write(json.dumps(feature, separators=(",", ":"), ensure_ascii=False) + "\n")
            n += 1
    return n


def export_tiles(conn, tmp: Path) -> None:
    minzoom = "CASE r.category " + " ".join(
        f"WHEN '{c}' THEN {z}" for c, z in MIN_ZOOM.items()) + " ELSE 10 END"
    layers = {}
    layers["roads"] = _geojsonseq(conn, tmp / "roads.geojsonl", f"""
        SELECT json_build_object(
            'type', 'Feature',
            'tippecanoe', json_build_object('minzoom', {minzoom}),
            'properties', json_build_object('k', {ROAD_KEY_SQL}, 'r', r.ref, 't', r.category,
                                            'q', s.km_quality, 'f', s.km_from, 'e', s.km_to),
            'geometry', ST_AsGeoJSON(s.geom, {COORD_DECIMALS})::json)
        FROM borna.road_segment s JOIN borna.road r USING (road_id)""")
    for layer, typ, value in (("surface", "surface", SURFACE_CLASS_SQL),
                              ("maxspeed", "maxspeed", "value"),
                              ("aadt", "aadt", "value::int"),
                              ("width", "width", WIDTH_M_SQL),
                              ("lanes", "lanes", "nullif(substring(value from '^[0-9]+'), '')::int")):
        layers[layer] = _geojsonseq(conn, tmp / f"{layer}.geojsonl", f"""
            SELECT json_build_object(
                'type', 'Feature',
                'tippecanoe', json_build_object('minzoom', {minzoom.replace('r.category', 'x.category')}),
                'properties', json_build_object('v', {value}, 't', x.category),
                'geometry', ST_AsGeoJSON(x.geom, {COORD_DECIMALS})::json)
            FROM ({EVENT_GEOM_SQL}) x
            WHERE NOT ST_IsEmpty(x.geom) AND {value} IS NOT NULL""", (typ,))
    # Point objects (level crossings, interchanges, borders, tolls) with their road and km.
    layers["objects"] = _geojsonseq(conn, tmp / "objects.geojsonl", f"""
        SELECT json_build_object(
            'type', 'Feature',
            'tippecanoe', json_build_object('minzoom', CASE p.type {" ".join(
                f"WHEN '{k}' THEN {z}" for k, z in OBJECT_MIN_ZOOM.items())} ELSE 11 END),
            'properties', json_strip_nulls(json_build_object(
                'y', p.type, 'n', p.props->>'name', 'x', p.props->>'ref',
                'k', {ROAD_KEY_SQL}, 'r', r.ref, 'm', p.km)),
            'geometry', ST_AsGeoJSON(p.geom, {COORD_DECIMALS})::json)
        FROM borna.point_object p JOIN borna.road r USING (road_id)""")
    # Ferry crossings, drawn as dashed lines when ferries are ticked.
    if conn.execute("SELECT to_regclass('raw.osm_ferries') IS NOT NULL").fetchone()[0]:
        layers["ferries"] = _geojsonseq(conn, tmp / "ferries.geojsonl", f"""
            SELECT json_build_object(
                'type', 'Feature', 'tippecanoe', json_build_object('minzoom', 6),
                'properties', json_strip_nulls(json_build_object('n', f.name)),
                'geometry', ST_AsGeoJSON(f.geom, {COORD_DECIMALS})::json)
            FROM raw.osm_ferries f
            WHERE EXISTS (SELECT 1 FROM borna.point_object p
                          WHERE p.type = 'ferry' AND (p.props->>'osm_id')::bigint = f.osm_id)""")
    # Motorways, expressways and national roads under construction (not part of the network).
    layers["construction"] = _geojsonseq(conn, tmp / "construction.geojsonl", f"""
        SELECT json_build_object(
            'type', 'Feature',
            'tippecanoe', json_build_object('minzoom', {minzoom.replace('r.category', 'x.category')}),
            'properties', json_build_object('r', x.ref, 't', x.category,
                                            'o', w.tags->>'opening_date'),
            'geometry', ST_AsGeoJSON(w.geom, {COORD_DECIMALS})::json)
        FROM borna.osm_way_ref x JOIN raw.osm_roads w USING (osm_id)
        WHERE w.highway = 'construction' AND x.category IN ('A', 'DX', 'DN', 'V')
          AND coalesce(w.tags->>'construction', '') NOT IN ('service', 'track', 'path', 'footway')""")
    print("tiles: features " + ", ".join(f"{k} {v:,}" for k, v in layers.items()), flush=True)
    out = WEB_DATA / "borna.pmtiles"
    subprocess.run(
        ["tippecanoe", "-o", str(out), "--force", "-Z5", "-z13", "--quiet",
         "--drop-densest-as-needed", "--extend-zooms-if-still-dropping",
         "--attribution", "© OpenStreetMap contributors (ODbL) · HG 540/2000 · Bornă",
         *[a for k in layers for a in ("-L", f"{k}:{tmp / (k + '.geojsonl')}")]],
        check=True,
    )
    print(f"tiles: {out.name} {out.stat().st_size / 1e6:.1f} MB")


def export_roads(conn) -> int:
    roads_dir = WEB_DATA / "roads"
    if roads_dir.exists():
        shutil.rmtree(roads_dir)
    roads_dir.mkdir(parents=True)

    segments: dict[int, list] = {}
    for road_id, f, t, q, coords in conn.execute(f"""
            SELECT road_id, km_from, km_to, km_quality,
                   ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(
                       ST_Transform(geom, 3844), {SIMPLIFY_M}), 4326), {COORD_DECIMALS})::json
                       -> 'coordinates'
            FROM borna.road_segment ORDER BY road_id, sequence"""):
        segments.setdefault(road_id, []).append([float(f), float(t), q, coords])

    attrs: dict[int, dict] = {}
    for road_id, typ, f, t, value, source in conn.execute("""
            SELECT road_id, type, km_from, km_to, value, source_id
            FROM borna.attribute_event ORDER BY road_id, type, km_from"""):
        attrs.setdefault(road_id, {}).setdefault(typ, []).append(
            [float(f), float(t), value, source])

    objects: dict[int, list] = {}
    for road_id, km, typ, name, ref in conn.execute("""
            SELECT road_id, km, type, props->>'name', props->>'ref'
            FROM borna.point_object ORDER BY road_id, km"""):
        objects.setdefault(road_id, []).append([float(km), typ, name, ref])

    owners = {(ref, county): owner for ref, county, owner in conn.execute("""
        SELECT ref, county, min(owner) FROM borna.register_entry
        WHERE owner IS NOT NULL GROUP BY 1, 2""")}

    counties: dict[str, list] = {}
    for ref, county, f, t, length in conn.execute("""
            SELECT ref, county, km_from, km_to, length_km FROM borna.register_entry
            WHERE list = 'county_dn' ORDER BY ref, km_from NULLS LAST"""):
        counties.setdefault(ref, []).append(
            [county, *(None if v is None else float(v) for v in (f, t, length))])

    geo_counties: dict[int, list[str]] = {}
    for road_id, names in conn.execute("""
            SELECT s.road_id, array_agg(DISTINCT c.name)
            FROM borna.road_segment s JOIN borna.road r USING (road_id)
            JOIN raw.osm_counties c ON ST_Intersects(s.geom, c.geom)
            WHERE r.source_id = 'osm' GROUP BY 1"""):
        geo_counties[road_id] = sorted(county_code(n) for n in names)

    # County roads HG 540 still lists although the road on the ground is a national road of about
    # the same length (DJ113 Covasna = DN11C since its transfer to the state): note it on the page.
    official_len = {ref: float(n) for ref, n in conn.execute(
        "SELECT ref, length_official FROM borna.road WHERE county IS NULL AND length_official > 0")}
    now_national: dict[tuple, str] = {}
    claims = DATA_OUT / "lrs" / "route_claims_dropped.csv"
    if claims.exists():
        county_len = {(ref, c): float(n) for ref, c, n in conn.execute(
            "SELECT ref, county, length_official FROM borna.road "
            "WHERE category IN ('DJ', 'DC') AND length_official > 0")}
        with open(claims, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                own, nat = county_len.get((row["ref"], row["county"])), \
                    official_len.get(row["national"])
                if own and nat and float(row["km"]) >= 0.5 * own and abs(nat - own) <= 0.25 * own:
                    now_national[(row["ref"], row["county"])] = row["national"]

    index = []
    for (road_id, key, ref, county, category, route, km_from, km_to, length, legal,
         status, path_km, orientation, n_ctrl, n_ms, controls_km, bbox) in conn.execute(f"""
            SELECT r.road_id, {ROAD_KEY_SQL}, r.ref, r.county, r.category, r.route_desc,
                   r.km_from_official, r.km_to_official, r.length_official, r.legal_source,
                   l.status, l.path_km, l.orientation, l.n_border, l.n_milestone, l.controls_km,
                   (SELECT ARRAY[round(ST_XMin(b)::numeric, 4), round(ST_YMin(b)::numeric, 4),
                                 round(ST_XMax(b)::numeric, 4), round(ST_YMax(b)::numeric, 4)]
                    FROM (SELECT ST_Extent(geom)::geometry AS b FROM borna.road_segment s
                          WHERE s.road_id = r.road_id) e WHERE b IS NOT NULL)
            FROM borna.road r LEFT JOIN borna.lrs_road l USING (road_id)"""):
        num = (lambda v: None if v is None else float(v))
        doc = {
            "key": key, "ref": ref, "county": county, "category": category, "route": route,
            "km_from": num(km_from), "km_to": num(km_to), "length_official": num(length),
            "legal_source": legal,
            "owner": owners.get((ref, county)),
            "lrs": {"status": status, "path_km": num(path_km), "orientation": orientation,
                    "control_points": (n_ctrl or 0) + (n_ms or 0),
                    "controls": [float(k) for k in controls_km or []]},
            "bbox": [float(v) for v in bbox] if bbox else None,
            "segments": segments.get(road_id, []),
            "attributes": attrs.get(road_id, {}),
            "objects": objects.get(road_id, []),  # [km, type, name, ref]
        }
        if county is None and ref in counties:
            doc["counties"] = counties[ref]
        if (ref, county) in now_national:
            doc["now_national"] = now_national[(ref, county)]
        short = route.split(" | ")[0] if route else ""
        through = (sorted({c[0] for c in counties.get(ref, [])}) or geo_counties.get(road_id, [])) \
            if county is None else []
        if county is None and ref not in counties and road_id in geo_counties:
            doc["through"] = geo_counties[road_id]
        index.append([key, ref, county, category, short[:140],
                      doc["bbox"], status or "no_geometry", through])
        (roads_dir / f"{key}.json").write_text(
            json.dumps(doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    index.sort(key=lambda r: (["A", "DX", "DN", "V", "DJ", "DC", "DP"].index(r[3])
                              if r[3] in ("A", "DX", "DN", "V", "DJ", "DC", "DP") else 9,
                              r[2] or "", len(r[1]), r[1]))
    (WEB_DATA / "index.json").write_text(
        json.dumps(index, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return len(index)


def export_sources(conn) -> None:
    rows = conn.execute("""SELECT source_id, name, url, licence, attribution, retrieved_at, notes
                           FROM borna.source ORDER BY source_id""").fetchall()
    doc = [{"id": r[0], "name": r[1], "url": r[2], "licence": r[3], "attribution": r[4],
            "retrieved": r[5].date().isoformat(), "version": (r[6] or "").removeprefix("version ")}
           for r in rows]
    (WEB_DATA / "sources.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                                           encoding="utf-8")


def export_downloads() -> None:
    dl = WEB_DATA / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    holdout = DATA_OUT / "lrs" / "holdout.json"
    if holdout.exists():
        shutil.copy(holdout, WEB_DATA / "accuracy.json")
    for src, name in ((DATA_OUT / "hg540" / "register.csv", "hg540_register.csv"),
                      (DATA_OUT / "qa" / "county_lengths.csv", "qa_county_lengths.csv"),
                      (DATA_OUT / "qa" / "county_surface.csv", "qa_county_surface.csv"),
                      (DATA_OUT / "qa" / "gaps_by_county.csv", "qa_gaps_by_county.csv"),
                      (DATA_OUT / "qa" / "osm_refs_not_in_register.csv",
                       "qa_osm_refs_not_in_register.csv")):
        if src.exists():
            shutil.copy(src, dl / name)


def main() -> int:
    WEB_DATA.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(PG_DSN) as conn, tempfile.TemporaryDirectory() as tmp:
        print("tiles: exporting road files…", flush=True)
        n = export_roads(conn)
        print(f"tiles: {n:,} road files + index.json")
        export_sources(conn)
        export_downloads()
        export_tiles(conn, Path(tmp))
    size = sum(p.stat().st_size for p in WEB_DATA.rglob("*") if p.is_file())
    print(f"tiles: web/public/data is {size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
