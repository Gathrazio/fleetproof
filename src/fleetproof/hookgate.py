"""Claude Code hook entry points.

These functions are what the plugin's ``type: "command"`` hooks invoke. They are
deterministic and run in a process separate from the agent whose "done" claim
they are judging — which is the whole product.

- :func:`stop_gate_main` — the Stop hook. Runs the independent checker at bridge
  tier and, if a blocking check failed, emits ``{"decision": "block", "reason":
  ...}`` so Claude Code refuses to let the agent stop on a false "done". Also
  sweeps the dispatch ledger for this session, so a bridge cannot stop while
  dispatches it started are still hanging half-finished.
- :func:`subagent_start_main` — the SubagentStart hook. Context-only by contract
  (it cannot block), so its whole job is getting the dispatch on record *before*
  the subagent does any work.
- :func:`subagent_stop_main` — the SubagentStop hook. The per-subagent gate: it
  records what the agent claimed, then grades it against that agent's tier of the
  check spec and blocks the stop when the claim does not survive.
- :func:`record_tool_main` — the PostToolUse hook. Appends an evidence record for
  the tool call that just ran. Never blocks (the tool already happened).

All read the hook payload as JSON on stdin, per the Claude Code hooks contract.

What the harness does and does not give us (verified against the hooks docs;
everything here is designed to fail open around the gaps):

- SubagentStart carries ``{session_id, transcript_path, cwd, hook_event_name,
  agent_id, agent_type}`` and cannot block.
- SubagentStop adds ``last_assistant_message`` and can block, same contract as Stop.
- The spawn *prompt* is in neither payload. So a captured dispatch records a
  placeholder prompt and says so, rather than inventing the prompt it did not see.
- There is no correlation field between the Task-tool call that spawned an agent
  and that agent's stop, so ``agent_id`` + ``session_id`` is the only join key.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .checker import run_checks, select_checks, spec_drifted
from .checks import (
    SPEC_DRIFT_NOTE,
    CheckSpecError,
    load_checks,
    short_spec_hash,
    spec_hash,
)
from .ledger import (
    CAPTURE_START,
    CAPTURE_STOP_ONLY,
    STATE_CONTRADICTED,
    STATE_DISPATCHED,
    STATE_VERIFIED,
    TIER_BRIDGE,
    LedgerError,
    close_dispatch,
    create_dispatch,
    find_dispatch_by_agent,
    list_dispatches,
    load_dispatch,
    record_report,
    record_verdict,
)
from .runlog import SESSION_ID_ENV, record


def _read_hook_input() -> dict[str, Any]:
    try:
        data = sys.stdin.read()
    except Exception:
        return {}
    # Tolerate a UTF-8 BOM (some shells prepend one when piping) — silently
    # losing the payload would silently lose session grouping and drift
    # detection, which is exactly the quiet degradation this tool exists to avoid.
    data = data.lstrip("\ufeff")
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


def _spec_gate() -> tuple[str | None, str | None]:
    """The bridge's spec verdict, as ``(block_reason_or_None, context_or_None)``.

    Split out of :func:`stop_gate` so the ledger sweep can be merged into the same
    decision without duplicating any of this wording.

    Graded at ``tier="bridge"``, which is not a narrowing for a v0.1 spec:
    ``select_checks("bridge")`` selects bridge-tier checks *plus every untiered
    check*, so an entirely untiered spec still selects everything and this gate
    behaves identically to the untiered v0.1 call. What it fixes is the tiered
    case — without a tier, the bridge's Stop hook grades itself on leaf- and
    lane-tier checks that describe some subagent's work, not the session's, and
    blocks the bridge on a failure that was never the bridge's to answer for.
    """
    try:
        checks = load_checks()
    except CheckSpecError:
        # No spec (or a broken one) means nothing to enforce. Fail open, but visibly:
        # a verification tool must never pretend it verified when it did not.
        return None, None

    if not checks:
        return None, None

    report = run_checks(checks, tier=TIER_BRIDGE)

    # Spec-drift check: did the checks.json that just graded this verdict differ
    # from the one the session's first verdict was graded against? An agent is
    # allowed to author checks.json, so a failing agent could quietly weaken it to
    # slip this gate. We don't block on drift alone (v0.1 policy) — we make it loud.
    session_id = os.environ.get(SESSION_ID_ENV)
    drifted, baseline = spec_drifted(report.spec_sha256, session_id)

    if report.verdict == "pass":
        # A pass with drift still passes, but must not pass *silently*.
        return None, (_drift_context(report.spec_sha256, baseline) if drifted else None)

    reason = _failure_reason(report)
    if drifted:
        reason += " " + SPEC_DRIFT_NOTE
    return reason, _evidence_context(report, drifted, baseline)


def _failure_reason(report) -> str:
    """The canonical "you said done, the checker says no" block reason."""
    failing = report.blocking_failures
    lines = [f"{r.id} ({r.detail})" for r in failing]
    return (
        f"FleetProof: {len(failing)}/{report.total} blocking check(s) failed — "
        + "; ".join(lines)
        + ". The agent reported done; the independent checker disagrees. "
        "Fix the failures and let the checker re-run before stopping."
    )


def ledger_sweep(session_id: str | None) -> tuple[list[str], list[str]]:
    """Return ``(stalled, in_fleet)`` dispatch run ids for a session.

    *stalled* — a dispatch that reported (or was graded) and then never terminated.
    Something started closing it out and stopped halfway, which is exactly the
    quiet half-finished state this tool exists to surface, so the bridge is blocked
    on it.

    *in_fleet* — still in state ``dispatched``. That is a legitimately-running
    background agent, not a fault, so it is reported and not blocked on. Blocking
    here would make it impossible to ever stop while a long subagent runs.
    """
    if not session_id:
        return [], []
    stalled: list[str] = []
    in_fleet: list[str] = []
    for record_obj in list_dispatches(session_id=session_id, non_terminal_only=True):
        if record_obj.state == STATE_DISPATCHED:
            in_fleet.append(record_obj.run_id)
        else:
            stalled.append(record_obj.run_id)
    return stalled, in_fleet


def _stalled_reason(stalled: list[str]) -> str:
    return (
        f"FleetProof: {len(stalled)} dispatch(es) reported but never terminated — "
        + ", ".join(stalled)
        + ". Verify or close each one (`fleetproof dispatch close <run_id>`) "
        "before stopping; a half-closed dispatch is how work goes missing."
    )


def _ledger_context(stalled: list[str], in_fleet: list[str]) -> str | None:
    """Operator-facing ledger summary, or None when this session has no dispatches."""
    parts: list[str] = []
    for run_id in stalled:
        parts.append(f"- [stalled] {run_id}: reported but never terminated")
    for run_id in in_fleet:
        parts.append(f"- [still in fleet] {run_id}: dispatched, no report yet")
    if not parts:
        return None
    parts.append("Board: `fleetproof fleet --open`")
    return "\n".join(parts)


def stop_gate() -> tuple[dict[str, Any] | None, int]:
    """Run the checker, sweep the ledger, and decide whether Claude may stop.

    Returns ``(decision_dict_or_None, exit_code)``. A None decision + exit 0 means
    "let the agent stop"; a block decision + exit 0 means "you claimed done but the
    independent checker disagrees — keep going."

    Two independent grounds to block: the check spec failed (v0.1), or this
    session left dispatches stalled (v0.2). A session with no dispatches produces
    byte-identical output to v0.1 — the sweep adds nothing when there is nothing
    to sweep.
    """
    reason, context = _spec_gate()

    session_id = os.environ.get(SESSION_ID_ENV)
    stalled, in_fleet = ledger_sweep(session_id)
    if stalled:
        stalled_reason = _stalled_reason(stalled)
        reason = f"{reason} {stalled_reason}" if reason else stalled_reason

    context_parts = [p for p in (context, _ledger_context(stalled, in_fleet)) if p]
    if reason is None and not context_parts:
        return None, 0

    decision: dict[str, Any] = {}
    if reason is not None:
        decision["decision"] = "block"
        decision["reason"] = reason
    if context_parts:
        decision["hookSpecificOutput"] = {
            "hookEventName": "Stop",
            "additionalContext": "\n".join(context_parts),
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


# === Subagent capture + gate ===

# The harness does not put the spawn prompt in either subagent payload, so a
# captured dispatch says exactly that instead of pretending to quote a prompt it
# never saw. Phase C's pilot measures how much this costs us.
UNCAPTURED_PROMPT = "[uncaptured] subagent {agent_type} spawn — prompt not in harness payload"

# Policy v0.2: any subagent captured inside a session is lane tier. Real nesting
# depth is not visible from the payload (no parent-agent field), so declaring it
# beats inferring it wrongly; measuring actual depth is a pilot question.
CAPTURED_SUBAGENT_TIER = "lane"

VERDICT_BY = "checker-via-hook"


def _agent_fields(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(agent_id, agent_type)`` from a subagent payload; None for either if absent."""
    out: list[str | None] = []
    for key in ("agent_id", "agent_type"):
        val = payload.get(key)
        out.append(val if isinstance(val, str) and val else None)
    return out[0], out[1]


