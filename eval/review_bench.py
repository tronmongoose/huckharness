"""Reviewer bench: replay the nightshift reviewer's labelled PR diffs through
local models and score agreement with the Opus verdicts.
Exports: labels_path, load_labels, judge, score, main."""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coding_harness.core import paths  # noqa: E402
from coding_harness.core.review_score import (  # noqa: E402
    DEFAULT_RULE,
    score,
    switch_ok,
)
from coding_harness.models import ollama  # noqa: E402

TRANSCRIPTS = str(Path.home() / ".claude/projects/*nsreview*/*.jsonl")
RESULTS = Path(__file__).resolve().parent / "results"
MAX_DIFF_CHARS = 24000
_PR_RE = re.compile(r"PR #(\d+): (.*)")
_DIFF_MARK = "--- diff ---\n"


def _verdict(text: str) -> str | None:
    """PASS or FAIL from the last JSON object in ``text``, else None."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        v = json.loads(text[start:end + 1]).get("verdict")
    except (ValueError, AttributeError):
        return None
    return v if v in ("PASS", "FAIL") else None


def _transcript(path: str) -> dict[str, Any] | None:
    """Prompt and final Opus text of one reviewer session."""
    prompt, last = None, None
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        content = (r.get("message") or {}).get("content")
        if r.get("type") == "user" and isinstance(content, str) and prompt is None:
            if content.startswith("You are reviewing"):
                prompt = content
        if r.get("type") == "assistant":
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "text":
                    last = c["text"]
    if not prompt or not last:
        return None
    label = _verdict(last)
    m = _PR_RE.search(prompt)
    if label is None or not m or _DIFF_MARK not in prompt:
        return None
    return {
        "session": Path(path).stem,
        "pr": int(m.group(1)),
        "title": m.group(2).strip(),
        "prompt": prompt,
        "diff_chars": len(prompt.split(_DIFF_MARK, 1)[1]),
        "opus": label,
    }


def labels_path() -> Path:
    """The label cache under the harness state root, resolved per call."""
    return paths.meta_dir() / "review-labels.json"


def load_labels(refresh: bool = False) -> list[dict[str, Any]]:
    """Labelled rows, cached outside the repo (the diffs are private code)."""
    labels = labels_path()
    if labels.is_file() and not refresh:
        return json.loads(labels.read_text())
    rows = [t for t in (_transcript(p) for p in sorted(glob.glob(TRANSCRIPTS))) if t]
    labels.parent.mkdir(parents=True, exist_ok=True)
    labels.write_text(json.dumps(rows))
    return rows


def _clamp(prompt: str) -> str:
    """Keep the diff under the model's window; note the cut in the prompt."""
    head, diff = prompt.split(_DIFF_MARK, 1)
    if len(diff) <= MAX_DIFF_CHARS:
        return prompt
    return head + _DIFF_MARK + diff[:MAX_DIFF_CHARS] + "\n[diff truncated by the bench]\n"


def judge(model: str, row: dict[str, Any], timeout: float) -> dict[str, Any]:
    """One diff-only verdict from ``model``; no repo inspection, unlike Opus."""
    t0 = time.monotonic()
    try:
        msg = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": _clamp(row["prompt"])}],
            tools=None, max_tokens=512, temperature=0.1, timeout=timeout,
            response_format={"type": "json_object"},
        )
        text, error = msg.get("content", "") or "", None
    except Exception as e:  # noqa: BLE001 the bench records the failure as a row
        text, error = "", f"{type(e).__name__}: {e}"
    return {"pr": row["pr"], "opus": row["opus"], "local": _verdict(text),
            "seconds": round(time.monotonic() - t0, 1), "error": error}


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--model`` repeatable; writes eval/results/review-bench-<date>.json."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--refresh-labels", action="store_true")
    ap.add_argument("--switch-check", action="store_true",
                    help="exit non-zero unless every benched model clears the switch rule")
    args = ap.parse_args(argv)
    rows = load_labels(args.refresh_labels)
    if args.limit:
        rows = rows[:args.limit]
    print(f"labels: {len(rows)} rows, opus FAIL={sum(r['opus'] == 'FAIL' for r in rows)}")
    report: dict[str, Any] = {"date": time.strftime("%Y-%m-%d"), "models": {}}
    blocked = False
    for model in args.model:
        ollama.assert_model_allowed(model)
        verdicts = [judge(model, r, args.timeout) for r in rows]
        s = score(verdicts)
        ok, reasons = switch_ok(s, DEFAULT_RULE)
        report["models"][model] = {"score": s, "switch_ok": ok, "blockers": reasons,
                                   "verdicts": verdicts}
        print(model, json.dumps(s), flush=True)
        for r in reasons:
            print(f"  blocker: {r}", flush=True)
        blocked = blocked or not ok
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"review-bench-{report['date']}.json"
    out.write_text(json.dumps(report, indent=1))
    print(f"wrote {out}", file=sys.stderr)
    return 1 if (args.switch_check and blocked) else 0


if __name__ == "__main__":
    sys.exit(main())
