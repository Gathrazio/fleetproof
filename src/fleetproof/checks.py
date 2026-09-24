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
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runlog import PROJECT_MARKER, project_root

DEFAULT_CHECKS_FILENAME = "checks.json"

_VALID_EXPECT_KEYS = {"exit", "regex", "file_exists"}

# The legal key sets, reified. Until these existed the legal set lived only
# implicitly in this parser's scatter of ``.get()`` reads, so an unknown key —
# ``"expects"`` for ``"expect"`` — was dropped without a word and the check
# fell back to the exit-0 default: a content assertion silently converted
# into an exit-code assertion, blocking, wrong, with a clean preflight
# (observed in a field deployment on Windows). Two sets, not one, because a
# manifest check's command key is ``cmd`` (``run`` aliased) and ``tier`` /
# ``succeeded_by`` are spec-only. What happens on an unknown key depends on
# the seat: authoring surfaces refuse (the author is there to fix it); the
# gate warns loudly but runs the check as its known keys declare, because a
# manifest pinned under an older release must not start failing mid-flight.
SPEC_CHECK_KEYS = frozenset({
    "id", "run", "expect", "block", "description", "tier", "owner", "redact",
    "succeeded_by", "consult_output_on_nonzero",
})
MANIFEST_CHECK_KEYS = frozenset({
    "id", "cmd", "run", "expect", "block", "description", "owner", "redact",
    "consult_output_on_nonzero",
})


def unknown_check_keys(entry: dict, legal: frozenset[str]) -> list[str]:
    """The entry's keys outside ``legal``, sorted; [] when every key is known."""
    return sorted(set(entry) - legal)


def unknown_key_message(where: str, cid: object, keys: list[str],
                        legal: frozenset[str]) -> str:
    """The one wording every unknown-key rejection and warning uses.

    Names the key AND the legal set, same rationale as
    :func:`unknown_tier_message`: a typo'd key is an operator mistake, and a
    message that names only the typo sends them to the docs to find the
    vocabulary.
    """
    named = ", ".join(repr(k) for k in keys)
    label = f"{where} ({cid})" if cid else where
    plural = "s" if len(keys) != 1 else ""
    return (f"{label}: unknown key{plural} {named} — legal keys: "
            + ", ".join(sorted(legal)) + ".")


def _warn_unknown_keys(message: str) -> None:
    sys.stderr.write(
        f"[fleetproof] WARNING: {message} Key(s) ignored; the check runs as "
        "its known keys declare — a spec or manifest pinned under an older "
        "release must not start failing mid-flight. Fix the key at the source.\n")

# The fleet's tier vocabulary. It lives here, in the lowest-level module that
# needs it, so :mod:`fleetproof.ledger` (which already imports this module for
# spec hashing) can re-export it instead of the two disagreeing about spelling.
VALID_TIERS = frozenset({"leaf", "lane", "coordinator", "bridge"})


def unknown_tier_message(tier: object) -> str:
    """The one wording every tier rejection uses: the bad value AND the legal set.

    An invented tier ("lead", "captain") is an operator typo, and a rejection
    that names only the typo sends them to the docs to find the vocabulary
    (asked for twice from a field deployment on Windows). Every site that
    validates a tier string — ledger, spec loader, checker, intent sidecar —
    composes its message from this so the legal list can never drift between
    surfaces.
    """
    return (f"unknown tier {str(tier)!r} — legal tiers: "
            + ", ".join(sorted(VALID_TIERS)))

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
    # Extra redaction regexes applied to this check's persisted output tails,
    # after the checker's builtin patterns. Validated compilable here, at spec
    # load, where the error still has someone to land on — a pattern that
    # first failed to compile inside the checker would silently skip the
    # redaction it promised.
    redact: tuple[str, ...] = ()
    # Phase succession: the id of another check in the same spec, at the same
    # tier, that takes over from this one. Once the successor has passed in
    # the current session, this check is retired — listed on the verdict as
    # retired, never counted as failed — so a "worktree is ahead" check can
    # hand off to a "merge landed on main" check without a spec edit at the
    # moment the worktree disappears (observed in a field deployment on
    # Windows). Validated at spec load: the target must exist, must not be
    # this check, must share its tier, and the chain must not cycle. See
    # :mod:`fleetproof.phase`. Additive: absent reads as None.
    succeeded_by: str | None = None
    # Regex checks gate the match on exit 0 (finding H1: a failing command's
    # error text must not pass a content assertion). On a test script that
    # already exits non-zero on failure, that gating makes a regex check add
    # nothing over the exit check — measured in a harness-free field trial on
    # macOS. Opting in here lets the regex decide on a completed-but-nonzero
    # command; a command that never completed still fails. Additive, default
    # off: every existing spec keeps H1's exact semantics.
    consult_output_on_nonzero: bool = False

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
    _validate_succession(checks)
    return checks


