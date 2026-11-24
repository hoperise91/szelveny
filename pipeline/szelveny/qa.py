"""QA: how Szelvény's network compares with official figures and with OpenStreetMap.

Writes data/out/qa/:
  county_lengths.csv     per county: NAP national network km (clipped to the county, cycle
                         paths excluded, one carriageway) vs KSH STADAT 24.1.2.1 (latest year)
  nap_osm_offsets.csv    NAP sections where OSM's way with the same number runs > OFFSET_M away
                         (or is missing), merged per road and km range: for the OSM community
  nap_refs_not_in_osm.csv  NAP road numbers no OSM way carries
  osm_refs_not_in_network.csv  OSM road numbers with no road in szelveny.road, with counties
  attribute_coverage.csv per category: share of km with each attribute (dual carriageways once)
  gaps.csv               OSM-only roads missing from OSM or matched >10% off their length
  README.md              the summary, readable on its own

EuroRegionalMap (NAP vs ERM road numbers) is added when its token is configured. Lists meant for
the Hungarian OSM community are not published before contacting them (ROADMAP Phase 3.4).

Run after `make reference attributes objects`.
"""

import csv
import json
import sys
from datetime import date

import psycopg

from szelveny import sources
from szelveny.config import CRS_METRIC, DATA_OUT, PG_DSN
from szelveny.hungary import county_code

CATEGORIES = ["M", "F", "OK", "BK", "ramp", "rest", "cycle", "local"]
NATIONAL = ("M", "F", "OK", "BK")
SAMPLE_M = 250    # NAP sections are sampled every 250 m for the OSM offset
OFFSET_M = 30     # sampled points farther than this from OSM's same-numbered way are listed
MISSING_M = 200   # no same-numbered OSM way within this distance: missing in OSM

# Union of km ranges per (road, key): counts each km once even when both carriageways of a dual
# road carry a value. Expects a CTE `ev(road_id, key, f, t)`.
UNION_SQL = """
    o AS (SELECT *, max(t) OVER (PARTITION BY road_id, key ORDER BY f, t
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_t
          FROM ev),
    g AS (SELECT *, sum(CASE WHEN prev_t IS NULL OR f > prev_t THEN 1 ELSE 0 END)
                    OVER (PARTITION BY road_id, key ORDER BY f, t) AS grp FROM o),
    u AS (SELECT road_id, key, min(f) AS f, max(t) AS t FROM g GROUP BY road_id, key, grp)"""


