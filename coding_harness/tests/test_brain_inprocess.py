"""The in-process brain backend over a temp slos-recall index, and backend selection."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("slos_recall")

from coding_harness.context import brain, brain_inprocess  # noqa: E402
from coding_harness.core.settings import SettingsError, load_settings  # noqa: E402
from coding_harness.tests import brain_db, fake_brain  # noqa: E402

QUERIES = ("larkspur", "budget", "launch plan", "clinic visit")


@pytest.fixture()
def block(tmp_path, monkeypatch):
    return brain_db.block(tmp_path, monkeypatch)


def _client(block, **overrides) -> brain.BrainClient:
    from slos_recall import testing

    merged = {**block, **overrides}
    backend = brain_inprocess.InProcessBackend(merged, embed_fn=testing.hash_embed)
    return brain.BrainClient(merged, backend=backend)


def test_search_returns_hits_with_tiers(block) -> None:
    hits = _client(block).search("larkspur")
    assert {h.path: h.to_dict()["tier"] for h in hits} == {
        "startup/plan.md": 1, "finance/budget.md": 2, "health/visit.md": 3, "life/unlabeled.md": 3}
    assert all(h.snippet.startswith("Larkspur") for h in hits)


def test_vault_filter_and_page(block) -> None:
    client = _client(block)
    assert [h.path for h in client.search("larkspur", vault="finance")] == ["finance/budget.md"]
    page = client.page("finance/budget.md")
    assert page["sensitivity"] == "confidential" and "1200" in page["content"]


def test_clearance_one_never_sees_confidential_or_restricted(block) -> None:
    client = _client(block, agent_id="low-agent")
    for query in QUERIES:
        assert {h.sensitivity for h in client.search(query)} <= {"public", "internal"}
    assert [h.path for h in client.search("larkspur")] == ["startup/plan.md"]


def test_unknown_agent_is_refused(block) -> None:
    with pytest.raises(brain.BrainError, match="unknown brain identity"):
        _client(block, agent_id="stranger").search("larkspur")


def test_page_above_clearance_and_missing_page_look_the_same(block) -> None:
    low = _client(block, agent_id="low-agent")
    for path in ("health/visit.md", "nope.md"):
        with pytest.raises(brain.BrainError, match="^not found$"):
            low.page(path)


def test_an_embedder_that_raises_falls_back_to_keywords(block) -> None:
    def broken(text):
        raise ConnectionError("ollama down")

    client = brain.BrainClient(block, backend=brain_inprocess.InProcessBackend(block, embed_fn=broken))
    assert [h.path for h in client.search("budget")] == ["finance/budget.md"]
    assert any("keyword search only" in w for w in client.last_warnings)


def test_a_slow_embedder_times_out_to_keywords(block) -> None:
    def slow(text):
        time.sleep(2)

    backend = brain_inprocess.InProcessBackend({**block, "embed_timeout_s": 0.1}, embed_fn=slow)
    warnings: list[str] = []
    assert [h.path for h in backend.search("budget", top_k=5, warnings=warnings)] == ["finance/budget.md"]
    assert any("timed out" in w for w in warnings)


def test_errors_name_no_absolute_path(block, tmp_path) -> None:
    with pytest.raises(brain.BrainError) as err:
        _client(block, db=str(tmp_path / "gone.db")).search("larkspur")
    assert str(tmp_path) not in str(err.value)
    assert brain_inprocess._scrub("open /a/b/c.db failed for x/y.md") == "open c.db failed for x/y.md"


def test_scrub_removes_whole_paths_with_spaces() -> None:
    quoted = brain_inprocess._scrub("No such file: '/tmp/my notes/a b.db' (errno 2)")
    assert quoted == "No such file: 'a b.db' (errno 2)"
    home = brain_inprocess._scrub("cannot open /Users/someone/second brain/idx.db")
    assert "/" not in home and "second brain" not in home and "someone" not in home


def test_index_age_comes_from_the_meta_stamp(block) -> None:
    assert _client(block).backend.age_hours() == pytest.approx(1.0, abs=0.05)


@pytest.mark.parametrize(("extra", "expected"), [
    ({"backend": "mcp"}, brain.MCPBackend),
    ({"backend": "inprocess"}, brain_inprocess.InProcessBackend),
    ({}, brain_inprocess.InProcessBackend),
])
def test_backend_selection(block, extra, expected) -> None:
    merged = {k: v for k, v in block.items() if k != "backend"}
    if expected is brain.MCPBackend:
        merged["command"] = sys.executable
    assert isinstance(brain.backend_for({**merged, **extra}), expected)


def test_a_command_without_backend_stays_on_mcp(tmp_path) -> None:
    assert isinstance(brain.backend_for(fake_brain.block(tmp_path)), brain.MCPBackend)


def test_inprocess_without_the_package_is_an_error_not_a_fallback(block, monkeypatch) -> None:
    monkeypatch.setattr(brain_inprocess.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(brain.BrainError, match="not importable"):
        brain.backend_for(block)
    no_backend = {k: v for k, v in block.items() if k != "backend"}
    with pytest.raises(brain.BrainError, match="command"):
        brain.backend_for(no_backend)


def test_a_missing_library_module_is_a_brain_error(block, monkeypatch) -> None:
    def fail(name):
        raise ImportError("no numpy", name="numpy")

    backend = brain_inprocess.InProcessBackend(block)
    monkeypatch.setattr(brain_inprocess.importlib, "import_module", fail)
    with pytest.raises(brain.BrainError, match="not importable"):
        backend.search("larkspur", top_k=3)


def test_settings_reject_an_unknown_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_SETTINGS", "on")
    user = tmp_path / "settings.json"
    monkeypatch.setattr("coding_harness.core.settings.USER_SETTINGS", user)
    user.write_text(json.dumps({"brain": {"backend": "http"}}))
    with pytest.raises(SettingsError, match="brain.backend"):
        load_settings(str(tmp_path))
    user.write_text(json.dumps({"brain": {"backend": "inprocess", "agent_id": "a"}}))
    assert load_settings(str(tmp_path)).brain["backend"] == "inprocess"


def test_harness_runs_without_slos_recall() -> None:
    code = (
        "import sys; sys.modules['slos_recall'] = None\n"
        "import coding_harness.cli\n"
        "from coding_harness.context import brain\n"
        "from coding_harness.modes.print_mode import build_registry\n"
        "assert 'Brain' not in build_registry(enable_mcp=False).tools\n"
        "try:\n"
        "    brain.backend_for({'backend': 'inprocess', 'agent_id': 'a'})\n"
        "except brain.BrainError as e:\n"
        "    print('refused:', e)\n"
        "print('numpy loaded:', 'numpy' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "refused:" in out.stdout and "numpy loaded: False" in out.stdout


def _pairs(client: brain.BrainClient, local: bool) -> set[tuple[str, int]]:
    """(path, tier) over the four queries after the harness's own tier cap."""
    cap = brain.allowed_tier(local)
    return {(h.path, brain.tier_of(h.sensitivity))
            for q in QUERIES for h in client.search(q, top_k=8)
            if brain.tier_of(h.sensitivity) <= cap}


