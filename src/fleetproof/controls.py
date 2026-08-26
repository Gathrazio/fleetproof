"""The grader control ledger: evidence that a check can tell pass from fail.

A blocking check demanded a health field that had never existed. Its author
had "validated" it against a positive-control sample they wrote themselves,
containing the same wrong field name — grader and test data, one author, one
belief — and the only party that disagreed was the running product, which
nobody asked. The lane then renamed a production field to satisfy the check
(observed in a field deployment on Windows). This module is the ask the
field said would have stopped it: a per-check record of the samples the
grader was exercised against, in both directions, with the *provenance* of
each sample said out loud — ``captured`` (a real emission of the target),
``authored`` (written by a person, usually the check's author), or — pass
direction only — ``pending-capture`` (the emission does not exist yet;
creation work, see :data:`PROVENANCE_PENDING`).

Records live under ``.fleetproof/controls/<check-id>.json``, deliberately
outside the hashed checks tree (``.fleetproof/checks/``): a control is
evidence about a grader, not a grader, and recording one must trip no drift
pin. ``dispatch intent`` reads them and warns, per blocking manifest check,
when no control exists or the only pass sample is authored; with
``--strict-controls`` the warning is a refusal.

How a sample reaches the check: the sample's absolute path is passed in the
check's environment as :data:`CONTROL_SAMPLE_ENV` (``FLEETPROOF_CONTROL_SAMPLE``).
A check that wants to be controllable reads that variable and grades the
file it names instead of the live target; a check that ignores it simply
grades the live target both times, and the recorded exits say so. The
convention is one variable and no flag because argv-form checks have no
place to splice an argument into, and a shell-form check must not be
rewritten by the tool that is supposed to be verifying it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import Check, checks_tree_dir
from .runlog import runs_dir

CONTROLS_DIRNAME = "controls"
CONTROL_SAMPLE_ENV = "FLEETPROOF_CONTROL_SAMPLE"

PROVENANCE_CAPTURED = "captured"
PROVENANCE_AUTHORED = "authored"
# The third pass-direction provenance: the emission does not exist yet. A
# creation check asserts a state the lane is about to build, so its pass
# direction is uncapturable before the work by construction — under strict
# mode the only routes were an authored sample (the shared-belief hazard the
# whole module exists against) or switching strict off (observed in a field
# deployment on Windows). ``pending-capture`` is the honest third state: no
# value, the fail direction captured instead, and the discipline moved to the
# moment it can be met — capture the pass emission before calling the work
# verified (``check control --upgrade``); the gate's verified transition
# names every control still pending.
PROVENANCE_PENDING = "pending-capture"
VALID_PROVENANCE = (PROVENANCE_CAPTURED, PROVENANCE_AUTHORED)
VALID_PASS_PROVENANCE = (PROVENANCE_CAPTURED, PROVENANCE_AUTHORED,
                         PROVENANCE_PENDING)

# Same shape rule as intent sidecars: a check id names a file, and a
# path-shaped id must never become a path.
_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ControlError(Exception):
    """A control could not be recorded or read as asked."""


def controls_dir() -> Path:
    """``.fleetproof/controls/``, beside the runs dir and outside the checks tree."""
    return runs_dir().parent / CONTROLS_DIRNAME


def control_path(check_id: str) -> Path | None:
    """The record file for ``check_id``, or None when the id cannot name one."""
    if not isinstance(check_id, str) or not _CONTROL_ID_RE.match(check_id):
        return None
    return controls_dir() / f"{check_id}.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sample_record(sample: Path) -> dict[str, Any]:
    resolved = Path(sample).resolve()
    if not resolved.is_file():
        raise ControlError(f"sample {sample} is not a file.")
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _observe(check: Check | None, sample_path: str, cwd: Path,
             expect_pass: bool) -> dict[str, Any]:
    """Run ``check`` with the sample in its environment; say what it did.

    ``observed_pass`` is the checker's own grade of that run — the same
    grader the gate uses — and ``agrees`` compares it with the direction the
    sample was recorded for. A check with no command (a bare file_exists)
    or no resolvable check at all records nulls: an observation nobody made
    is not an observation.
    """
    if check is None or check.run is None:
        return {"observed_exit": None, "observed_pass": None, "agrees": None,
                "detail": None}
    from .checker import check_env, run_check
    env = check_env({CONTROL_SAMPLE_ENV: sample_path})
    result = run_check(check, cwd, env=env)
    return {
        "observed_exit": result.returncode,
        "observed_pass": result.passed,
        "agrees": result.passed == expect_pass,
        "detail": result.detail,
    }


def record_control(
    check_id: str,
    *,
    pass_sample: Path | None,
    provenance: str,
    fail_sample: Path | None = None,
    fail_provenance: str | None = None,
    note: str = "",
    check: Check | None = None,
    check_source: str | None = None,
    cwd: Path | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Write ``.fleetproof/controls/<check_id>.json`` and return its contents.

    Both samples are hashed and their absolute paths kept, so a later reader
    can tell whether the sample on disk is the one that was controlled
    against. When ``check`` is given (resolved by the CLI from a manifest
    file or the repo spec) it is run once per sample with
    :data:`CONTROL_SAMPLE_ENV` set, and the observed exit and grade recorded
    per direction. Overwrites an existing record: the newest control is the
    one that describes the grader as it is now.

    Provenance is per sample. ``provenance`` describes the pass direction
    (:data:`VALID_PASS_PROVENANCE`); ``pending-capture`` records the pass
    direction with no value at all — the emission does not exist yet — and
    must be omitted, not pointed at a file. ``fail_provenance`` describes the
    fail direction; omitted it follows ``provenance``, except beside a
    pending-capture pass, where it must be said explicitly — the strict rule
    keys on a CAPTURED fail, and a defaulted claim of capture is not a claim.
    The top-level ``provenance`` key is still written (mirroring the pass
    direction) so an 0.5.0 reader keeps reading what it always read.
    """
    path = control_path(check_id)
    if path is None:
        raise ControlError(
            f"check id {check_id!r} cannot name a control file; use a plain name "
            "(letters, digits, dot, dash, underscore).")
    if provenance not in VALID_PASS_PROVENANCE:
        raise ControlError(
            f"provenance must be one of {list(VALID_PASS_PROVENANCE)}; "
            f"got {provenance!r}.")
    if provenance == PROVENANCE_PENDING and pass_sample is not None:
        raise ControlError(
            "a pending-capture pass sample has no value: omit --pass-sample; "
            "the real emission arrives later via `check control --upgrade`.")
    if provenance != PROVENANCE_PENDING and pass_sample is None:
        raise ControlError(
            f"a {provenance} pass sample needs a file; only pending-capture "
            "records the pass direction without one.")
    if fail_provenance is None:
        if provenance == PROVENANCE_PENDING and fail_sample is not None:
            raise ControlError(
                "a fail sample beside a pending-capture pass needs an explicit "
                "--fail-provenance (captured or authored) — strict mode keys on "
                "a CAPTURED fail, and a defaulted claim of capture is no claim.")
        fail_provenance = provenance
    if fail_sample is not None and fail_provenance not in VALID_PROVENANCE:
        raise ControlError(
            f"fail_provenance must be one of {list(VALID_PROVENANCE)}; "
            f"got {fail_provenance!r}.")
    work_dir = Path(cwd) if cwd is not None else Path.cwd()
    if provenance == PROVENANCE_PENDING:
        pass_rec: dict[str, Any] = {"provenance": PROVENANCE_PENDING}
    else:
        pass_rec = _sample_record(pass_sample)
        pass_rec.update(_observe(check, pass_rec["path"], work_dir, expect_pass=True))
        pass_rec["provenance"] = provenance
    fail_rec: dict[str, Any] | None = None
    if fail_sample is not None:
        fail_rec = _sample_record(fail_sample)
        fail_rec.update(_observe(check, fail_rec["path"], work_dir, expect_pass=False))
        fail_rec["provenance"] = fail_provenance
    record = {
        "check_id": check_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "by": by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "provenance": provenance,
        "note": note or "",
        "pass_sample": pass_rec,
        "fail_sample": fail_rec,
        "check_source": check_source,
        "cmd": check.run if check is not None else None,
        "sample_env": CONTROL_SAMPLE_ENV,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def upgrade_pass_sample(
    check_id: str,
    pass_sample: Path,
    *,
    note: str = "",
    check: Check | None = None,
    check_source: str | None = None,
    cwd: Path | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Promote an existing control's pass direction from a captured real emission.

    The other half of ``pending-capture``: once the work exists and emits,
    the real emission replaces whatever the pass direction held (nothing, or
    an authored sample), provenance flips to ``captured``, and the fail
    direction is left exactly as recorded. The prior pass provenance is kept
    as ``upgraded_from`` so the record says the promotion happened rather
    than reading as if the sample was captured all along.
    """
    existing = load_control(check_id)
    if existing is None:
        raise ControlError(
            f"no control record for {check_id!r} to upgrade; record one first "
            f"(fleetproof check control {check_id} ...).")
    work_dir = Path(cwd) if cwd is not None else Path.cwd()
    pass_rec = _sample_record(pass_sample)
    pass_rec.update(_observe(check, pass_rec["path"], work_dir, expect_pass=True))
    pass_rec["provenance"] = PROVENANCE_CAPTURED
    existing["upgraded_from"] = sample_provenance(existing, "pass_sample")
    existing["pass_sample"] = pass_rec
    existing["provenance"] = PROVENANCE_CAPTURED  # the 0.5.0 mirror follows the pass
    existing["recorded_at"] = datetime.now(timezone.utc).isoformat()
    existing["by"] = (by or os.environ.get("USER")
                      or os.environ.get("USERNAME") or "unknown")
    if note:
        existing["note"] = note
    if check is not None:
        existing["check_source"] = check_source
        existing["cmd"] = check.run
    path = control_path(check_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return existing


def load_control(check_id: str) -> dict[str, Any] | None:
    """The control record for ``check_id``, or None when absent or unreadable.

    Migrates a legacy record on read: before per-sample provenance the one
    record-level ``provenance`` key was the record's only claim (defined for
    the pass direction), so a sample dict without its own ``provenance``
    inherits it. Migration is read-side only — the file on disk is never
    rewritten by loading it.
    """
    path = control_path(check_id)
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    legacy = raw.get("provenance")
    for key in ("pass_sample", "fail_sample"):
        sample = raw.get(key)
        if isinstance(sample, dict) and "provenance" not in sample:
            sample["provenance"] = legacy
    return raw


def sample_provenance(control: dict[str, Any], direction: str) -> str | None:
    """The provenance of ``direction`` (``pass_sample``/``fail_sample``), or None.

    Reads the per-sample field, falling back to the record-level legacy key
    for a record that was handed in un-migrated. None when the direction was
    never recorded at all — an absent sample has no provenance to claim.
    """
    sample = control.get(direction)
    if not isinstance(sample, dict):
        return None
    if sample.get("provenance"):
        return str(sample["provenance"])
    legacy = control.get("provenance")
    return str(legacy) if legacy else None


def control_warnings(checks: list[Check]) -> list[str]:
    """One warning per BLOCKING check whose control cannot satisfy strict mode.

    Advisory checks are not warned about: a wrong advisory check wastes a
    cycle; a wrong blocking check has an agent, a deploy path, and a deadline
    pointed at it. Strict is satisfied by a CAPTURED pass sample, or — the
    creation-work shape, where the pass direction cannot exist before the
    work — a captured FAIL sample beside a pass marked ``pending-capture``
    (the capture obligation moves to the verified transition, which names
    every control still pending). An authored-only pass never satisfies it.
    """
    out: list[str] = []
    for check in checks:
        if not check.block:
            continue
        control = load_control(check.id)
        if control is None:
            out.append(
                f"blocking check '{check.id}' has no grader control "
                f"({controls_dir() / (check.id + '.json')} absent). Record one "
                f"from a captured real emission: fleetproof check control "
                f"{check.id} --pass-sample <captured-emission> --provenance captured")
            continue
        pass_prov = sample_provenance(control, "pass_sample")
        fail_captured = sample_provenance(control, "fail_sample") == PROVENANCE_CAPTURED
        if pass_prov == PROVENANCE_CAPTURED:
            continue
        if pass_prov == PROVENANCE_PENDING:
            if fail_captured:
                continue
            out.append(
                f"blocking check '{check.id}': pass sample is pending-capture "
                "with no captured fail sample — a pending pass counts only "
                "beside a captured FAIL emission (the direction creation work "
                "CAN capture before it starts).")
            continue
        out.append(
            f"blocking check '{check.id}': its only pass sample is authored "
            "(provenance=authored) — the grader and its test data share a "
            "belief. A positive control must be a captured real emission.")
    return out


def pending_pass_checks(checks: list[Check]) -> list[str]:
    """Ids of BLOCKING checks whose control's pass sample is still pending-capture.

    The verified transition's read: these are the controls whose capture
    obligation came due the moment the work verified — the pass direction now
    exists, so ``check control --upgrade`` can promote it from the real
    emission. Checks with no control at all are not listed; the intent-time
    warnings already own that hole.
    """
    out: list[str] = []
    for check in checks:
        if not check.block:
            continue
        control = load_control(check.id)
        if (control is not None
                and sample_provenance(control, "pass_sample") == PROVENANCE_PENDING):
            out.append(check.id)
    return out


def controls_are_outside_the_checks_tree() -> bool:
    """True by construction; kept as a named fact for the test that pins it."""
    return controls_dir().resolve() != checks_tree_dir().resolve()
