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
- The spawn *prompt* is in neither payload. A captured dispatch records a
  placeholder prompt and says so, rather than inventing the prompt it did not
  see — unless the dispatcher declared it up front via an intent sidecar
  (``.fleetproof/intents/<agent_type>.json``), which the start capture consumes.
- There is no correlation field between the Task-tool call that spawned an agent
  and that agent's stop, so ``agent_id`` + ``session_id`` is the only join key.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from pathlib import Path

from .checker import (
    CheckReport,
    run_checks,
    select_checks,
    session_spec_baseline,
    spec_drifted,
)
from .checks import (
    SPEC_DRIFT_NOTE,
    Check,
    CheckSpecError,
    load_checks,
    short_spec_hash,
    spec_hash,
)
from .ledger import (
    CAPTURE_START,
    STATE_CONTRADICTED,
    STATE_DISPATCHED,
    STATE_VERIFIED,
    TIER_BRIDGE,
    TIER_SOURCE_DEFAULTED,
    LedgerError,
    close_dispatch,
    consume_intent,
    create_dispatch,
    find_dispatch_by_agent,
    intents_dir,
    list_dispatches,
    load_dispatch,
    record_orphan_stop,
    record_report,
    record_verdict,
)
from .runlog import SESSION_ID_ENV, record, runs_dir
from .telemetry import build_telemetry


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
    session_id = os.environ.get(SESSION_ID_ENV)
    try:
        checks = load_checks()
    except CheckSpecError as e:
        # Fail open only when nothing was ever promised (an unconfigured repo).
        # A session baseline proves a spec existed and graded work earlier this
        # session — a promise — so a now-unreadable spec blocks instead of
        # silently switching the gate off (finding C3: deleting, emptying, or
        # corrupting checks.json must not read as "verified").
        baseline = session_spec_baseline(session_id)
        if baseline:
            return (
                "FleetProof: a check spec graded this session earlier (baseline "
                f"{short_spec_hash(baseline)}) but is now missing or unreadable "
                f"({e}). Restore .fleetproof/checks.json before stopping — a "
                "promised gate cannot be switched off by removing its spec.",
                None,
            )
        return None, None

    if not checks:
        baseline = session_spec_baseline(session_id)
        if baseline:
            return (
                "FleetProof: .fleetproof/checks.json now declares zero checks, "
                "but a spec graded this session earlier (baseline "
                f"{short_spec_hash(baseline)}). An emptied spec mid-session is "
                "how a gate gets neutered; restore the checks before stopping.",
                None,
            )
        return None, None

    report = run_checks(checks, tier=TIER_BRIDGE)

    # Spec-drift check: did the checks.json that just graded this verdict differ
    # from the one the session's first verdict was graded against? An agent is
    # allowed to author checks.json, so a failing agent could quietly weaken it to
    # slip this gate. We don't block on drift alone (v0.1 policy) — we make it loud.
    drifted, baseline = spec_drifted(report.spec_sha256, session_id)

    if report.total == 0 and any(c.block for c in checks):
        # The spec has blocking checks, yet none governs the bridge tier — every
        # check was tiered to a lower rung (finding C4: one word per check
        # disables the gate). An absent grade is not a passing grade.
        reason = (
            "FleetProof: no check in .fleetproof/checks.json governs the bridge "
            "tier — every blocking check is tiered to a lower rung, so this "
            "session's own work would go ungraded. Add a bridge-tier or untiered "
            "check (or correct the tiers) before stopping."
        )
        if drifted:
            reason += " " + SPEC_DRIFT_NOTE
        return reason, (_drift_context(report.spec_sha256, baseline) if drifted else None)

    if report.verdict == "pass":
        # A pass with drift still passes, but must not pass *silently*. Said once
        # per (session, baseline→current) pair, though: the first stop after a
        # spec edit announces it for review; repeating the same note on every
        # later stop turns a review prompt into a nag the harness re-engages the
        # agent over (observed live: nine forced continuations in one session).
        if drifted and _drift_unnoted(session_id, baseline, report.spec_sha256):
            return None, _drift_context(report.spec_sha256, baseline)
        return None, None

    reason = _failure_reason(report)
    if drifted:
        reason += " " + SPEC_DRIFT_NOTE
    return reason, _evidence_context(report, drifted, baseline)


