"""Phase 2 QA: how Bornă's network compares with the official figures, and where data is missing.

Writes data/out/qa/:
  county_lengths.csv     per county and network: HG 540 register km, INS km (latest year),
                         km matched in OSM (geometry clipped to the county)
  county_surface.csv     per county and network: surface shares from INS vs OSM surface tags
  attribute_coverage.csv per category: share of matched km with each attribute (exact, dual
                         carriageways counted once)
  gaps_by_county.csv     register roads missing from OSM or matched >10% off their length
  osm_refs_not_in_register.csv  OSM road numbers HG 540 doesn't list, with counties
  README.md              the summary, readable on its own (for sharing, e.g. with OSM Romania)

Run after `make lrs` and `make attributes`.
"""

import csv
import json
import sys
from datetime import date

import psycopg

from borna import sources
from borna.config import DATA_OUT, PG_DSN
from borna.romania import county_code

NETWORKS = ("national", "dj", "dc")  # INS categories: Nationale, Judetene, Comunale
INS_CATEGORY = {"Nationale": "national", "Judetene": "dj", "Comunale": "dc"}
# INS surface classes -> comparable classes. OSM can't tell heavy from light pavement (both are
# 'asphalt'), so 'modernised' and 'light pavement' are compared together as 'paved'.
INS_SURFACE = {"Modernizate": "paved", "Cu imbracaminti usoare rutiere": "paved",
               "Pietruite": "gravel", "De pamint": "earth"}
SURFACE_CLASS_SQL = """
    CASE WHEN value IN ('asphalt', 'concrete', 'concrete:plates', 'concrete:lanes', 'paved',
                        'paving_stones', 'sett', 'cobblestone', 'unhewn_cobblestone', 'chipseal',
                        'metal', 'wood') THEN 'paved'
         WHEN value IN ('gravel', 'fine_gravel', 'compacted', 'pebblestone', 'rock', 'shells')
              THEN 'gravel'
         WHEN value IN ('dirt', 'ground', 'earth', 'grass', 'mud', 'sand', 'soil') THEN 'earth'
         WHEN value = 'unpaved' THEN 'unpaved'
         ELSE 'other' END"""

# Union of km ranges per (road, key): counts each km once even when both carriageways of a dual
# road carry a value. Expects a CTE `ev(road_id, key, f, t)`.
UNION_SQL = """
    o AS (SELECT *, max(t) OVER (PARTITION BY road_id, key ORDER BY f, t
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_t
          FROM ev),
    g AS (SELECT *, sum(CASE WHEN prev_t IS NULL OR f > prev_t THEN 1 ELSE 0 END)
                    OVER (PARTITION BY road_id, key ORDER BY f, t) AS grp FROM o),
    u AS (SELECT road_id, key, min(f) AS f, max(t) AS t FROM g GROUP BY road_id, key, grp)"""

# km range of each road in each county: county roads have one county; national roads are split
# by the per-county DN tables of HG 540.
ROAD_COUNTY_SQL = """
    road_county AS (
        SELECT road_id, county, NULL::numeric AS f, NULL::numeric AS t,
               lower(category) AS network
        FROM borna.road WHERE county IS NOT NULL AND category IN ('DJ', 'DC')
        UNION ALL
        SELECT r.road_id, e.county, e.km_from, e.km_to, 'national'
        FROM borna.road r
        JOIN borna.register_entry e ON e.ref = r.ref AND e.list = 'county_dn'
        WHERE r.county IS NULL AND e.km_from IS NOT NULL AND e.km_to IS NOT NULL)"""


