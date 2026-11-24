"""Phase 1: load data/raw/ into PostGIS (raw schema) and register each source in borna.source.

Usage: python -m borna.importer [source ...]    (default: all)
Every import replaces its raw.* tables.
"""

import csv
import subprocess
import sys

import duckdb
import psycopg

from borna import sources
from borna.config import CRS_STORAGE, DATA_RAW, PG_DSN, ROOT
from borna.romania import COUNTIES, county_code

OSM_STYLE = ROOT / "pipeline" / "osm2pgsql" / "roads.lua"


def _require(source_id: str) -> dict:
    m = sources.read_manifest(source_id)
    if not sources.files_present(m):
        raise SystemExit(f"{source_id}: no download found, run `make download SOURCES={source_id}`")
    return m


def import_osm(conn) -> dict:
    m = _require("osm")
    subprocess.run(
        ["osm2pgsql", "--create", "--output=flex", f"--style={OSM_STYLE}",
         "--schema=raw", f"--database={PG_DSN}", str(DATA_RAW / "osm" / m["files"][0])],
        check=True,
    )
    return m


def import_overture(conn) -> dict:
    m = _require("overture")
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres; INSTALL spatial; LOAD spatial")
    con.execute(f"ATTACH '{PG_DSN}' AS pg (TYPE postgres)")

    for type_, geom_type in (("segment", "LineString"), ("connector", "Point")):
        table = f"raw.overture_{type_}"
        path = DATA_RAW / "overture" / f"{type_}.parquet"
        # Nested columns go over as JSON text and geometry as hex WKB, then get proper
        # PostGIS types below. Driven by the file's own schema so new Overture columns pass through.
        cols, json_cols = [], []
        for name, type_str, *_ in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall():
            if name == "bbox":
                continue
            if type_str.startswith("GEOMETRY"):
                cols.append(f"ST_AsHEXWKB({name}) AS geom")
            elif type_str.startswith(("STRUCT", "MAP")) or type_str.endswith("]"):
                cols.append(f'to_json("{name}")::VARCHAR AS "{name}"')
                json_cols.append(name)
            else:
                cols.append(f'"{name}"')

        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        print(f"overture: loading {path.name} into {table}…", flush=True)
        con.execute(f"CREATE TABLE pg.{table} AS SELECT {', '.join(cols)} FROM '{path}'")

        alters = [f'ALTER COLUMN "{c}" TYPE jsonb USING "{c}"::jsonb' for c in json_cols]
        alters.append(f"ALTER COLUMN geom TYPE geometry({geom_type}, {CRS_STORAGE}) "
                      f"USING ST_SetSRID(geom::geometry, {CRS_STORAGE})")
        conn.execute(f"ALTER TABLE {table} {', '.join(alters)}")
        conn.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (id)")
        conn.execute(f"CREATE INDEX ON {table} USING gist (geom)")
        conn.execute(f"ANALYZE {table}")
        conn.commit()
    return m


def import_gisco(conn) -> dict:
    m = _require("gisco")
    for name, table in zip(m["files"], ("raw.gisco_nuts", "raw.gisco_lau")):
        subprocess.run(
            ["ogr2ogr", "-f", "PostgreSQL", f"PG:{PG_DSN}", str(DATA_RAW / "gisco" / name),
             "-where", "CNTR_CODE = 'RO'", "-nln", table, "-overwrite", "-lco", "GEOMETRY_NAME=geom",
             "-lco", "FID=fid", "-nlt", "PROMOTE_TO_MULTI", "-t_srs", f"EPSG:{CRS_STORAGE}"],
            check=True,
        )
    conn.commit()
    # County (ISO 3166-2:RO) on NUTS 3 by name, and on each LAU by the NUTS 3 unit containing it.
    # LAU_ID is the SIRUTA code of the commune / town.
    conn.execute("ALTER TABLE raw.gisco_nuts ADD COLUMN county text")
    for nuts_id, name in conn.execute(
            "SELECT nuts_id, name_latn FROM raw.gisco_nuts WHERE levl_code = 3").fetchall():
        conn.execute("UPDATE raw.gisco_nuts SET county = %s WHERE nuts_id = %s",
                     (county_code(name.removeprefix("Municipiul ")), nuts_id))
    conn.execute("""
        ALTER TABLE raw.gisco_lau ADD COLUMN county text, ADD COLUMN siruta text;
        UPDATE raw.gisco_lau l SET siruta = split_part(l.gisco_id, '_', 2),
               county = n.county
        FROM raw.gisco_nuts n
        WHERE n.levl_code = 3 AND ST_Within(ST_PointOnSurface(l.geom), n.geom);
    """)
    return m


