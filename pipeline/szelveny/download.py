"""Fetch open sources into data/raw/<source_id>/ (NAP has its own module, nap.py).

Usage: python -m szelveny.download [source ...]    (default: all)
Each download is skipped when the local copy already matches the published version.
"""

import hashlib
import re
import socket
import sys
from email.utils import parsedate_to_datetime

import duckdb
import requests
import urllib3.util.connection
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from szelveny import sources
from szelveny.config import HU_BBOX

GEOFABRIK_URL = "https://download.geofabrik.de/europe/hungary-latest.osm.pbf"
OVERTURE_STAC = "https://stac.overturemaps.org/catalog.json"
OVERTURE_S3 = "s3://overturemaps-us-west-2/release/{release}/theme=transportation/type={type}/*"
GISCO = "https://gisco-services.ec.europa.eu/distribution/v2/"
KSH_STADAT = "https://www.ksh.hu/stadat_files/sza/hu/"
# STADAT tables for QA: national road length per county; road and rail network by surface.
KSH_TABLES = {"sza0039": "24.1.2.1. Az országos közutak hossza vármegye és régió szerint",
              "sza0030": "24.1.1.30. Út- és vasúthálózat"}


# IPv4 only: CI machines have no IPv6, and a failed IPv6 attempt otherwise hides the real error.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

