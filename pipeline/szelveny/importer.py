"""Phase 1: load data/raw/ into PostGIS (raw schema) and register each source in szelveny.source.

Usage: python -m szelveny.importer [source ...]    (default: all)
Every import replaces its raw.* tables.
"""

import subprocess
import sys

import duckdb
import psycopg

from szelveny import sources
from szelveny.config import CRS_STORAGE, DATA_RAW, PG_DSN, ROOT
from szelveny.hungary import county_code

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
             "-where", "CNTR_CODE = 'HU'", "-nln", table, "-overwrite", "-lco", "GEOMETRY_NAME=geom",
             "-lco", "FID=fid", "-nlt", "PROMOTE_TO_MULTI", "-t_srs", f"EPSG:{CRS_STORAGE}"],
            check=True,
        )
    conn.commit()
    # County (ISO 3166-2:HU) on NUTS 3 by name, and on each LAU by the NUTS 3 unit containing it.
    # LAU_ID is the KSH settlement code (településkód).
    conn.execute("ALTER TABLE raw.gisco_nuts ADD COLUMN county text")
    for nuts_id, name in conn.execute(
            "SELECT nuts_id, name_latn FROM raw.gisco_nuts WHERE levl_code = 3").fetchall():
        conn.execute("UPDATE raw.gisco_nuts SET county = %s WHERE nuts_id = %s",
                     (county_code(name), nuts_id))
    conn.execute("""
        ALTER TABLE raw.gisco_lau ADD COLUMN county text, ADD COLUMN ksh_code text;
        UPDATE raw.gisco_lau l SET ksh_code = split_part(l.gisco_id, '_', 2),
               county = n.county
        FROM raw.gisco_nuts n
        WHERE n.levl_code = 3 AND ST_Within(ST_PointOnSurface(l.geom), n.geom);
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
}

SOURCES = {
    "osm": import_osm,
    "overture": import_overture,
    "gisco": import_gisco,
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
            print(f"{name}: registered in szelveny.source (version {manifest['version']})")
            for row in conn.execute(SUMMARY_SQL[name]).fetchall():
                print(f"  {row[0]:<24} {row[1]:>10,}  {'' if row[2] is None else f'({row[2]:,})'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
