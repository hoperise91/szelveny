from borna import sources


def test_manifest_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "DATA_RAW", tmp_path)
    m = sources.write_manifest("demo", name="Demo", url="https://example.org", licence="CC0-1.0",
                               attribution="Demo", version="2026-01-01", files=["a.bin"])
    assert sources.read_manifest("demo") == m
    assert not sources.files_present(m)
    (tmp_path / "demo" / "a.bin").write_bytes(b"x")
    assert sources.files_present(m)


def test_missing_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "DATA_RAW", tmp_path)
    assert sources.read_manifest("nope") is None
    assert not sources.files_present(None)