# One session for every source: retries with backoff ride out brief network or server hiccups
# (public servers, CI runners), instead of failing the whole weekly run.
http = requests.Session()
http.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=5, status_forcelist=(429, 500, 502, 503, 504))))
http.mount("http://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=5, status_forcelist=(429, 500, 502, 503, 504))))


def _md5(path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_osm() -> None:
    out = sources.raw_dir("osm") / "hungary-latest.osm.pbf"
    # Geofabrik redirects "latest" (sometimes via a moved folder) to the dated extract
    # (hungary-261118.osm.pbf); only the dated file has an .md5 next to it, so follow every hop.
    url = GEOFABRIK_URL
    for _ in range(5):
        head = http.head(url, allow_redirects=False, timeout=30)
        if not head.is_redirect:
            break
        url = requests.compat.urljoin(url, head.headers["Location"])
    md5 = http.get(url + ".md5", timeout=30)
    md5.raise_for_status()
    remote_md5 = md5.text.split()[0]
    current = sources.read_manifest("osm")
    if sources.files_present(current) and current.get("md5") == remote_md5:
        print(f"osm: up to date ({current['version']})")
        return

    tmp = out.with_suffix(".part")
    with http.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        data_date = parsedate_to_datetime(r.headers["Last-Modified"]).date().isoformat()
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\rosm: {done / total:6.1%} of {total / 1e6:.0f} MB", end="", flush=True)
    print()
    if _md5(tmp) != remote_md5:
        tmp.unlink()
        raise RuntimeError("osm: md5 mismatch, download corrupted")
    tmp.rename(out)

    sources.write_manifest(
        "osm",
        name="OpenStreetMap (Geofabrik Hungary extract)",
        url=GEOFABRIK_URL,
        licence="ODbL-1.0",
        attribution="© OpenStreetMap contributors",
        version=data_date,
        files=[out.name],
        md5=remote_md5,
        terms_url="https://www.openstreetmap.org/copyright",
        terms_date="2026-11-24",
        terms_copy="docs/legal/OSM_copyright_2026-11-24.html",
    )
    print(f"osm: saved {out.name} ({data_date})")


def download_overture() -> None:
    release = http.get(OVERTURE_STAC, timeout=30).json()["latest"]
    current = sources.read_manifest("overture")
    if sources.files_present(current) and current["version"] == release:
        print(f"overture: up to date ({release})")
        return

    out_dir = sources.raw_dir("overture")
    xmin, ymin, xmax, ymax = HU_BBOX
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial; SET s3_region='us-west-2'")

    files = []
    for type_, where in (("segment", "subtype = 'road'"), ("connector", "TRUE")):
        out = out_dir / f"{type_}.parquet"
        tmp = out.with_suffix(".part")
        print(f"overture: fetching {type_}s for the Hungary bbox (release {release})…", flush=True)
        con.execute(f"""
            COPY (
                SELECT * EXCLUDE (theme, type)
                FROM read_parquet('{OVERTURE_S3.format(release=release, type=type_)}', hive_partitioning = 1)
                WHERE bbox.xmin <= {xmax} AND bbox.xmax >= {xmin}
                  AND bbox.ymin <= {ymax} AND bbox.ymax >= {ymin}
                  AND {where}
            ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)
        """)
        tmp.rename(out)
        n = con.execute(f"SELECT count(*) FROM '{out}'").fetchone()[0]
        print(f"overture: {type_}.parquet, {n:,} rows")
        files.append(out.name)

    sources.write_manifest(
        "overture",
        name="Overture Maps, transportation theme",
        url=f"https://docs.overturemaps.org/release/{release}/",
        licence="ODbL-1.0",
        attribution="© OpenStreetMap contributors, Overture Maps Foundation (incl. data from TomTom)",
        version=release,
        files=files,
        bbox=list(HU_BBOX),
        terms_url="https://docs.overturemaps.org/attribution/",
        terms_date="2026-11-24",
        terms_copy="docs/legal/Overture_attribution_2026-11-24.html",
    )




def _fetch_file(url: str, out) -> None:
    tmp = out.with_suffix(out.suffix + ".part")
    with http.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            f.writelines(r.iter_content(1 << 20))
    tmp.rename(out)


def download_gisco() -> None:
    # Newest NUTS and LAU releases, 1:1M polygons in EPSG:4326 (whole EU; Hungary is
    # selected at import).
    releases = {}
    for theme in ("nuts", "lau"):
        datasets = http.get(f"{GISCO}{theme}/datasets.json", timeout=30).json()
        releases[theme] = max(datasets).split("-")[1]  # 'nuts-2024' -> '2024'
    version = f"NUTS {releases['nuts']}, LAU {releases['lau']}"
    current = sources.read_manifest("gisco")
    if sources.files_present(current) and current["version"] == version:
        print(f"gisco: up to date ({version})")
        return

    files = []
    for theme, prefix in (("nuts", "NUTS_RG_01M"), ("lau", "LAU_RG_01M")):
        name = f"{prefix}_{releases[theme]}_4326.gpkg"
        print(f"gisco: fetching {name}…", flush=True)
        _fetch_file(f"{GISCO}{theme}/gpkg/{name}", sources.raw_dir("gisco") / name)
        files.append(name)

    sources.write_manifest(
        "gisco",
        name="Eurostat GISCO administrative units (NUTS, LAU)",
        url=GISCO,
        licence="Eurostat GISCO administrative units terms: non-commercial use, source acknowledged",
        attribution="© EuroGeographics for the administrative boundaries; Eurostat GISCO",
        version=version,
        files=files,
        non_commercial=True,
        owner_decision="2026-11-24: approved; Szelvény is not used commercially",
        terms_url="https://ec.europa.eu/eurostat/web/gisco/geodata/administrative-units",
        terms_date="2026-11-24",
        terms_copy="docs/legal/Eurostat_GISCO_administrative_units_2026-11-24.html",
    )
    print(f"gisco: saved {', '.join(files)}")




def download_ksh() -> None:
    # KSH publishes its website content under CC BY 4.0 (www.ksh.hu/copyright). The CSVs are
    # cp1250; the page carries the table's "Utolsó frissítés" (last update) date.
    updated = {}
    for table in KSH_TABLES:
        page = http.get(f"{KSH_STADAT}{table}.html", timeout=60)
        page.raise_for_status()
        m = re.search(r"Utols\S* friss\S*\s*(?:<[^>]+>\s*)*(\d{4})\. (\w+) (\d{1,2})\.",
                      page.content.decode("cp1250", "replace"))
        if not m:
            raise RuntimeError(f"ksh: {table}: last-update date not found, page layout changed?")
        updated[table] = " ".join(m.groups())
    version = "; ".join(f"{t} {d}" for t, d in updated.items())
    current = sources.read_manifest("ksh")
    if sources.files_present(current) and current["version"] == version:
        print(f"ksh: up to date ({version})")
        return
    files = []
    for table in KSH_TABLES:
        r = http.get(f"{KSH_STADAT}{table}.csv", timeout=60)
        r.raise_for_status()
        out = sources.raw_dir("ksh") / f"{table}.csv"
        out.write_text(r.content.decode("cp1250"), encoding="utf-8")
        files.append(out.name)
    sources.write_manifest(
        "ksh",
        name="KSH STADAT: " + "; ".join(KSH_TABLES.values()),
        url=KSH_STADAT,
        licence="CC-BY-4.0",
        attribution="Forrás: KSH (www.ksh.hu)",
        version=version,
        files=files,
        terms_url="https://www.ksh.hu/copyright",
        terms_date="2026-11-24",
        terms_copy="docs/legal/KSH_copyright_2026-11-24.html",
    )
    print(f"ksh: saved {', '.join(files)} ({version})")


SOURCES = {
    "osm": download_osm,
    "overture": download_overture,
    "gisco": download_gisco,
    "ksh": download_ksh,
}


def main(argv: list[str]) -> int:
    names = argv or list(SOURCES)
    unknown = set(names) - set(SOURCES)
    if unknown:
        print(f"unknown source(s): {', '.join(sorted(unknown))}; known: {', '.join(SOURCES)}",
              file=sys.stderr)
        return 2
    for name in names:
        SOURCES[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