def _placeholder_prompt(agent_type: str | None) -> str:
    return UNCAPTURED_PROMPT.format(agent_type=agent_type or "unknown")


def capture_subagent_start(payload: dict[str, Any]) -> str:
    """Put a spawning subagent on the ledger before it does any work."""
    agent_id, agent_type = _agent_fields(payload)
    return create_dispatch(
        _placeholder_prompt(agent_type),
        tier=CAPTURED_SUBAGENT_TIER,
        agent={"agent_id": agent_id, "agent_type": agent_type, "capture": CAPTURE_START},
        by="hook",
    )


def subagent_start_main() -> int:
    """SubagentStart hook: record the dispatch. Cannot block, must never crash.

    Emits nothing on success — a context-only hook that printed on every spawn
    would just be noise in the transcript.
    """
    payload = _read_hook_input()
    _apply_session_id(payload)
    try:
        capture_subagent_start(payload)
    except Exception as e:
        # A capture failure must not break the spawn, but a silently missing
        # capture is exactly the invisible hole this tool exists to prevent.
        sys.stderr.write(f"[fleetproof] subagent-start capture failed: {e}\n")
    return 0


def _subagent_block(reason: str, context: str | None = None) -> dict[str, Any]:
    decision: dict[str, Any] = {"decision": "block", "reason": reason}
    if context:
        decision["hookSpecificOutput"] = {
            "hookEventName": "SubagentStop",
            "additionalContext": context,
        }
    return decision