def _write(path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def county_lengths(conn) -> list[tuple]:
    year = conn.execute("SELECT max(year) FROM raw.ins_trn139a WHERE km IS NOT NULL").fetchone()[0]
    conn.execute("CREATE TEMP TABLE county_map (name text, county text)")
    for (name,) in conn.execute("SELECT name FROM raw.osm_counties").fetchall():
        conn.execute("INSERT INTO county_map VALUES (%s, %s)", (name, county_code(name)))
    return conn.execute(f"""
        WITH reg AS (
            SELECT county,
                   CASE list WHEN 'county_dn' THEN 'national' ELSE list END AS network,
                   sum(length_km) AS km
            FROM borna.register_entry WHERE list IN ('county_dn', 'dj', 'dc') GROUP BY 1, 2
        ), ins AS (
            SELECT county, CASE category {' '.join(f"WHEN '{k}' THEN '{v}'"
                                                   for k, v in INS_CATEGORY.items())} END AS network,
                   sum(km) AS km
            FROM raw.ins_trn139a
            WHERE year = {year} AND surface = 'Total' AND county IS NOT NULL
              AND category IN ({', '.join(f"'{k}'" for k in INS_CATEGORY)})
            GROUP BY 1, 2
        ), parts AS (
            SELECT m.county, ST_Subdivide(c.geom, 256) AS geom
            FROM raw.osm_counties c JOIN county_map m USING (name)
        ), osm AS (
            SELECT p.county,
                   CASE WHEN r.category IN ('A', 'DN', 'V', 'DX') THEN 'national'
                        ELSE lower(r.category) END AS network,
                   (sum(ST_Length(ST_Transform(ST_Intersection(s.geom, p.geom), 3844))) / 1000)::numeric AS km
            FROM borna.road_segment s
            JOIN borna.road r USING (road_id)
            JOIN parts p ON ST_Intersects(s.geom, p.geom)
            WHERE r.county IS NULL OR r.county = p.county
            GROUP BY 1, 2
        ), keys AS (SELECT county, network FROM reg UNION SELECT county, network FROM ins)
        SELECT k.county, k.network, round(reg.km, 1), round(ins.km, 1), round(osm.km, 1),
               round(100 * (reg.km - ins.km) / nullif(ins.km, 0), 1),
               round(100 * osm.km / nullif(reg.km, 0), 1)
        FROM keys k
        LEFT JOIN reg USING (county, network)
        LEFT JOIN ins USING (county, network)
        LEFT JOIN osm USING (county, network)
        WHERE k.network IN ('national', 'dj', 'dc')
        ORDER BY k.county, array_position(ARRAY['national', 'dj', 'dc'], k.network)
    """).fetchall(), year


def county_surface(conn, year: int) -> list[tuple]:
    ins_case = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in INS_SURFACE.items())
    cat_case = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in INS_CATEGORY.items())
    return conn.execute(f"""
        WITH ev AS (
            SELECT road_id, {SURFACE_CLASS_SQL} AS key, km_from AS f, km_to AS t
            FROM borna.attribute_event WHERE type = 'surface' AND source_id = 'osm'
        ), {UNION_SQL}, {ROAD_COUNTY_SQL},
        osm AS (
            SELECT rc.county, rc.network, u.key AS class,
                   sum(CASE WHEN rc.f IS NULL THEN u.t - u.f
                            ELSE greatest(0, least(u.t, rc.t) - greatest(u.f, rc.f)) END) AS km
            FROM u JOIN road_county rc USING (road_id)
            GROUP BY 1, 2, 3
        ), osm_share AS (
            SELECT county, network,
                   sum(km) FILTER (WHERE class IN ('paved', 'gravel', 'earth')) AS known,
                   sum(km) FILTER (WHERE class = 'paved') AS paved,
                   sum(km) FILTER (WHERE class = 'gravel') AS gravel,
                   sum(km) FILTER (WHERE class = 'earth') AS earth,
                   sum(km) FILTER (WHERE class = 'unpaved') AS unpaved
            FROM osm GROUP BY 1, 2
        ), ins AS (
            SELECT county, CASE category {cat_case} END AS network,
                   CASE surface {ins_case} END AS class, sum(km) AS km
            FROM raw.ins_trn139a
            WHERE year = {year} AND county IS NOT NULL
              AND category IN ({', '.join(f"'{k}'" for k in INS_CATEGORY)})
              AND surface IN ({', '.join(f"'{k}'" for k in INS_SURFACE)})
            GROUP BY 1, 2, 3
        ), ins_share AS (
            SELECT county, network, sum(km) AS total,
                   sum(km) FILTER (WHERE class = 'paved') AS paved,
                   sum(km) FILTER (WHERE class = 'gravel') AS gravel,
                   sum(km) FILTER (WHERE class = 'earth') AS earth
            FROM ins GROUP BY 1, 2
        )
        SELECT i.county, i.network,
               round(100 * i.paved / nullif(i.total, 0)), round(100 * i.gravel / nullif(i.total, 0)),
               round(100 * i.earth / nullif(i.total, 0)),
               round(100 * o.paved / nullif(o.known, 0)), round(100 * o.gravel / nullif(o.known, 0)),
               round(100 * o.earth / nullif(o.known, 0)),
               round(o.known, 1), round(o.unpaved, 1)
        FROM ins_share i LEFT JOIN osm_share o USING (county, network)
        ORDER BY i.county, array_position(ARRAY['national', 'dj', 'dc'], i.network)
    """).fetchall()


