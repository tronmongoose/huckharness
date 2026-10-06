"""Hash-chained tool-call audit log for the coding harness.

Each tool dispatch (whether allowed or blocked by Sentinel) writes one line to
``<meta_dir>/audit.jsonl``. Lines are tamper-evident:
each entry's ``hash`` is sha256(prev_hash || canonical_json(payload)), so
modifying any historical entry breaks the chain at that point and every entry
after it.

This chain is **separate** from the authority hash chain in ``authority.db``
(this module never touches that). Visibility into the wider
system comes via two routes:

1. Every Sentinel subprocess invocation writes to
   ``its own log`` automatically (the hook does
   it). That's the existing tool-call telemetry the rest of the system reads.

2. After every audit append, this module also writes a short anchor line to
   ``<meta_dir>/anchors.jsonl`` containing
   ``{ts, session_id, seq, hash}``. External tools can spot-check the latest
   anchor against the chain head without parsing every entry.

Appends are serialized across processes by an flock over the read-head-then-write
sequence (see ``_chain_lock``). That day arrived: before the lock, two sessions
racing on 2026-08-22 both wrote seq 1300 and forked the live chain. A break that
already exists is never edited away — it is acknowledged in ``audit_breaks``.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_harness.core.paths import meta_dir
from coding_harness.security import audit_breaks

# ── Paths ────────────────────────────────────────────────────────────

META_DIR = meta_dir()
AUDIT_PATH = META_DIR / "audit.jsonl"
ANCHORS_PATH = META_DIR / "anchors.jsonl"
LOCK_PATH = META_DIR / "audit.lock"

GENESIS_HASH = "0" * 64

# ── Cross-process serialization ──────────────────────────────────────
#
# Every append reads the chain head and then writes a line derived from it.
# Without a lock spanning that pair, two processes read the same head and write
# the same seq — which is what forked the live chain at seq 1300 on 2026-08-22,
# exactly as this module's docstring predicted it would once more than one
# session ran at a time. flock is released by the kernel when the holder dies,
# so a crashed writer cannot wedge the chain.

CHAIN_LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.01


class ChainLockTimeout(RuntimeError):
    """Raised when the audit chain lock cannot be taken within the deadline."""


@contextmanager
def _chain_lock(timeout_s: float = CHAIN_LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold an exclusive lock over the read-head-then-write sequence.

    Fails closed: a writer that cannot serialize refuses rather than appending
    an entry that would fork the chain.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    with LOCK_PATH.open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ChainLockTimeout(
                        f"audit chain lock busy for {timeout_s}s ({LOCK_PATH})"
                    ) from None
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _chained(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize an append across processes. Every writer to the chain wears this."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _chain_lock():
            return fn(*args, **kwargs)
    return wrapper


# ── Hashing ──────────────────────────────────────────────────────────


def _canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON for hashing. Sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _hash_entry(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


# ── Ed25519 per-action signing (P3) ──────────────────────────────────
#
# The hash chain proves *nothing was rewritten*; a signature proves *who wrote
# it*. An attacker who can rewrite the whole file can recompute every hash, but
# cannot forge a signature without the agent's private seed. Signing is
# fail-soft and additive: rows written without a resolvable key stay unsigned
# and remain valid, so the entire pre-P3 chain verifies unchanged.
#
# Key material comes from an optional ``authority_runtime`` keystore: 32-byte
# Ed25519 seeds per agent name, public keys in AGENTS_YAML. The signature covers the canonical payload INCLUDING ``hash`` and
# excluding only ``sig``/``signer`` — so it binds the whole chain-linked entry.

_SIGN_EXCLUDE = ("sig", "signer")
AGENTS_YAML = Path(os.path.expanduser(os.environ.get("HARNESS_AGENTS_FILE", "~/.config/bjorn/agents.yaml")))

_keystore: Any = None
_keystore_tried = False
_signer_cache: dict[str, Any] = {}
_pubkeys: dict[str, str] | None = None


def _signable(payload: dict[str, Any]) -> str:
    return _canonical({k: v for k, v in payload.items() if k not in _SIGN_EXCLUDE})


def _get_keystore() -> Any:
    global _keystore, _keystore_tried
    if _keystore_tried:
        return _keystore
    _keystore_tried = True
    try:
        from authority_runtime.keys import AgentKeyStore
        _keystore = AgentKeyStore()
    except Exception:  # noqa: BLE001 — signing is optional; no keystore ⇒ unsigned
        _keystore = None
    return _keystore


def _identity_to_name(agent_identity: str | None) -> str | None:
    """``carryall:startup-agent#9+r5Tjdf`` → ``startup-agent``."""
    if not agent_identity:
        return None
    name = agent_identity
    if name.startswith("carryall:"):
        name = name[len("carryall:"):]
    name = name.split("#", 1)[0]
    return name or None


