"""NAP (napportal.kozut.hu): download the subscribed static packages and load them into raw.nap_*.

Usage: python -m szelveny.nap [--load-only] [package-slug ...]    (default: all subscribed)

Each package is fetched from the GeoPackage link NAP documents on its metadata page
("Adatelérés"; the owner approved calling these links directly, 2026-11-24). Rules (CLAUDE.md,
NAP ÁSZF §11, §37, §38):
  - at most once a week per package (the server sends no ETag/Last-Modified, so the guard is
    the retrieval date in data/raw/nap/manifest.json);
  - one request at a time, a pause between packages, retries with backoff;
  - a failed or empty download never replaces the last good copy;
  - no other GeoServer queries (no filters, no other layers).

The manifest (licence, terms, per-package metadata and licence check) is kept by hand; this
module only updates each package's `download` record and the top-level version/files.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, date, datetime

import psycopg

from szelveny import sources
from szelveny.config import CRS_STORAGE, DATA_RAW, PG_DSN
from szelveny.download import http

MANIFEST = DATA_RAW / "nap" / "manifest.json"
WFS = ("https://napphub.kozut.hu/geoserver/{ws}/{name}/ows?service=wfs&version=1.0.0"
       "&request=GetFeature&resultType=results&typeNames={ws}:{name}"
       "&format_options=CHARSET:UTF-8&outputFormat=gpkg")
USER_AGENT = "szelveny-pipeline (+https://szelveny.hu; contact@szelveny.hu)"
MIN_AGE_DAYS = 7      # ÁSZF §37: fetch each static package at most once a week
PAUSE_S = 5           # between packages


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save_manifest(m: dict) -> None:
    MANIFEST.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def package_url(layer: str) -> str:
    """'nhp_21616037:wfs_21616037' -> the package's documented GeoPackage link."""
    ws, name = layer.split(":")
    return WFS.format(ws=ws, name=name)


def is_due(pkg: dict, today: date) -> bool:
    """True when the package has no good local copy or the copy is at least a week old."""
    dl = pkg.get("download")
    if not dl or not (DATA_RAW / "nap" / dl["file"]).exists():
        return True
    return (today - date.fromisoformat(dl["retrieved_at"][:10])).days >= MIN_AGE_DAYS


def _features(path) -> int:
    """Feature count of a single-layer GeoPackage (raises if the file isn't one)."""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        (table,) = con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'").fetchone()
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(pkg: dict) -> dict:
    """Download one package; returns its new `download` record. Keeps the old file on failure."""
    slug = pkg["slug"]
    out = DATA_RAW / "nap" / slug / f"{slug}.gpkg"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with http.get(package_url(pkg["wfs_layer"]), headers={"User-Agent": USER_AGENT},
                  stream=True, timeout=600) as r:
        if r.status_code in (401, 403):
            # Never work around access controls (CLAUDE.md): stop and tell the owner.
            raise SystemExit(f"nap: {slug}: HTTP {r.status_code}, access refused; not retrying")
        r.raise_for_status()
        if "geopackage" not in r.headers.get("Content-Type", ""):
            raise RuntimeError(f"nap: {slug}: unexpected {r.headers.get('Content-Type')}")
        with open(tmp, "wb") as f:
            f.writelines(r.iter_content(1 << 20))
    n = _features(tmp)
    if n == 0:
        tmp.unlink()
        raise RuntimeError(f"nap: {slug}: empty package, keeping the last good copy")
    sha = _sha256(tmp)
    old = pkg.get("download") or {}
    tmp.replace(out)
    return {
        "file": f"{slug}/{slug}.gpkg",
        "format": "GeoPackage",
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "bytes": out.stat().st_size,
        "sha256": sha,
        "features": n,
        "changed": old.get("sha256") != sha,
    }


def download(slugs: list[str] | None = None) -> list[str]:
    """Fetch the due packages; returns the slugs that failed (their last good copy stays)."""
    m = load_manifest()
    today = date.today()
    failed, fetched = [], 0
    for pkg in m["packages"]:
        if slugs and pkg["slug"] not in slugs:
            continue
        if not is_due(pkg, today):
            print(f"nap: {pkg['slug']}: up to date ({pkg['download']['retrieved_at'][:10]})")
            continue
        if fetched:
            time.sleep(PAUSE_S)
        fetched += 1
        print(f"nap: {pkg['slug']}: fetching {pkg['name']}…", flush=True)
        try:
            pkg["download"] = fetch(pkg)
        except (OSError, RuntimeError, TypeError, sqlite3.Error) as e:
            print(f"nap: {pkg['slug']}: FAILED ({e}); keeping the last good copy", file=sys.stderr)
            failed.append(pkg["slug"])
            continue
        d = pkg["download"]
        print(f"nap: {pkg['slug']}: {d['features']:,} features, {d['bytes'] / 1e6:.1f} MB"
              f"{'' if d['changed'] else ', unchanged'}")
        save_manifest(m)  # after every package, so an interrupted run keeps what it got
    present = [p for p in m["packages"] if p.get("download")]
    m["files"] = [p["download"]["file"] for p in present]
    m["version"] = max((p["download"]["retrieved_at"][:10] for p in present), default=None)
    m["retrieved_at"] = m["version"]
    save_manifest(m)
    return failed


def load(conn, slugs: list[str] | None = None) -> None:
    """Replace raw.nap_<slug> with each package's last good copy (geometry in EPSG:4326)."""
    m = load_manifest()
    for pkg in m["packages"]:
        if (slugs and pkg["slug"] not in slugs) or not pkg.get("download"):
            continue
        table = f"raw.nap_{pkg['slug']}"
        subprocess.run(
            ["ogr2ogr", "-f", "PostgreSQL", f"PG:{PG_DSN}", str(DATA_RAW / "nap" / pkg["download"]["file"]),
             "-nln", table, "-overwrite", "-lco", "GEOMETRY_NAME=geom", "-lco", "FID=fid",
             "-t_srs", f"EPSG:{CRS_STORAGE}", "-nlt", "PROMOTE_TO_MULTI"],
            check=True,
        )
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"nap: {table}: {n:,} rows (retrieved {pkg['download']['retrieved_at'][:10]})")
    sources.register(conn, m)
    conn.commit()


def main(argv: list[str]) -> int:
    load_only = "--load-only" in argv
    slugs = [a for a in argv if not a.startswith("--")] or None
    known = {p["slug"] for p in load_manifest()["packages"]}
    if slugs and (unknown := set(slugs) - known):
        print(f"unknown package(s): {', '.join(sorted(unknown))}; known: {', '.join(sorted(known))}",
              file=sys.stderr)
        return 2
    failed = [] if load_only else download(slugs)
    with psycopg.connect(PG_DSN) as conn:
        load(conn, slugs)
    if failed:
        print(f"nap: {len(failed)} package(s) not refreshed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