# The first line of every gate block. A block rendered as prose gets read as
# conversational input and argued with — one agent restated its report through
# 19 block/retry cycles (observed in a field deployment on Windows). The
# marker is the machine-shaped opener nothing else in a transcript looks like.
GATE_BLOCK_MARKER = "[FLEETPROOF GATE — AUTOMATED BLOCK, NOT A USER MESSAGE]"

# Ceiling on a composed failure reason, so it survives as a single payload.
# The additionalContext channel proved unreliable across retries in the field,
# which is why the reason itself carries everything, capped.
FAILURE_REASON_CAP = 1500

# How much of the cap the failing-checks header may consume before it, too,
# is truncated — the per-check lines and the exit instructions must survive.
_FAILURE_HEADER_CAP = 600

_BLOCK_EXITS = (
    "You may not stop by restating your report. Exits: (1) fix the failure "
    "and stop again; (2) if this check is NOT satisfiable from your seat, say "
    "exactly that in your report — the dispatcher must park or re-dispatch "
    "you; do not loop."
)

_TRUNCATION_MARK = " ...(truncated)"


def _capped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:max(limit - len(_TRUNCATION_MARK), 0)] + _TRUNCATION_MARK


def _failure_reason(report) -> str:
    """The canonical "you said done, the checker says no" block reason.

    Leads with the gate marker and carries everything every time — the failing
    ids, the full per-check breakdown, the spec hash and evidence run id, and
    the two legitimate exits. Capped so it survives as one payload; the cap
    eats the per-check lines, never the exit instructions.
    """
    failing = report.blocking_failures
    header = _capped(
        f"{len(failing)}/{report.total} blocking check(s) failed — "
        + "; ".join(f"{r.id} ({r.detail})" for r in failing) + ".",
        _FAILURE_HEADER_CAP,
    )
    evidence = (f"Spec {short_spec_hash(report.spec_sha256)} · "
                f"evidence run {report.run_id or 'unrecorded'}")
    fixed = (GATE_BLOCK_MARKER, header, evidence, _BLOCK_EXITS)
    per_check = "Per-check: " + "; ".join(
        f"[{'pass' if r.passed else ('FAIL' if r.blocking else 'warn')}] "
        f"{r.id}: {r.detail}"
        for r in report.results)
    budget = FAILURE_REASON_CAP - sum(len(part) + 1 for part in fixed)
    per_check = _capped(per_check, max(budget, len("Per-check:")))
    return "\n".join([GATE_BLOCK_MARKER, header, per_check, evidence, _BLOCK_EXITS])


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


def _drift_marker_path() -> Path:
    return runs_dir().parent / "drift-noted.json"


def _drift_unnoted(session_id: str | None, baseline: str | None, current: str | None) -> bool:
    """True the first time this exact drift is seen for this session — and records it.

    Best-effort marker file under ``.fleetproof/``. Any read/write failure reads
    as "unnoted", which errs loud (the note repeats) rather than quiet (a drift
    that was never announced) — a gate must not lose loudness to its own I/O.
    """
    key = f"{session_id or 'no-session'}:{baseline or '?'}=>{current or '?'}"
    path = _drift_marker_path()
    try:
        noted = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(noted, list):
            noted = []
    except Exception:
        noted = []
    if key in noted:
        return False
    noted.append(key)
    try:
        path.write_text(json.dumps(noted, indent=1), encoding="utf-8")
    except Exception:
        pass
    return True


def _evidence_context(report, drifted: bool = False, baseline_hash: str | None = None) -> str:
    parts = []
    if drifted:
        parts.append(_drift_context(report.spec_sha256, baseline_hash))
    for r in report.results:
        status = "pass" if r.passed else ("FAIL" if r.blocking else "warn")
        parts.append(f"- [{status}] {r.id}: {r.detail}")
    parts.append(f"Spec hash: {short_spec_hash(report.spec_sha256)}")
    if getattr(report, "cwd", None):
        parts.append(f"cwd: {report.cwd}")
    if report.run_id:
        parts.append(f"Evidence recorded under run {report.run_id} in .fleetproof/runs/.")
    return "\n".join(parts)


def stop_gate_main() -> int:
    # Consume stdin per contract; the verdict is checker-driven, but the payload
    # still carries the session id we group this run's evidence under.
    payload = _read_hook_input()
    _apply_session_id(payload)
    decision, code = stop_gate()
    if decision is not None and _suppress_on_retry(payload, decision):
        return code
    if decision is not None:
        sys.stdout.write(json.dumps(decision))
    return code