def attribute_coverage(conn) -> list[tuple]:
    return conn.execute(f"""
        WITH ev AS (
            SELECT road_id, type AS key, km_from AS f, km_to AS t
            FROM borna.attribute_event
        ), {UNION_SQL},
        covered AS (
            SELECT r.category, u.key AS type, sum(u.t - u.f) AS km
            FROM u JOIN borna.road r USING (road_id) GROUP BY 1, 2
        ), total AS (
            SELECT r.category, sum(s.km_to - s.km_from) AS km
            FROM borna.road_segment s JOIN borna.road r USING (road_id) GROUP BY 1
        )
        SELECT c.category, c.type, round(c.km, 1), round(t.km, 1),
               round(100 * c.km / nullif(t.km, 0), 1)
        FROM covered c JOIN total t USING (category)
        ORDER BY array_position(ARRAY['A', 'DN', 'V', 'DJ', 'DC', 'DP'], c.category), c.type
    """).fetchall()


def gaps(conn) -> tuple[list[tuple], list[tuple]]:
    missing = conn.execute("""
        SELECT coalesce(r.county, '(national)'), r.category, r.ref, l.status,
               l.official_km, l.path_km,
               round(100 * (l.path_km - l.official_km) / nullif(l.official_km, 0), 1),
               r.route_desc
        FROM borna.lrs_road l JOIN borna.road r USING (road_id)
        WHERE l.status <> 'ok'
        ORDER BY 1, array_position(ARRAY['A', 'DN', 'V', 'DJ', 'DC', 'DP'], r.category), r.ref
    """).fetchall()
    unknown = conn.execute("""
        SELECT w.ref, w.category, count(*),
               (SELECT string_agg(DISTINCT c.county, ' ' ORDER BY c.county)
                FROM borna.osm_way_county c JOIN borna.osm_way_ref w2 USING (osm_id)
                WHERE w2.ref = w.ref),
               round((sum(ST_Length(o.geom::geography)) / 1000)::numeric, 1)
        FROM borna.osm_way_ref w
        JOIN raw.osm_roads o USING (osm_id)
        WHERE NOT EXISTS (SELECT 1 FROM borna.road r WHERE r.ref = w.ref)
        GROUP BY 1, 2 ORDER BY 5 DESC
    """).fetchall()
    return missing, unknown


def _pct(a, b) -> str:
    return f"{100 * a / b:.0f}%" if b else "–"