def _load_signing_key(name: str) -> Any:
    if name in _signer_cache:
        return _signer_cache[name]
    ks = _get_keystore()
    key = None
    if ks is not None:
        try:
            if ks.has_key(name):
                key = ks.load_signing_key(name)
        except Exception:  # noqa: BLE001 — missing/unreadable key ⇒ unsigned
            key = None
    _signer_cache[name] = key
    return key


def _maybe_sign(payload: dict[str, Any], agent_identity: str | None) -> None:
    """Add ``signer`` + ``sig`` to ``payload`` in place, if a key resolves.

    Must run AFTER ``payload['hash']`` is set (the signature binds the hash).
    Never raises into the append path — a signing failure leaves the row
    unsigned, which verify_chain accepts.
    """
    name = _identity_to_name(agent_identity)
    if not name:
        return
    key = _load_signing_key(name)
    if key is None:
        return
    try:
        sig = key.sign(_signable(payload).encode("utf-8")).signature.hex()
    except Exception:  # noqa: BLE001 — signing best-effort
        return
    payload["signer"] = name
    payload["sig"] = sig


def _pubkey_map() -> dict[str, str]:
    global _pubkeys
    if _pubkeys is not None:
        return _pubkeys
    _pubkeys = {}
    try:
        import yaml
        data = yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8")) or {}
        agents = data.get("agents") if isinstance(data.get("agents"), dict) else data
        if isinstance(agents, dict):
            for name, rec in agents.items():
                if isinstance(rec, dict) and rec.get("public_key"):
                    _pubkeys[name] = str(rec["public_key"])
    except Exception:  # noqa: BLE001 — no agents.yaml ⇒ signatures unverifiable
        pass
    return _pubkeys


def _arg_digest(args: Any) -> str:
    """Short content hash of tool args. Used so the audit doesn't have to store
    full payloads (which can be huge for bash output) but a tampered arg is
    still detectable."""
    return hashlib.sha256(_canonical(args if isinstance(args, dict) else {"_": args}).encode("utf-8")).hexdigest()[:16]


def _result_digest(result: Any) -> str:
    blob = result if isinstance(result, str) else json.dumps(result, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:16]


# ── Chain head ───────────────────────────────────────────────────────


def _read_head() -> tuple[str, int]:
    """Return (prev_hash, next_seq) from the tail of the audit file.

    On a fresh install (no file yet) returns (GENESIS_HASH, 0)."""
    if not AUDIT_PATH.exists():
        return GENESIS_HASH, 0
    last_line = ""
    with AUDIT_PATH.open("rb") as f:
        # Walk back from EOF to find the last newline-terminated line. The file
        # is append-only and small enough that this is fine.
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return GENESIS_HASH, 0
            chunk = min(size, 4096)
            f.seek(-chunk, os.SEEK_END)
            tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return GENESIS_HASH, 0
    for line in reversed(tail.strip().splitlines()):
        if line.strip():
            last_line = line
            break
    if not last_line:
        return GENESIS_HASH, 0
    try:
        entry = json.loads(last_line)
        return entry["hash"], int(entry["seq"]) + 1
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        # Corrupt tail — fail loud rather than silently re-anchoring on genesis,
        # which would hide tampering.
        raise RuntimeError(
            f"audit chain tail at {AUDIT_PATH} is unreadable; refusing to "
            f"append (would mask tampering)"
        ) from e


# ── Append ───────────────────────────────────────────────────────────


