"""QA: where Szelvény's network is incomplete and how well the km lookup works.

Writes data/out/qa/:
  attribute_coverage.csv per category: share of matched km with each attribute (exact, dual
                         carriageways counted once)
  gaps.csv               roads missing from OSM or matched >10% off their official length
  osm_refs_not_in_network.csv  OSM road numbers with no road in szelveny.road, with counties
  README.md              the summary, readable on its own

The comparisons with official figures (NAP vs KSH per county, NAP vs OSM geometry, NAP vs
EuroRegionalMap) come with the NAP reference network in Phase 3.

Run after `make lrs` and `make attributes`.
"""

import csv
import json
import sys
from datetime import date

import psycopg

from szelveny import sources
from szelveny.config import DATA_OUT, PG_DSN
from szelveny.hungary import CATEGORIES

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
        ), total AS (
            SELECT r.category, sum(s.km_to - s.km_from) AS km
            FROM szelveny.road_segment s JOIN szelveny.road r USING (road_id) GROUP BY 1
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


def readme(coverage, missing, unknown, holdout, versions) -> str:
    lines = [
        "# Szelvény: data quality report",
        "",
        f"Generated {date.today():%Y-%m-%d} from: " + "; ".join(versions) + ".",
        "",
        "## Attribute coverage (share of matched km)",
        "",
        "| Category | Surface | Speed limit | Lanes |", "|---|---:|---:|---:|",
    ]
    cov = {(c, t): p for c, t, _km, _tot, p in coverage}
    for cat in CATEGORIES:
        cells = [f"{cov.get((cat, t), 0):.0f}%" for t in ("surface", "maxspeed", "lanes")]
        lines.append(f"| {cat} | " + " | ".join(cells) + " |")

    by_status = {}
    for row in missing:
        by_status[(row[1], row[3])] = by_status.get((row[1], row[3]), 0) + 1
    lines += ["", "## Gaps", "",
              ("Roads not found in OSM by their number (`no_geometry`) or matched more than 10% "
               "off their official length (`partial`):"), "",
              "| Category | Not in OSM | Length off by >10% |", "|---|---:|---:|"]
    for cat in CATEGORIES:
        lines.append(f"| {cat} | {by_status.get((cat, 'no_geometry'), 0)} | "
                     f"{by_status.get((cat, 'partial'), 0)} |")
    lines += ["", (f"OSM road numbers with no road in the network ({len(unknown)}), longest first "
              "(km of OSM ways: both carriageways of a motorway count, so roughly double the "
              "road length):"), ""]
    lines += [f"- **{ref}** ({cat}): {km} km of OSM ways, counties {counties or '–'}"
              for ref, cat, _n, counties, km in unknown[:15]]
    lines += ["", "Full lists: `gaps.csv`, `osm_refs_not_in_network.csv`.", ""]
    if holdout:
        lines += ["## Km lookup accuracy", "",
                  (f"{holdout['located']} OSM km posts were kept out of calibration and looked "
                  f"up by their km: median error {holdout['median_m']:.0f} m, 80% within "
                  f"{holdout['p80_m']:.0f} m, 95% within {holdout['p95_m']:.0f} m "
                  f"({100 * holdout['within_50m_share']:.0f}% within 50 m)."), ""]
    return "\n".join(lines)


def main() -> int:
    out = DATA_OUT / "qa"
    out.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(PG_DSN) as conn:
        coverage = attribute_coverage(conn)
        missing, unknown = gaps(conn)

    _write(out / "attribute_coverage.csv",
           ["category", "attribute", "covered_km", "matched_km", "covered_pct"], coverage)
    _write(out / "gaps.csv",
           ["county", "category", "ref", "status", "official_km", "osm_path_km", "diff_pct",
            "route_desc"], missing)
    _write(out / "osm_refs_not_in_network.csv",
           ["ref", "category", "ways", "counties", "osm_way_km_both_carriageways"], unknown)

    hp = DATA_OUT / "lrs" / "holdout.json"
    holdout = json.loads(hp.read_text()) if hp.exists() else None
    versions = []
    for sid, label in (("osm", "OpenStreetMap"), ("nap", "NAP")):
        if m := sources.read_manifest(sid):
            versions.append(f"{label} {m.get('version') or m.get('retrieved_at')}")
    text = readme(coverage, missing, unknown, holdout, versions)
    (out / "README.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nqa: wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