def _validate_succession(checks: list[Check]) -> None:
    """Every ``succeeded_by`` names another check in this spec, at the same
    tier, and no chain of successors cycles. Raised here, at load, where the
    error still has someone to land on — a dangling successor discovered in
    a gate would either retire nothing (silently wrong) or crash the gate."""
    by_id = {c.id: c for c in checks}
    for check in checks:
        target = check.succeeded_by
        if target is None:
            continue
        if target == check.id:
            raise CheckSpecError(
                f"check {check.id!r}: 'succeeded_by' must name a different check.")
        if target not in by_id:
            raise CheckSpecError(
                f"check {check.id!r}: 'succeeded_by' names {target!r}, which is "
                "not a check in this spec.")
        if by_id[target].tier != check.tier:
            raise CheckSpecError(
                f"check {check.id!r}: 'succeeded_by' {target!r} is at tier "
                f"{by_id[target].tier!r}, not this check's tier {check.tier!r} — "
                "a successor must be selected wherever its predecessor is.")
    for check in checks:
        seen = [check.id]
        cursor = check.succeeded_by
        while cursor is not None:
            if cursor in seen:
                raise CheckSpecError(
                    "'succeeded_by' chain cycles: " + " -> ".join(seen + [cursor]) + ".")
            seen.append(cursor)
            cursor = by_id[cursor].succeeded_by


# The key a dispatch manifest uses for its command. A manifest predates the
# spec's ``run`` vocabulary and every field deployment writes ``cmd``; ``run``
# is accepted as an alias so a check can be moved between the two files by
# copying the entry.
MANIFEST_CMD_KEY = "cmd"
MANIFEST_CHECK_DESCRIPTION = "dispatch-manifest check"


def parse_manifest_check(entry: Any, where: str, *,
                         on_unknown: str = "warn") -> Check:
    """Parse one dispatch-manifest check entry with the spec's own parser.

    A manifest check accepts everything a ``checks.json`` check accepts —
    ``expect`` (every kind), ``block`` (default true), ``owner``, ``redact``,
    ``description`` — with two differences: the command key is ``cmd``
    (``run`` accepted as an alias), and ``tier`` is refused, because a
    manifest is already per-dispatch and the dispatch's own tier is the only
    tier its checks can be graded at. Everything else is delegated to
    :func:`_parse_check`, so the two files can never disagree about what an
    expectation means. In the field every lane-grading check was a manifest
    check and the manifest shape was ``{"id", "cmd"}`` only — so ``owner``,
    the anti-wedge field, governed nothing that graded a lane (observed in a
    field deployment on Windows).

    ``where`` names the entry in error text (the manifest has no file:index
    the spec parser could name). Raises :class:`CheckSpecError`; the gate
    turns that into skipped-with-stderr, never a half-run check.

    ``on_unknown`` decides what an unknown key does: ``"refuse"`` raises
    (authoring surfaces — intent, preflight, ``dispatch new`` — where the
    author is in the seat to fix the typo), ``"warn"`` says it loudly on
    stderr and parses the known keys exactly as before (the gate: a manifest
    pinned under 0.5.0 must not start failing mid-flight).
    """
    if not isinstance(entry, dict):
        raise CheckSpecError(f"{where} is not an object.")
    if "tier" in entry:
        raise CheckSpecError(
            f"{where}: 'tier' is not a manifest field — a manifest is graded at "
            "its own dispatch's tier.")
    if "succeeded_by" in entry:
        raise CheckSpecError(
            f"{where}: 'succeeded_by' is not a manifest field — succession is "
            "between checks of the repo spec; retire a manifest check with "
            "`fleetproof phase advance --retire <id>` instead.")
    unknown = unknown_check_keys(entry, MANIFEST_CHECK_KEYS)
    if unknown:
        message = unknown_key_message(where, entry.get("id"), unknown,
                                      MANIFEST_CHECK_KEYS)
        if on_unknown == "refuse":
            raise CheckSpecError(message)
        _warn_unknown_keys(message)
    normalized = dict(entry)
    for key in unknown:
        normalized.pop(key, None)
    cmd = normalized.pop(MANIFEST_CMD_KEY, None)
    if cmd is None:
        cmd = normalized.pop("run", None)
    else:
        normalized.pop("run", None)
    if isinstance(cmd, str) and not cmd.strip():
        raise CheckSpecError(f"{where}: '{MANIFEST_CMD_KEY}' must not be blank.")
    normalized["run"] = cmd
    normalized.setdefault("description", MANIFEST_CHECK_DESCRIPTION)
    return _parse_check(normalized, 0, where=where, run_key=MANIFEST_CMD_KEY)