@_chained
def append(
    *,
    session_id: str,
    tool: str,
    args: Any,
    result: Any,
    allowed: bool,
    sentinel_reason: str,
    sentinel_path: str,  # "fast" or "slow"
    error: str | None = None,
    denied_by_operator: bool = False,
    carryall_audit_id: str | None = None,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append one tool-call audit entry. Returns the written entry dict.

    The entry intentionally stores **digests** of args and result, not full
    payloads, to keep the chain compact. Full payloads live in the session log
    (separate file). Tampering with either is detectable: changing the session
    log changes nothing here, changing the audit changes the hash chain.

    ``denied_by_operator`` distinguishes a "Sentinel allowed but operator
    pressed N at the diff prompt" outcome from a Sentinel block. Both leave
    the file untouched, but the model gets a different result string and the
    audit log makes the difference auditable.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "session_id": session_id,
        "seq": seq,
        "tool": tool,
        "args_digest": _arg_digest(args),
        "result_digest": _result_digest(result) if result is not None else None,
        "allowed": allowed,
        "sentinel_reason": sentinel_reason,
        "sentinel_path": sentinel_path,
        "error": error,
        "denied_by_operator": denied_by_operator,
        "carryall_audit_id": carryall_audit_id,
        "agent_identity": agent_identity,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    # Short anchor for outside-the-harness spot checks.
    anchor = {
        "ts": payload["ts"],
        "session_id": session_id,
        "seq": seq,
        "hash": payload["hash"],
    }
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(anchor) + "\n")

    return payload


# ── Turn-level telemetry append ───────────────────────────


@_chained
def append_turn(
    *,
    session_id: str,
    user_turn: int,
    model: str,
    route_reason: str,
    tokens_in: int,
    tokens_out: int,
    thinking_tokens: int | None,
    tool_call_count: int,
    inner_steps: int,
    halted_reason: str,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append one per-user-turn telemetry entry to the same hash chain.

    Distinguished from tool-call entries by ``kind == "turn"``. The fields are
    aggregates over the whole user-prompt loop:
      - tokens_in / tokens_out : sum across all model calls in the turn
      - thinking_tokens        : Gemma-only (CoT consumes output budget); null
                                  for models that don't expose it
      - tool_call_count        : total tool invocations the model made this turn
      - inner_steps            : agent-loop iterations spent on this prompt
      - halted_reason          : "model_done" | "max_turns" | "error" | "interrupted" | "deadline" | "stuck"

    Hash chain semantics are identical to ``append()`` — a single linear chain
    so a tampered turn entry breaks every following entry.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "turn",
        "session_id": session_id,
        "seq": seq,
        "user_turn": user_turn,
        "model": model,
        "route_reason": route_reason,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "thinking_tokens": thinking_tokens,
        "tool_call_count": tool_call_count,
        "inner_steps": inner_steps,
        "halted_reason": halted_reason,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    anchor = {
        "ts": payload["ts"],
        "session_id": session_id,
        "seq": seq,
        "hash": payload["hash"],
    }
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(anchor) + "\n")

    return payload


# ── Per-bead outcome receipts ─────────────────────


@_chained
def append_receipt(
    *,
    session_id: str,
    bead_id: str,
    bead_class: str,
    tokens: int,
    inner_steps: int | None,
    verify_passed: bool,
    diff_files: int,
    diff_lines: int,
    pr_url: str | None,
    exit_code: int,
    agent_identity: str | None = None,
    repo: str | None = None,
    outcome: str | None = None,
    failure_detail: str | None = None,
) -> dict[str, Any]:
    """Append a per-bead outcome receipt (``kind == "receipt"``) to the chain.

    Emitted once per nightshift bead after the PR is opened. ``merged`` and
    30-day survival are unknown at emit time (the PR is open for morning
    review) and are recorded later by ``append_receipt_reconcile`` as a
    separate append — the chain is append-only, never updated in place.
    The morning-merge-rate KPI joins the two by ``bead_id``.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()
    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "receipt",
        "session_id": session_id,
        "seq": seq,
        "bead_id": bead_id,
        "bead_class": bead_class,
        "tokens": tokens,
        "inner_steps": inner_steps,
        "verify_passed": verify_passed,
        "diff_files": diff_files,
        "diff_lines": diff_lines,
        "pr_url": pr_url,
        "exit_code": exit_code,
        "repo": repo,
        # verify_passed conflates "the turn crashed", "the gate rejected it"
        # and "it published" into one bool, so 33 consecutive failures carried
        # no recoverable reason. outcome names the branch; failure_detail
        # carries the gate text that explains it.
        "outcome": outcome,
        "failure_detail": failure_detail,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": payload["ts"], "session_id": session_id,
            "seq": seq, "hash": payload["hash"],
        }) + "\n")
    return payload


@_chained
def append_receipt_reconcile(
    *,
    bead_id: str,
    pr_url: str | None,
    merged: bool,
    merged_at: str | None,
    survival_days: int | None = None,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append a ``kind == "receipt_reconcile"`` row recording a bead's PR
    outcome (merged y/n, and optionally 30-day survival), joined by bead_id."""
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()
    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "receipt_reconcile",
        "seq": seq,
        "bead_id": bead_id,
        "pr_url": pr_url,
        "merged": merged,
        "merged_at": merged_at,
        "survival_days": survival_days,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": payload["ts"], "session_id": bead_id,
            "seq": seq, "hash": payload["hash"],
        }) + "\n")
    return payload


