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
import sys
from typing import Any

from .checker import run_checks
from .checks import CheckSpecError, load_checks
from .runlog import record


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
    if report.verdict == "pass":
        return None, 0

    failing = report.blocking_failures
    lines = [f"{r.id} ({r.detail})" for r in failing]
    reason = (
        f"FleetProof: {len(failing)}/{report.total} blocking check(s) failed — "
        + "; ".join(lines)
        + ". The agent reported done; the independent checker disagrees. "
        "Fix the failures and let the checker re-run before stopping."
    )
    decision = {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": _evidence_context(report),
        },
    }
    return decision, 0


def _evidence_context(report) -> str:
    parts = []
    for r in report.results:
        status = "pass" if r.passed else ("FAIL" if r.blocking else "warn")
        parts.append(f"- [{status}] {r.id}: {r.detail}")
    if report.run_id:
        parts.append(f"Evidence recorded under run {report.run_id} in .fleetproof/runs/.")
    return "\n".join(parts)


def stop_gate_main() -> int:
    _read_hook_input()  # consume stdin per contract; the verdict is checker-driven
    decision, code = stop_gate()
    if decision is not None:
        sys.stdout.write(json.dumps(decision))
    return code


def record_tool_main() -> int:
    """PostToolUse recorder: accrete an evidence record. Always non-blocking."""
    payload = _read_hook_input()
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
