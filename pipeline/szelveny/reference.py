"""The reference network from NAP's national road network (Országos közúthálózat).

Usage: python -m szelveny.reference    (after `make nap`)

NAP carries official chainage on every section (ROADMAP decision gate, 2026-11-24), so the
national network needs no calibration: each NAP section becomes one szelveny.road_segment with
km_quality 'official', its carriageway (pkod) and NAP's own szelvény strings. Roads get their
name (route), category class and administrator (kezelő) from "Közút neve, kategóriája és
kezelője", which shares section ids with the network package.

Checked on the 2026-11-24 data: every section is one line drawn in the chainage direction
(left carriageways too), and NAP's km posts lie a median 1.3 m from where their km
interpolates on the sections.

Re-running replaces the NAP roads (and their segments and events, by cascade). OSM-only roads
(`make lrs`) are left alone.
"""

import csv
import sys

import psycopg

from szelveny import sources
from szelveny.config import CRS_METRIC, DATA_OUT, PG_DSN, ROOT
from szelveny.lrs import check_checkpoints

CHECKPOINT_TARGET_M = 20  # ROADMAP Phase 3: ±20 m on official sections

# NAP kutka -> (szelveny.road.category, NAP's class name)
KUTKA = {
    "1": ("M", "autópálya"),
    "2": ("M", "autóút"),
    "3": ("F", "I. rendű főút"),
    "4": ("F", "II. rendű főút"),
    "5": ("OK", "összekötő út"),
    "6": ("BK", "bekötő út"),
    "7": ("BK", "állomáshoz vezető bekötő út"),
    "8": ("ramp", "autópálya vagy autóút csomóponti ága"),
    "9": ("ramp", "egyéb csomóponti ág"),
    "K": ("cycle", "kerékpárút"),
    "P": ("rest", "gyorsforgalmi utak pihenő útja"),
}

# NAP szelvény '20 + 1007' -> 21.007 km (hungary.parse_szelveny in SQL).
KM_SQL = "(split_part({c}, '+', 1)::numeric + trim(split_part({c}, '+', 2))::numeric / 1000)"


def build(conn) -> None:
    kutka_values = ", ".join(f"('{k}', '{c}', '{n}')" for k, (c, n) in KUTKA.items())
    kf, kt = KM_SQL.format(c="h.kszelv"), KM_SQL.format(c="h.vszelv")
    params = {"date": _nap_date(), "licence": _nap_licence()}
    conn.execute(f"""
        CREATE TEMP TABLE kutka (kutka text PRIMARY KEY, category text, class text);
        INSERT INTO kutka VALUES {kutka_values};

        CREATE TEMP TABLE sec AS
        SELECT h.id, h.kszam, h.pkod::smallint AS carriageway, h.kutka, h.kszelv, h.vszelv,
               {kf} AS f, {kt} AS t, h.szho, h.szak_tipus, h.szak_street,
               n.utnev, n.kezelo, ST_LineMerge(h.geom) AS geom
        FROM raw.nap_halozat h LEFT JOIN raw.nap_kozut_nev n USING (id)
        WHERE h.geom IS NOT NULL AND h.kszam IS NOT NULL;

        DELETE FROM szelveny.road WHERE source_id = 'nap';

        -- One road per number. Category and class: the kutka with the most length (road 8 is
        -- mostly first-class main road with an expressway stretch). Name and administrator:
        -- the most common. Official length: undivided plus right-carriageway sections.
        INSERT INTO szelveny.road (ref, category, road_class, administrator, county, route_desc,
                                   km_from_official, km_to_official, length_official, source_id)
        SELECT s.kszam, k.category, k.class, adm.kezelo, NULL, name.utnev,
               round(min(s.f), 3), round(max(s.t), 3),
               round(sum(s.szho) FILTER (WHERE s.carriageway IN (0, 1)) / 1000.0, 3), 'nap'
        FROM sec s
        JOIN LATERAL (SELECT kutka FROM sec x WHERE x.kszam = s.kszam
                      GROUP BY kutka ORDER BY sum(szho) DESC, kutka LIMIT 1) main ON true
        JOIN kutka k ON k.kutka = main.kutka
        LEFT JOIN LATERAL (SELECT utnev FROM sec x WHERE x.kszam = s.kszam AND utnev IS NOT NULL
                           GROUP BY utnev ORDER BY count(*) DESC LIMIT 1) name ON true
        LEFT JOIN LATERAL (SELECT kezelo FROM sec x WHERE x.kszam = s.kszam AND kezelo IS NOT NULL
                           GROUP BY kezelo ORDER BY sum(szho) DESC LIMIT 1) adm ON true
        GROUP BY s.kszam, k.category, k.class, adm.kezelo, name.utnev;

        INSERT INTO szelveny.road_segment (road_id, sequence, km_from, km_to, geom, km_quality,
                                           carriageway, km_from_text, km_to_text, shared_with,
                                           source_ref, source_id)
        SELECT r.road_id,
               row_number() OVER (PARTITION BY r.road_id ORDER BY s.carriageway, s.f, s.t, s.id),
               round(s.f, 3), round(s.t, 3), s.geom, 'official', s.carriageway,
               s.kszelv, s.vszelv, CASE WHEN s.szak_tipus = 2 THEN s.szak_street END,
               s.id::text, 'nap'
        FROM sec s JOIN szelveny.road r ON r.ref = s.kszam AND r.source_id = 'nap'
        WHERE ST_GeometryType(s.geom) = 'ST_LineString' AND s.t >= s.f;
    """)
    # The administrator along the road, where it differs between sections (M0, shared
    # motorway stretches): one event per section.
    conn.execute("""
        INSERT INTO szelveny.attribute_event (road_id, km_from, km_to, type, value, carriageway,
                                              source_id, source_date, licence)
        SELECT r.road_id, round(s.f, 3), round(s.t, 3), 'administrator', s.kezelo, s.carriageway,
               'nap', %(date)s, %(licence)s
        FROM sec s JOIN szelveny.road r ON r.ref = s.kszam AND r.source_id = 'nap'
        WHERE s.kezelo IS NOT NULL AND s.t >= s.f
    """, params)