# ── Mode transitions ──────────────────────────────────────


@_chained
def append_mode_change(
    *,
    session_id: str,
    from_mode: str,
    to_mode: str,
    reason: str,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append a Plan↔Act mode transition entry to the audit chain.

    Distinguished from tool-call entries by ``kind == "mode_change"``. The
    transition is recorded inside the same hash chain so any tampering
    (e.g., back-dating a switch to Act mode) breaks every following entry.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "mode_change",
        "session_id": session_id,
        "seq": seq,
        "from_mode": from_mode,
        "to_mode": to_mode,
        "reason": reason,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    anchor = {
        "ts": payload["ts"],
        "session_id": session_id,
        "seq": seq,
        "hash": payload["hash"],
    }
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(anchor) + "\n")

    return payload


@_chained
def append_autonomy_change(
    *,
    session_id: str,
    from_level: str,
    to_level: str,
    reason: str,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append an autonomy-level transition (``kind == "autonomy_change"``).

    A level change widens or narrows what the session may run without
    asking, so it sits on the same tamper-evident chain as the mode changes
    and the tool calls it governs.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "autonomy_change",
        "session_id": session_id,
        "seq": seq,
        "from_level": from_level,
        "to_level": to_level,
        "reason": reason,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": payload["ts"], "session_id": session_id,
            "seq": seq, "hash": payload["hash"],
        }) + "\n")
    return payload


@_chained
def append_compaction(
    *,
    session_id: str,
    messages_before: int,
    messages_after: int,
    chars_before: int,
    chars_after: int,
    summary: str,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append a context-compaction entry to the audit chain.

    Distinguished by ``kind == "compaction"``. Compaction rewrites what the
    model can see of its own history, so the rewrite itself must be on the
    tamper-evident chain — a back-dated or hidden compaction breaks every
    following entry. Stores a digest of the summary, not the text; the full
    summary lives in the session log.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "compaction",
        "session_id": session_id,
        "seq": seq,
        "messages_before": messages_before,
        "messages_after": messages_after,
        "chars_before": chars_before,
        "chars_after": chars_after,
        "summary_digest": _result_digest(summary),
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    anchor = {
        "ts": payload["ts"],
        "session_id": session_id,
        "seq": seq,
        "hash": payload["hash"],
    }
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(anchor) + "\n")

    return payload


# ── Envelope changes (session envelope grants/revokes) ───────────────


@_chained
def append_envelope_change(
    *,
    session_id: str,
    action: str,  # "grant" | "revoke" | "expire"
    grant: dict[str, Any] | None,
    reason: str,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append a session-envelope change (JIT grant, revoke) to the audit chain.

    Distinguished by ``kind == "envelope_change"``. A grant that widened what
    the session could touch, or a revoke that killed it, is on the same
    tamper-evident chain as the tool calls it authorized — so a back-dated
    grant breaks every following entry.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "envelope_change",
        "session_id": session_id,
        "seq": seq,
        "action": action,
        "grant": grant,
        "reason": reason,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    anchor = {
        "ts": payload["ts"],
        "session_id": session_id,
        "seq": seq,
        "hash": payload["hash"],
    }
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(anchor) + "\n")

    return payload


# ── Verification ─────────────────────────────────────────────────────


