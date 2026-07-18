"""Check-spec loader.

A check spec is a JSON file — ``.fleetproof/checks.json`` by default — that
declares, up front, what "done" is supposed to mean for this repo. An agent is
allowed to *author* this file. It is never allowed to grade itself against it:
that job belongs to :mod:`fleetproof.checker`, which runs in a separate process.

Spec shape::

    {
      "checks": [
        {
          "id": "tests-pass",
          "run": "python -m pytest -q",
          "expect": "exit0",
          "block": true,
          "description": "Unit tests must pass."
        }
      ]
    }

``expect`` is one of:
    "exit0"                        command must exit 0 (default if omitted)
    {"exit": N}                    command must exit with code N
    {"regex": "pattern"}           combined stdout+stderr must match the regex
    {"file_exists": "path"}        path (relative to the run cwd) must exist

JSON, not YAML, on purpose: the runtime is stdlib-only, so there is no third-
party parser to trust in a tool whose entire job is being trustworthy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runlog import PROJECT_MARKER, project_root

DEFAULT_CHECKS_FILENAME = "checks.json"

_VALID_EXPECT_KEYS = {"exit", "regex", "file_exists"}


class CheckSpecError(Exception):
    """Raised when a check spec is missing or malformed."""


@dataclass(frozen=True)
class Check:
    """One declared check. Immutable — the spec is a contract, not a scratchpad."""
    id: str
    run: str | None
    expect: dict[str, Any]  # normalized: {"kind": ..., ...}
    block: bool
    description: str = ""

    def describe_expectation(self) -> str:
        kind = self.expect["kind"]
        if kind == "exit0":
            return "exit code 0"
        if kind == "exit":
            return f"exit code {self.expect['code']}"
        if kind == "regex":
            return f"output matches /{self.expect['pattern']}/"
        if kind == "file_exists":
            return f"file exists: {self.expect['path']}"
        return kind


def default_checks_path(start: Path | None = None) -> Path:
    """Return the default check-spec location for the project rooted at ``start``."""
    return project_root(start) / PROJECT_MARKER / DEFAULT_CHECKS_FILENAME


def load_checks(path: Path | None = None) -> list[Check]:
    """Load and validate the check spec. Raises CheckSpecError on any problem."""
    spec_path = Path(path) if path is not None else default_checks_path()
    if not spec_path.exists():
        raise CheckSpecError(
            f"No check spec found at {spec_path}. Run `fleetproof init` to create one."
        )
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CheckSpecError(f"Check spec {spec_path} is not valid JSON: {e}") from e

    if not isinstance(raw, dict) or "checks" not in raw:
        raise CheckSpecError("Check spec must be an object with a top-level 'checks' array.")
    entries = raw["checks"]
    if not isinstance(entries, list):
        raise CheckSpecError("'checks' must be an array.")

    checks: list[Check] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(entries):
        check = _parse_check(entry, i)
        if check.id in seen_ids:
            raise CheckSpecError(f"Duplicate check id: {check.id!r}")
        seen_ids.add(check.id)
        checks.append(check)
    return checks


def _parse_check(entry: Any, index: int) -> Check:
    where = f"checks[{index}]"
    if not isinstance(entry, dict):
        raise CheckSpecError(f"{where} must be an object.")

    cid = entry.get("id")
    if not cid or not isinstance(cid, str):
        raise CheckSpecError(f"{where} is missing a string 'id'.")

    run = entry.get("run")
    if run is not None and not isinstance(run, str):
        raise CheckSpecError(f"{where} ({cid}): 'run' must be a string or omitted.")

    block = entry.get("block", True)
    if not isinstance(block, bool):
        raise CheckSpecError(f"{where} ({cid}): 'block' must be true or false.")

    description = entry.get("description", "")
    if not isinstance(description, str):
        raise CheckSpecError(f"{where} ({cid}): 'description' must be a string.")

    expect = _normalize_expect(entry.get("expect", "exit0"), cid, where)

    # A file_exists check may have no command; every other kind needs one.
    if run is None and expect["kind"] != "file_exists":
        raise CheckSpecError(
            f"{where} ({cid}): a '{expect['kind']}' check requires a 'run' command."
        )

    return Check(id=cid, run=run, expect=expect, block=block, description=description)


def _normalize_expect(expect: Any, cid: str, where: str) -> dict[str, Any]:
    if expect == "exit0":
        return {"kind": "exit0"}
    if not isinstance(expect, dict) or len(expect) != 1:
        raise CheckSpecError(
            f"{where} ({cid}): 'expect' must be \"exit0\" or a single-key object "
            f"({sorted(_VALID_EXPECT_KEYS)})."
        )
    key, val = next(iter(expect.items()))
    if key not in _VALID_EXPECT_KEYS:
        raise CheckSpecError(f"{where} ({cid}): unknown expect kind {key!r}.")
    if key == "exit":
        if not isinstance(val, int) or isinstance(val, bool):
            raise CheckSpecError(f"{where} ({cid}): expect.exit must be an integer.")
        return {"kind": "exit", "code": val}
    if key == "regex":
        if not isinstance(val, str):
            raise CheckSpecError(f"{where} ({cid}): expect.regex must be a string.")
        return {"kind": "regex", "pattern": val}
    # file_exists
    if not isinstance(val, str):
        raise CheckSpecError(f"{where} ({cid}): expect.file_exists must be a path string.")
    return {"kind": "file_exists", "path": val}


STARTER_SPEC: dict[str, Any] = {
    "checks": [
        {
            "id": "tests-pass",
            "run": "python -m pytest -q",
            "expect": "exit0",
            "block": True,
            "description": "Unit tests must pass before a task can be called done.",
        },
        {
            "id": "build-succeeds",
            "run": "python -m build",
            "expect": "exit0",
            "block": True,
            "description": "The package must build. Edit or delete if you do not ship a package.",
        },
        {
            "id": "changelog-updated",
            "run": None,
            "expect": {"file_exists": "CHANGELOG.md"},
            "block": False,
            "description": "Advisory reminder — non-blocking example of a file_exists check.",
        },
    ]
}
