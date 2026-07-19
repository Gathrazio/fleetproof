"""Claude Code hook entry points.

These functions are what the plugin's ``type: "command"`` hooks invoke. They are
deterministic and run in a process separate from the agent whose "done" claim
they are judging — which is the whole product.

- :func:`stop_gate_main` — the Stop hook. Runs the independent checker and, if a
  blocking check failed, emits ``{"decision": "block", "reason": ...}`` so Claude
  Code refuses to let the agent stop on a false "done".
- :func:`record_tool_main` — the PostToolUse hook. Appends an evidence record for
  the tool call that just ran. Never blocks (the tool already happened).

Both read the hook payload as JSON on stdin, per the Claude Code hooks contract.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .checker import run_checks, spec_drifted
from .checks import SPEC_DRIFT_NOTE, CheckSpecError, load_checks, short_spec_hash
from .runlog import SESSION_ID_ENV, record


def _read_hook_input() -> dict[str, Any]:
    try:
        data = sys.stdin.read()
    except Exception:
        return {}
    if not data.strip():
        return {}
    try:
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _apply_session_id(payload: dict[str, Any]) -> None:
    """Thread the Claude Code session id into the run log.

    Every hook invocation receives a JSON payload on stdin whose ``session_id``
    field identifies the session (see the "hook input" section of
    https://code.claude.com/docs/en/hooks). Each hook fires as its own OS
    process with a fresh run id, so without this the report shows one session as
    several unrelated root runs. Carrying the id via env lets the root record
    (written in this same process) group them back together.
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid:
        os.environ[SESSION_ID_ENV] = sid


def stop_gate() -> tuple[dict[str, Any] | None, int]:
    """Run the checker and decide whether Claude may stop.

    Returns ``(decision_dict_or_None, exit_code)``. A None decision + exit 0 means
    "let the agent stop"; a block decision + exit 0 means "you claimed done but the
    independent checker disagrees — keep going."
    """
    try:
        checks = load_checks()
    except CheckSpecError:
        # No spec (or a broken one) means nothing to enforce. Fail open, but visibly:
        # a verification tool must never pretend it verified when it did not.
        return None, 0

    if not checks:
        return None, 0

    report = run_checks(checks)

    # Spec-drift check: did the checks.json that just graded this verdict differ
    # from the one the session's first verdict was graded against? An agent is
    # allowed to author checks.json, so a failing agent could quietly weaken it to
    # slip this gate. We don't block on drift alone (v0.1 policy) — we make it loud.
    session_id = os.environ.get(SESSION_ID_ENV)
    drifted, baseline = spec_drifted(report.spec_sha256, session_id)

    if report.verdict == "pass":
        if drifted:
            # A pass with drift still passes, but must not pass *silently*.
            return {
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": _drift_context(report.spec_sha256, baseline),
                },
            }, 0
        return None, 0

    failing = report.blocking_failures
    lines = [f"{r.id} ({r.detail})" for r in failing]
    reason = (
        f"FleetProof: {len(failing)}/{report.total} blocking check(s) failed — "
        + "; ".join(lines)
        + ". The agent reported done; the independent checker disagrees. "
        "Fix the failures and let the checker re-run before stopping."
    )
    if drifted:
        reason += " " + SPEC_DRIFT_NOTE
    decision = {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": _evidence_context(report, drifted, baseline),
        },
    }
    return decision, 0


def _drift_context(current_hash: str | None, baseline_hash: str | None) -> str:
    """The unmissable drift annotation, with both hashes for a reviewer to diff."""
    return (
        f"{SPEC_DRIFT_NOTE}\n"
        f"- current spec hash:  {short_spec_hash(current_hash)}\n"
        f"- session baseline:   {short_spec_hash(baseline_hash)}"
    )


def _evidence_context(report, drifted: bool = False, baseline_hash: str | None = None) -> str:
    parts = []
    if drifted:
        parts.append(_drift_context(report.spec_sha256, baseline_hash))
    for r in report.results:
        status = "pass" if r.passed else ("FAIL" if r.blocking else "warn")
        parts.append(f"- [{status}] {r.id}: {r.detail}")
    parts.append(f"Spec hash: {short_spec_hash(report.spec_sha256)}")
    if report.run_id:
        parts.append(f"Evidence recorded under run {report.run_id} in .fleetproof/runs/.")
    return "\n".join(parts)


def stop_gate_main() -> int:
    # Consume stdin per contract; the verdict is checker-driven, but the payload
    # still carries the session id we group this run's evidence under.
    _apply_session_id(_read_hook_input())
    decision, code = stop_gate()
    if decision is not None:
        sys.stdout.write(json.dumps(decision))
    return code


def record_tool_main() -> int:
    """PostToolUse recorder: accrete an evidence record. Always non-blocking."""
    payload = _read_hook_input()
    _apply_session_id(payload)
    tool_name = str(payload.get("tool_name", "unknown"))
    try:
        with record("claude-tool", tool_name, {"tool_name": tool_name}) as handle:
            handle.set_output({
                "tool_name": tool_name,
                "tool_input": payload.get("tool_input"),
                "tool_response_present": "tool_response" in payload,
            })
    except Exception:
        # An evidence-recorder that crashes must not break the agent's turn.
        return 0
    return 0