@dataclass
class ScanResult:
    """Outcome of one walk over the chain."""

    ok: bool
    entries: int = 0
    acknowledged: int = 0
    signed_verified: int = 0
    signed_unverifiable: int = 0
    error: str | None = None
    break_info: dict[str, Any] | None = None
    acks_applied: list[dict[str, Any]] = field(default_factory=list)


def scan_chain(path: Path | None = None,
               breaks: dict[int, dict[str, Any]] | None = None) -> ScanResult:
    """Walk the audit file from genesis, recomputing every hash.

    A discontinuity stops the walk unless ``audit_breaks`` holds an
    acknowledgement pinned to that exact line, hashes and prefix digest, in
    which case the walk resumes from the entry as a new segment head and the
    break is counted. The file is read as bytes so the prefix digest an
    acknowledgement pins is over the literal stored bytes.
    """
    p = path or AUDIT_PATH
    if not p.exists():
        return ScanResult(ok=True)
    acks = audit_breaks.load() if breaks is None else breaks
    prev = GENESIS_HASH
    expected_seq = 0
    entries = 0
    applied: list[dict[str, Any]] = []
    signed_verified = 0
    signed_unverifiable = 0
    running = hashlib.sha256()
    with p.open("rb") as f:
        for lineno, raw in enumerate(f, start=1):
            prefix_digest = running.hexdigest()
            running.update(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                return ScanResult(ok=False, acknowledged=len(applied),
                                  error=f"line {lineno}: malformed JSON ({e})")

            if entry.get("prev_hash") != prev or entry.get("seq") != expected_seq:
                info = {
                    "line": lineno,
                    "expected_prev": prev,
                    "observed_prev": str(entry.get("prev_hash")),
                    "observed_seq": entry.get("seq"),
                    "prefix_sha256": prefix_digest,
                }
                ack = acks.get(lineno)
                if ack is not None and audit_breaks.matches(ack, info):
                    applied.append(ack)
                    prev = info["observed_prev"]
                    expected_seq = info["observed_seq"]
                elif entry.get("prev_hash") != prev:
                    return ScanResult(
                        ok=False, acknowledged=len(applied), break_info=info,
                        error=(f"line {lineno}: prev_hash mismatch "
                               f"(expected {prev[:12]}, got {str(entry.get('prev_hash'))[:12]})"))
                else:
                    return ScanResult(
                        ok=False, acknowledged=len(applied), break_info=info,
                        error=(f"line {lineno}: seq mismatch "
                               f"(expected {expected_seq}, got {entry.get('seq')})"))

            stored_hash = entry.get("hash")
            # ``sig``/``signer`` are added AFTER hashing, so they're excluded
            # from the hash recompute. Pre-P3 rows lack them ⇒ no-op.
            payload_for_hash = {
                k: v for k, v in entry.items()
                if k not in ("hash", "sig", "signer")
            }
            recomputed = _hash_entry(prev, payload_for_hash)
            if recomputed != stored_hash:
                return ScanResult(
                    ok=False, acknowledged=len(applied),
                    error=(f"line {lineno}: hash mismatch "
                           f"(stored {str(stored_hash)[:12]}, recomputed {recomputed[:12]})"))
            if entry.get("sig"):
                outcome = _verify_signature(entry)
                if outcome is False:
                    return ScanResult(
                        ok=False, acknowledged=len(applied),
                        error=(f"line {lineno}: signature verification FAILED "
                               f"for signer {entry.get('signer')!r}"))
                if outcome is None:
                    signed_unverifiable += 1
                else:
                    signed_verified += 1
            prev = stored_hash
            expected_seq += 1
            entries += 1
    return ScanResult(ok=True, entries=entries, acknowledged=len(applied),
                      signed_verified=signed_verified,
                      signed_unverifiable=signed_unverifiable, acks_applied=applied)


def verify_chain(path: Path | None = None) -> tuple[bool, str]:
    """Walk the audit file and recompute every hash.

    Returns ``(ok, message)``. Reached in production through ``main`` below
    (``make audit-verify``); a tamper-evident log nothing verifies is decoration.
    """
    result = scan_chain(path)
    if not result.ok:
        return False, result.error or "chain invalid"
    tail = f"; {result.signed_verified} signed+verified" if result.signed_verified else ""
    if result.signed_unverifiable:
        tail += f", {result.signed_unverifiable} signed-unverifiable(no pubkey)"
    if result.acknowledged:
        plural = "s" if result.acknowledged != 1 else ""
        tail += f"; {result.acknowledged} acknowledged break{plural}"
    return True, f"chain ok ({result.entries} entries{tail})"


def _verify_signature(entry: dict[str, Any]) -> bool | None:
    """Verify one entry's Ed25519 signature.

    Returns True (verified), False (signature present but INVALID — tamper),
    or None (signer's public key unavailable, so unverifiable — not a failure).
    """
    signer = entry.get("signer")
    pub_b64 = _pubkey_map().get(signer) if signer else None
    if not pub_b64:
        return None
    try:
        import base64

        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except Exception:  # noqa: BLE001 — no nacl ⇒ can't verify, not a tamper
        return None
    try:
        vk = VerifyKey(base64.b64decode(pub_b64))
        vk.verify(_signable(entry).encode("utf-8"), bytes.fromhex(entry["sig"]))
        return True
    except BadSignatureError:
        return False
    except Exception:  # noqa: BLE001 — malformed key/sig ⇒ unverifiable
        return None


@_chained
def append_session_resume(
    *,
    session_id: str,
    messages: int,
    user_turns: int,
    agent_identity: str | None = None,
) -> dict[str, Any]:
    """Append a ``kind == "session_resume"`` entry to the audit chain.

    A resume brings a session's authority back to life in a new process, so
    it belongs on the tamper-evident chain beside mode and envelope changes:
    the chain should show that turns after a restart continued a prior
    session rather than appearing from nowhere.
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    prev_hash, seq = _read_head()

    payload = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "kind": "session_resume",
        "session_id": session_id,
        "seq": seq,
        "messages": messages,
        "user_turns": user_turns,
        "prev_hash": prev_hash,
    }
    payload["hash"] = _hash_entry(prev_hash, payload)
    _maybe_sign(payload, agent_identity)

    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    anchor = {
        "ts": payload["ts"],
        "session_id": session_id,
        "seq": seq,
        "hash": payload["hash"],
    }
    with ANCHORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(anchor) + "\n")

    return payload


# ── Entry point ──────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Verify the chain and report; 0 intact, 1 broken. Wired as ``make audit-verify``."""
    parser = argparse.ArgumentParser(
        prog="python -m coding_harness.security.audit",
        description="Verify the hash-chained tool-call audit log.",
    )
    parser.add_argument(
        "--path", type=Path, default=None,
        help="audit jsonl to check (default: this harness's audit log)",
    )
    parser.add_argument(
        "--acknowledge-break", action="store_true",
        help="record the first unacknowledged break so the walk resumes past it",
    )
    parser.add_argument("--line", type=int, help="the break's line, as proof you read it")
    parser.add_argument("--reason", help="why this discontinuity is accepted")
    args = parser.parse_args(argv)
    path = args.path if args.path is not None else AUDIT_PATH

    if args.acknowledge_break:
        return _acknowledge(path, args.line, args.reason)

    ok, message = verify_chain(path)
    print(f"{'OK' if ok else 'BROKEN'}: {path}: {message}")
    return 0 if ok else 1


def _acknowledge(path: Path, line: int | None, reason: str | None) -> int:
    """Record an acknowledgement for the first unacknowledged break. 0 done, 2 refused.

    Refuses unless the caller names the exact line, so acknowledging is a
    deliberate act about a specific discontinuity rather than a blanket reset.
    """
    if line is None or not reason:
        print("refusing: --acknowledge-break requires --line and --reason")
        return 2
    result = scan_chain(path)
    if result.ok:
        print(f"refusing: {path} verifies with no unacknowledged break")
        return 2
    info = result.break_info
    if info is None:
        print(f"refusing: {path} is invalid but not at a chain discontinuity: {result.error}")
        return 2
    if info["line"] != line:
        print(f"refusing: first unacknowledged break is at line {info['line']}, not {line}")
        return 2
    record = audit_breaks.record(
        info, reason=reason, acknowledged_by=os.environ.get("USER", "unknown"),
    )
    print(f"acknowledged break at line {record['line']} "
          f"(seq {record['observed_seq']}, prefix {record['prefix_sha256'][:12]})")
    ok, message = verify_chain(path)
    print(f"{'OK' if ok else 'BROKEN'}: {path}: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