def _nap_manifest() -> dict:
    m = sources.read_manifest("nap")
    if not m:
        raise SystemExit("reference: no NAP manifest, run `make nap` first")
    return m


def _nap_date() -> str:
    m = _nap_manifest()
    pkg = next(p for p in m["packages"] if p["slug"] == "halozat")
    return pkg["download"]["retrieved_at"][:10]


def _nap_licence() -> str:
    return _nap_manifest()["licence"]


def report(conn) -> list[tuple]:
    """Per category: roads, official km, mapped km (EOV), and sections."""
    return conn.execute(f"""
        SELECT r.category, count(*), round(sum(r.length_official)), round(sum(m.km)), sum(m.n)
        FROM szelveny.road r
        CROSS JOIN LATERAL (
            SELECT (sum(ST_Length(ST_Transform(s.geom, {CRS_METRIC})))
                    FILTER (WHERE s.carriageway IN (0, 1)) / 1000)::numeric AS km, count(*) AS n
            FROM szelveny.road_segment s WHERE s.road_id = r.road_id) m
        WHERE r.source_id = 'nap'
        GROUP BY 1 ORDER BY array_position(ARRAY['M','F','OK','BK','ramp','cycle','rest'], r.category)
    """).fetchall()


def main() -> int:
    with psycopg.connect(PG_DSN) as conn:
        conn.execute((ROOT / "pipeline" / "sql" / "20_reference.sql").read_text())
        lrs_sql = (ROOT / "pipeline" / "sql" / "10_lrs.sql").read_text()
        conn.execute(lrs_sql.replace("{CRS_METRIC}", str(CRS_METRIC)))  # locate(), measure()
        sources.register(conn, _nap_manifest())
        build(conn)
        conn.commit()
        rows = report(conn)
        checkpoints = check_checkpoints(conn)
    print(f"{'cat':<6}{'roads':>7}{'official km':>13}{'mapped km':>11}{'sections':>10}")
    for cat, n, okm, mkm, segs in rows:
        print(f"{cat:<6}{n:>7}{okm or 0:>13}{mkm or 0:>11}{segs:>10}")

    out = DATA_OUT / "lrs"
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "checkpoints.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(checkpoints[0]) if checkpoints else ["road"])
        w.writeheader()
        w.writerows(checkpoints)
    fails = [c for c in checkpoints if c["error_m"] is None or c["error_m"] > CHECKPOINT_TARGET_M]
    print(f"checkpoints: {len(checkpoints) - len(fails)}/{len(checkpoints)} within {CHECKPOINT_TARGET_M} m")
    for c in fails:
        err = "not found" if c["error_m"] is None else f"{c['error_m']:.0f} m"
        print(f"  ✗ {c['road']} km {c['km']}: {err}  {c['note']}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