def _pin_drift_reason(pinned: str | None, current: str | None) -> str:
    return (
        "FleetProof: the check spec changed after this work was dispatched "
        f"(pinned {short_spec_hash(pinned)}, now {short_spec_hash(current)}). "
        "A dispatched agent is graded against the spec that was in force when it "
        "was dispatched, so this stop is blocked until the spec is restored or the "
        "work is re-dispatched against the new one. " + SPEC_DRIFT_NOTE
    )


def _try_close(run_id: str) -> None:
    """Terminate a dispatch, tolerating an already-closed one."""
    try:
        close_dispatch(run_id, by="hook")
    except LedgerError as e:
        sys.stderr.write(f"[fleetproof] could not close dispatch {run_id}: {e}\n")


def subagent_stop(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """The per-subagent gate. Returns ``(decision_or_None, exit_code)``.

    Order matters, and each step is a separate reason to refuse the stop:

    1. Find (or back-fill) this agent's dispatch — a subagent we never saw start
       still gets a record, marked ``stop-only``.
    2. No final message means no report. Block without transitioning: an agent
       that went idle saying nothing has not reported, and recording it as
       ``reported`` would launder silence into a claim.
    3. If the spec changed since this dispatch was pinned, block. At lane tier and
       deeper the pinned spec is the contract; grading against a spec the agent
       could have edited mid-flight is not verification.
    4. Grade the agent's own tier of the spec. Blocking failure -> ``contradicted``
       and block (the agent gets its turn back, and the retry re-reports onto this
       same dispatch). Otherwise -> ``verified`` and terminate.

    When the agent's tier selects no checks, no verdict is recorded at all: the
    dispatch is terminated still-ungraded, because an absent grade must never read
    as a passing grade.
    """
    session_id = os.environ.get(SESSION_ID_ENV)
    agent_id, agent_type = _agent_fields(payload)

    dispatch = find_dispatch_by_agent(session_id, agent_id)
    if dispatch is None:
        run_id = create_dispatch(
            _placeholder_prompt(agent_type),
            tier=CAPTURED_SUBAGENT_TIER,
            agent={"agent_id": agent_id, "agent_type": agent_type,
                   "capture": CAPTURE_STOP_ONLY},
            by="hook",
        )
        dispatch = load_dispatch(run_id)
        if dispatch is None:  # pragma: no cover - would mean the write vanished
            raise LedgerError(f"back-filled dispatch {run_id} did not read back")

    last_message = payload.get("last_assistant_message")
    last_message = last_message if isinstance(last_message, str) else ""
    if not last_message.strip():
        return _subagent_block(
            "FleetProof report-before-idle: subagent produced no final report "
            "message, so there is nothing to verify. State what you did, what you "
            "verified, and what you did not, then stop.",
            f"- dispatch {dispatch.run_id} stays in state '{dispatch.state}'; "
            "no report was recorded.",
        ), 0

    if dispatch.state in (STATE_DISPATCHED, STATE_CONTRADICTED):
        record_report(
            dispatch.run_id,
            {"summary": last_message, "source": "last_assistant_message"},
            by="hook",
        )
        dispatch = load_dispatch(dispatch.run_id) or dispatch

    pinned = dispatch.spec_sha256_pinned
    current = spec_hash()
    if pinned and current and pinned != current:
        return _subagent_block(
            _pin_drift_reason(pinned, current),
            f"- dispatch {dispatch.run_id} pinned spec {short_spec_hash(pinned)}\n"
            f"- current spec              {short_spec_hash(current)}",
        ), 0

    try:
        checks = load_checks()
    except CheckSpecError:
        # No spec to grade against. Fail open on the stop, but do not manufacture
        # a verdict out of the absence of checks.
        checks = []
    selected = select_checks(checks, dispatch.tier) if checks else []
    if not selected:
        _try_close(dispatch.run_id)
        return None, 0

    report = run_checks(selected, record_to_log=True, tier=dispatch.tier)
    if report.blocking_failures:
        record_verdict(
            dispatch.run_id,
            STATE_CONTRADICTED,
            detail="; ".join(r.id for r in report.blocking_failures),
            by=VERDICT_BY,
        )
        return _subagent_block(_failure_reason(report), _evidence_context(report)), 0

    record_verdict(
        dispatch.run_id,
        STATE_VERIFIED,
        detail=f"{report.passed}/{report.total} checks passed at tier {dispatch.tier}",
        by=VERDICT_BY,
    )
    _try_close(dispatch.run_id)
    return None, 0


def subagent_stop_main() -> int:
    payload = _read_hook_input()
    _apply_session_id(payload)
    try:
        decision, code = subagent_stop(payload)
    except Exception as e:
        # The gate broke. Fail open — never wedge a fleet on our own bug — but say
        # so on stderr, because an unenforced gate that looks enforced is worse
        # than no gate.
        sys.stderr.write(f"[fleetproof] subagent-stop gate failed open: {e}\n")
        return 0
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
