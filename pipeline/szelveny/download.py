"""Phase 1: fetch public sources into data/raw/<source_id>/.

Usage: python -m szelveny.download [source ...]    (default: all)
Each download is skipped when the local copy already matches the published version.
"""

import hashlib
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
        attribution="© OpenStreetMap contributors, Overture Maps Foundation",
        version=release,
        files=files,
        bbox=list(HU_BBOX),
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
        licence="Eurostat GISCO reuse policy (attribution required)",
        attribution="© EuroGeographics for the administrative boundaries; Eurostat GISCO",
        version=version,
        files=files,
    )
    print(f"gisco: saved {', '.join(files)}")




SOURCES = {
    "osm": download_osm,
    "overture": download_overture,
    "gisco": download_gisco,
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