def _suppress_on_retry(payload: dict[str, Any], decision: dict[str, Any]) -> bool:
    """Whether to swallow a *context-only* decision on a stop-hook continuation.

    ``stop_hook_active`` (hooks contract) is true when this stop is already the
    continuation a prior Stop-hook decision forced. Emitting context-only output
    again re-engages the agent again — the loop only ends at the harness's block
    cap (observed live: nine forced continuations). A real ``block`` decision is
    never suppressed: re-grading the retry is the gate's entire job, and a false
    "done" must not become passable by simply stopping twice.
    """
    return bool(payload.get("stop_hook_active")) and "decision" not in decision


# === Subagent capture + gate ===

# The harness does not put the spawn prompt in either subagent payload, so a
# captured dispatch says exactly that instead of pretending to quote a prompt it
# never saw. The intent sidecar is the dispatcher's way around the gap; this
# placeholder is what every dispatch without one records.
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
    """Put a spawning subagent on the ledger before it does any work.

    When the dispatcher left an intent sidecar for this agent type
    (``.fleetproof/intents/<agent_type>.json``, written via ``fleetproof
    dispatch intent``), the dispatch is created with the intent's real prompt,
    manifest, and tier — the harness still does not carry the spawn prompt, so
    the sidecar is the only route it has onto the record. The intent is
    consumed on match: one intent, one spawn, and a second spawn of the same
    agent type gets exactly the placeholder capture a repo without sidecars
    always got. Degradations (malformed intent, malformed manifest, bad tier)
    are surfaced on stderr and in the manifest notes, never fatal — this runs
    inside a hook that must not break the spawn.
    """
    agent_id, agent_type = _agent_fields(payload)
    intent, notes = consume_intent(agent_type)
    for note in notes:
        sys.stderr.write(f"[fleetproof] {note}\n")
    if intent is None and not notes:
        # A clean miss — no sidecar matched by name or role. Said loudly,
        # because the silent version of this is a placeholder prompt and a
        # defaulted tier that nobody notices until the verdicts are worthless
        # (observed in a field deployment on Windows).
        sys.stderr.write(
            f"[fleetproof] no intent matched spawn '{agent_type or 'unknown'}' "
            "— captured with placeholder prompt at defaulted tier "
            f"'{CAPTURED_SUBAGENT_TIER}'. Sidecars present: "
            f"{_sidecar_listing()}.\n")
    prompt = intent["prompt"] if intent else _placeholder_prompt(agent_type)
    manifest = intent["manifest"] if intent else None
    intent_tier = intent["tier"] if intent else None
    # Only an intent-declared tier is a declaration. The lane fallback records
    # tier_source="defaulted": the one provenance field that could reveal an
    # intent miss must not assert a declaration nobody made.
    return create_dispatch(
        prompt,
        tier=intent_tier or CAPTURED_SUBAGENT_TIER,
        tier_source=None if intent_tier else TIER_SOURCE_DEFAULTED,
        manifest=manifest,
        agent={"agent_id": agent_id, "agent_type": agent_type, "capture": CAPTURE_START},
        intent_source=intent["source"] if intent else None,
        by="hook",
    )


def _sidecar_listing() -> str:
    """The intent files currently on disk, or 'none' — for the miss note."""
    try:
        names = sorted(p.name for p in intents_dir().glob("*.json"))
    except OSError:
        names = []
    return ", ".join(names) if names else "none"


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
        f"{GATE_BLOCK_MARKER}\n"
        "FleetProof: the check spec changed after this work was dispatched "
        f"(pinned {short_spec_hash(pinned)}, now {short_spec_hash(current)}). "
        "A dispatched agent is graded against the spec that was in force when it "
        "was dispatched, so this stop is blocked until the spec is restored or the "
        "work is re-dispatched against the new one. " + SPEC_DRIFT_NOTE
    )


def _try_close(run_id: str) -> None:
    """Terminate a dispatch, tolerating an already-closed one.

    No terminate reason is passed: the reason vocabulary exists to split apart
    the ways a dispatch dies *from ``dispatched``*, and this close only ever
    runs after a report is on record (post-verdict, or post-grading with an
    empty selection) — a state where the reason plays no part in how the
    outcome reads back.
    """
    try:
        close_dispatch(run_id, by="hook")
    except LedgerError as e:
        sys.stderr.write(f"[fleetproof] could not close dispatch {run_id}: {e}\n")


