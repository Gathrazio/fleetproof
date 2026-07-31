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
          "description": "Unit tests must pass.",
          "tier": "bridge"
        }
      ]
    }

``tier`` is optional and scopes a check to one rung of the fleet
(``leaf``/``lane``/``coordinator``/``bridge``); omitting it means the check is
untiered, which the checker treats as bridge-tier — a v0.1 spec written before
tiers existed is a spec about the whole session, i.e. about the bridge.

``expect`` is one of:
    "exit0"                        command must exit 0 (default if omitted)
    {"exit": N}                    command must exit with code N
    {"regex": "pattern"}           combined stdout+stderr must match the regex
    {"file_exists": "path"}        path (relative to the run cwd) must exist

JSON, not YAML, on purpose: the runtime is stdlib-only, so there is no third-
party parser to trust in a tool whose entire job is being trustworthy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runlog import PROJECT_MARKER, project_root

DEFAULT_CHECKS_FILENAME = "checks.json"

_VALID_EXPECT_KEYS = {"exit", "regex", "file_exists"}

# The fleet's tier vocabulary. It lives here, in the lowest-level module that
# needs it, so :mod:`fleetproof.ledger` (which already imports this module for
# spec hashing) can re-export it instead of the two disagreeing about spelling.
VALID_TIERS = frozenset({"leaf", "lane", "coordinator", "bridge"})

# The line a drifted verdict carries everywhere it surfaces (gate output, report,
# CLI). One canonical string so the three surfaces never disagree about wording.
SPEC_DRIFT_NOTE = (
    "NOTE: .fleetproof/checks.json was modified during this session "
    "(spec drift). Review the diff before trusting this verdict."
)


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
    # Additive: absent from a v0.1 spec entry, and read back as None there. None
    # means "untiered", which the checker scopes to the bridge tier.
    tier: str | None = None

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


def spec_hash(path: Path | None = None) -> str | None:
    """SHA-256 (hex) of the check-spec bytes, or ``None`` if it can't be read.

    Hashed over the raw file bytes, not the parsed spec: the point is to notice
    that *the file the agent authored changed* mid-session, which a byte hash
    catches even for edits (whitespace, key reordering) that parse identically.
    Returns None rather than raising so a missing/unreadable spec degrades to
    "no hash on record" instead of breaking a gate run.
    """
    spec_path = Path(path) if path is not None else default_checks_path()
    try:
        return hashlib.sha256(spec_path.read_bytes()).hexdigest()
    except OSError:
        return None


def short_spec_hash(full: str | None, length: int = 12) -> str:
    """A git-style short form of a spec hash for display; 'unknown' when absent."""
    if not full:
        return "unknown"
    return full[:length]


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

    tier = entry.get("tier")
    if tier is not None and (not isinstance(tier, str) or tier not in VALID_TIERS):
        raise CheckSpecError(
            f"{where} ({cid}): 'tier' must be one of {sorted(VALID_TIERS)} or omitted."
        )

    expect = _normalize_expect(entry.get("expect", "exit0"), cid, where)

    # A file_exists check may have no command; every other kind needs one.
    if run is None and expect["kind"] != "file_exists":
        raise CheckSpecError(
            f"{where} ({cid}): a '{expect['kind']}' check requires a 'run' command."
        )

    return Check(id=cid, run=run, expect=expect, block=block, description=description,
                 tier=tier)


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