def readme(lengths, year, surface, coverage, missing, unknown, holdout, versions) -> str:
    nat = {n: [0.0, 0.0, 0.0] for n in NETWORKS}
    for _county, network, reg, ins, osm, *_ in lengths:
        for i, v in enumerate((reg, ins, osm)):
            nat[network][i] += float(v or 0)
    label = {"national": "National (A, DN, bypasses)", "dj": "County (DJ)", "dc": "Communal (DC)"}
    lines = [
        "# Bornă: data quality report",
        "",
        f"Generated {date.today():%d.%m.%Y} from: " + "; ".join(versions) + ".",
        "",
        "## Network length: official register vs statistics vs OpenStreetMap",
        "",
        f"| Network | HG 540 register (km) | INS {year} (km) | Matched in OSM (km) | OSM / register |",
        "|---|---:|---:|---:|---:|",
    ]
    for n in NETWORKS:
        reg, ins, osm = nat[n]
        lines.append(f"| {label[n]} | {reg:,.0f} | {ins:,.0f} | {osm:,.0f} | {_pct(osm, reg)} |")
    worst = sorted((r for r in lengths if r[5] is not None and r[1] != "national"),
                   key=lambda r: abs(r[5]), reverse=True)[:8]
    lines += [
        "",
        ("HG 540 and INS count roads differently (INS reports the network actually administered "
        "each year; HG 540 changes only when amended). Largest county differences "
        "(register vs INS):"),
        "",
        "| County | Network | Register km | INS km | Difference |",
        "|---|---|---:|---:|---:|",
    ]
    for county, network, reg, ins, _osm, diff, _ in worst:
        lines.append(f"| {county} | {network.upper()} | {reg:,.0f} | {ins:,.0f} | {diff:+.0f}% |")

    def share(rows, network, col):
        known = [(float(r[8] or 0), r[col]) for r in rows if r[1] == network and r[col] is not None]
        tot = sum(k for k, _ in known)
        return sum(k * float(v) for k, v in known) / tot if tot else None

    lines += [
        "",
        "## Surface: INS vs OSM tags",
        "",
        ("Share of each network's length (OSM: of the matched length with a surface tag; INS "
        "'modernised' and 'light pavement' combined as paved, since OSM can't tell them apart)."),
        "",
        "| Network | Paved: INS / OSM | Gravel: INS / OSM | Earth: INS / OSM |",
        "|---|---|---|---|",
    ]
    for n in NETWORKS:
        ins_tot = [r for r in surface if r[1] == n and r[2] is not None]
        ins_paved = sum(float(r[2]) for r in ins_tot) / len(ins_tot) if ins_tot else 0
        ins_gravel = sum(float(r[3] or 0) for r in ins_tot) / len(ins_tot) if ins_tot else 0
        ins_earth = sum(float(r[4] or 0) for r in ins_tot) / len(ins_tot) if ins_tot else 0
        o = [share(surface, n, c) for c in (5, 6, 7)]
        lines.append(f"| {label[n]} | {ins_paved:.0f}% / {o[0] or 0:.0f}% | "
                     f"{ins_gravel:.0f}% / {o[1] or 0:.0f}% | {ins_earth:.0f}% / {o[2] or 0:.0f}% |")
    lines += ["", ("(INS shares are county averages; OSM shares are length-weighted. "
              "Per county: `county_surface.csv`.)"), "",
              "## Attribute coverage (share of matched km)", "",
              "| Category | Surface | Speed limit | Lanes | AADT |", "|---|---:|---:|---:|---:|"]
    cov = {(c, t): p for c, t, _km, _tot, p in coverage}
    for cat in ("A", "DN", "DJ", "DC"):
        cells = [f"{cov.get((cat, t), 0):.0f}%" for t in ("surface", "maxspeed", "lanes", "aadt")]
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")

    by_status = {}
    for row in missing:
        by_status.setdefault((row[1], row[3]), 0)
        by_status[(row[1], row[3])] += 1
    lines += ["", "## Gaps", "",
              ("Register roads not found in OSM by their number (`no_geometry`) or matched more "
              "than 10% off their official length (`partial`):"), "",
              "| Category | Not in OSM | Length off by >10% |", "|---|---:|---:|"]
    for cat in ("A", "DN", "V", "DJ", "DC", "DP"):
        lines.append(f"| {cat} | {by_status.get((cat, 'no_geometry'), 0)} | "
                     f"{by_status.get((cat, 'partial'), 0)} |")
    lines += ["", (f"OSM road numbers that HG 540 doesn't list ({len(unknown)}), longest first "
              "(km of OSM ways: both carriageways of a motorway count, so roughly double the "
              "road length):"), ""]
    lines += [f"- **{ref}** ({cat}): {km} km of OSM ways, counties {counties or '–'}"
              for ref, cat, _n, counties, km in unknown[:15]]
    lines += ["", "Full lists: `gaps_by_county.csv`, `osm_refs_not_in_register.csv`.", ""]
    if holdout:
        lines += ["## Km lookup accuracy", "",
                  (f"{holdout['located']} OSM milestones on national roads were kept out of "
                  "calibration and looked up by their km: median error "
                  f"{holdout['median_m']:.0f} m, 80% within {holdout['p80_m']:.0f} m, 95% within "
                  f"{holdout['p95_m']:.0f} m ({100 * holdout['within_50m_share']:.0f}% within 50 m). "
                  "This is the best case: roads with few control points drift more "
                  "(0.1-0.9 km on the roads spot-checked), and Bornă marks those km values as "
                  "interpolated or estimated."), ""]
    return "\n".join(lines)


def main() -> int:
    out = DATA_OUT / "qa"
    out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(PG_DSN) as conn:
        print("qa: county lengths…", flush=True)
        lengths, year = county_lengths(conn)
        print("qa: surfaces…", flush=True)
        surface = county_surface(conn, year)
        coverage = attribute_coverage(conn)
        missing, unknown = gaps(conn)

    _write(out / "county_lengths.csv",
           ["county", "network", "register_km", f"ins_{year}_km", "osm_matched_km",
            "register_vs_ins_pct", "osm_vs_register_pct"], lengths)
    _write(out / "county_surface.csv",
           ["county", "network", "ins_paved_pct", "ins_gravel_pct", "ins_earth_pct",
            "osm_paved_pct", "osm_gravel_pct", "osm_earth_pct", "osm_surface_known_km",
            "osm_unpaved_unspecified_km"], surface)
    _write(out / "attribute_coverage.csv",
           ["category", "attribute", "covered_km", "matched_km", "covered_pct"], coverage)
    _write(out / "gaps_by_county.csv",
           ["county", "category", "ref", "status", "official_km", "osm_path_km", "diff_pct",
            "route_desc"], missing)
    _write(out / "osm_refs_not_in_register.csv",
           ["ref", "category", "ways", "counties", "osm_way_km_both_carriageways"], unknown)

    hp = DATA_OUT / "lrs" / "holdout.json"
    holdout = json.loads(hp.read_text()) if hp.exists() else None
    versions = []
    for sid, label in (("hg540", "HG 540/2000 forma consolidată"), ("osm", "OpenStreetMap"),
                       ("ins_trn139a", "INS TRN139A updated"), ("aadt_end", "open AADT")):
        if m := sources.read_manifest(sid):
            versions.append(f"{label} {m['version']}")
    text = readme(lengths, year, surface, coverage, missing, unknown, holdout, versions)
    (out / "README.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nqa: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
