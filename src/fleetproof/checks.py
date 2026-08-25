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
untiered, and an untiered check fires at *every* tier. That is deliberate: a
v0.1 spec written before tiers existed must keep gating everything it ever
gated, at every gate — scoping untiered checks to the bridge alone left the
default subagent gate grading nothing at all (adversarial pass, finding C1).
Narrowing a check to one rung is an explicit act: write the tier.

``run`` is a shell command line (string) or an argv array (list of single-line
strings). The array form executes with no shell involved at all — no cmd.exe
quoting rules — and is the right form whenever an argument could be mistaken
for a shell metacharacter.

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

# Who can actually satisfy a check: any tier, or the operator — a seat outside
# the fleet entirely, for criteria no agent can close out (a license renewal, a
# signing ceremony). Validated with the same strictness as tiers: a typo'd
# owner silently defaulting would silently re-arm the check at every seat.
VALID_OWNERS = frozenset(VALID_TIERS | {"operator"})

# The line a drifted verdict carries everywhere it surfaces (gate output, report,
# CLI). One canonical string so the three surfaces never disagree about wording.
SPEC_DRIFT_NOTE = (
    "NOTE: .fleetproof/checks.json was modified during this session "
    "(spec drift). Review the diff before trusting this verdict."
)

# The tree-drift sibling: the spec file itself is unchanged, but the check
# scripts it invokes are not. One canonical string, same rationale as above.
TREE_DRIFT_NOTE = (
    "NOTE: check scripts under .fleetproof/checks/ were modified during this "
    "session (check-tree drift). Review the diff before trusting this verdict."
)

# Where the check scripts live, next to checks.json. Anything under it is part
# of the checks tree: the spec names the commands, these files are what the
# commands run, and a pin that covers only the spec is a filename check.
CHECKS_TREE_DIRNAME = "checks"


class CheckSpecError(Exception):
    """Raised when a check spec is missing or malformed."""


@dataclass(frozen=True)
class Check:
    """One declared check. Immutable — the spec is a contract, not a scratchpad."""
    id: str
    # A shell command line (string, run through the platform shell for
    # compatibility) or an argv array (list of strings, run with no shell at
    # all — no cmd.exe quoting rules, no metacharacter surprises).
    run: str | list[str] | None
    expect: dict[str, Any]  # normalized: {"kind": ..., ...}
    block: bool
    description: str = ""
    # Additive: absent from a v0.1 spec entry, and read back as None there. None
    # means "untiered", which the checker scopes to the bridge tier.
    tier: str | None = None
    # Which seat can actually satisfy this check (a tier, or "operator").
    # Additive: absent means the check blocks wherever it is selected (the
    # pre-ownership behaviour). When present and the check is graded at a
    # different tier, it runs but grades advisory for that run — a blocking
    # check nobody at the graded seat can fix is a wedge, not a gate
    # (observed in a field deployment on Windows).
    owner: str | None = None

    def describe_run(self) -> str | None:
        """The command for display: string form verbatim, argv form joined
        with spaces. The join is rendering only — the spec (and every record
        derived from it) keeps the list verbatim, because a joined argv is
        not re-parseable into the argv it came from."""
        if self.run is None:
            return None
        if isinstance(self.run, str):
            return self.run
        return " ".join(self.run)

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


# Domain separator seeding the tree hash, so a repo with no checks/ directory
# can never produce a tree hash equal to its plain spec hash — a reader who
# confuses the two pins would silently compare apples to oranges.
_TREE_HASH_PREFIX = b"fleetproof-checks-tree/1\x00"


def checks_tree_dir(spec_path: Path | None = None) -> Path:
    """The check-scripts directory for the spec at ``spec_path`` (its sibling)."""
    base = Path(spec_path) if spec_path is not None else default_checks_path()
    return base.parent / CHECKS_TREE_DIRNAME


