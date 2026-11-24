"""Source manifests: every downloaded dataset records where it came from, when, and under
what licence. `download` writes data/raw/<source_id>/manifest.json; `importer` turns it
into a szelveny.source row."""

import json
from datetime import UTC, datetime
from pathlib import Path

from szelveny.config import DATA_RAW


def raw_dir(source_id: str) -> Path:
    d = DATA_RAW / source_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_manifest(source_id: str, *, name: str, url: str, licence: str, attribution: str,
                   version: str, files: list[str], **extra) -> dict:
    manifest = {
        "source_id": source_id,
        "name": name,
        "url": url,
        "licence": licence,
        "attribution": attribution,
        "version": version,  # data date or release of the dataset itself
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": files,
        **extra,
    }
    (raw_dir(source_id) / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest


def read_manifest(source_id: str) -> dict | None:
    path = DATA_RAW / source_id / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else None


def files_present(manifest: dict | None) -> bool:
    if not manifest:
        return False
    d = DATA_RAW / manifest["source_id"]
    return all((d / f).exists() for f in manifest["files"])


def register(conn, manifest: dict) -> None:
    """Upsert the manifest into szelveny.source."""
    conn.execute(
        """
        INSERT INTO szelveny.source (source_id, name, url, licence, attribution, retrieved_at, notes)
        VALUES (%(source_id)s, %(name)s, %(url)s, %(licence)s, %(attribution)s,
                %(retrieved_at)s, 'version ' || %(version)s)
        ON CONFLICT (source_id) DO UPDATE SET
            name = EXCLUDED.name, url = EXCLUDED.url, licence = EXCLUDED.licence,
            attribution = EXCLUDED.attribution, retrieved_at = EXCLUDED.retrieved_at,
            notes = EXCLUDED.notes
        """,
        manifest,
    )
