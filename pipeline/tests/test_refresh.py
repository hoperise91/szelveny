import copy
import json

from borna import refresh


def _summary(**roads):
    return {
        "date": "2026-11-16",
        "sources": {"osm": "2026-11-15", "hg540": "2026-09-08"},
        "accuracy": {"median_m": 12.0, "within_50m_share": 0.88},
        "checkpoints": [{"road": "DN12", "km": "0.000", "error_m": 0.0}],
        "roads": {k: {"ref": k, "county": None, "cat": v[0], "status": "ok", "km": v[1],
                      "hash": f"{k}{v[1]}"} for k, v in roads.items()},
    }


PREV = _summary(A1=("A", 500.0), DN1=("DN", 640.0), DN12=("DN", 100.0))


def test_baseline_has_no_changes_and_passes():
    assert refresh.compare(None, PREV) == {"baseline": True, "roads": 3}
    assert refresh.check(None, PREV) == []


def test_new_motorway_section_is_reported():
    cur = copy.deepcopy(PREV)
    cur["date"] = "2026-11-23"
    cur["sources"]["osm"] = "2026-11-22"
    cur["roads"]["A1"].update(km=528.0, hash="new")
    cur["roads"]["A7"] = {"ref": "A7", "county": None, "cat": "A", "status": "osm_only",
                          "km": 30.0, "hash": "a7"}
    ch = refresh.compare(PREV, cur)
    assert [r["key"] for r in ch["added"]] == ["A7"]
    assert ch["length"] == [{"key": "A1", "from": 500.0, "to": 528.0}]
    assert ch["sources"] == {"osm": {"from": "2026-11-15", "to": "2026-11-22"}}
    assert refresh.check(PREV, cur) == []
    text = refresh.report(cur, ch, [], None)
    assert text.startswith("**Published**") and "New: **A7**" in text and "+28.0 km" in text


def test_broken_motorway_is_held_back():
    cur = copy.deepcopy(PREV)
    cur["roads"]["A1"].update(km=420.0, hash="broken")
    problems = refresh.check(PREV, cur)
    assert any("A1" in p for p in problems)
    assert refresh.report(cur, refresh.compare(PREV, cur), problems, None).startswith("**Held back**")


def test_failed_checkpoint_and_lost_roads_are_held_back():
    cur = copy.deepcopy(PREV)
    cur["checkpoints"][0]["error_m"] = 75.0
    del cur["roads"]["DN12"]
    problems = refresh.check(PREV, cur)
    assert any("checkpoint DN12" in p for p in problems)
    assert any("road count" in p for p in problems)


def test_main_writes_summary_and_history_only_when_published(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "roads").mkdir(parents=True)
    (web / "roads" / "DN1.json").write_text(json.dumps(
        {"key": "DN1", "ref": "DN1", "county": None, "category": "DN",
         "lrs": {"status": "ok", "path_km": 640.0}}))
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
    assert "DN1" in json.loads((web / "meta" / "summary.json").read_text())["roads"]
    assert (tmp_path / "out" / "status").read_text() == "published\n"


def test_waiting_sources_are_listed_but_never_hold_back():
    waiting = [{"name": "TENtec", "url": "https://t.example", "back": False, "detail": "HTTP 503"},
               {"name": "ANCPI", "url": "https://a.example", "back": True, "detail": "HTTP 200"}]
    text = refresh.report(PREV, refresh.compare(None, PREV), [], None, waiting)
    assert text.startswith("**Published**")
    assert "TENtec](https://t.example): still down (HTTP 503)" in text
    assert "ANCPI](https://a.example): **back online**" in text


def test_saved_copies_are_named_in_the_note(tmp_path, monkeypatch):
    from borna import sources
    monkeypatch.setattr(sources, "DATA_RAW", tmp_path / "raw")
    sources.write_manifest("hg540", name="HG 540", url="u", licence="public", attribution="a",
                           version="2026-09-08", files=["anexe.html"])
    (tmp_path / "fallbacks").write_text("hg540\n")
    saved = refresh.fallbacks(tmp_path)
    assert saved[0]["id"] == "hg540" and saved[0]["version"] == "2026-09-08"
    text = refresh.report(PREV, refresh.compare(None, PREV), [], None, (), saved)
    assert "### Saved copies used" in text and "hg540: version 2026-09-08" in text


def test_osm_only_road_entering_the_register_is_announced():
    prev = _summary(A1=("A", 500.0), VOF=("V", 6.1))
    prev["roads"]["VOF"]["status"] = "osm_only"
    cur = copy.deepcopy(prev)
    cur["roads"]["VOF"].update(status="ok", hash="official")
    ch = refresh.compare(prev, cur)
    assert ch["official"] == ["VOF"] and ch["status"] == []
    text = refresh.report(cur, ch, [], None)
    assert "**VOF is now in HG 540/2000**" in text
    assert "Waiting for official data" not in text
    still = refresh.report(prev, refresh.compare(None, prev), [], None)
    assert "1 national roads are open in OpenStreetMap" in still and "VOF" in still


def test_missing_accuracy_is_reported_not_fatal():
    cur = copy.deepcopy(PREV)
    cur["accuracy"] = {}
    assert refresh.check(PREV, cur) == []
    assert "not measured this run" in refresh.report(cur, refresh.compare(PREV, cur), [], None)
