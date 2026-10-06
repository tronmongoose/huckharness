"""Agent session loop — model call → tool dispatch → repeat.

This is the smallest loop that's still real:

    while True:
        msg = model.chat(messages, tools=tools)
        messages.append(msg)
        if not msg.tool_calls:
            return msg.content   # terminal turn
        for call in msg.tool_calls:
            result = registry.dispatch(call.name, call.args)
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": result.content})

Iteration cap (``MAX_TURNS``) prevents runaway loops if a small local model
keeps calling the same tool. Every step is event-streamed via the registry's
``event_sink`` so print mode (and later RPC mode) can render progress.

Multi-turn (REPL) sessions reuse one ``Session`` instance and call
``run_turn(prompt)`` for each user line. The conversation history (``_messages``)
is owned by the Session and persists across turns so the model sees prior
tool results when answering follow-ups.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from coding_harness.core import (
    compaction,
    context_budget,
    cost,
    diffs,
    done_gate,
    memory_proposals,
    paths,
    review,
    stuck,
    turn_loop,
)
from coding_harness.core import (
    verify as verify,  # re-exported: tests patch session.verify
)
from coding_harness.core.git import ShadowRepo
from coding_harness.core.mode import Mode
from coding_harness.core.router import (
    RouteDecision,
    decide_route,
    generation_local_only,
)
from coding_harness.core.turn_guards import MAX_REVIEW_ROUNDS as MAX_REVIEW_ROUNDS
from coding_harness.core.turn_loop import MAX_REPAIR_ROUNDS as MAX_REPAIR_ROUNDS

# Re-exported: callers and tests patch the backends as session.<backend>.chat.
from coding_harness.models import anthropic as anthropic
from coding_harness.models import claude_cli as claude_cli
from coding_harness.models import ollama as ollama
from coding_harness.models import transport
from coding_harness.models.profile import (
    ModelProfile,
    profile_hash,
    resolve_profile,
    sampling_kwargs,
)
from coding_harness.security import audit
from coding_harness.security.snapshot import RestoreReport, Snapshotter
from coding_harness.tools.base import WritePlan
from coding_harness.tools.registry import DispatchEvent, ToolRegistry

if TYPE_CHECKING:
    from coding_harness.core.envelope import SessionEnvelope


class _Interrupted(Exception):
    """Raised inside the turn loop when ``interrupt()`` is signalled.

    Cooperative cancellation: checked at the top of each inner step, between
    tool calls, and between streamed deltas. A blocking model read cannot be
    killed mid-request, so the earliest a stop takes effect is the next
    checkpoint — surfaced honestly as ``halted_reason="interrupted"``.

    ``reason`` is ``"interrupted"`` (operator) or ``"deadline"`` (the turn's
    wall-clock budget ran out); it becomes the result's ``halted_reason``.
    """

    def __init__(self, reason: str = "interrupted") -> None:
        super().__init__(reason)
        self.reason = reason


# Signature: (user_prompt, tool_name, args) -> True to allow, False to deny.
PasteEchoCallback = Callable[[str, str, dict[str, Any]], bool]

MAX_TURNS = 25  # per-prompt safety net for tool-call loops on small models

DEFAULT_FRONTIER_MODEL = os.environ.get(
    "CLAWROUTER_FRONTIER_MODEL", "claude-sonnet-4-6"
)

SESSIONS_DIR = paths.meta_dir() / "sessions"


def _new_session_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


@dataclass
class SessionResult:
    final_text: str
    turns: int  # agent turns spent on this prompt (not session lifetime)
    session_id: str
    session_log_path: Path
    halted_reason: str  # "model_done" | "max_turns" | "error" | "interrupted" | "deadline" | "stuck"
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    # Every file Edit/Write/Bash touched this prompt, on every exit path, so a
    # caller can keep the work on disk even when the turn did not finish.
    files_changed: list[str] = field(default_factory=list)
    # Green-before-done verdict: True/False from the last gate run, None when
    # the gate did not run or could not decide (red baseline, deadline).
    checks_passed: bool | None = None
    checks_report: str = ""


@dataclass
class Session:
    model: str
    registry: ToolRegistry
    system_prompt: str
    event_sink: Callable[[DispatchEvent], None] | None = None
    session_id: str = field(default_factory=_new_session_id)
    snapshotter: Snapshotter | None = None
    # Router wiring. Defaults preserve pre-router behavior:
    # decide_route runs per turn unless force_local or explicit_model is set,
    # and frontier_model is consulted only when the router picks frontier.
    force_local: bool = False
    explicit_model: bool = False
    frontier_model: str = DEFAULT_FRONTIER_MODEL
    # Frontier backend for complexity_high turns (P3):
    #   "api"        → anthropic.chat, raw API key, non-streaming (default;
    #                  off whenever the router's local_only switch is set)
    #   "claude-cli" → claude_cli.chat, Max-plan quota, text-only, streaming.
    # claude-cli bypasses the local_only + cost-cap dollar gates (it spends no
    # API dollars); the sensitivity gate still binds.
    frontier_backend: str = "api"
    cli_frontier_model: str = "sonnet"
    # Paste-echo gate. When set, called before every Bash dispatch
    # whose command looks substantially like the user's prompt. Returning
    # True allows dispatch (Sentinel still reviews afterwards); returning
    # False denies and feeds a synthetic refusal back to the model.
    # ``None`` means no operator is available — default to deny, which is
    # the safe behavior in print mode and tests that don't opt in.
    paste_echo_callback: PasteEchoCallback | None = None
    # Plan/Act mode. Wires through to ``self.registry.mode`` in
    # ``__post_init__`` so the registry's tool-visibility filter and the
    # session agree from the first turn. Mid-session changes go through
    # ``set_mode``, which records an audit chain entry.
    mode: Mode = Mode.ACT
    # Session envelope (deny-by-default scope, P1). None ⇒ the registry gate
    # stays disabled, so print/repl/nightshift are unaffected. When set, it is
    # wired to ``registry.envelope`` in ``__post_init__``.
    envelope: SessionEnvelope | None = None
    # Agent identity stamped onto the audit chain. Threaded to
    # ``registry.agent_identity`` so every dispatched row carries provenance.
    agent_identity: str | None = None
    # Auto context compaction. Checked once at the top of each
    # run_turn; the summarizer is ALWAYS the local model (self.model) so
    # compaction never widens where session history travels. 0 disables.
    compaction_threshold_chars: int = compaction.DEFAULT_THRESHOLD_CHARS
    compaction_keep_recent: int = compaction.DEFAULT_KEEP_RECENT_USERS
    # In-loop verify-repair. After Edit/Write, run the
    # deterministic gate (py_compile + ruff) on touched Python files and feed
    # any failure back as the next observation, capped at MAX_REPAIR_ROUNDS.
    # Kill switch: HARNESS_VERIFY_REPAIR=0. Defaults ON.
    verify_repair: bool = field(
        default_factory=lambda: os.environ.get("HARNESS_VERIFY_REPAIR", "1") != "0"
    )
    # Agentic review. When the model declares done, a local reviewer (Ollama,
    # HARNESS_REVIEW_MODEL) grades the prompt's diffs against the task;
    # concerns feed back for a repair round, capped at MAX_REVIEW_ROUNDS.
    # Default ON; kill switch HARNESS_REVIEW=0, backend HARNESS_REVIEW_BACKEND.
    agentic_review: bool = field(default_factory=review.enabled)
    # Green-before-done gate (P1-4). done_checks are the caller's shell
    # commands (bead AC verification, --check); empty means test_cmd, and a
    # None test_cmd means discover one from the repo layout. A final run is
    # compared against a baseline taken before the first step, so only
    # failures this prompt introduced count. Kill switches: HARNESS_DONE_GATE=0
    # (gate + baseline), HARNESS_TARGETED_TESTS=0 (tests after each edit).
    done_checks: list[str] = field(default_factory=list)
    test_cmd: list[str] | None = None
    done_gate_enabled: bool = field(
        default_factory=lambda: os.environ.get("HARNESS_DONE_GATE", "1") != "0"
    )
    targeted_tests_enabled: bool = field(
        default_factory=lambda: os.environ.get("HARNESS_TARGETED_TESTS", "1") != "0"
    )
    # Shadow git checkpoints at every turn boundary, for per-turn diffs,
    # single-file revert and whole-tree rewind. Opt-in: serve turns it on for
    # the GUI. Never in a home dir or /, where one checkpoint copies a disk.
    shadow_checkpoints: bool = False
    # Set on a helper session a tool spawned (Explore sets "explore"); history
    # lists and --continue skip transcripts that carry it.
    subagent: str | None = None

    def __post_init__(self) -> None:
        self.registry.session_id = self.session_id
        self.registry.mode = self.mode
        self.registry.envelope = self.envelope
        self.registry.agent_identity = self.agent_identity
        self._interrupt = threading.Event()
        # Operator messages posted mid-turn; the step loop drains them before its next model call.
        self._steer: list[str] = []
        self._steer_lock = threading.Lock()
        self._steer_open = False  # True only between run_turn's start and finish_turn
        self._deadline_at: float | None = None
        self.profile: ModelProfile = resolve_profile(self.model)
        if self.registry.event_sink is None:
            self.registry.event_sink = self.event_sink
        if self.snapshotter is None:
            self.snapshotter = Snapshotter(self.session_id)
        # Wire the registry's snapshot_callback to a closure that knows the
        # current user-prompt turn + within-turn seq.
        if self.registry.snapshot_callback is None:
            self.registry.snapshot_callback = self._snapshot_callback
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._session_log_path = SESSIONS_DIR / f"{self.session_id}.jsonl"
        self._messages: list[dict[str, Any]] = []
        self._started: bool = False
        self._closed: bool = False
        self._total_turns: int = 0
        self._user_turn: int = 0  # counts run_turn() invocations
        self._seq_in_turn: int = 0
        # Index of this turn's prompt in _messages; mid-turn compaction keeps it verbatim.
        self._turn_anchor: int | None = None
        self.last_prompt_tokens: int = 0
        # Set once confidential-or-higher second-brain content enters the
        # history. Every later turn then stays local: a frontier model would
        # receive the whole history, not just the new prompt.
        self.sensitive_context = False
        # "code" sends the tool surface; "chat" sends none and runs the chat-role
        # model, so a question never waits on the big coder or its tool schemas.
        self.kind = "code"
        # Path -> bytes before the prompt's first write (None for a new file),
        # so the reviewer sees one diff per file, not the intermediate states.
        self._first_pre_image: dict[str, bytes | None] = {}
        self._cwd = os.getcwd()
        self.shadow: ShadowRepo | None = (
            ShadowRepo(self.session_id, self._cwd, log=self._log)
            if self.shadow_checkpoints and diffs.shadow_ok(self._cwd) else None
        )
        self._turn_checkpoints: dict[int, str] = {}  # turn -> shadow sha at its start
        self._turn_end_checkpoints: dict[int, str] = {}
        self._turn_files: dict[int, list[str]] = {}  # turn -> files_changed
        self._reverted: dict[int, set[str]] = {}  # turn -> paths the operator reverted
        self.last_rewind: dict[str, Any] = {}  # the latest rewind event payload
        # Brain notes the operator pinned ({path, tier, content}); context.recall
        # renders them into each local turn. Proposals run after a turn only
        # where a person reviews them (the GUI serve sets this).
        self.pinned: list[dict[str, Any]] = []
        self.propose_memories: bool = False
        # The per-turn skill hint is for a person to read. REPL and serve set
        # this; print mode and eval leave it off.
        self.recall_hints: bool = False

    @property
    def session_log_path(self) -> Path:
        return self._session_log_path

    def set_kind(self, kind: str) -> None:
        """Switch between code and chat; the model follows the kind's role."""
        from coding_harness.core.model_roles import role_model

        if kind not in ("code", "chat"):
            raise ValueError(f"kind must be code or chat, not {kind!r}")
        self.kind = kind
        self.model = role_model(kind)
        self.explicit_model = True
        self.profile = resolve_profile(self.model)
        self._emit("session_kind", {"kind": kind, "model": self.model})

    def mark_sensitive(self, source: str, tier: int) -> None:
        """Pin the rest of this session to local models; logged so a resume keeps it."""
        if self.sensitive_context:
            return
        self.sensitive_context = True
        self._emit("sensitive_context", {"source": source, "tier": tier})

    def restore(
        self, messages: list[dict[str, Any]], *, user_turns: int, sensitive: bool = False,
    ) -> None:
        """Adopt a replayed history so this session continues a prior one.

        Marks the session started so ``_ensure_started`` does not prepend a
        second system message — the replayed list already carries the fresh
        one. Must be called before the first ``run_turn``; a session that has
        already produced turns cannot be rewritten under itself.
        """
        if self._started or self._messages:
            raise RuntimeError("restore() on an already-started session")
        self._messages = list(messages)
        self._user_turn = user_turns
        self._started = True
        self.sensitive_context = sensitive
        self._emit("session_resumed", {
            "session_id": self.session_id,
            "messages": len(self._messages),
            "user_turns": user_turns,
        })

    def set_mode(self, mode: Mode, *, reason: str = "user") -> Mode:
        """Switch between Plan and Act mid-session.

        Records the transition in the audit chain (via
        ``audit.append_mode_change``) so a tampered "back to act" claim
        breaks the chain just like a tampered tool call would. Idempotent:
        switching to the current mode is a no-op and writes no entry.
        Returns the new mode.
        """
        if mode is self.mode:
            return self.mode
        previous = self.mode
        self.mode = mode
        self.registry.mode = mode
        try:
            audit.append_mode_change(
                session_id=self.session_id,
                from_mode=previous.value,
                to_mode=mode.value,
                reason=reason,
                agent_identity=self.agent_identity,
            )
        except Exception as e:  # noqa: BLE001 — audit failures shouldn't tip mode
            self._log("mode_change_audit_error", {
                "error": f"{type(e).__name__}: {e}",
            })
        self._emit("mode_change", {
            "from": previous.value,
            "to": mode.value,
            "reason": reason,
        })
        return self.mode

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    @property
    def total_turns(self) -> int:
        """Cumulative agent turns across all run_turn calls in this session."""
        return self._total_turns

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "kind": kind,
            **payload,
        }
        with self._session_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _log_tool_result(
        self,
        tool_msg: dict[str, Any],
        *,
        is_error: bool,
        error_class: str | None = None,
    ) -> None:
        """Record a tool result in the transcript so the session can be replayed.

        Without this the transcript holds assistant turns whose tool_calls have
        no answering results — a message list no backend will accept on resume.
        Written via ``_log`` (not ``_emit``): live listeners already saw the
        result through the registry's dispatch events.
        """
        self._log("tool_result", {
            "turn": self._total_turns,
            "tool_call_id": tool_msg.get("tool_call_id"),
            "name": tool_msg.get("name"),
            "content": tool_msg.get("content", ""),
            "is_error": is_error,
            "error_class": error_class,
        })

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(DispatchEvent(kind=kind, payload=payload))
        self._log(kind, payload)

    def _emit_ephemeral(self, kind: str, payload: dict[str, Any]) -> None:
        """Emit to the event sink WITHOUT writing the transcript.

        Streamed ``assistant_delta`` tokens ride this path: listeners see them
        live, but the JSONL transcript keeps only the assembled
        ``assistant_message`` so replays stay compact.
        """
        if self.event_sink is not None:
            self.event_sink(DispatchEvent(kind=kind, payload=payload))

    def interrupt(self) -> None:
        """Signal cooperative cancellation of the in-flight turn.

        Thread-safe: only sets an Event. The turn thread notices at its next
        checkpoint and unwinds via ``_Interrupted`` to a
        ``halted_reason="interrupted"`` result. A no-op if no turn is running.
        """
        self._interrupt.set()

    def steer(self, text: str) -> bool:
        """Queue an operator message for the running turn's next model call; False when no turn runs."""
        with self._steer_lock:
            if not self._steer_open:
                return False
            self._steer.append(text)
        self._emit_ephemeral("steer_queued", {"turn": self._user_turn, "text": text})
        return True

    def steer_gate(self, open_: bool) -> list[str]:
        """Open or close steering for a turn; returns and clears whatever was still queued."""
        with self._steer_lock:
            self._steer_open = open_
            out, self._steer = self._steer, []
        return out

    def close_steer(self) -> None:
        """Close steering; anything the turn never read goes out as ``steer_dropped``, never to the next turn."""
        dropped = self.steer_gate(False)
        if dropped:
            self._emit_ephemeral("steer_dropped", {"turn": self._user_turn, "texts": dropped})

    def take_steers(self) -> list[str]:
        """Pop every queued steer message, oldest first."""
        with self._steer_lock:
            out, self._steer = self._steer, []
        return out

    def _remaining_s(self) -> float | None:
        """Seconds left on this turn's deadline; None when no deadline is set."""
        if self._deadline_at is None:
            return None
        return max(0.0, self._deadline_at - time.monotonic())

    def _check_interrupt(self) -> None:
        """Checkpoint: unwind the turn on an operator interrupt or an expired deadline."""
        if self._interrupt.is_set():
            raise _Interrupted("interrupted")
        if self._remaining_s() == 0.0:
            raise _Interrupted("deadline")

    def _profile_for(self, model: str) -> ModelProfile:
        """The session profile, or a fresh one when a turn override picks another model."""
        if model == self.model:
            return self.profile
        return resolve_profile(model)

    def _read_timeout_kw(self) -> dict[str, float]:
        """``timeout=`` for ollama.chat, capped to the deadline; empty when none is set."""
        remaining = self._remaining_s()
        if remaining is None:
            return {}
        return {"timeout": max(5.0, min(transport.read_timeout_s(), remaining))}

    def _sampling_kw(self, model: str, num_predict: int | None) -> dict[str, Any]:
        """Profile sampling kwargs, with the truncation-recovery output budget applied."""
        kw = sampling_kwargs(self._profile_for(model))
        if num_predict is not None:
            kw["max_tokens"] = num_predict
        return kw

    def _call_id(self, call: dict[str, Any]) -> str:
        """The model's call id, or a generated one when it omitted it."""
        return call.get("id") or f"call_{self._total_turns}_{uuid.uuid4().hex[:6]}"

    def _retry_scope(self, *, single: bool = False):
        """Transport retries bounded by the turn deadline; ``single`` also caps them at one attempt."""
        attempts = 1 if single and self._deadline_at is not None else None
        return transport.retry_scope(deadline=self._deadline_at, attempts=attempts)

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        self._messages.append({"role": "system", "content": self.system_prompt})
        tools = self.registry.to_openai_tools()
        self._emit("session_start", {
            "session_id": self.session_id,
            "model": self.model,
            "cwd": os.getcwd(),
            "profile": asdict(self.profile),
            "profile_hash": profile_hash(self.profile),
            "mode": self.mode.value,
            "autonomy": self.registry.autonomy.value,
            "tools": [t["function"]["name"] for t in tools],
            **({"subagent": self.subagent} if self.subagent else {}),
            "identity": self.agent_identity,
            "envelope": self.envelope.to_dict() if self.envelope is not None else None,
        })

    def _snapshot_callback(self, plan: WritePlan) -> None:
        self._seq_in_turn += 1
        self._first_pre_image.setdefault(str(plan.file_path), plan.pre_image)
        assert self.snapshotter is not None  # set in __post_init__
        ref = self.snapshotter.capture(
            plan, turn=self._user_turn, seq=self._seq_in_turn,
        )
        self._log("snapshot_capture", {
            "turn": ref.turn,
            "seq": ref.seq,
            "path": str(plan.file_path),
            "existed": plan.existed,
        })

    def rewind(self, n: int = 1) -> RestoreReport:
        """Restore filesystem + truncate conversation by ``n`` user turns.

        With a shadow checkpoint for the first rewound turn the whole tree goes
        back to it (Bash side effects included), then the Write/Edit pre-images
        of exactly those turns replay on top. Without one, the pre-images of the
        last ``n`` snapshotted turns restore as before. Returns the snapshot
        RestoreReport; calling rewind twice does not double-restore.
        """
        assert self.snapshotter is not None
        if n < 1:
            return RestoreReport(0, 0, 0, [])
        target = max(1, self._user_turn - n + 1)
        sha = self._turn_checkpoints.get(target) if self.shadow is not None else None
        shadow_report = self.shadow.restore(sha) if self.shadow is not None and sha else None
        if shadow_report is not None and shadow_report.errors:
            return self._rewind_failed(shadow_report.errors)
        if shadow_report is not None:
            count = sum(1 for d in self.snapshotter.list_turn_dirs() if int(d.name[5:]) >= target)
            report = self.snapshotter.restore(last_n_turns=count) if count else RestoreReport(0, 0, 0, [])
        else:
            report = self.snapshotter.restore(last_n_turns=n)

        # Truncate _messages to before the n-th most recent user message.
        user_indices = [
            i for i, m in enumerate(self._messages)
            if m.get("role") == "user" and not (m.get("_operator_note") or m.get("_steer"))
        ]
        if user_indices:
            cut = user_indices[-n] if len(user_indices) >= n else user_indices[0]
            self._messages = self._messages[:cut]

        if shadow_report is not None:
            self._user_turn = target - 1
            self._drop_turn_state(target)
        else:
            self._user_turn = max(0, self._user_turn - report.turns_rewound)
        # _seq_in_turn always resets at the next run_turn(); no reset needed.

        payload: dict[str, Any] = {
            "turns_rewound": report.turns_rewound,
            "files_restored": report.files_restored,
            "files_deleted": report.files_deleted,
            "errors": report.errors,
            "messages_remaining": len(self._messages),
            "user_turn": self._user_turn,
        }
        if shadow_report is not None:
            payload.update(
                shadow_restored=shadow_report.restored,
                shadow_removed=shadow_report.removed,
                errors=report.errors + shadow_report.errors,
            )
        self.last_rewind = payload
        self._emit("rewind", payload)
        return report

    def _rewind_failed(self, errors: list[str]) -> RestoreReport:
        """Report a shadow restore that went wrong, leaving history and turn count alone."""
        payload: dict[str, Any] = {
            "turns_rewound": 0, "files_restored": 0, "files_deleted": 0,
            "errors": errors, "messages_remaining": len(self._messages),
            "user_turn": self._user_turn, "failed": True,
        }
        self.last_rewind = payload
        self._emit("rewind", payload)
        return RestoreReport(0, 0, 0, errors)

    def _drop_turn_state(self, first: int) -> None:
        """Forget checkpoints and file lists of turns ``first`` and later."""
        for store in (self._turn_checkpoints, self._turn_end_checkpoints, self._turn_files, self._reverted):
            for t in [t for t in store if t >= first]:
                del store[t]

    def _checkpoint(self, when: str) -> str | None:
        """Shadow-commit the tree at a turn boundary; None when the shadow is off or broke."""
        if self.shadow is None:
            return None
        sha = self.shadow.checkpoint(f"turn {self._user_turn} {when}")
        if sha is None:
            # A broken shadow would pay its git timeout at every boundary.
            self.shadow = None
            return None
        store = self._turn_checkpoints if when == "start" else self._turn_end_checkpoints
        store[self._user_turn] = sha
        return sha

    def _end_turn(self, files: list[str]) -> None:
        """Record the turn's changed files and its closing checkpoint."""
        self._turn_files[self._user_turn] = files
        self._checkpoint("end")

    def diff(self, turn: int | None = None) -> dict[str, Any]:
        """What ``turn`` changed (None: the whole session) as ``{turn, base, files}``."""
        first = turn if turn is not None else min(self._turn_checkpoints, default=None)
        base = self._turn_checkpoints.get(first) if first is not None else None
        if self.shadow is not None and base:
            # A finished turn diffs to its own end checkpoint, so later turns
            # and reverts of other turns stay out; the session view is live.
            end = self._turn_end_checkpoints.get(turn) if turn is not None else None
            files = self.shadow.diff_files(base, end)
            reverted = self._reverted.get(turn, set()) if turn is not None else set()
            if end is not None and reverted:
                # The end checkpoint predates the revert: drop reverted files
                # the tree now matches base on.
                live = {f["path"] for f in self.shadow.diff_files(base)}
                files = [f for f in files if f["path"] not in reverted or f["path"] in live]
            return {"turn": turn, "base": base, "files": files}
        if turn is not None:
            touched = self._turn_files.get(turn, [])
        else:
            touched = sorted({p for fs in self._turn_files.values() for p in fs})
        assert self.snapshotter is not None
        files = diffs.snapshot_files(self.snapshotter, turn, touched, self._cwd)
        return {"turn": turn, "base": None, "files": files}

    def revert_file(self, path: str, turn: int | None = None) -> bool:
        """Put ``path`` back as it was before ``turn`` (None: the session) and tell the model."""
        assert self.snapshotter is not None
        first = turn if turn is not None else min(self._turn_checkpoints, default=None)
        sha = self._turn_checkpoints.get(first) if first is not None else None
        full = path if os.path.isabs(path) else os.path.join(self._cwd, path)
        if self.shadow is not None and sha:
            ok = self.shadow.restore_file(sha, full)
        else:
            ok = diffs.revert_from_snapshot(self.snapshotter, turn, full)
        if not ok:
            return False
        for t in range(first or 1, self._user_turn + 1):
            self.snapshotter.forget(t, full)
        shown = diffs.rel(full, self._cwd)
        if turn is not None:
            self._reverted.setdefault(turn, set()).add(shown)
        when = f"turn {first}" if first is not None else "this session"
        # User-role so the model reads it, tagged so rewind does not count it
        # as a turn; the chat adapters drop the tag before the wire.
        note = {
            "role": "user", "content": f"Operator reverted {shown} to its state before {when}",
            "_operator_note": True,
        }
        self._messages.append(note)
        self._log("operator_note", {"content": note["content"], "turn": self._user_turn})
        self._emit("file_reverted", {"path": shown, "turn": turn})
        return True

    def _maybe_compact(self) -> None:
        """Fold old history into a local-model summary when over budget.

        Runs at most once per run_turn, before the new user prompt joins the
        history. Fail-open on every path: a compaction problem never blocks
        the turn. After compaction, rewind() can only truncate conversation
        back to the oldest surviving user message — snapshots still restore
        files beyond that boundary.
        """
        if self.compaction_threshold_chars <= 0:
            return
        self._compact(threshold_chars=self.compaction_threshold_chars)

    def compact(self) -> compaction.CompactionResult:
        """Compact now regardless of size (the REPL's /compact)."""
        return self._compact(threshold_chars=0)

    def _summary_model(self) -> str:
        """The summarize-role model; history folding needs no coder."""
        from coding_harness.core.model_roles import role_model

        return role_model("summarize")

    def _compact(self, *, threshold_chars: int) -> compaction.CompactionResult:
        """One compaction pass; audits and emits when history was folded."""
        result = compaction.compact(
            self._messages,
            model=self._summary_model(),
            threshold_chars=threshold_chars,
            keep_recent_users=self.compaction_keep_recent,
        )
        self._record_compaction(result, mid_turn=False)
        return result

    def _compact_steps(self) -> compaction.CompactionResult:
        """Fold this turn's earlier steps into a local summary, keeping the task verbatim."""
        with self._retry_scope(single=True):
            result = compaction.compact_steps(
                self._messages, model=self._summary_model(), anchor=self._turn_anchor,
            )
        if result.compacted:
            self._turn_anchor = 1
        self._record_compaction(result, mid_turn=True)
        return result

    def _shrink_context(self, model: str, tools: list[dict[str, Any]]) -> None:
        """Keep the next prompt under the token budget: elide stale Reads, then compact steps."""
        if not context_budget.enabled():
            return
        budget = context_budget.token_budget(self._profile_for(model).num_ctx)
        before = context_budget.estimate_tokens(self._messages, tools)
        if before <= budget:
            return
        count = context_budget.elide_superseded_reads(self._messages)
        after = context_budget.estimate_tokens(self._messages, tools) if count else before
        if count:
            self._emit("context_elide", {
                "turn": self._total_turns, "count": count, "budget": budget,
                "tokens_before": before, "tokens_after": after,
            })
        if after > budget:
            self._compact_steps()

    def _record_compaction(self, result: compaction.CompactionResult, *, mid_turn: bool) -> None:
        """Adopt a folded history, audit it and emit ``compaction``; log a summarizer failure."""
        if not result.compacted:
            if result.reason.startswith("summarizer_error"):
                self._log("compaction_error", {"reason": result.reason, "mid_turn": mid_turn})
            return
        self._messages = result.messages
        try:
            audit.append_compaction(
                session_id=self.session_id,
                messages_before=result.messages_before,
                messages_after=result.messages_after,
                chars_before=result.chars_before,
                chars_after=result.chars_after,
                summary=result.summary,
                agent_identity=self.agent_identity,
            )
        except Exception as e:  # noqa: BLE001 — telemetry must not tip the turn
            self._log("compaction_audit_error", {"error": f"{type(e).__name__}: {e}"})
        self._emit("compaction", {
            "messages_before": result.messages_before,
            "messages_after": result.messages_after,
            "chars_before": result.chars_before,
            "chars_after": result.chars_after,
            "summary": result.summary,
            "mid_turn": mid_turn,
        })

    def _decide_route_for_turn(
        self, user_prompt: str, model_override: str | None = None,
    ) -> tuple[str, str, RouteDecision | None]:
        """Pick (selected_model, route_reason, decision) for this user turn.

        Override precedence (highest first):
          1. ``model_override`` — operator pinned this turn to a model
             (model agility, sl-pb9m). ``"claude-cli"`` pins to the Max-plan
             text-only backend; any other value is an Ollama tag, validated
             against the banned-origin list at call time.
          2. ``force_local`` — skip router entirely; use ``self.model`` on Ollama.
          3. ``explicit_model`` — user passed ``--model X``; honor verbatim on Ollama.
          4. ``decide_route(prompt)`` — sensitivity gate + complexity score.

        ``route_reason`` is the audit-log enum value documented in sl-nmc.3 AC #2:
        ``override_turn_cli | override_turn_model | override_force_local |
        override_explicit_model | sensitivity_block | complexity_high |
        complexity_low | cost_cap_local | frontier_no_tools_local``.
        """
        if model_override:
            if model_override == "claude-cli":
                # Operator pins never bypass the sensitivity gate: run the
                # router for its sensitivity verdict, then stamp the route
                # as the operator's explicit frontier escalation so the
                # dispatch-time lethal-trifecta gate sees an honest decision.
                decision = decide_route(user_prompt)
                if decision.sensitivity_flag or self.sensitive_context:
                    return self.model, "sensitivity_block", decision
                decision = replace(
                    decision,
                    route="frontier",
                    route_reason="operator turn pin (claude-cli)",
                )
                return self.cli_frontier_model, "override_turn_cli", decision
            return model_override, "override_turn_model", None
        if self.force_local:
            return self.model, "override_force_local", None
        if self.explicit_model:
            return self.model, "override_explicit_model", None

        decision = decide_route(user_prompt)
        frontier_bound = (decision.route == "frontier"
                          or decision.classification.get("route") == "frontier")
        if frontier_bound and (decision.sensitivity_flag or self.sensitive_context):
            # Sensitivity gate flipped a frontier-bound query back to local.
            # This wins over the raw complexity score for audit-trail clarity.
            return self.model, "sensitivity_block", decision
        if decision.route == "frontier":
            # The claude-cli backend spends Max-plan quota, not API dollars, so
            # the two dollar-protecting gates below (local_only + cost cap) do
            # not apply to it. The sensitivity gate above already ran and is
            # NOT bypassed — a sensitive prompt never reaches here. (P3.)
            if self.frontier_backend == "claude-cli":
                # claude-cli is text-only by construction. A turn that carries
                # tools cannot act there — the model just announces it has no
                # tool access. Capability beats complexity score:
                # keep tool-bearing turns on the local model, honestly logged.
                if self.registry.to_openai_tools():
                    return self.model, "frontier_no_tools_local", decision
                return self.cli_frontier_model, "complexity_high", decision
            # Global force-local switch (config/limits.yaml generation.local_only).
            # The harness anthropic adapter uses the raw API key, which is out of
            # credits — route frontier-bound turns to the local coding model
            # instead. Same single knob the pipelines honor.
            if generation_local_only():
                return self.model, "local_only", decision
            # Spend gate: if the daily/monthly cap is already hit, downgrade this
            # turn to local rather than spend more frontier. Runs
            # AFTER the sensitivity gate, so sensitive prompts never reach it.
            allowed, _reason = cost.under_cap()
            if not allowed:
                return self.model, "cost_cap_local", decision
            return self.frontier_model, "complexity_high", decision
        return self.model, "complexity_low", decision

    def run_turn(
        self,
        user_prompt: str,
        *,
        model_override: str | None = None,
        deadline_s: float | None = None,
    ) -> SessionResult:
        """Run one prompt (see ``_run_turn``); steering closes however the turn exits."""
        try:
            return self._run_turn(
                user_prompt, model_override=model_override, deadline_s=deadline_s,
            )
        finally:
            self.close_steer()

    def _run_turn(
        self,
        user_prompt: str,
        *,
        model_override: str | None = None,
        deadline_s: float | None = None,
    ) -> SessionResult:
        """Run one user prompt through the agent loop. Reusable across turns
        in the same session — REPL mode calls this repeatedly with shared
        history. ``MAX_TURNS`` budgets agent steps per prompt, not per session.

        ``model_override`` pins THIS turn to a specific model (model agility):
        an Ollama tag, or ``"claude-cli"`` for a Max-plan text-only turn.
        Callers validate Ollama tags against the banned-origin list up front;
        ollama.chat re-asserts at call time regardless.

        ``deadline_s`` is a wall-clock budget for the whole turn. When it runs
        out the loop unwinds at its next checkpoint to
        ``halted_reason="deadline"`` with ``error=None`` and the files edited
        so far in ``files_changed``; model reads are capped to the remaining
        budget so a stalled server cannot overrun it by a full read timeout.

        The per-step work lives in ``core/turn_loop.py`` and the gates in
        ``core/turn_guards.py``; this method owns routing and step order.
        """
        if self._closed:
            raise RuntimeError("session is closed")
        self._ensure_started()
        self._interrupt.clear()
        self.steer_gate(True)
        self._deadline_at = (
            time.monotonic() + deadline_s if deadline_s is not None else None
        )
        # The registry caps brokered permission waits on wall-clock seconds.
        self.registry.deadline_at = (
            time.time() + deadline_s if deadline_s is not None else None
        )
        with self._retry_scope(single=True):
            self._maybe_compact()

        self._user_turn += 1
        self._seq_in_turn = 0
        self._first_pre_image = {}
        self._messages.append({"role": "user", "content": user_prompt})
        self._turn_anchor = len(self._messages) - 1
        self._log("user_message", {"content": user_prompt, "turn": self._user_turn})
        checkpoint = self._checkpoint("start")
        self._emit("turn_start", {
            "turn": self._user_turn, "prompt": user_prompt, "checkpoint": checkpoint,
        })

        # Route decision is per user-prompt turn, not per inner agent step.
        # Once a turn picks frontier vs local, it stays there for the whole
        # tool-call loop — switching mid-loop would scramble cache and audit.
        selected_model, route_reason, decision = self._decide_route_for_turn(
            user_prompt, model_override,
        )
        self._emit("route_decision", {
            "turn": self._user_turn,
            "route_reason": route_reason,
            "model": selected_model,
            "route": decision.route if decision else "local",
            "sensitivity_flag": decision.sensitivity_flag if decision else False,
            "score": (
                decision.classification.get("score") if decision else None
            ),
        })
        cwd = os.getcwd()
        state = turn_loop.TurnState(
            user_prompt=user_prompt, selected_model=selected_model,
            route_reason=route_reason, decision=decision,
            tools=[] if self.kind == "chat" else self.registry.to_openai_tools(), cwd=cwd,
            gate=done_gate.Gate(
                done_gate.resolve_cmds(self.done_checks, self.test_cmd, cwd)
                if self.done_gate_enabled else []
            ),
        )
        result = self._step_loop(state)
        memory_proposals.after_turn(self, state, result)
        return result

    def _step_loop(self, state: turn_loop.TurnState) -> SessionResult:  # LONG-FN: the step order and every exit path, on one screen
        """Model call, tool dispatch, stuck check and repairs, up to ``MAX_TURNS`` steps."""
        step = 0
        try:
            for step in range(1, MAX_TURNS + 1):
                self._check_interrupt()
                self._total_turns += 1
                try:
                    msg = turn_loop.call_model(self, state, step)
                except _Interrupted:
                    raise
                except Exception as e:  # noqa: BLE001
                    if self._remaining_s() == 0.0:
                        # The read timeout was sized to the deadline, so a
                        # fault past it is the budget expiring, not a server
                        # error: keep the on-disk work instead of discarding it.
                        raise _Interrupted("deadline") from e
                    err = f"{type(e).__name__}: {e}"
                    self._emit("error", {"turn": self._total_turns, "error": err})
                    return turn_loop.finish_turn(self, state, "", step, "error", error=err)
                usage = turn_loop.absorb_reply(self, state, msg)
                if turn_loop.handle_truncated(self, state, msg, usage):
                    continue
                tool_calls = msg.get("tool_calls") or []
                state.tool_call_count += len(tool_calls)
                if not tool_calls:
                    final = msg.get("content") or ""
                    if turn_loop.finish_or_repair(self, state, final):
                        continue
                    return turn_loop.finish_turn(self, state, final, step, "model_done")
                calls, errors, mutated, last_msg, edited = turn_loop.dispatch_step(
                    self, state, tool_calls,
                )
                if turn_loop.check_stuck(self, state, calls, errors, mutated, last_msg):
                    return turn_loop.finish_turn(self, state, stuck.HALT_TEXT, step, "stuck")
                turn_loop.run_repairs(self, state, edited)
        except _Interrupted as stop:
            return turn_loop.finish_halted(self, state, stop.reason, step or 1)
        except Exception as e:  # noqa: BLE001
            # A fault outside the model call (a tool dispatch, the gate, the
            # verifier) still owes the caller a result with the files on disk.
            err = f"{type(e).__name__}: {e}"
            self._emit("error", {"turn": self._total_turns, "error": err})
            return turn_loop.finish_turn(self, state, "", step or 1, "error", error=err)
        return turn_loop.finish_turn(
            self, state, "(halted: hit MAX_TURNS without model terminating)",
            MAX_TURNS, "max_turns",
        )

    def close(self) -> None:
        """Emit session_done. Idempotent. REPL mode calls this on exit so the
        session log has a definitive terminator. One-shot ``run()`` calls it
        for the same reason."""
        if self._closed:
            return
        self._closed = True
        if self.snapshotter is not None:
            self.snapshotter.prune()
        # Tear down any MCP servers attached to this session's registry
        # before the session_done event is emitted. Subprocess teardown is
        # best-effort; close_mcp_clients swallows exceptions on its own.
        self.registry.close_mcp_clients()
        if self._started:
            self._emit("session_done", {
                "turns": self._total_turns,
                "halted_reason": "closed",
            })

    def run(
        self, user_prompt: str, *, deadline_s: float | None = None,
    ) -> SessionResult:
        """One-shot: lazily start, run a single prompt, emit session_done with
        that prompt's halted_reason. Preserves the original single-shot shape
        so existing callers (print mode) don't change semantics."""
        result = self.run_turn(user_prompt, deadline_s=deadline_s)
        self._emit("session_done", {
            "turns": result.turns,
            "halted_reason": result.halted_reason,
        })
        self._closed = True
        return result