def _try_build_telemetry(run_id: str, check_report=None, checks=None) -> None:
    """Best-effort telemetry build, on the checker's side of the boundary.

    A telemetry failure must not wedge the gate. The cost of swallowing one is
    an era-stamped run without telemetry.json — which the summary surfaces as
    a ``telemetry_missing`` integrity defect, the visible form a capture
    failure is supposed to take.
    """
    try:
        build_telemetry(run_id, check_report=check_report, checks=checks)
    except Exception as e:
        sys.stderr.write(f"[fleetproof] telemetry build failed for {run_id}: {e}\n")


def _manifest_checks(dispatch) -> list[Check]:
    """Runnable blocking checks declared on the dispatch's own manifest.

    A manifest check is an ``{"id", "cmd"}`` entry: always blocking, always
    expect-exit0. The richer expectation kinds stay a ``checks.json`` feature —
    the manifest is a per-dispatch contract, and its checks exist so a
    manifest-bearing dispatch can grade without pre-registering into the
    spec-hash-pinned repo file.

    An entry that is not that shape is skipped with a stderr note rather than
    run half-parsed. A skipped check is never a passing check: its id never
    reaches the executed set, so the deliverable it vouched for reads as
    uncovered — the error lands on the self-critical side of the metric.
    """
    entries = (dispatch.manifest or {}).get("checks")
    if not isinstance(entries, list):
        return []
    out: list[Check] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        where = f"manifest check [{i}] on dispatch {dispatch.run_id}"
        if not isinstance(entry, dict):
            sys.stderr.write(f"[fleetproof] {where} is not an object; skipped\n")
            continue
        cid, cmd = entry.get("id"), entry.get("cmd")
        if not isinstance(cid, str) or not cid or not isinstance(cmd, str) or not cmd.strip():
            sys.stderr.write(
                f"[fleetproof] {where} needs a string 'id' and 'cmd'; skipped\n")
            continue
        if "\n" in cmd or "\r" in cmd:
            # Same rule the spec loader enforces (finding H5): cmd.exe executes
            # only the first line, and a check that half-runs is worse than one
            # that never runs.
            sys.stderr.write(f"[fleetproof] {where}: 'cmd' must be a single line; skipped\n")
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append(Check(id=cid, run=cmd, expect={"kind": "exit0"}, block=True,
                         description="dispatch-manifest check"))
    return out