def test_parity_between_mcp_and_inprocess(block, tmp_path) -> None:
    mcp = brain.BrainClient(fake_brain.block(tmp_path), backend=brain.MCPBackend(fake_brain.block(tmp_path)))
    inproc = _client(block)
    try:
        for local in (True, False):
            assert _pairs(mcp, local) == _pairs(inproc, local)
        assert len(_pairs(inproc, True)) == 4 and _pairs(inproc, False) == {("startup/plan.md", 1)}
    finally:
        mcp.close()


def _sleepy(seconds: float, started: threading.Event | None = None):
    """An embedder that takes ``seconds`` and then returns the hash vector."""
    from slos_recall import testing

    def run(text):
        if started is not None:
            started.set()
        time.sleep(seconds)
        return testing.hash_embed(text)

    return run


def test_concurrent_searches_do_not_queue_behind_the_embedder(block) -> None:
    client = brain.BrainClient(block, backend=brain_inprocess.InProcessBackend(
        block, embed_fn=_sleepy(1.0)))
    t0 = time.monotonic()
    workers = [threading.Thread(target=client.search, args=("budget",)) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert time.monotonic() - t0 < 1.6


def test_age_answers_while_a_search_is_embedding(block) -> None:
    started = threading.Event()
    backend = brain_inprocess.InProcessBackend(block, embed_fn=_sleepy(1.0, started))
    worker = threading.Thread(target=backend.search, args=("budget",), kwargs={"top_k": 3})
    worker.start()
    assert started.wait(2.0)
    t0 = time.monotonic()
    assert backend.age_hours() == pytest.approx(1.0, abs=0.05)
    assert time.monotonic() - t0 < 0.3
    worker.join()


def test_a_hung_embedder_strands_at_most_one_thread(block) -> None:
    release = threading.Event()
    calls: list[str] = []

    def hung(text):
        calls.append(text)
        release.wait(10)

    backend = brain_inprocess.InProcessBackend({**block, "embed_timeout_s": 0.1}, embed_fn=hung)
    try:
        notes: list[list[str]] = []
        for _ in range(6):
            w: list[str] = []
            t0 = time.monotonic()
            assert [h.path for h in backend.search("budget", top_k=3, warnings=w)] == ["finance/budget.md"]
            notes.append(w)
            if len(notes) > 1:
                assert time.monotonic() - t0 < 0.1
        assert len(calls) == 1
        assert any("timed out" in x for x in notes[0])
        assert all(any("keyword search only" in x for x in w) for w in notes[1:])
    finally:
        release.set()


def test_a_successful_embed_after_the_cooldown_clears_it(block, monkeypatch) -> None:
    monkeypatch.setattr(brain_inprocess, "EMBED_COOLDOWN_S", 0.3)
    slow = {"left": 1}

    def once_slow(text):
        from slos_recall import testing

        if slow["left"]:
            slow["left"] -= 1
            time.sleep(0.2)
        return testing.hash_embed(text)

    backend = brain_inprocess.InProcessBackend({**block, "embed_timeout_s": 0.05}, embed_fn=once_slow)
    first: list[str] = []
    backend.search("budget", top_k=3, warnings=first)
    assert any("timed out" in w for w in first)
    time.sleep(0.4)
    later: list[str] = []
    backend.search("budget", top_k=3, warnings=later)
    assert later == [] and backend._cooldown_until == 0.0


def test_warnings_belong_to_the_latest_call(block) -> None:
    def broken(text):
        raise ConnectionError("down")

    backend = brain_inprocess.InProcessBackend(block, embed_fn=broken)
    client = brain.BrainClient(block, backend=backend)
    client.search("budget")
    assert client.last_warnings
    backend.embed_fn = _sleepy(0.0)
    client.search("budget")
    assert client.last_warnings == []
    backend.embed_fn = broken
    client.search("budget")
    backend.agent_id = "stranger"
    with pytest.raises(brain.BrainError):
        client.search("budget")
    assert client.last_warnings == []


def test_probe_checks_the_identity(block) -> None:
    assert _client(block).probe() == pytest.approx(1.0, abs=0.05)
    with pytest.raises(brain.BrainError, match="unknown brain identity"):
        _client(block, agent_id="stranger").probe()
