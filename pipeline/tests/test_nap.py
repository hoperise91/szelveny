import json
import sqlite3
from datetime import date

from szelveny import nap


def _manifest(tmp_path, monkeypatch, download=None):
    monkeypatch.setattr(nap, "DATA_RAW", tmp_path)
    monkeypatch.setattr(nap, "MANIFEST", tmp_path / "nap" / "manifest.json")
    (tmp_path / "nap").mkdir()
    pkg = {"nap_id": 68, "slug": "pihenoparkolok", "name": "Pihenőparkolók",
           "wfs_layer": "nhp_208622538:wfs_208622538"}
    if download:
        pkg["download"] = download
    nap.save_manifest({"source_id": "nap", "packages": [pkg]})
    return pkg


def test_package_url_is_the_documented_gpkg_link():
    url = nap.package_url("nhp_21616037:wfs_21616037")
    assert url.startswith("https://napphub.kozut.hu/geoserver/nhp_21616037/wfs_21616037/ows?")
    assert "typeNames=nhp_21616037:wfs_21616037" in url and url.endswith("outputFormat=gpkg")


def test_weekly_guard(tmp_path, monkeypatch):
    pkg = _manifest(tmp_path, monkeypatch, {"file": "pihenoparkolok/pihenoparkolok.gpkg",
                                             "retrieved_at": "2026-11-24T12:00:00+00:00"})
    assert nap.is_due(pkg, date(2026, 11, 25))            # no file on disk: due
    f = tmp_path / "nap" / "pihenoparkolok" / "pihenoparkolok.gpkg"
    f.parent.mkdir()
    f.write_bytes(b"x")
    assert not nap.is_due(pkg, date(2026, 11, 30))        # 6 days: not yet
    assert nap.is_due(pkg, date(2026, 12, 1))             # 7 days: due


def test_failed_fetch_keeps_last_good_copy(tmp_path, monkeypatch):
    old = {"file": "pihenoparkolok/pihenoparkolok.gpkg", "retrieved_at": "2026-11-01T00:00:00+00:00",
           "sha256": "abc", "features": 247}
    _manifest(tmp_path, monkeypatch, old)

    def boom(pkg):
        raise RuntimeError("HTTP 503")
    monkeypatch.setattr(nap, "fetch", boom)
    assert nap.download() == ["pihenoparkolok"]
    kept = json.loads((tmp_path / "nap" / "manifest.json").read_text())["packages"][0]["download"]
    assert kept == old


def test_feature_count_of_a_geopackage(tmp_path):
    db = tmp_path / "t.gpkg"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
        con.execute("INSERT INTO gpkg_contents VALUES ('wfs_1', 'features')")
        con.execute("CREATE TABLE wfs_1 (id INTEGER)")
        con.executemany("INSERT INTO wfs_1 VALUES (?)", [(1,), (2,)])
    assert nap._features(db) == 2