def checks_tree_hash(path: Path | None = None) -> str | None:
    """SHA-256 (hex) over the whole checks tree: spec bytes plus every script.

    The spec pin (:func:`spec_hash`) notices an edited ``checks.json``; it says
    nothing about the check *scripts* the spec's commands invoke, which were
    rewritten mid-dispatch — including by the graded agent itself — with zero
    drift signal (observed in a field deployment on Windows). This hash covers
    both: the spec bytes, then every file under the sibling ``checks/``
    directory (recursive, files only, sorted by posix-relative name), each as
    UTF-8 name bytes + file bytes. An absent directory hashes the spec bytes
    alone under the domain prefix, so old repos get a stable value without
    creating anything. Returns None when the spec — or any script — cannot be
    read: a tree we could not fully see is a tree we cannot vouch for, and a
    missing pin degrades to "not drift-tested" rather than a false attestation.
    """
    spec_path = Path(path) if path is not None else default_checks_path()
    try:
        spec_bytes = spec_path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(_TREE_HASH_PREFIX)
    digest.update(spec_bytes)
    tree = checks_tree_dir(spec_path)
    entries: list[tuple[str, Path]] = []
    try:
        for file in tree.rglob("*"):
            if file.is_file():
                entries.append((file.relative_to(tree).as_posix(), file))
    except OSError:
        return None
    for name, file in sorted(entries):
        try:
            file_bytes = file.read_bytes()
        except OSError:
            return None
        digest.update(name.encode("utf-8"))
        digest.update(file_bytes)
    return digest.hexdigest()


def short_spec_hash(full: str | None, length: int = 12) -> str:
    """A git-style short form of a spec hash for display; 'unknown' when absent.

    Tolerates a non-string (a poisoned run record can put anything here — finding
    H3); a gate must never crash on the data it is auditing.
    """
    if not full or not isinstance(full, str):
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
        # utf-8-sig: Windows editors routinely save UTF-8 with a BOM, and a
        # BOM-prefixed spec must not silently disable the gate (finding C3).
        raw = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise CheckSpecError(f"Check spec {spec_path} is not valid JSON: {e}") from e
    except UnicodeDecodeError as e:
        raise CheckSpecError(
            f"Check spec {spec_path} is not UTF-8 (save it as UTF-8): {e}"
        ) from e
    except OSError as e:
        raise CheckSpecError(f"Check spec {spec_path} could not be read: {e}") from e

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
    if run is not None and not isinstance(run, (str, list)):
        raise CheckSpecError(
            f"{where} ({cid}): 'run' must be a string, an array of strings, "
            "or omitted.")
    if isinstance(run, str) and ("\n" in run or "\r" in run):
        # cmd.exe executes only the first line and silently discards the rest,
        # inheriting line 1's exit code (finding H5) — a check that half-runs is
        # worse than one that refuses to parse.
        raise CheckSpecError(
            f"{where} ({cid}): 'run' must be a single line — on Windows only the "
            "first line would execute. Chain with '&&' or call a script instead."
        )
    if isinstance(run, list):
        # Argv form: executed with no shell at all, so the H5 first-line hazard
        # cannot arise — but a newline inside an argv element is almost
        # certainly a quoting accident, and an argv with no elements cannot
        # name a program to run.
        if not run:
            raise CheckSpecError(
                f"{where} ({cid}): argv-form 'run' needs at least one element.")
        for j, element in enumerate(run):
            if not isinstance(element, str):
                raise CheckSpecError(
                    f"{where} ({cid}): 'run'[{j}] must be a string.")
            if "\n" in element or "\r" in element:
                raise CheckSpecError(
                    f"{where} ({cid}): 'run'[{j}] must be a single line.")

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

    owner = entry.get("owner")
    if owner is not None and (not isinstance(owner, str) or owner not in VALID_OWNERS):
        raise CheckSpecError(
            f"{where} ({cid}): 'owner' must be one of {sorted(VALID_OWNERS)} or omitted."
        )

    expect = _normalize_expect(entry.get("expect", "exit0"), cid, where)

    # A file_exists check may have no command; every other kind needs one.
    if run is None and expect["kind"] != "file_exists":
        raise CheckSpecError(
            f"{where} ({cid}): a '{expect['kind']}' check requires a 'run' command."
        )

    return Check(id=cid, run=run, expect=expect, block=block, description=description,
                 tier=tier, owner=owner)


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