def _write(path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def attribute_coverage(conn) -> list[tuple]:
    return conn.execute(f"""
        WITH ev AS (
            SELECT road_id, type AS key, km_from AS f, km_to AS t
            FROM szelveny.attribute_event
        ), {UNION_SQL},
        covered AS (
            SELECT r.category, u.key AS type, sum(u.t - u.f) AS km
            FROM u JOIN szelveny.road r USING (road_id) GROUP BY 1, 2
        ), total AS (   -- one carriageway: undivided and right-carriageway sections
            SELECT r.category, sum(s.km_to - s.km_from) AS km
            FROM szelveny.road_segment s JOIN szelveny.road r USING (road_id)
            WHERE s.carriageway IN (0, 1) GROUP BY 1
        )
        SELECT c.category, c.type, round(c.km, 1), round(t.km, 1),
               round(100 * c.km / nullif(t.km, 0), 1)
        FROM covered c JOIN total t USING (category)
        ORDER BY array_position(ARRAY{list(CATEGORIES)}, c.category), c.type
    """).fetchall()


def gaps(conn) -> tuple[list[tuple], list[tuple]]:
    missing = conn.execute(f"""
        SELECT coalesce(r.county, '(national)'), r.category, r.ref, l.status,
               l.official_km, l.path_km,
               round(100 * (l.path_km - l.official_km) / nullif(l.official_km, 0), 1),
               r.route_desc
        FROM szelveny.lrs_road l JOIN szelveny.road r USING (road_id)
        WHERE l.status <> 'ok'
        ORDER BY 1, array_position(ARRAY{list(CATEGORIES)}, r.category), r.ref
    """).fetchall()
    unknown = conn.execute("""
        SELECT w.ref, w.category, count(*),
               (SELECT string_agg(DISTINCT c.county, ' ' ORDER BY c.county)
                FROM szelveny.osm_way_county c JOIN szelveny.osm_way_ref w2 USING (osm_id)
                WHERE w2.ref = w.ref),
               round((sum(ST_Length(o.geom::geography)) / 1000)::numeric, 1)
        FROM szelveny.osm_way_ref w
        JOIN raw.osm_roads o USING (osm_id)
        WHERE NOT EXISTS (SELECT 1 FROM szelveny.road r WHERE r.ref = w.ref)
        GROUP BY 1, 2 ORDER BY 5 DESC
    """).fetchall()
    return missing, unknown


def county_lengths(conn) -> tuple[list[tuple], int]:
    year = conn.execute("SELECT max(year) FROM raw.ksh_county_roads WHERE km IS NOT NULL").fetchone()[0]
    ksh = {c: float(km) for c, km in conn.execute(
        "SELECT county, km FROM raw.ksh_county_roads WHERE year = %s AND county IS NOT NULL", (year,))}
    conn.execute(f"""
        CREATE TEMP TABLE cpart AS
        SELECT name, ST_Subdivide(ST_Transform(geom, {CRS_METRIC}), 256) AS g FROM raw.osm_counties;
        CREATE INDEX ON cpart USING gist (g);
    """)
    nap = {}
    for name, km in conn.execute(f"""
            SELECT c.name, sum(ST_Length(ST_Intersection(ST_Transform(s.geom, {CRS_METRIC}), c.g))) / 1000
            FROM szelveny.road_segment s JOIN szelveny.road r USING (road_id)
            JOIN cpart c ON ST_Intersects(ST_Transform(s.geom, {CRS_METRIC}), c.g)
            WHERE r.source_id = 'nap' AND r.category <> 'cycle' AND s.carriageway IN (0, 1)
            GROUP BY c.name"""):
        nap[county_code(name)] = nap.get(county_code(name), 0) + float(km)
    rows = [(c, round(nap.get(c, 0), 1), ksh.get(c), _pct(nap.get(c, 0) - ksh[c], ksh[c]) if c in ksh else "")
            for c in sorted(set(nap) | set(ksh))]
    return rows, year


def nap_osm_offsets(conn) -> tuple[list[tuple], list[tuple]]:
    """Sample NAP sections every SAMPLE_M and measure the distance to OSM's way with the same
    number. Returns (summary per category, runs of far or missing points per road)."""
    conn.execute(f"""
        CREATE TEMP TABLE osmw AS
        SELECT x.ref, ST_Transform(w.geom, {CRS_METRIC}) AS g
        FROM szelveny.osm_way_ref x JOIN raw.osm_roads w USING (osm_id)
        WHERE w.highway <> 'construction';
        CREATE INDEX ON osmw USING gist (g);
        CREATE INDEX ON osmw (ref);

        CREATE TEMP TABLE pts AS
        SELECT r.ref, r.category, s.km_from + (s.km_to - s.km_from) * f.frac AS km,
               ST_LineInterpolatePoint(g.g, f.frac) AS p
        FROM szelveny.road_segment s JOIN szelveny.road r USING (road_id)
        CROSS JOIN LATERAL (SELECT ST_Transform(s.geom, {CRS_METRIC}) AS g) g
        CROSS JOIN LATERAL (
            SELECT (i + 0.5) / n AS frac
            FROM (SELECT greatest(1, round(ST_Length(g.g) / {SAMPLE_M}))::int AS n) k,
                 generate_series(0, k.n - 1) AS i) f
        WHERE r.source_id = 'nap' AND r.category IN {NATIONAL} AND s.carriageway IN (0, 1);

        CREATE TEMP TABLE off AS
        SELECT p.ref, p.category, p.km, p.p,
               (SELECT ST_Distance(o.g, p.p) FROM osmw o
                WHERE o.ref = p.ref AND ST_DWithin(o.g, p.p, {MISSING_M})
                ORDER BY o.g <-> p.p LIMIT 1) AS d
        FROM pts p;
    """)
    summary = conn.execute(f"""
        SELECT category, count(*),
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d)::numeric, 1),
               round(percentile_cont(0.95) WITHIN GROUP (ORDER BY d)::numeric, 1),
               round(100.0 * avg((d <= 10)::int), 1), round(100.0 * avg((d <= 20)::int), 1),
               round(100.0 * avg((d IS NULL)::int), 1)
        FROM off GROUP BY 1 ORDER BY array_position(ARRAY{list(NATIONAL)}, category)
    """).fetchall()
    # consecutive far/missing points on a road merged into one km range
    runs = conn.execute(f"""
        WITH bad AS (
            SELECT ref, category, km, p, d,
                   km - row_number() OVER (PARTITION BY ref ORDER BY km) * {SAMPLE_M / 1000} AS grp
            FROM off WHERE d IS NULL OR d > {OFFSET_M})
        SELECT ref, category, round(min(km), 3), round(max(km), 3),
               CASE WHEN bool_and(d IS NULL) THEN 'missing in OSM' ELSE 'offset' END,
               round(max(d)::numeric), count(*),
               round(ST_X(ST_Transform(ST_Centroid(ST_Collect(p)), 4326))::numeric, 5),
               round(ST_Y(ST_Transform(ST_Centroid(ST_Collect(p)), 4326))::numeric, 5)
        FROM bad GROUP BY ref, category, round(grp::numeric, 2)
        HAVING count(*) >= 2   -- a single far point is often a junction or a carriageway split
        ORDER BY count(*) DESC
    """).fetchall()
    return summary, runs


def nap_refs_not_in_osm(conn) -> list[tuple]:
    return conn.execute(f"""
        SELECT r.ref, r.category, r.road_class, r.length_official, r.route_desc
        FROM szelveny.road r
        WHERE r.source_id = 'nap' AND r.category IN {NATIONAL}
          AND NOT EXISTS (SELECT 1 FROM szelveny.osm_way_ref x WHERE x.ref = r.ref)
        ORDER BY r.length_official DESC NULLS LAST
    """).fetchall()


def km_posts(conn) -> dict:
    """Distance from each km post to where locate() puts its km: NAP's own posts (consistency)
    and OSM's (independent)."""
    out = {}
    for label, sql in (
        ("nap", """SELECT ST_Distance(l.geom::geography, ST_GeometryN(k.geom, 1)::geography)
                   FROM raw.nap_szelvenyjelek k CROSS JOIN LATERAL szelveny.locate(k.kszam, k.kmjel::numeric) l
                   WHERE k.pkod IN ('0', '1')"""),
        ("osm", """SELECT ST_Distance(l.geom::geography, p.geom::geography)
                   FROM raw.osm_road_points p
                   CROSS JOIN LATERAL szelveny.locate(regexp_replace(upper(p.ref), '\\s', '', 'g'),
                                                      replace(p.distance, ',', '.')::numeric) l
                   WHERE p.kind = 'milestone' AND p.ref ~* '^(M ?[0-9]{1,2}|[0-9]{1,5})$'
                     AND p.distance ~ '^[0-9]+([.,][0-9]+)?$'"""),
    ):
        n, med, p95, w20 = conn.execute(f"""
            SELECT count(*), round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d)::numeric, 1),
                   round(percentile_cont(0.95) WITHIN GROUP (ORDER BY d)::numeric, 1),
                   round(100.0 * avg((d <= 20)::int), 1)
            FROM ({sql}) x(d)""").fetchone()
        out[label] = {"n": n, "median_m": med, "p95_m": p95, "within_20m_pct": w20}
    return out


def _pct(a, b) -> str:
    return f"{100 * a / b:+.1f}%" if b else "–"


def readme(lengths, year, offsets, runs, nap_missing, posts, coverage, unknown, versions) -> str:
    nap_total = sum(r[1] for r in lengths)
    ksh_total = sum(r[2] or 0 for r in lengths)
    lines = [
        "# Szelvény: data quality report",
        "",
        f"Generated {date.today():%Y-%m-%d} from: " + "; ".join(versions) + ".",
        "",
        f"## Network length per county: NAP vs KSH {year}",
        "",
        (f"NAP national network (one carriageway, no cycle paths) clipped to each county: "
         f"**{nap_total:,.0f} km**; KSH STADAT 24.1.2.1: **{ksh_total:,.0f} km** "
         f"({_pct(nap_total - ksh_total, ksh_total)})."),
        "",
        "| County | NAP km | KSH km | Difference |", "|---|---:|---:|---:|",
    ]
    lines += [f"| {c} | {n:,.1f} | {k:,.0f} | {d} |" for c, n, k, d in lengths if k is not None]
    lines += ["", "## Szelvény accuracy: km posts", "",
              "Distance from each km post to where Szelvény puts its km:", "",
              "| Km posts | Count | Median | 95% within | Within 20 m |", "|---|---:|---:|---:|---:|"]
    for label, name in (("nap", "NAP Szelvényjelek (consistency)"), ("osm", "OpenStreetMap (independent)")):
        p = posts[label]
        lines.append(f"| {name} | {p['n']:,} | {p['median_m']} m | {p['p95_m']} m | {p['within_20m_pct']}% |")
    lines += ["", ("OSM km posts are mapped by volunteers at the roadside stone, which stands a few metres "
              "off the centreline, so some offset is expected."), "",
              f"## NAP vs OpenStreetMap geometry (sampled every {SAMPLE_M} m)", "",
              "| Category | Points | Median | 95% within | ≤ 10 m | ≤ 20 m | Missing in OSM |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    lines += [f"| {c} | {n:,} | {m} m | {p} m | {a}% | {b}% | {mi}% |" for c, n, m, p, a, b, mi in offsets]
    lines += ["", (f"{len(runs):,} stretches where OSM's same-numbered way is more than {OFFSET_M} m away "
              f"or missing (at least two points in a row): `nap_osm_offsets.csv`. Longest:"), ""]
    lines += [f"- **{ref}** km {f}–{t}: {kind}{f', up to {mx} m' if mx is not None else ''} "
              f"({lat}, {lon})" for ref, _c, f, t, kind, mx, _n, lon, lat in runs[:12]]
    lines += ["", f"NAP road numbers that no OSM way carries: {len(nap_missing):,} "
              "(`nap_refs_not_in_osm.csv`); longest: "
              + ", ".join(f"{r[0]} ({r[3]} km)" for r in nap_missing[:10]) + ".",
              "", (f"OSM road numbers with no road in the network: {len(unknown):,} "
              "(`osm_refs_not_in_network.csv`)."),
              "", "## Attribute coverage (share of road km)", "",
              "| Category | Surface | Lanes | Speed limit | AADT | Built-up |", "|---|---:|---:|---:|---:|---:|"]
    cov = {(c, t): p for c, t, _km, _tot, p in coverage}
    for cat in NATIONAL:
        cells = [f"{min(cov.get((cat, t), 0), 100):.0f}%" for t in ("surface", "lanes", "maxspeed", "aadt", "built_up")]
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")
    lines += ["", ("NAP lists only posted speed limits, so 'Speed limit' coverage is the share of road "
              "with a restriction sign, not a data gap."), "",
              ("*These lists could help the Hungarian OSM community; contact them (talk-hu, Matrix) "
              "before publishing a diff.*"), ""]
    return "\n".join(lines)


def main() -> int:
    out = DATA_OUT / "qa"
    out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(PG_DSN) as conn:
        print("qa: county lengths…", flush=True)
        lengths, year = county_lengths(conn)
        print("qa: NAP vs OSM geometry…", flush=True)
        offsets, runs = nap_osm_offsets(conn)
        nap_missing = nap_refs_not_in_osm(conn)
        print("qa: km posts…", flush=True)
        posts = km_posts(conn)
        coverage = attribute_coverage(conn)
        missing, unknown = gaps(conn)

    _write(out / "county_lengths.csv", ["county", "nap_km", f"ksh_{year}_km", "nap_vs_ksh"], lengths)
    _write(out / "nap_osm_offsets.csv", ["ref", "category", "km_from", "km_to", "kind", "max_offset_m",
                                         "points", "lon", "lat"], runs)
    _write(out / "nap_refs_not_in_osm.csv", ["ref", "category", "class", "length_km", "name"], nap_missing)
    _write(out / "attribute_coverage.csv",
           ["category", "attribute", "covered_km", "matched_km", "covered_pct"], coverage)
    _write(out / "gaps.csv",
           ["county", "category", "ref", "status", "official_km", "osm_path_km", "diff_pct",
            "route_desc"], missing)
    _write(out / "osm_refs_not_in_network.csv",
           ["ref", "category", "ways", "counties", "osm_way_km_both_carriageways"], unknown)
    (out / "km_posts.json").write_text(json.dumps(posts, indent=2, default=float) + "\n")

    versions = []
    for sid, label in (("nap", "NAP"), ("osm", "OpenStreetMap"), ("ksh", "KSH")):
        if m := sources.read_manifest(sid):
            versions.append(f"{label} {m.get('version') or m.get('retrieved_at')}")
    text = readme(lengths, year, offsets, runs, nap_missing, posts, coverage, unknown, versions)
    (out / "README.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nqa: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
