"""Phase succession — a check that has outlived its phase steps aside.

A bridge's closeout stop was refused because a blocking check asserted on a
worktree the merge had just removed: the check was right for the build phase
and wrong the moment the work landed, and the only way to swap it for the
"merge landed on main" check was a spec edit — which, under pinned dispatches,
is drift that wedges everything in flight (observed in a field deployment on
Windows). Two mechanisms close that gap, both outside the hashed spec and the
checks tree so neither trips a drift pin:

*Succession by evidence.* A spec check may declare ``succeeded_by: "<id>"``,
naming another check in the same spec at the same tier. Once the successor
has PASSED in the current session, the predecessor is no longer selected: it
is listed on the verdict as ``retired (succeeded by <id>)`` and is never
counted as failed. "Passed in the current session" means exactly one thing —
a persisted checker run (``fleetproof check`` sub-invocation under
``.fleetproof/runs/``) whose session id equals this one and whose
``output.json`` records that check id with ``passed: true``. Preflight
records nothing and so is never evidence; an unrecorded ``check --no-record``
likewise. The checker also retires a predecessor whose successor passed in
the very run being graded, so the swap costs no extra blocked stop.

*Retirement by operator.* ``fleetproof phase advance --retire <id> --note
"..."`` records retirements in ``.fleetproof/phase.json`` — per repo state,
not per session, exactly like ``arming.json`` — and a retired check is
skipped the same way, rendered as ``retired (phase advance: <note>)``.
``phase reset`` clears it. Like a disarm, a retirement is attributed
(who, when, why) and echoed on every verdict it touches: anything that can
write the repo can retire a check, but not quietly.

File shape (``.fleetproof/phase.json``)::

    {
      "retired": {
        "<check-id>": {"at": "<ISO-8601>", "by": "<who>", "note": "<why>"}
      },
      "set_at": "<last write, ISO-8601>",
      "by": "<who wrote last>"
    }

An absent, unreadable, or malformed file reads as nothing retired — the
fail-safe direction: a retirement nobody recorded is a retirement nobody
asked for.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import Check
from .runlog import list_run_records, runs_dir

PHASE_FILENAME = "phase.json"

# The two reasons a check can be retired, as the verdict renders them.
RETIRED_BY_SUCCESSION = "succeeded by {successor}"
RETIRED_BY_PHASE_ADVANCE = "phase advance: {note}"


def phase_path() -> Path:
    """The ``.fleetproof/phase.json`` file, resolved beside the runs dir."""
    return runs_dir().parent / PHASE_FILENAME


def _empty_phase() -> dict[str, Any]:
    return {"retired": {}, "set_at": None, "by": None}


def load_phase() -> dict[str, Any]:
    """The phase state, normalized; nothing retired when the file is absent
    or unusable. Only well-formed entries survive: a retired map whose value
    is not an object is dropped, never guessed at."""
    out = _empty_phase()
    try:
        raw = json.loads(phase_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(raw, dict):
        return out
    retired = raw.get("retired")
    if isinstance(retired, dict):
        for check_id, meta in retired.items():
            if isinstance(check_id, str) and check_id and isinstance(meta, dict):
                out["retired"][check_id] = {
                    "at": meta.get("at"),
                    "by": meta.get("by"),
                    "note": str(meta.get("note") or ""),
                }
    out["set_at"] = raw.get("set_at")
    out["by"] = raw.get("by")
    return out


def _who(by: str | None) -> str:
    return by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _write_phase(state: dict[str, Any], by: str | None) -> Path:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "retired": dict(state.get("retired") or {}),
        "set_at": now,
        "by": _who(by),
    }
    path = phase_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def retire_checks(check_ids: list[str], note: str, by: str | None = None) -> Path:
    """Record ``check_ids`` as retired, with ``note`` (required, non-blank).

    Additive over what is already retired; re-retiring an id refreshes its
    note and stamp. Returns the path written. Nothing here consults the
    spec: a retirement may name a check that is not (or not yet) in the
    spec, and ``phase status`` says which retirements currently match one.
    """
    note = (note or "").strip()
    if not note:
        raise ValueError("phase advance needs a non-empty --note; retiring a "
                         "check requires a reason.")
    ids = [c.strip() for c in check_ids if isinstance(c, str) and c.strip()]
    if not ids:
        raise ValueError("phase advance needs at least one --retire <check-id>.")
    state = load_phase()
    now = datetime.now(timezone.utc).isoformat()
    who = _who(by)
    for check_id in ids:
        state["retired"][check_id] = {"at": now, "by": who, "note": note}
    return _write_phase(state, by)


def reset_phase(by: str | None = None) -> Path:
    """Clear every retirement. The file is rewritten empty (not deleted), so
    the reset itself is attributed and dated."""
    return _write_phase(_empty_phase(), by)


def session_passed_check_ids(session_id: str | None) -> set[str]:
    """Check ids that PASSED in a persisted checker run of ``session_id``.

    The one definition of "the successor has passed in the current session":
    a ``fleetproof check`` sub-invocation on a run record carrying this
    session id, whose ``output.json`` lists the id with ``passed: true``.
    No session means no evidence — a session-less run has no "current
    session" to have passed anything in.
    """
    if not session_id:
        return set()
    passed: set[str] = set()
    for run in list_run_records():
        if (run.session_id or None) != session_id:
            continue
        for sub in run.sub_invocations:
            if sub.tool != "fleetproof" or sub.subcmd != "check":
                continue
            payload = sub.load_output()
            if not isinstance(payload, dict):
                continue
            for entry in payload.get("checks") or []:
                if (isinstance(entry, dict) and entry.get("passed") is True
                        and isinstance(entry.get("id"), str)):
                    passed.add(entry["id"])
    return passed


def _retired_entry(check: Check, reason: str, *, successor: str | None = None,
                   note: str | None = None) -> dict[str, Any]:
    return {"id": check.id, "reason": reason, "succeeded_by": successor,
            "note": note, "blocking": check.block}


def apply_phase(
    checks: list[Check],
    session_id: str | None,
    phase: dict[str, Any] | None = None,
    passed_ids: set[str] | None = None,
) -> tuple[list[Check], list[dict[str, Any]]]:
    """Split ``checks`` into ``(active, retired)`` for this session.

    A check is retired when ``phase.json`` names it, or when its
    ``succeeded_by`` successor has passed in this session
    (:func:`session_passed_check_ids`) — or is itself retired by succession,
    so a chain A -> B -> C retires A and B once C has passed. A successor
    retired only by operator action does not retire its predecessor: an
    operator's retirement is evidence about that one check, not about the
    work the predecessor was asserting. Order is preserved. ``phase`` and
    ``passed_ids`` are injectable for the callers that already loaded them.
    """
    phase = phase if phase is not None else load_phase()
    passed = passed_ids if passed_ids is not None else session_passed_check_ids(session_id)
    retired_meta = phase.get("retired") or {}

    retired: dict[str, dict[str, Any]] = {}
    for check in checks:
        meta = retired_meta.get(check.id)
        if isinstance(meta, dict):
            retired[check.id] = _retired_entry(
                check, RETIRED_BY_PHASE_ADVANCE.format(note=meta.get("note") or "no note"),
                note=meta.get("note") or None)

    # Succession closes transitively: a predecessor whose successor passed,
    # or whose successor was itself succeeded (never merely operator-retired).
    succeeded: set[str] = set()
    changed = True
    while changed:
        changed = False
        for check in checks:
            successor = check.succeeded_by
            if not successor or check.id in retired:
                continue
            if successor in passed or successor in succeeded:
                retired[check.id] = _retired_entry(
                    check, RETIRED_BY_SUCCESSION.format(successor=successor),
                    successor=successor)
                succeeded.add(check.id)
                changed = True
    active = [c for c in checks if c.id not in retired]
    return active, [retired[c.id] for c in checks if c.id in retired]


def format_retired_lines(retired: list[dict[str, Any]] | None) -> list[str]:
    """The verdict's rendering of retirements, one per line, or []."""
    lines = []
    for entry in retired or []:
        if not isinstance(entry, dict):
            continue
        lines.append(f"  [retired] {entry.get('id')}: retired ({entry.get('reason')})")
    return lines