def import_ins_trn139a(conn) -> dict:
    m = _require("ins_trn139a")
    rows = []
    with open(DATA_RAW / "ins_trn139a" / m["files"][0], encoding="utf-8") as f:
        reader = csv.reader(f, skipinitialspace=True)
        next(reader)  # header
        for category, surface, area, year, _unit, value in reader:
            area = area.strip()
            county = None
            if area == "Municipiul Bucuresti":
                county = "B"
            elif area.upper() in COUNTIES:
                county = county_code(area)
            km = value.strip()
            rows.append((category.strip().lstrip("- ").strip(), surface.strip(), area, county,
                         int(year.removeprefix("Anul ")), km if km.replace(".", "").isdigit() else None))
    conn.execute("""
        DROP TABLE IF EXISTS raw.ins_trn139a;
        CREATE TABLE raw.ins_trn139a (
            category text,   -- Total | Nationale | Judetene si comunale | Judetene | Comunale
            surface  text,   -- Total | Modernizate | din modernizate: Autostrazi | … | De pamint
            area     text,   -- as published (country, macro-region, region, county)
            county   text,   -- ISO 3166-2:RO code when the area is a county, else NULL
            year     integer,
            km       numeric
        )
    """)
    with conn.cursor().copy("COPY raw.ins_trn139a FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    return m


def import_aadt_end(conn) -> dict:
    m = _require("aadt_end")
    subprocess.run(
        ["ogr2ogr", "-f", "PostgreSQL", f"PG:{PG_DSN}", str(DATA_RAW / "aadt_end" / m["files"][0]),
         "MajorRoadSource", "-nln", "raw.end_major_roads", "-overwrite",
         "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid", "-nlt", "LINESTRING",
         "-t_srs", f"EPSG:{CRS_STORAGE}"],
        check=True,
    )
    conn.commit()
    # annualTrafficFlow is vehicles per year; AADT = / 365.
    conn.execute("""
        ALTER TABLE raw.end_major_roads ADD COLUMN aadt integer;
        UPDATE raw.end_major_roads SET aadt = round(annualtrafficflow / 365.0);
    """)
    return m


SUMMARY_SQL = {
    "osm": """
        SELECT 'raw.osm_roads' AS "table", count(*) AS rows,
               count(*) FILTER (WHERE ref IS NOT NULL) AS with_ref FROM raw.osm_roads
        UNION ALL SELECT 'raw.osm_road_points', count(*),
               count(*) FILTER (WHERE kind = 'milestone') FROM raw.osm_road_points
        UNION ALL SELECT 'raw.osm_ferries', count(*), count(*) FILTER (WHERE name IS NOT NULL)
            FROM raw.osm_ferries
        UNION ALL SELECT 'raw.osm_road_routes', count(*),
               count(*) FILTER (WHERE ref IS NOT NULL) FROM raw.osm_road_routes
    """,
    "overture": """
        SELECT 'raw.overture_segment' AS "table", count(*) AS rows,
               count(*) FILTER (WHERE routes IS NOT NULL) AS with_ref FROM raw.overture_segment
        UNION ALL SELECT 'raw.overture_connector', count(*), NULL FROM raw.overture_connector
    """,
    "gisco": """
        SELECT 'raw.gisco_nuts' AS "table", count(*) AS rows,
               count(*) FILTER (WHERE county IS NOT NULL) AS with_county FROM raw.gisco_nuts
        UNION ALL SELECT 'raw.gisco_lau', count(*), count(*) FILTER (WHERE county IS NOT NULL)
        FROM raw.gisco_lau
    """,
    "ins_trn139a": """
        SELECT 'raw.ins_trn139a' AS "table", count(*) AS rows,
               count(DISTINCT county) AS counties FROM raw.ins_trn139a
    """,
    "aadt_end": """
        SELECT 'raw.end_major_roads' AS "table", count(*) AS rows,
               count(DISTINCT roadnationalcode) AS roads FROM raw.end_major_roads
    """,
}

SOURCES = {
    "osm": import_osm,
    "overture": import_overture,
    "gisco": import_gisco,
    "ins_trn139a": import_ins_trn139a,
    "aadt_end": import_aadt_end,
}


def main(argv: list[str]) -> int:
    names = argv or list(SOURCES)
    unknown = set(names) - set(SOURCES)
    if unknown:
        print(f"unknown source(s): {', '.join(sorted(unknown))}; known: {', '.join(SOURCES)}",
              file=sys.stderr)
        return 2
    with psycopg.connect(PG_DSN) as conn:
        for name in names:
            manifest = SOURCES[name](conn)
            sources.register(conn, manifest)
            conn.commit()
            print(f"{name}: registered in borna.source (version {manifest['version']})")
            for row in conn.execute(SUMMARY_SQL[name]).fetchall():
                print(f"  {row[0]:<24} {row[1]:>10,}  {'' if row[2] is None else f'({row[2]:,})'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
