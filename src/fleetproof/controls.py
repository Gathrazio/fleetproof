"""The grader control ledger: evidence that a check can tell pass from fail.

A blocking check demanded a health field that had never existed. Its author
had "validated" it against a positive-control sample they wrote themselves,
containing the same wrong field name — grader and test data, one author, one
belief — and the only party that disagreed was the running product, which
nobody asked. The lane then renamed a production field to satisfy the check
(observed in a field deployment on Windows). This module is the ask the
field said would have stopped it: a per-check record of the samples the
grader was exercised against, in both directions, with the *provenance* of
the pass sample said out loud — ``captured`` (a real emission of the target)
or ``authored`` (written by a person, usually the check's author).

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
VALID_PROVENANCE = (PROVENANCE_CAPTURED, PROVENANCE_AUTHORED)

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
    pass_sample: Path,
    provenance: str,
    fail_sample: Path | None = None,
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
    """
    path = control_path(check_id)
    if path is None:
        raise ControlError(
            f"check id {check_id!r} cannot name a control file; use a plain name "
            "(letters, digits, dot, dash, underscore).")
    if provenance not in VALID_PROVENANCE:
        raise ControlError(
            f"provenance must be one of {list(VALID_PROVENANCE)}; got {provenance!r}.")
    work_dir = Path(cwd) if cwd is not None else Path.cwd()
    pass_rec = _sample_record(pass_sample)
    pass_rec.update(_observe(check, pass_rec["path"], work_dir, expect_pass=True))
    fail_rec: dict[str, Any] | None = None
    if fail_sample is not None:
        fail_rec = _sample_record(fail_sample)
        fail_rec.update(_observe(check, fail_rec["path"], work_dir, expect_pass=False))
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


def load_control(check_id: str) -> dict[str, Any] | None:
    """The control record for ``check_id``, or None when absent or unreadable."""
    path = control_path(check_id)
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def control_warnings(checks: list[Check]) -> list[str]:
    """One warning per BLOCKING check with no control, or an authored-only
    pass sample. Advisory checks are not warned about: a wrong advisory
    check wastes a cycle; a wrong blocking check has an agent, a deploy
    path, and a deadline pointed at it.
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
        if control.get("provenance") != PROVENANCE_CAPTURED:
            out.append(
                f"blocking check '{check.id}': its only pass sample is authored "
                "(provenance=authored) — the grader and its test data share a "
                "belief. A positive control must be a captured real emission.")
    return out


def controls_are_outside_the_checks_tree() -> bool:
    """True by construction; kept as a named fact for the test that pins it."""
    return controls_dir().resolve() != checks_tree_dir().resolve()
