"""The reviewer bench keeps its label cache under the harness state root."""
import json

import review_bench


def test_labels_resolve_under_the_temp_meta_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_META_DIR", str(tmp_path / "meta"))
    assert review_bench.labels_path() == tmp_path / "meta" / "review-labels.json"


def test_refresh_writes_the_cache_under_meta_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_META_DIR", str(tmp_path / "meta"))
    monkeypatch.setattr(review_bench, "TRANSCRIPTS", str(tmp_path / "none" / "*.jsonl"))
    assert review_bench.load_labels(refresh=True) == []
    written = tmp_path / "meta" / "review-labels.json"
    assert json.loads(written.read_text()) == []
