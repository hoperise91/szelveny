import copy
import json

from szelveny import refresh


def _summary(**roads):
    return {
        "date": "2026-11-16",
        "sources": {"osm": "2026-11-15", "nap": "2026-11-24"},
        "accuracy": {"median_m": 12.0, "within_50m_share": 0.88},
        "checkpoints": [{"road": "12", "km": "0.000", "error_m": 0.0}],
        "roads": {k: {"ref": k, "county": None, "cat": v[0], "status": "ok", "km": v[1],
                      "hash": f"{k}{v[1]}"} for k, v in roads.items()},
    }


PREV = _summary(M1=("M", 170.0), **{"1": ("F", 190.0), "12": ("F", 60.0)})


def test_baseline_has_no_changes_and_passes():
    assert refresh.compare(None, PREV) == {"baseline": True, "roads": 3}
    assert refresh.check(None, PREV) == []


def test_new_motorway_section_is_reported():
    cur = copy.deepcopy(PREV)
    cur["date"] = "2026-11-23"
    cur["sources"]["osm"] = "2026-11-22"
    cur["roads"]["M1"].update(km=198.0, hash="new")
    cur["roads"]["M8"] = {"ref": "M8", "county": None, "cat": "M", "status": "osm_only",
                          "km": 30.0, "hash": "m8"}
    ch = refresh.compare(PREV, cur)
    assert [r["key"] for r in ch["added"]] == ["M8"]
    assert ch["length"] == [{"key": "M1", "from": 170.0, "to": 198.0}]
    assert ch["sources"] == {"osm": {"from": "2026-11-15", "to": "2026-11-22"}}
    assert refresh.check(PREV, cur) == []
    text = refresh.report(cur, ch, [], None)
    assert text.startswith("**Published**") and "New: **M8**" in text and "+28.0 km" in text


def test_broken_motorway_is_held_back():
    cur = copy.deepcopy(PREV)
    cur["roads"]["M1"].update(km=120.0, hash="broken")
    problems = refresh.check(PREV, cur)
    assert any("M1" in p for p in problems)
    assert refresh.report(cur, refresh.compare(PREV, cur), problems, None).startswith("**Held back**")


def test_failed_checkpoint_and_lost_roads_are_held_back():
    cur = copy.deepcopy(PREV)
    cur["checkpoints"][0]["error_m"] = 75.0
    del cur["roads"]["12"]
    problems = refresh.check(PREV, cur)
    assert any("checkpoint 12" in p for p in problems)
    assert any("road count" in p for p in problems)


def test_main_writes_summary_and_history_only_when_published(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "roads").mkdir(parents=True)
    (web / "roads" / "M1.json").write_text(json.dumps(
        {"key": "M1", "ref": "M1", "county": None, "category": "M",
         "lrs": {"status": "ok", "path_km": 170.0}}))
    (web / "sources.json").write_text(json.dumps([{"id": "osm", "version": "2026-11-22"}]))
    (web / "accuracy.json").write_text(json.dumps({"median_m": 12.0, "within_50m_share": 0.88}))
    monkeypatch.setattr(refresh, "WEB_DATA", web)
    monkeypatch.setattr(refresh, "OUT", tmp_path / "out")
    summarize = refresh.summarize
    monkeypatch.setattr(refresh, "summarize", lambda: summarize(web, tmp_path / "lrs"))
    monkeypatch.setattr(refresh, "_fetch_json", lambda url: None)
    monkeypatch.setattr(refresh, "watch", list)
    assert refresh.main([]) == 0
    assert json.loads((web / "changes.json").read_text())[0]["baseline"] is True
    assert "M1" in json.loads((web / "meta" / "summary.json").read_text())["roads"]
    assert (tmp_path / "out" / "status").read_text() == "published\n"


def test_waiting_sources_are_listed_but_never_hold_back():
    waiting = [{"name": "TENtec", "url": "https://t.example", "back": False, "detail": "HTTP 503"},
               {"name": "EuroGeographics", "url": "https://a.example", "back": True, "detail": "HTTP 200"}]
    text = refresh.report(PREV, refresh.compare(None, PREV), [], None, waiting)
    assert text.startswith("**Published**")
    assert "TENtec](https://t.example): still down (HTTP 503)" in text
    assert "EuroGeographics](https://a.example): **back online**" in text


def test_saved_copies_are_named_in_the_note(tmp_path, monkeypatch):
    from szelveny import sources
    monkeypatch.setattr(sources, "DATA_RAW", tmp_path / "raw")
    sources.write_manifest("osm", name="OSM", url="u", licence="ODbL-1.0", attribution="a",
                           version="2026-11-22", files=["hungary-latest.osm.pbf"])
    (tmp_path / "fallbacks").write_text("osm\n")
    saved = refresh.fallbacks(tmp_path)
    assert saved[0]["id"] == "osm" and saved[0]["version"] == "2026-11-22"
    text = refresh.report(PREV, refresh.compare(None, PREV), [], None, (), saved)
    assert "### Saved copies used" in text and "osm: version 2026-11-22" in text


def test_osm_only_road_entering_the_official_network_is_announced():
    prev = _summary(M1=("M", 170.0), M85=("M", 6.1))
    prev["roads"]["M85"]["status"] = "osm_only"
    cur = copy.deepcopy(prev)
    cur["roads"]["M85"].update(status="ok", hash="official")
    ch = refresh.compare(prev, cur)
    assert ch["official"] == ["M85"] and ch["status"] == []
    text = refresh.report(cur, ch, [], None)
    assert "**M85 is now in the official network**" in text
    assert "Waiting for official data" not in text
    still = refresh.report(prev, refresh.compare(None, prev), [], None)
    assert "1 roads are in OpenStreetMap but not in the official network" in still and "M85" in still


def test_missing_accuracy_is_reported_not_fatal():
    cur = copy.deepcopy(PREV)
    cur["accuracy"] = {}
    assert refresh.check(PREV, cur) == []
    assert "not measured this run" in refresh.report(cur, refresh.compare(PREV, cur), [], None)