def subagent_stop(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """The per-subagent gate. Returns ``(decision_or_None, exit_code)``.

    Order matters, and each step is a separate reason to refuse the stop:

    1. Find this agent's dispatch. An unpaired stop — no dispatch to join —
       records an orphan and terminates ungraded: harness-internal helper
       agents (summaries, titles) emit SubagentStop with no SubagentStart and
       no agent_type, and back-filling those as graded dispatches manufactured
       phantom verified verdicts (observed in a field deployment on Windows).
       Nobody ordered that work, so there is no claim to hold it to.
    2. No final message means no report. Block without transitioning: an agent
       that went idle saying nothing has not reported, and recording it as
       ``reported`` would launder silence into a claim.
    3. If the spec changed since this dispatch was pinned — and this tier has
       anything runnable to grade — block. At lane tier and deeper the pinned
       spec is the contract; grading against a spec the agent could have edited
       mid-flight is not verification. With nothing runnable the drift is noted
       on stderr and the stop proceeds ungraded: blocking would wedge tiers
       that have no stake in the edit at all.
    4. Grade the agent's own tier of the spec, unioned with the checks declared
       on this dispatch's own manifest (blocking, expect-exit0). Blocking
       failure -> ``contradicted`` and block (the agent gets its turn back, and
       the retry re-reports onto this same dispatch). Otherwise -> ``verified``
       and terminate.

    When nothing is runnable — the tier selects no repo checks *and* the
    manifest declares none — no verdict is recorded at all: the dispatch is
    terminated still-ungraded, because an absent grade must never read as a
    passing grade. A manifest-bearing dispatch can therefore always grade,
    which is the L21 fix: its checks no longer need to live in ``checks.json``.
    """
    session_id = os.environ.get(SESSION_ID_ENV)
    agent_id, agent_type = _agent_fields(payload)

    dispatch = find_dispatch_by_agent(session_id, agent_id)
    if dispatch is None:
        orphan_message = payload.get("last_assistant_message")
        record_orphan_stop(
            agent_id=agent_id,
            agent_type=agent_type,
            session_id=session_id,
            last_assistant_message=(
                orphan_message if isinstance(orphan_message, str) else ""),
        )
        sys.stderr.write(
            f"[fleetproof] unpaired subagent stop "
            f"(agent_type={agent_type!r}) recorded as an orphan — not graded\n")
        return None, 0

    last_message = payload.get("last_assistant_message")
    last_message = last_message if isinstance(last_message, str) else ""
    if not last_message.strip():
        return _subagent_block(
            f"{GATE_BLOCK_MARKER}\n"
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

    try:
        checks = load_checks()
    except CheckSpecError as e:
        if dispatch.spec_sha256_pinned:
            # A pin proves a spec existed when this work was ordered. Fail closed
            # (finding C3): grading "nothing to check" against a promise is how a
            # gate gets switched off by deleting its spec.
            return _subagent_block(
                "FleetProof: this dispatch pinned check spec "
                f"{short_spec_hash(dispatch.spec_sha256_pinned)}, but the spec is "
                f"now missing or unreadable ({e}). Restore .fleetproof/checks.json "
                "(byte-exact) before stopping.",
                f"- dispatch {dispatch.run_id} pinned spec "
                f"{short_spec_hash(dispatch.spec_sha256_pinned)}",
            ), 0
        # No spec and no pin: an unconfigured repo. Fail open on the stop, but do
        # not manufacture a verdict out of the absence of checks.
        checks = []
    selected = select_checks(checks, dispatch.tier) if checks else []
    # Union with the dispatch's own manifest checks, repo spec first. A manifest
    # check whose id collides with a selected repo check is dropped: the repo
    # spec is the more attested source, and two commands under one id would make
    # the executed-id set (which coverage joins on) ambiguous.
    selected_ids = {c.id for c in selected}
    runnable = selected + [c for c in _manifest_checks(dispatch)
                           if c.id not in selected_ids]

    # Pin-drift is tested only once the runnable set is known. An empty set
    # means there is nothing the drifted spec could corrupt at this tier —
    # blocking anyway wedged every in-flight dispatch on someone else's spec
    # edit, at tiers with nothing to grade (observed in a field deployment on
    # Windows). The drift still gets said out loud; it just cannot gate a
    # verdict that was never going to exist.
    pinned = dispatch.spec_sha256_pinned
    current = spec_hash()
    drifted = bool(pinned and current and pinned != current)

    if not runnable:
        if drifted:
            sys.stderr.write(
                f"[fleetproof] spec drift noted on dispatch {dispatch.run_id} "
                f"(pinned {short_spec_hash(pinned)}, now "
                f"{short_spec_hash(current)}) — nothing runnable at tier "
                f"{dispatch.tier}, stop allowed ungraded\n")
        _try_close(dispatch.run_id)
        # The grading *happened* and selected nothing — recorded as an empty
        # CheckReport so the telemetry layer can tell "checked nothing on
        # purpose" (unverifiable) apart from "grading never ran" (ungraded).
        _try_build_telemetry(
            dispatch.run_id,
            check_report=CheckReport(spec_sha256=spec_hash(), tier=dispatch.tier),
            checks=[],
        )
        return None, 0

    if drifted:
        return _subagent_block(
            _pin_drift_reason(pinned, current),
            f"- dispatch {dispatch.run_id} pinned spec {short_spec_hash(pinned)}\n"
            f"- current spec              {short_spec_hash(current)}",
        ), 0

    report = run_checks(runnable, record_to_log=True, tier=dispatch.tier)
    if report.blocking_failures:
        record_verdict(
            dispatch.run_id,
            STATE_CONTRADICTED,
            detail="; ".join(r.id for r in report.blocking_failures),
            by=VERDICT_BY,
        )
        _try_build_telemetry(dispatch.run_id, check_report=report, checks=runnable)
        return _subagent_block(_failure_reason(report), _evidence_context(report)), 0

    record_verdict(
        dispatch.run_id,
        STATE_VERIFIED,
        detail=f"{report.passed}/{report.total} checks passed at tier {dispatch.tier}",
        by=VERDICT_BY,
    )
    _try_close(dispatch.run_id)
    # Built after the close, so the outcome class derives from a finished
    # lifecycle rather than a snapshot mid-transition.
    _try_build_telemetry(dispatch.run_id, check_report=report, checks=runnable)
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
    if decision is not None and _suppress_on_retry(payload, decision):
        return code
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
