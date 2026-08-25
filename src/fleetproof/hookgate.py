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
  and that agent's stop, so ``agent_id`` + ``session_id`` is the primary join
  key — with one fallback: a CLI-created dispatch that declared its spawn name
  up front (``dispatch new --agent-name``) is joined by ``agent_type`` and
  adopts the id at its first matching stop.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

from .checker import (
    CHECK_ENV_AGENT_TYPE,
    CHECK_ENV_RUN_ID,
    CHECK_ENV_SESSION_ID,
    CHECK_ENV_TIER,
    CheckReport,
    run_checks,
    select_checks,
    session_spec_baseline,
    spec_drifted,
    tree_drifted,
)
from .checks import (
    SPEC_DRIFT_NOTE,
    TREE_DRIFT_NOTE,
    Check,
    CheckSpecError,
    checks_tree_hash,
    load_checks,
    parse_manifest_check,
    short_spec_hash,
    spec_hash,
)
from .ledger import (
    CAPTURE_START,
    REASON_ABANDONED,
    STATE_CONTRADICTED,
    STATE_DISPATCHED,
    STATE_VERIFIED,
    TIER_BRIDGE,
    TIER_COORDINATOR,
    TIER_SOURCE_DEFAULTED,
    TIER_SOURCE_INHERITED,
    LedgerError,
    close_dispatch,
    consume_intent,
    create_dispatch,
    find_dispatch_for_stop,
    find_inheritable_intent,
    intents_dir,
    list_dispatches,
    list_ungraded_terminations,
    load_dispatch,
    record_block,
    record_orphan_stop,
    record_report,
    record_verdict,
    ungraded_termination_line,
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


# === Arming: separate "can't end a turn" from "claims done" ===
#
# A Stop gate conflates ending a turn with claiming the work done, and
# blocking checks that can only pass at the end of a release train deadlocked
# the bridge mid-phase. The field workaround — flip every check to
# block:false, flip back at publish — is a spec edit, which post-B1 is drift
# that wedges every in-flight dispatch. So the switch lives OUTSIDE the hashed
# spec and outside the checks tree: .fleetproof/arming.json is never pinned,
# and flipping it trips no drift mechanism.
#
# Arming is per tier, for the two tiers that end many turns per task: the
# bridge (its own Stop gate) and the coordinator (graded in the subagent gate
# at tier "coordinator"). A coordinator has the bridge's exact problem — any
# blocking coordinator-tier check that can only pass at the end wedges every
# mid-task stop — and without a switch the field authored its coordinator
# checks block:false, leaving the coordinator's own deliverable ungated
# (observed in a field deployment on Windows). Lanes and leaves are NEVER
# disarmable: a lane's "done" is the claim this tool exists to grade, and
# `--tier lane` is rejected by the CLI with that sentence. Two things arming
# never touches: the ledger sweep still blocks on stalled dispatches (a
# half-closed dispatch is bookkeeping, not phase), and the abandonment ladder
# counts contradictions exactly as before — an advisory verdict is not a
# contradiction, so it neither strikes nor resets the ladder.

ARMING_FILENAME = "arming.json"
ARMED = "armed"
ADVISORY = "advisory"
# Historical names, kept: every 0.4.x caller spells the values this way.
BRIDGE_ARMED = ARMED
BRIDGE_ADVISORY = ADVISORY
ARMING_STATES = (ARMED, ADVISORY)
# The tiers a gate can be set to advisory for. Ordered: the file and the
# board render them in this order.
ARMABLE_TIERS = (TIER_BRIDGE, TIER_COORDINATOR)
DEFAULT_ARMING_TIER = TIER_BRIDGE
LANE_NEVER_DISARMED = (
    "lanes are always graded — arming exists for tiers that end many turns "
    "per task (bridge, coordinator); a lane's stop is the claim being verified.")

# The advisory sibling of GATE_BLOCK_MARKER: same machine-shaped opener, but
# it says plainly that nothing is blocked and why the gate is down.
ADVISORY_MARKER_TEMPLATE = "[FLEETPROOF ADVISORY — {tier} gate disarmed: {note}]"


def arming_path() -> Path:
    """The ``.fleetproof/arming.json`` file, resolved beside the runs dir."""
    return runs_dir().parent / ARMING_FILENAME


def _armed_default() -> dict[str, Any]:
    return {
        TIER_BRIDGE: ARMED, TIER_COORDINATOR: ARMED,
        "note": "", "notes": {TIER_BRIDGE: "", TIER_COORDINATOR: ""},
        "set_at": None, "by": None,
        "set": {},
    }


def load_arming() -> dict[str, Any]:
    """The arming state for every armable tier, normalized; all-armed when
    the file is absent or unusable.

    File shape (``.fleetproof/arming.json``)::

        {
          "bridge": "armed" | "advisory",
          "coordinator": "armed" | "advisory",
          "note": "<the bridge's note>",           # 0.4.0 key, kept as-is
          "notes": {"bridge": "...", "coordinator": "..."},
          "set_at": "<last write, ISO-8601>", "by": "<who>",
          "set": {"<tier>": {"set_at": ..., "by": ...}}
        }

    One note per tier, not one shared note: the bridge disarms for a
    publish phase and the coordinator for a build phase, and a board that
    could only show one reason for two switches would be showing the wrong
    reason half the time. The 0.4.0 top-level ``note`` stays the bridge's
    note so a 0.4.0 file (``{"bridge", "note", "set_at", "by"}``) reads
    exactly as it did, with the coordinator armed.

    Armed is the fail-safe direction, per tier: no file, a corrupt file, or an
    unknown value all read as the gate ON — a disarm nobody recorded is a
    disarm nobody asked for. An unknown *bridge* value invalidates the whole
    file (the 0.4.0 rule); an unknown coordinator value arms the coordinator
    alone.
    """
    out = _armed_default()
    try:
        raw = json.loads(arming_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict) or raw.get(TIER_BRIDGE) not in ARMING_STATES:
        return out
    out[TIER_BRIDGE] = raw[TIER_BRIDGE]
    if raw.get(TIER_COORDINATOR) in ARMING_STATES:
        out[TIER_COORDINATOR] = raw[TIER_COORDINATOR]
    notes = raw.get("notes") if isinstance(raw.get("notes"), dict) else {}
    out["notes"][TIER_BRIDGE] = str(notes.get(TIER_BRIDGE) or raw.get("note") or "")
    out["notes"][TIER_COORDINATOR] = str(notes.get(TIER_COORDINATOR) or "")
    out["note"] = out["notes"][TIER_BRIDGE]
    out["set_at"] = raw.get("set_at")
    out["by"] = raw.get("by")
    if isinstance(raw.get("set"), dict):
        out["set"] = {k: v for k, v in raw["set"].items()
                      if k in ARMABLE_TIERS and isinstance(v, dict)}
    return out


def tier_arming(arming: dict[str, Any], tier: str | None) -> tuple[str, str]:
    """``(state, note)`` governing ``tier``; always ``(armed, "")`` for a tier
    that cannot be disarmed (lane, leaf, or unknown)."""
    if tier not in ARMABLE_TIERS:
        return ARMED, ""
    state = arming.get(tier)
    if state not in ARMING_STATES:
        return ARMED, ""
    return state, str((arming.get("notes") or {}).get(tier) or "")


def tier_set_meta(arming: dict[str, Any], tier: str) -> dict[str, Any]:
    """``{"set_at", "by"}`` for one tier's last write; the file-level values
    stand in for a 0.4.0 file that has no per-tier record."""
    meta = (arming.get("set") or {}).get(tier) or {}
    return {"set_at": meta.get("set_at") or arming.get("set_at"),
            "by": meta.get("by") or arming.get("by")}


def set_arming(state: str, note: str = "", by: str | None = None,
               tier: str = DEFAULT_ARMING_TIER) -> Path:
    """Write one tier's arming state, leaving the other tier's as it was.
    Returns the path written.

    The note travels in the file and is echoed by ``fleet`` — a disarmed gate
    with no visible reason is indistinguishable from a neutered one, which is
    exactly the ambiguity the note exists to remove. ``tier`` must be armable;
    a lane is refused here as firmly as at the CLI.
    """
    if state not in ARMING_STATES:
        raise ValueError(
            f"state must be {ARMED!r} or {ADVISORY!r}; got {state!r}.")
    if tier not in ARMABLE_TIERS:
        raise ValueError(f"tier {tier!r} cannot be armed or disarmed: {LANE_NEVER_DISARMED}")
    current = load_arming()
    who = by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    now = datetime.now(timezone.utc).isoformat()
    current[tier] = state
    current["notes"][tier] = note or ""
    current["set"][tier] = {"set_at": now, "by": who}
    path = arming_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        TIER_BRIDGE: current[TIER_BRIDGE],
        TIER_COORDINATOR: current[TIER_COORDINATOR],
        # The 0.4.0 key: the bridge's note, so a 0.4.0 reader sees what it saw.
        "note": current["notes"][TIER_BRIDGE],
        "notes": dict(current["notes"]),
        "set_at": now,
        "by": who,
        "set": dict(current["set"]),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _advisory_context(note: str, body: str, tier: str = TIER_BRIDGE) -> str:
    return (ADVISORY_MARKER_TEMPLATE.format(tier=tier, note=note or "no note recorded")
            + "\n" + body)


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

    Arming: when ``.fleetproof/arming.json`` says the bridge gate is
    ``advisory``, everything here still runs and still renders — but every
    ground to block converts to context carrying the advisory marker, and the
    decision never blocks. The disarm is recorded, attributed, and echoed by
    ``fleet``; the ledger sweep (in :func:`stop_gate`) is not arming's to
    switch off.
    """
    session_id = os.environ.get(SESSION_ID_ENV)
    arming_state, advisory_note = tier_arming(load_arming(), TIER_BRIDGE)
    advisory = arming_state == ADVISORY
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
            reason = (
                "FleetProof: a check spec graded this session earlier (baseline "
                f"{short_spec_hash(baseline)}) but is now missing or unreadable "
                f"({e}). Restore .fleetproof/checks.json before stopping — a "
                "promised gate cannot be switched off by removing its spec.")
            if advisory:
                return None, _advisory_context(advisory_note, reason)
            return reason, None
        return None, None

    if not checks:
        baseline = session_spec_baseline(session_id)
        if baseline:
            reason = (
                "FleetProof: .fleetproof/checks.json now declares zero checks, "
                "but a spec graded this session earlier (baseline "
                f"{short_spec_hash(baseline)}). An emptied spec mid-session is "
                "how a gate gets neutered; restore the checks before stopping.")
            if advisory:
                return None, _advisory_context(advisory_note, reason)
            return reason, None
        return None, None

    report = run_checks(checks, tier=TIER_BRIDGE,
                        identity=_check_identity(None, None, TIER_BRIDGE, session_id))

    # Spec-drift check: did the checks.json that just graded this verdict differ
    # from the one the session's first verdict was graded against? An agent is
    # allowed to author checks.json, so a failing agent could quietly weaken it to
    # slip this gate. We don't block on drift alone (v0.1 policy) — we make it loud.
    # The checks-tree hash rides alongside, additively: a rewritten check script
    # under .fleetproof/checks/ with a byte-identical checks.json is the same
    # quiet weakening. Spec drift subsumes tree drift (the tree hash covers the
    # spec bytes), so the tree is consulted only when the spec is unchanged and
    # the note names the actual culprit.
    drifted, baseline = spec_drifted(report.spec_sha256, session_id)
    tree_drift, tree_baseline = (False, None)
    if not drifted:
        tree_drift, tree_baseline = tree_drifted(report.tree_sha256, session_id)
    any_drift = drifted or tree_drift
    drift_note = SPEC_DRIFT_NOTE if drifted else TREE_DRIFT_NOTE
    if drifted:
        drift_ctx = _drift_context(report.spec_sha256, baseline)
    elif tree_drift:
        drift_ctx = _drift_context(report.tree_sha256, tree_baseline,
                                   note=TREE_DRIFT_NOTE, what="check-tree")
    else:
        drift_ctx = None

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
        if any_drift:
            reason += " " + drift_note
        if advisory:
            return None, _advisory_context(
                advisory_note, reason + ("\n" + drift_ctx if drift_ctx else ""))
        return reason, drift_ctx

    if report.verdict == "pass":
        # A pass with drift still passes, but must not pass *silently*. Said once
        # per (session, baseline→current) pair, though: the first stop after a
        # spec edit announces it for review; repeating the same note on every
        # later stop turns a review prompt into a nag the harness re-engages the
        # agent over (observed live: nine forced continuations in one session).
        if any_drift and _drift_unnoted(
                session_id,
                baseline if drifted else tree_baseline,
                report.spec_sha256 if drifted else report.tree_sha256):
            return None, drift_ctx
        return None, None

    context = _evidence_context(report)
    if drift_ctx:
        context = drift_ctx + "\n" + context
    if advisory:
        # Checks ran, a blocking one failed, and nothing blocks: the failure
        # renders in full so a disarmed gate is loud, never silent.
        body = (f"{len(report.blocking_failures)}/{report.total} blocking "
                "check(s) failed — rendered as context only; the bridge gate "
                "is disarmed and check failures do not block this stop.\n"
                + context)
        return None, _advisory_context(advisory_note, body)
    reason = _failure_reason(report)
    if any_drift:
        reason += " " + drift_note
    return reason, context


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
    _announce_ungraded_terminations(session_id)

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


def _announce_ungraded_terminations(session_id: str | None) -> None:
    """One stderr line per bridge stop naming this session's ungraded terminations.

    The simpler of the two designs on offer, chosen and documented here: every
    ungraded terminal dispatch in the session is named on every bridge stop,
    rather than only those since the previous stop. No marker file, no
    state, nothing to get out of sync — and a bridge sees the line for as
    long as the condition holds, which is the point: a claim that was
    closed with no verdict does not stop being ungraded because a turn
    ended. Stderr only, never a block and never additionalContext — the
    dispatch is already terminal, the sweep has nothing to hold the bridge
    on, and the existing "a terminated dispatch is not the sweep's business"
    contract stays byte-identical on stdout. Wording shared with the fleet
    board (:func:`fleetproof.ledger.ungraded_termination_line`).
    """
    if not session_id:
        return
    line = ungraded_termination_line(
        list_ungraded_terminations(session_id), scope_known=True)
    if line:
        sys.stderr.write(f"[fleetproof] {line}\n")


def _drift_context(
    current_hash: str | None,
    baseline_hash: str | None,
    note: str = SPEC_DRIFT_NOTE,
    what: str = "spec",
) -> str:
    """The unmissable drift annotation, with both hashes for a reviewer to diff.

    ``note``/``what`` select which drift is being annotated: the spec file
    itself, or the check-tree (scripts under ``.fleetproof/checks/``).
    """
    return (
        f"{note}\n"
        f"- current {what} hash:  {short_spec_hash(current_hash)}\n"
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


def _evidence_context(report) -> str:
    parts = []
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

# How many contradicted stops one dispatch gets before the gate stops arguing.
# An unsatisfiable blocking check produced an unbounded block/retry loop — 19
# cycles, each a full checker run, with no exit the agent could take (observed
# in a field deployment on Windows). The third contradiction is terminal: the
# verdict stands, the dispatch is abandoned, and fixing the spec or the seat
# becomes the dispatcher's move, not the wedged agent's.
MAX_CONTRADICTIONS = 3

_ABANDONED_CONTEXT = (
    "[FLEETPROOF] dispatch {run_id} abandoned after {n} contradicted stops on "
    "{check_ids} — the work is NOT verified. Park it, fix the spec/tier, or "
    "re-dispatch.")

# Internal marker on a decision dict whose context must reach the transcript
# even on a stop-hook continuation: the abandonment notice is the loop's
# terminus and the dispatcher's only signal, so _suppress_on_retry must not
# swallow it. Popped before serialization — it never appears in hook output.
_NEVER_SUPPRESS_KEY = "_fleetproof_never_suppress"


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
    consumed on match: one intent, one spawn.

    A second spawn of the same agent type — which is what re-messaging a
    live teammate looks like from the hook's seat — finds no sidecar. Before
    inheritance that meant a placeholder capture with an empty manifest, and
    at a tier the repo spec leaves empty (the configuration per-dispatch
    manifests encourage) the stop then had nothing runnable and closed
    ungraded while the board said done (observed in a field deployment on
    Windows). So a clean miss now looks for the newest dispatch in the same
    session for the same agent_type that carried a declared intent, and
    inherits its prompt, manifest, tier, and intent attribution — recorded
    as ``tier_source="inherited"`` with ``inherited_from`` naming the source,
    and said on stderr. Only when there is nothing to inherit does the spawn
    get the placeholder capture a repo without sidecars always got.
    Degradations (malformed intent, malformed manifest, bad tier) are surfaced
    on stderr and in the manifest notes, never fatal — this runs inside a
    hook that must not break the spawn.
    """
    agent_id, agent_type = _agent_fields(payload)
    intent, notes = consume_intent(agent_type)
    for note in notes:
        sys.stderr.write(f"[fleetproof] {note}\n")
    inherited_from: str | None = None
    if intent is None and not notes:
        source = find_inheritable_intent(os.environ.get(SESSION_ID_ENV), agent_type)
        if source is not None:
            inherited_from = source.run_id
            intent = {
                "prompt": source.prompt,
                "manifest": source.manifest,
                # An inherited tier is the source's tier whatever its own
                # provenance was; the inherited record says only that it was
                # inherited, and from where.
                "tier": source.tier,
                "source": source.intent_source,
            }
            sys.stderr.write(
                f"[fleetproof] no intent sidecar for '{agent_type or 'unknown'}' "
                f"— inherited intent from dispatch {source.run_id} "
                "(re-message of a live teammate?)\n")
        else:
            # A clean miss — no sidecar matched by name or role, and nothing
            # in this session to inherit. Said loudly, because the silent
            # version of this is a placeholder prompt and a defaulted tier
            # that nobody notices until the verdicts are worthless (observed
            # in a field deployment on Windows).
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
    # intent miss must not assert a declaration nobody made. An inherited
    # tier is "inherited" whatever the source's own provenance was.
    if inherited_from:
        tier_source: str | None = TIER_SOURCE_INHERITED
    elif intent_tier:
        tier_source = None
    else:
        tier_source = TIER_SOURCE_DEFAULTED
    return create_dispatch(
        prompt,
        tier=intent_tier or CAPTURED_SUBAGENT_TIER,
        tier_source=tier_source,
        manifest=manifest,
        agent={"agent_id": agent_id, "agent_type": agent_type, "capture": CAPTURE_START},
        intent_source=intent["source"] if intent else None,
        inherited_from=inherited_from,
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


def _block_dispatch(
    run_id: str, reason: str, context: str | None = None,
    checker_run_id: str | None = None,
) -> dict[str, Any]:
    """A subagent block that is also written to the dispatch (``blocks/NNN.txt``).

    The reason is what the agent reads; persisting it verbatim is what lets an
    operator later tell a wedge caused by a wrong check from one caused by
    wording the agent argued with. Best-effort: a ledger write failure is
    said on stderr and the block still goes out — the gate's decision must
    not depend on its own bookkeeping.
    """
    try:
        record_block(run_id, reason, checker_run_id=checker_run_id)
    except (LedgerError, OSError) as e:
        sys.stderr.write(f"[fleetproof] could not record block on {run_id}: {e}\n")
    return _subagent_block(reason, context)


def _pin_drift_reason(
    what: str, pinned: str | None, current: str | None, note: str,
) -> str:
    """The pin-drift block, naming WHICH pin drifted.

    ``what`` is "check spec" (checks.json itself changed) or "check scripts
    under .fleetproof/checks/" (spec byte-identical, graders rewritten) — the
    operator unwedging a fleet needs to know where to look, and one word
    covering both would send them diffing the wrong file.
    """
    return (
        f"{GATE_BLOCK_MARKER}\n"
        f"FleetProof: the {what} changed after this work was dispatched "
        f"(pinned {short_spec_hash(pinned)}, now {short_spec_hash(current)}). "
        "A dispatched agent is graded against the spec that was in force when it "
        "was dispatched, so this stop is blocked until the spec is restored or the "
        "work is re-dispatched against the new one. " + note
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


def _check_identity(run_id: str | None, agent_type: str | None,
                    tier: str | None, session_id: str | None) -> dict[str, str]:
    """The four identity variables a gate-run check sees (empty when unknown).

    The bridge gate has no dispatch of its own, so it passes empty run id and
    agent type with the bridge tier; the subagent gate passes the graded
    dispatch's. See :data:`fleetproof.checker.CHECK_ENV_KEYS`.
    """
    return {
        CHECK_ENV_RUN_ID: run_id or "",
        CHECK_ENV_AGENT_TYPE: agent_type or "",
        CHECK_ENV_TIER: tier or "",
        CHECK_ENV_SESSION_ID: session_id or "",
    }


def _manifest_checks(dispatch) -> list[Check]:
    """Runnable checks declared on the dispatch's own manifest, as full Checks.

    A manifest check carries the full spec-check shape — ``expect`` of every
    kind, ``block`` (default true), ``owner``, ``redact``, ``description`` —
    parsed by the same :func:`fleetproof.checks.parse_manifest_check` the
    spec loader's rules are built from, so the two files cannot disagree
    about what a check means. The command key is ``cmd`` (``run`` accepted);
    ``tier`` is refused because a manifest is already per-dispatch. The
    legacy ``{"id", "cmd"}`` entry parses exactly as before: blocking,
    expect-exit0.

    This used to be the ``{"id", "cmd"}`` shape only, with the safety fields
    "a checks.json feature". In the field every lane-grading check was a
    manifest check and zero spec checks selected at lane tier, so ``owner`` —
    the field that keeps a check nobody at the graded seat can satisfy from
    wedging that seat — governed nothing that graded a lane (observed in a
    field deployment on Windows). Ownership on a manifest check now has the
    spec path's exact semantics: at any tier but the owner's it runs, renders
    ``owner: <seat> — advisory at tier <tier>``, and cannot contradict the
    dispatch.

    An entry that does not parse is skipped with a stderr note rather than
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
        try:
            check = parse_manifest_check(entry, where)
        except CheckSpecError as e:
            sys.stderr.write(f"[fleetproof] {e}; skipped\n")
            continue
        if check.id in seen:
            continue
        seen.add(check.id)
        out.append(check)
    return out


def subagent_stop(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """The per-subagent gate. Returns ``(decision_or_None, exit_code)``.

    Order matters, and each step is a separate reason to refuse the stop:

    1. Find this agent's dispatch: by agent_id, then by name — a CLI-created
       dispatch (``dispatch new --agent-name``) has no agent_id until the
       first matching stop adopts it (see
       :func:`fleetproof.ledger.find_dispatch_for_stop`). An unpaired stop —
       no dispatch to join either way — records an orphan and terminates
       ungraded: harness-internal helper agents (summaries, titles) emit
       SubagentStop with no SubagentStart and no agent_type, and back-filling
       those as graded dispatches manufactured phantom verified verdicts
       (observed in a field deployment on Windows). Nobody ordered that work,
       so there is no claim to hold it to.
    2. No final message means no report. Block without transitioning: an agent
       that went idle saying nothing has not reported, and recording it as
       ``reported`` would launder silence into a claim.
    3. If the spec — or any check script under ``.fleetproof/checks/`` —
       changed since this dispatch was pinned, and this tier has anything
       runnable to grade: block, naming which pin drifted. At lane tier and
       deeper the pinned checks tree is the contract; grading against a spec
       or a grader the agent could have edited mid-flight is not verification.
       With nothing runnable the drift is noted on stderr and the stop
       proceeds ungraded: blocking would wedge tiers that have no stake in
       the edit at all.
    4. Grade the agent's own tier of the spec, unioned with the checks declared
       on this dispatch's own manifest (full spec-check shape; see
       :func:`_manifest_checks`). Blocking
       failure -> ``contradicted`` and block (the agent gets its turn back, and
       the retry re-reports onto this same dispatch) — unless this is the
       dispatch's :data:`MAX_CONTRADICTIONS`-th contradiction, which is
       terminal: verdict recorded, dispatch abandoned (reason
       ``abandoned-after-3-contradictions``, never ``verified``), stop
       allowed, and the dispatcher told in context that the work is NOT
       verified. A wedge must terminate, not loop; a later stop from the same
       agent finds only a terminal dispatch and lands on the orphan path.
       Otherwise -> ``verified`` and terminate.

       Arming applies here for the armable tiers only (see the arming block
       above): a dispatch at tier ``coordinator`` (or ``bridge``) while that
       tier's gate is advisory has its blocking failures demoted for the
       decision — mirroring :func:`_spec_gate` — so the verdict is recorded
       as ``verified`` with an ``advisory:`` detail that names the failures
       and the disarm, the failure renders in full as context under the
       advisory marker, nothing blocks, and no ``contradicted`` transition is
       written (so the ladder neither strikes nor resets). A lane dispatch
       is graded identically whatever ``arming.json`` says.

    When nothing is runnable — the tier selects no repo checks *and* the
    manifest declares none — no verdict is recorded at all: the dispatch is
    terminated still-ungraded, because an absent grade must never read as a
    passing grade. A manifest-bearing dispatch can therefore always grade,
    which is the L21 fix: its checks no longer need to live in ``checks.json``.
    """
    session_id = os.environ.get(SESSION_ID_ENV)
    agent_id, agent_type = _agent_fields(payload)

    # Name fallback runs BEFORE the orphan path: a CLI-created dispatch
    # (`dispatch new --agent-name`) is waiting for exactly this stop to learn
    # its agent_id, and orphaning it would fork the ledger the flag exists to
    # unify. An ambiguous name match surfaces on stderr and orphans anyway.
    dispatch, join_notes = find_dispatch_for_stop(session_id, agent_id, agent_type)
    for note in join_notes:
        sys.stderr.write(f"[fleetproof] {note}\n")
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
        return _block_dispatch(
            dispatch.run_id,
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
            return _block_dispatch(
                dispatch.run_id,
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
    # verdict that was never going to exist. Both pins are compared: the spec
    # bytes and the checks tree (spec + scripts under .fleetproof/checks/).
    # Spec drift subsumes tree drift, so the tree pin adds signal exactly when
    # checks.json is byte-identical and a grader script is not. A record with
    # no tree pin (written before the tree hash existed) is not tree-tested.
    pinned = dispatch.spec_sha256_pinned
    current = spec_hash()
    spec_pin_drift = bool(pinned and current and pinned != current)
    tree_pinned = dispatch.tree_sha256_pinned
    tree_current = checks_tree_hash()
    tree_pin_drift = (not spec_pin_drift) and bool(
        tree_pinned and tree_current and tree_pinned != tree_current)
    drifted = spec_pin_drift or tree_pin_drift

    if not runnable:
        if drifted:
            what = "spec" if spec_pin_drift else "check-script"
            shown_pinned = pinned if spec_pin_drift else tree_pinned
            shown_current = current if spec_pin_drift else tree_current
            sys.stderr.write(
                f"[fleetproof] {what} drift noted on dispatch {dispatch.run_id} "
                f"(pinned {short_spec_hash(shown_pinned)}, now "
                f"{short_spec_hash(shown_current)}) — nothing runnable at tier "
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

    if spec_pin_drift:
        return _block_dispatch(
            dispatch.run_id,
            _pin_drift_reason("check spec", pinned, current, SPEC_DRIFT_NOTE),
            f"- dispatch {dispatch.run_id} pinned spec {short_spec_hash(pinned)}\n"
            f"- current spec              {short_spec_hash(current)}",
        ), 0
    if tree_pin_drift:
        return _block_dispatch(
            dispatch.run_id,
            _pin_drift_reason("check scripts under .fleetproof/checks/",
                              tree_pinned, tree_current, TREE_DRIFT_NOTE),
            f"- dispatch {dispatch.run_id} pinned check tree {short_spec_hash(tree_pinned)}\n"
            f"- current check tree        {short_spec_hash(tree_current)}",
        ), 0

    # The agent type the dispatch knows beats the payload's: an adopted CLI
    # dispatch recorded its spawn name up front, and a payload with none
    # (harness helper) should not blank a name the ledger already has.
    agent_type_known = (dispatch.agent or {}).get("agent_type") or agent_type
    arming_state, arming_note = tier_arming(load_arming(), dispatch.tier)
    report = run_checks(
        runnable, record_to_log=True, tier=dispatch.tier,
        identity=_check_identity(dispatch.run_id, agent_type_known, dispatch.tier,
                                 session_id))
    if report.blocking_failures and arming_state == ADVISORY:
        # This tier's gate is disarmed: the failure is recorded and rendered
        # in full, and nothing blocks. The verdict line says "advisory" first
        # so a verified-with-failures row can never be misread as clean.
        failing_ids = "; ".join(r.id for r in report.blocking_failures)
        record_verdict(
            dispatch.run_id, STATE_VERIFIED,
            detail=(f"advisory: {len(report.blocking_failures)}/{report.total} "
                    f"blocking check(s) failed ({failing_ids}) — {dispatch.tier} "
                    f"gate disarmed, failures did not block"),
            by=VERDICT_BY)
        _try_close(dispatch.run_id)
        _try_build_telemetry(dispatch.run_id, check_report=report, checks=runnable)
        body = (f"{len(report.blocking_failures)}/{report.total} blocking "
                f"check(s) failed on dispatch {dispatch.run_id} — rendered as "
                f"context only; the {dispatch.tier} gate is disarmed and check "
                "failures do not block this stop.\n" + _evidence_context(report))
        return {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStop",
                "additionalContext": _advisory_context(arming_note, body, dispatch.tier),
            },
        }, 0
    if report.blocking_failures:
        failing_ids = "; ".join(r.id for r in report.blocking_failures)
        prior_contradictions = sum(
            1 for t in dispatch.transitions
            if isinstance(t, dict) and t.get("state") == STATE_CONTRADICTED)
        record_verdict(dispatch.run_id, STATE_CONTRADICTED,
                       detail=failing_ids, by=VERDICT_BY)
        if prior_contradictions + 1 >= MAX_CONTRADICTIONS:
            # The ladder's top rung: this contradiction is terminal. Blocking
            # again would be round N+1 of a loop that has already proven it
            # cannot converge from this seat — so the verdict stands, the
            # dispatch is abandoned (never verified), the agent may stop, and
            # the dispatcher is told in context that the work is not done.
            try:
                close_dispatch(dispatch.run_id, by="hook", reason=REASON_ABANDONED)
            except LedgerError as e:
                sys.stderr.write(
                    f"[fleetproof] could not abandon dispatch {dispatch.run_id}: {e}\n")
            _try_build_telemetry(dispatch.run_id, check_report=report, checks=runnable)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "SubagentStop",
                    "additionalContext": _ABANDONED_CONTEXT.format(
                        run_id=dispatch.run_id,
                        n=prior_contradictions + 1,
                        check_ids=failing_ids),
                },
                _NEVER_SUPPRESS_KEY: True,
            }, 0
        _try_build_telemetry(dispatch.run_id, check_report=report, checks=runnable)
        return _block_dispatch(
            dispatch.run_id, _failure_reason(report), _evidence_context(report),
            checker_run_id=report.run_id), 0

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
    never_suppress = bool(decision.pop(_NEVER_SUPPRESS_KEY, False)) if decision else False
    if decision is not None and not never_suppress and _suppress_on_retry(payload, decision):
        return code
    if decision is not None:
        sys.stdout.write(json.dumps(decision))
    return code


def record_tool_main() -> int:
    """PostToolUse recorder: accrete an evidence record. Always non-blocking.

    ``tool_input`` is recorded verbatim, by design: it is arbitrary code and
    arbitrary prose, and pattern-redacting it would be false comfort — a
    reviewer would trust a surface that still leaks anything shaped unlike
    the patterns. The redaction layer covers the two surfaces it can be
    honest about: check output tails (checker builtins + per-check patterns)
    and the env snapshot (:data:`fleetproof.runlog.SENSITIVE_ENV_PATTERNS`).
    Treat ``.fleetproof/runs/`` as sensitive as the shell history it records.
    """
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