def _parse_check(entry: Any, index: int, *, where: str | None = None,
                 run_key: str = "run") -> Check:
    # ``where`` overrides the spec's "checks[i]" location for a caller that
    # has a better name for the entry; ``run_key`` is only how the command
    # field is *named* in error text (the manifest says ``cmd``).
    where = where or f"checks[{index}]"
    if not isinstance(entry, dict):
        raise CheckSpecError(f"{where} must be an object.")

    cid = entry.get("id")
    if not cid or not isinstance(cid, str):
        raise CheckSpecError(f"{where} is missing a string 'id'.")

    # Spec entries reach here directly; manifest entries were already
    # key-checked (against MANIFEST_CHECK_KEYS) and scrubbed by
    # :func:`parse_manifest_check`, so this can only fire for the spec. Warn,
    # never refuse: the spec has no authoring command in front of it, and a
    # stray key in a pinned checks.json turning into a hard block would wedge
    # every in-flight dispatch on the pin's fail-closed path.
    unknown = unknown_check_keys(entry, SPEC_CHECK_KEYS)
    if unknown:
        _warn_unknown_keys(unknown_key_message(where, cid, unknown, SPEC_CHECK_KEYS))

    run = entry.get("run")
    if run is not None and not isinstance(run, (str, list)):
        raise CheckSpecError(
            f"{where} ({cid}): '{run_key}' must be a string, an array of strings, "
            "or omitted.")
    if isinstance(run, str) and ("\n" in run or "\r" in run):
        # cmd.exe executes only the first line and silently discards the rest,
        # inheriting line 1's exit code (finding H5) — a check that half-runs is
        # worse than one that refuses to parse.
        raise CheckSpecError(
            f"{where} ({cid}): '{run_key}' must be a single line — on Windows only the "
            "first line would execute. Chain with '&&' or call a script instead."
        )
    if isinstance(run, list):
        # Argv form: executed with no shell at all, so the H5 first-line hazard
        # cannot arise — but a newline inside an argv element is almost
        # certainly a quoting accident, and an argv with no elements cannot
        # name a program to run.
        if not run:
            raise CheckSpecError(
                f"{where} ({cid}): argv-form '{run_key}' needs at least one element.")
        for j, element in enumerate(run):
            if not isinstance(element, str):
                raise CheckSpecError(
                    f"{where} ({cid}): '{run_key}'[{j}] must be a string.")
            if "\n" in element or "\r" in element:
                raise CheckSpecError(
                    f"{where} ({cid}): '{run_key}'[{j}] must be a single line.")

    block = entry.get("block", True)
    if not isinstance(block, bool):
        raise CheckSpecError(f"{where} ({cid}): 'block' must be true or false.")

    description = entry.get("description", "")
    if not isinstance(description, str):
        raise CheckSpecError(f"{where} ({cid}): 'description' must be a string.")

    tier = entry.get("tier")
    if tier is not None and (not isinstance(tier, str) or tier not in VALID_TIERS):
        raise CheckSpecError(
            f"{where} ({cid}): 'tier' {unknown_tier_message(tier)} (or omit it)."
        )

    owner = entry.get("owner")
    if owner is not None and (not isinstance(owner, str) or owner not in VALID_OWNERS):
        raise CheckSpecError(
            f"{where} ({cid}): 'owner' must be one of {sorted(VALID_OWNERS)} or omitted."
        )

    redact_raw = entry.get("redact", [])
    if redact_raw is None:
        redact_raw = []
    if not isinstance(redact_raw, list):
        raise CheckSpecError(
            f"{where} ({cid}): 'redact' must be an array of regex strings.")
    for j, pattern in enumerate(redact_raw):
        if not isinstance(pattern, str):
            raise CheckSpecError(f"{where} ({cid}): 'redact'[{j}] must be a string.")
        try:
            re.compile(pattern)
        except re.error as e:
            raise CheckSpecError(
                f"{where} ({cid}): 'redact'[{j}] is not a valid regex: {e}") from e

    succeeded_by = entry.get("succeeded_by")
    if succeeded_by is not None and (
            not isinstance(succeeded_by, str) or not succeeded_by.strip()):
        raise CheckSpecError(
            f"{where} ({cid}): 'succeeded_by' must be a check id string or omitted.")

    expect = _normalize_expect(entry.get("expect", "exit0"), cid, where)

    consult = entry.get("consult_output_on_nonzero", False)
    if not isinstance(consult, bool):
        raise CheckSpecError(
            f"{where} ({cid}): 'consult_output_on_nonzero' must be true or false.")
    if consult and expect["kind"] != "regex":
        raise CheckSpecError(
            f"{where} ({cid}): 'consult_output_on_nonzero' only applies to a "
            "'regex' expect — other kinds never consult output.")

    # A file_exists check may have no command; every other kind needs one.
    if run is None and expect["kind"] != "file_exists":
        raise CheckSpecError(
            f"{where} ({cid}): a '{expect['kind']}' check requires a '{run_key}' command."
        )

    return Check(id=cid, run=run, expect=expect, block=block, description=description,
                 tier=tier, owner=owner, redact=tuple(redact_raw),
                 succeeded_by=succeeded_by.strip() if succeeded_by else None,
                 consult_output_on_nonzero=consult)


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
