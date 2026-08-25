"""The Dispatch Ledger — who was told to do what, and what actually happened.

v0.1 could verify one agent's "done" claim against a check spec. It could not
answer the question a fleet operator actually asks: *of everything I dispatched,
what came back, and did any of it check out?* A subagent that was launched, wrote
nothing, and died leaves no trace in a run log that only records tool calls.

So a **dispatch** is recorded at launch, before any work happens, as its own
top-level run:

    <runs-dir>/<run-id>/
        _root.json      root_tool="dispatch", parent_run_id -> the dispatching run
        dispatch.json   the prompt, the tier, the manifest, the transitions
        report.json     what the dispatched agent claimed (latest report)
        reports/NNN.json  every report, in order — a retry overwrites
                          report.json but never this
        blocks/NNN.txt    the verbatim block text the agent received, per block;
                          NNN.meta.json beside it names the checker run

Two properties matter more than convenience:

*Nothing grades itself.* The dispatching agent writes ``dispatch.json``; the
report is the dispatched agent's own claim; the ``verified``/``contradicted``
transition is appended by the checker, in a different process. This module only
does bookkeeping — pure comparison, no LLM, no judgement.

*Reality over tidiness.* The lifecycle allows ``terminated`` from any state,
including straight from ``dispatched``. An agent killed before it ever reported
is a thing that happens; a ledger that refused to record it would be lying to
keep its state machine pretty.

Lifecycle::

    dispatched -> reported -> verified ------------------> terminated
         |            ^          contradicted -> terminated
         |            |               |
         |            +---------------+   (re-report: the gate blocked, the agent
         |                                 fixed it, the SAME dispatch reports again)
         +----------------------------------------------> terminated

The current state of a dispatch is its last transition. ``terminated`` is the
only terminal state. "Open" means not yet reported.

``contradicted -> reported`` is legal on purpose: when the subagent gate blocks a
stop, the harness hands the *same* agent back its turn, so the retry is the same
dispatch by construction. Re-opening a dispatch a human re-scoped is operator
policy (dispatch it again); re-reporting after a failed gate is ledger law.

Public API:
    create_dispatch(prompt, *, tier, manifest, parent_run_id, agent, by) -> run_id
    infer_tier(parent_run_id) -> str
    record_report(run_id, report, by=...)
    record_block(run_id, text, checker_run_id=...) -> Path
    record_verdict(run_id, verdict, detail=..., by=...)
    close_dispatch(run_id, by=..., reason=...)
    load_dispatch(run_id) -> DispatchRecord | None
    list_dispatches(*, session_id, open_only, non_terminal_only) -> list[DispatchRecord]
    list_ungraded_terminations(session_id) -> list[DispatchRecord]
    find_dispatch_by_agent(session_id, agent_id) -> DispatchRecord | None
    find_dispatch_for_stop(session_id, agent_id, agent_type) -> (record_or_None, notes)
    find_inheritable_intent(session_id, agent_type) -> DispatchRecord | None
    derive_manifest(prompt) -> dict
    write_intent(agent_type, prompt, *, manifest, tier) -> Path
    consume_intent(agent_type) -> (fields_or_None, notes)
    record_orphan_stop(*, agent_id, agent_type, session_id, ...) -> Path
    list_orphan_stops(session_id) -> list[dict]
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import VALID_TIERS, checks_tree_hash, spec_hash, unknown_tier_message
from .config import telemetry_era_stamp
from .runlog import (
    PARENT_RUN_ID_ENV,
    RUN_ID_ENV,
    SESSION_ID_ENV,
    _generate_run_id,
    runs_dir,
)

# === Constants ===

DISPATCH_FILENAME = "dispatch.json"
REPORT_FILENAME = "report.json"
ROOT_FILENAME = "_root.json"

# Per-attempt history beside the latest-wins files. report.json held only the
# newest claim, so an agent that reported three times under the gate left
# three report hashes in its transitions and one body on disk — the other two
# were gone; and the verbatim block text an agent was handed existed nowhere
# in the ledger at all, reconstructible only from the checker run plus the
# template (both observed in a field deployment on Windows). For a tool whose
# product is an auditable record of what was claimed and what was said back,
# both are kept now, numbered in the order they happened. Additive: a dispatch
# dir without these directories is an older record and still loads.
REPORTS_DIRNAME = "reports"
BLOCKS_DIRNAME = "blocks"
BLOCK_TEXT_SUFFIX = ".txt"
BLOCK_META_SUFFIX = ".meta.json"
_SEQUENCE_WIDTH = 3

# The root_tool every dispatch run carries, so runlog readers (list/show/report)
# can tell a dispatch apart from an ordinary recorded invocation.
DISPATCH_ROOT_TOOL = "dispatch"

STATE_DISPATCHED = "dispatched"
STATE_REPORTED = "reported"
STATE_VERIFIED = "verified"
STATE_CONTRADICTED = "contradicted"
# The third verdict: the checks ran, a blocking one FAILED, and the gate that
# graded it was disarmed (advisory) for that tier, so nothing blocked. Its own
# value, never ``verified`` with a note in the detail: a failed check that
# reads ``verified`` on the board is the false-attestation shape the whole
# tier_source/ungraded work exists to prevent, and it is what the first cut
# of per-tier arming recorded. Not ``contradicted`` either — the stop was
# allowed, the ladder must not strike, and the class of thing it is (work
# whose grade was suspended by an operator's recorded switch) is neither.
STATE_ADVISORY = "advisory"
STATE_TERMINATED = "terminated"

# Legal successors per state. ``terminated`` is reachable from everywhere on
# purpose (see the module docstring): the ledger records what happened, it does
# not enforce that agents die politely.
_ALLOWED_NEXT: dict[str, frozenset[str]] = {
    STATE_DISPATCHED: frozenset({STATE_REPORTED, STATE_TERMINATED}),
    STATE_REPORTED: frozenset({STATE_VERIFIED, STATE_CONTRADICTED, STATE_ADVISORY,
                               STATE_TERMINATED}),
    STATE_VERIFIED: frozenset({STATE_TERMINATED}),
    STATE_ADVISORY: frozenset({STATE_TERMINATED}),
    # Back to reported: the gate blocked this agent's stop, so the retry lands on
    # the same dispatch. Its report is overwritten; the transition list is the
    # audit trail of how many tries it took.
    STATE_CONTRADICTED: frozenset({STATE_REPORTED, STATE_TERMINATED}),
    STATE_TERMINATED: frozenset(),
}

TERMINAL_STATES = frozenset({STATE_TERMINATED})
VERDICT_STATES = frozenset({STATE_VERIFIED, STATE_CONTRADICTED, STATE_ADVISORY})

# Machine-set reasons a dispatch can be terminated for. The closed vocabulary
# exists because "terminated from dispatched" is two very different stories —
# an idle agent swept off the board versus an operator closing shop — and the
# split cannot be derived after the fact from a transition that says nothing.
# An absent reason stays legal (older callers, older records); readers treat it
# as "unclassified", never guess one of these.
REASON_SWEEP_IDLE = "sweep-idle"
REASON_OPERATOR_CLOSE = "operator-close"
REASON_SESSION_END = "session-end"
# Machine-set by the subagent gate's escalation ladder: the third contradicted
# stop on one dispatch is terminal — the gate stops arguing, the dispatch is
# abandoned, and the work is on record as never verified.
REASON_ABANDONED = "abandoned-after-3-contradictions"
VALID_TERMINATE_REASONS = frozenset({
    REASON_SWEEP_IDLE,
    REASON_OPERATOR_CLOSE,
    REASON_SESSION_END,
    REASON_ABANDONED,
})

# The one open-ended reason: "parked: <operator text>". Prefix-namespaced so
# readers can classify a parked dispatch without a vocabulary entry per park,
# while the vocabulary above stays closed for everything else. The remainder
# must be non-blank — a reason-shaped non-reason is what --reason exists to
# prevent.
REASON_PARKED_PREFIX = "parked: "


def is_parked_reason(reason: str | None) -> bool:
    """True for a well-formed parked reason (prefix + non-blank remainder)."""
    return (isinstance(reason, str)
            and reason.startswith(REASON_PARKED_PREFIX)
            and bool(reason[len(REASON_PARKED_PREFIX):].strip()))

# Tier vocabulary is defined in :mod:`fleetproof.checks` (the lower-level module,
# which a check spec's optional "tier" field also validates against) and
# re-exported here so callers can import it from whichever layer they already use.
TIER_LEAF = "leaf"
TIER_LANE = "lane"
TIER_COORDINATOR = "coordinator"
TIER_BRIDGE = "bridge"
TIERS = VALID_TIERS

TIER_SOURCE_INFERRED = "inferred"
TIER_SOURCE_DECLARED = "declared"
# The third provenance: nobody declared this tier and nothing inferred it — it
# is the capture fallback. Split out from "declared" because a defaulted tier
# is the strongest available hint that an intent went missing, and recording
# it as declared asserted the opposite (observed in a field deployment on
# Windows).
TIER_SOURCE_DEFAULTED = "defaulted"
# The fourth provenance: no sidecar matched this spawn, but an earlier dispatch
# in the same session for the same agent_type carried a declared intent, and
# this dispatch took its prompt, manifest, and tier from that record
# (``inherited_from`` names it). Intent sidecars are consumed once, so
# re-messaging a live teammate fires a fresh SubagentStart with nothing to
# match — and a defaulted capture with an empty manifest graded NOTHING at a
# tier the repo spec left empty, then rendered as done (observed in a field
# deployment on Windows: dispatched, reported, terminated, 8 ms, no verdict).
# Inheritance keeps the follow-up turn under the same contract as the first.
TIER_SOURCE_INHERITED = "inherited"

VALID_TIER_SOURCES = frozenset({
    TIER_SOURCE_INFERRED,
    TIER_SOURCE_DECLARED,
    TIER_SOURCE_DEFAULTED,
    TIER_SOURCE_INHERITED,
})

# Evidence kinds a reported deliverable may claim, weakest last. "executed" means
# a command ran and its result is on record; "believed" means nobody checked.
VALID_EVIDENCE = frozenset({"executed", "observed", "believed"})

# How a harness subagent came to be in the ledger. "start" means we saw it spawn
# (SubagentStart fired) and the dispatch was on record before it did any work;
# "stop-only" means the first we heard of it was its own stop, so the record was
# back-filled. The distinction matters: a stop-only sighting proves the start hook
# is not firing, which is a hole in the capture surface, not a normal case.
# "cli" means the dispatcher recorded it via `fleetproof dispatch new
# --agent-name` before spawning: the agent_type is known, the agent_id is not
# yet — it arrives by adoption at the first matching SubagentStop (see
# :func:`find_dispatch_for_stop`), which is what lets a CLI-created dispatch
# share the hook lifecycle instead of forking the ledger.
CAPTURE_START = "start"
CAPTURE_STOP_ONLY = "stop-only"
CAPTURE_CLI = "cli"
VALID_CAPTURE = frozenset({CAPTURE_START, CAPTURE_STOP_ONLY, CAPTURE_CLI})

# Ceiling on an ancestor walk, so a malformed or circular parent chain costs a
# bounded amount of work instead of hanging a hook.
MAX_CHAIN_WALK = 64

# Run ids embed a timestamp plus a hash of pid+perf-counter, so a collision needs
# two dispatches in the same second on the same pid. A handful of retries is more
# than enough; failing loudly beats silently reusing a run directory.
_RUN_ID_ATTEMPTS = 8

EMPTY_MANIFEST_KEYS = ("deliverables", "allowed_paths", "checks")

DERIVED_V1_NO_MANIFEST = "derived-v1: no structured manifest in prompt"
DERIVED_V1_MALFORMED = "derived-v1: embedded manifest was malformed and ignored"

# Where dispatch-intent sidecars live, next to the runs directory. An intent is
# the dispatcher's declaration of what it is about to spawn — the harness does
# not put the spawn prompt in the SubagentStart payload, so the sidecar is the
# only way a real prompt (and manifest, and tier) reaches the captured dispatch.
INTENTS_DIRNAME = "intents"

INTENT_MANIFEST_MALFORMED = (
    "intent-sidecar: declared manifest was malformed and ignored; "
    "derived from the intent prompt instead"
)

# What may name an intent file. The agent_type on the consume side comes out of
# the harness payload; building a path from a payload-controlled string is how
# a hook gets walked out of its own directory, so anything that is not a plain
# name gets no sidecar lookup at all.
_INTENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Where orphan-stop records live, beside the runs directory. An orphan is a
# SubagentStop with no dispatch to join — harness-internal helper agents emit
# stops with no SubagentStart and no agent_type. It is deliberately NOT a
# dispatch: back-filling one manufactures a graded record for work nobody
# ordered, and once its tier's checks happen to pass, a phantom "verified"
# verdict (observed in a field deployment on Windows). An orphan is a
# sighting, kept out of every dispatch count and verdict tally.
ORPHANS_DIRNAME = "orphans"

# How much of an orphan's last message is kept: enough to identify what the
# agent was, not a transcript store.
ORPHAN_MESSAGE_LIMIT = 200


class LedgerError(Exception):
    """Raised on an illegal state transition or an unusable dispatch/report."""


# === Reading a dispatch back ===

@dataclass
class DispatchRecord:
    """One dispatch, as read back off disk. Absent fields read back as None."""
    run_id: str
    run_dir: Path
    parent_run_id: str | None
    session_id: str | None
    prompt: str
    tier: str | None
    tier_source: str | None
    manifest: dict[str, Any] = field(default_factory=dict)
    spec_sha256_pinned: str | None = None
    # The checks-tree pin: spec bytes plus every script under
    # ``.fleetproof/checks/`` (see :func:`fleetproof.checks.checks_tree_hash`).
    # Additive: absent on records written before the tree pin existed, reading
    # back as None — and a None pin is never drift-tested.
    tree_sha256_pinned: str | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    # The harness subagent this dispatch stands for, when one is known. Additive:
    # a dispatch created by hand (or by v0.2 Phase A) has no agent block, and it
    # reads back as None.
    agent: dict[str, Any] | None = None
    # Which intent sidecar this dispatch's prompt came from — file name plus a
    # SHA-256 of the sidecar bytes as consumed, so a forged or replaced sidecar
    # is attributable after the fact. Additive: None on placeholder captures
    # and on every record written before this field existed.
    intent_source: dict[str, Any] | None = None
    # The telemetry-era cutover this dispatch was created under, stamped at
    # creation from deployment config. Additive: absent on every record created
    # before the cutover (or before this field existed), reading back as None —
    # which is what "pre-telemetry" means. Era membership lives here, on the
    # dispatch record, precisely so it can never be inferred from whether some
    # later, deletable file happens to exist.
    telemetry_era: str | None = None
    # The dispatch this one took its intent from, when ``tier_source`` is
    # ``inherited`` (see :func:`find_inheritable_intent`). Additive: None on
    # every record that matched its own sidecar, defaulted, or predates this.
    inherited_from: str | None = None

    @property
    def state(self) -> str:
        """The last recorded transition's state.

        A dispatch.json with an empty transitions list is a damaged record; it
        reads back as ``dispatched`` rather than raising, so one bad file cannot
        take out the whole board.
        """
        if not self.transitions:
            return STATE_DISPATCHED
        return str(self.transitions[-1].get("state") or STATE_DISPATCHED)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        """True while this dispatch has not reported yet.

        A dispatch terminated before it ever reported is *not* open — it is dead.
        Use :attr:`is_terminal` to ask "is it still running?".
        """
        return self.state == STATE_DISPATCHED

    @property
    def verdict(self) -> str | None:
        """The last verified/contradicted transition's state, or None if never graded.

        Kept separate from :attr:`state` because a graded dispatch that was then
        terminated still has a verdict worth showing on the board.
        """
        for entry in reversed(self.transitions):
            state = entry.get("state")
            if state in VERDICT_STATES:
                return str(state)
        return None

    @property
    def terminate_reason(self) -> str | None:
        """The machine-set reason on the terminate transition, or None.

        None covers both older records (written before reasons existed) and any
        terminate appended without one; both read as "unclassified" downstream.
        """
        for entry in reversed(self.transitions):
            if entry.get("state") == STATE_TERMINATED:
                reason = entry.get("reason")
                return str(reason) if reason else None
        return None

    @property
    def terminated_ungraded(self) -> bool:
        """True when this dispatch claimed something and was closed with no verdict.

        Precisely: the state is ``terminated``; no ``verified``/``contradicted``
        transition was ever appended (:attr:`verdict` is None); at least one
        ``reported`` transition exists (there was a claim to grade); and the
        terminate reason is not a park (``parked: ...`` is the operator closing
        it on purpose, with the reason on record — it renders as ``parked``,
        not as a silent miss). A dispatch terminated straight from
        ``dispatched`` is excluded: it never claimed anything, so nothing
        went ungraded — it died, and the terminate reason says how.

        This is the shape of the silent fail-open observed in a field
        deployment on Windows: dispatched, reported, terminated 8 ms later,
        no verdict, ever — and the board's ``done`` column read clean.
        """
        if not self.is_terminal or self.verdict is not None:
            return False
        if is_parked_reason(self.terminate_reason):
            return False
        return any(isinstance(t, dict) and t.get("state") == STATE_REPORTED
                   for t in self.transitions)

    @property
    def agent_id(self) -> str | None:
        """The harness agent id this dispatch tracks, or None if not agent-backed."""
        if not isinstance(self.agent, dict):
            return None
        agent_id = self.agent.get("agent_id")
        return str(agent_id) if agent_id else None

    @property
    def has_report(self) -> bool:
        return (self.run_dir / REPORT_FILENAME).exists()

    def load_report(self) -> dict[str, Any] | None:
        """Load report.json (the latest report), or None when absent/unreadable."""
        return _read_json(self.run_dir / REPORT_FILENAME)

    @property
    def report_count(self) -> int:
        """How many reports this dispatch has received, counting every attempt.

        Read off ``reports/``; an older dispatch dir (no ``reports/``) counts
        its single ``report.json`` as one, so the count never reads lower
        than what is visibly on disk.
        """
        listed = _sequence_files(self.run_dir / REPORTS_DIRNAME, ".json")
        if listed:
            return len(listed)
        return 1 if self.has_report else 0

    def load_reports(self) -> list[dict[str, Any]]:
        """Every report in the order received (``reports/NNN.json``).

        Falls back to the single ``report.json`` for an older dispatch dir.
        Unreadable entries are skipped, never invented.
        """
        paths = _sequence_files(self.run_dir / REPORTS_DIRNAME, ".json")
        if not paths:
            single = self.load_report()
            return [single] if single is not None else []
        out = []
        for path in paths:
            raw = _read_json(path)
            if raw is not None:
                out.append(raw)
        return out

    @property
    def block_count(self) -> int:
        """How many times the gate blocked this dispatch's stop (``blocks/``)."""
        return len(_sequence_files(self.run_dir / BLOCKS_DIRNAME, BLOCK_TEXT_SUFFIX))

    def load_blocks(self) -> list[dict[str, Any]]:
        """Every block the agent received, in order: ``{seq, text, at, checker_run_id}``.

        ``text`` is the verbatim reason the harness handed the agent; the
        meta sidecar (optional, may be missing on a partially written block)
        supplies ``at`` and the checker run whose evidence composed it.
        """
        out: list[dict[str, Any]] = []
        for path in _sequence_files(self.run_dir / BLOCKS_DIRNAME, BLOCK_TEXT_SUFFIX):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            seq = path.name[: -len(BLOCK_TEXT_SUFFIX)]
            meta = _read_json(path.with_name(seq + BLOCK_META_SUFFIX)) or {}
            out.append({
                "seq": seq,
                "text": text,
                "at": meta.get("at"),
                "checker_run_id": meta.get("checker_run_id"),
            })
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "tier": self.tier,
            "tier_source": self.tier_source,
            "manifest": self.manifest,
            "spec_sha256_pinned": self.spec_sha256_pinned,
            "tree_sha256_pinned": self.tree_sha256_pinned,
            "transitions": self.transitions,
            "started_at": self.started_at,
            "agent": self.agent,
            "intent_source": self.intent_source,
            "telemetry_era": self.telemetry_era,
            "inherited_from": self.inherited_from,
            "state": self.state,
            "verdict": self.verdict,
            "has_report": self.has_report,
            "report_count": self.report_count,
            "block_count": self.block_count,
            "terminated_ungraded": self.terminated_ungraded,
        }


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None if missing/unreadable/not an object.

    utf-8-sig, not utf-8: the ledger's own files never carry a BOM, but this
    also reads operator-supplied files (an intent sidecar written from a
    Windows shell), and a BOM must not turn a real file into "unreadable"
    (same rationale as the spec loader's utf-8-sig read of checks.json).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _sequence_files(directory: Path, suffix: str) -> list[Path]:
    """The ``NNN<suffix>`` files in ``directory``, in sequence order; [] if absent."""
    if not directory.is_dir():
        return []
    out = []
    for path in directory.iterdir():
        stem = path.name[: -len(suffix)] if path.name.endswith(suffix) else None
        if stem and stem.isdigit() and path.is_file():
            out.append(path)
    return sorted(out, key=lambda p: p.name)


def _next_sequence_name(directory: Path, suffix: str) -> str:
    """The next zero-padded sequence stem for ``directory`` (001, 002, ...).

    Derived from the highest existing stem, not the file count, so a gap left
    by an unreadable or deleted entry can never make two attempts share a
    number.
    """
    highest = 0
    for path in _sequence_files(directory, suffix):
        highest = max(highest, int(path.name[: -len(suffix)]))
    return f"{highest + 1:0{_SEQUENCE_WIDTH}d}"


def load_dispatch(run_id: str) -> DispatchRecord | None:
    """Load one dispatch by run id, or None if that run is not a dispatch."""
    run_dir = runs_dir() / run_id
    dispatch = _read_json(run_dir / DISPATCH_FILENAME)
    if dispatch is None:
        return None
    root = _read_json(run_dir / ROOT_FILENAME) or {}
    transitions = dispatch.get("transitions")
    manifest = dispatch.get("manifest")
    agent = dispatch.get("agent")
    intent_source = dispatch.get("intent_source")
    return DispatchRecord(
        run_id=run_id,
        run_dir=run_dir,
        parent_run_id=root.get("parent_run_id"),
        session_id=root.get("session_id"),
        prompt=str(dispatch.get("prompt") or ""),
        tier=dispatch.get("tier"),
        tier_source=dispatch.get("tier_source"),
        manifest=manifest if isinstance(manifest, dict) else {},
        spec_sha256_pinned=dispatch.get("spec_sha256_pinned"),
        tree_sha256_pinned=dispatch.get("tree_sha256_pinned"),
        transitions=transitions if isinstance(transitions, list) else [],
        started_at=root.get("started_at"),
        agent=agent if isinstance(agent, dict) else None,
        intent_source=intent_source if isinstance(intent_source, dict) else None,
        telemetry_era=dispatch.get("telemetry_era") or None,
        inherited_from=str(dispatch.get("inherited_from") or "") or None,
    )


def list_dispatches(
    *,
    session_id: str | None = None,
    open_only: bool = False,
    non_terminal_only: bool = False,
) -> list[DispatchRecord]:
    """Every dispatch in the runs directory, newest first.

    ``open_only`` keeps dispatches that have not reported yet; ``non_terminal_only``
    keeps everything not terminated (i.e. "still in the fleet"). Run ids are
    timestamp-prefixed, so reverse directory order is newest-first.
    """
    rd = runs_dir()
    if not rd.exists():
        return []
    out: list[DispatchRecord] = []
    for run_dir in sorted(rd.iterdir(), reverse=True):
        if not run_dir.is_dir() or run_dir.name.startswith("."):
            continue
        if not (run_dir / DISPATCH_FILENAME).exists():
            continue
        record = load_dispatch(run_dir.name)
        if record is None:
            continue
        if session_id is not None and (record.session_id or None) != session_id:
            continue
        if open_only and not record.is_open:
            continue
        if non_terminal_only and record.is_terminal:
            continue
        out.append(record)
    return out


def list_ungraded_terminations(session_id: str | None) -> list[DispatchRecord]:
    """Dispatches in ``session_id`` that :attr:`DispatchRecord.terminated_ungraded`, newest first.

    ``None`` session means every dispatch on disk (the caller says so in its
    wording). This is the count both the fleet board and the bridge Stop
    gate announce: an absent grade is not a passing grade, and a board
    column nobody is required to read is not an announcement.
    """
    return [r for r in list_dispatches(session_id=session_id) if r.terminated_ungraded]


def ungraded_termination_line(ungraded: list[DispatchRecord], scope_known: bool) -> str | None:
    """The one line that says a claim was closed with no verdict, or None when none was.

    Shared by the fleet board and the bridge Stop gate so the dispatcher
    reads the same sentence in both places. The ``ungraded`` column and its
    legend were already honest; they were honest on a board nobody is
    required to look at, and a re-messaged teammate's second turn closed
    ungraded and rendered ``done`` with nobody the wiser (observed in a
    field deployment on Windows). Silence when the count is zero — the line
    is a signal, not a header.
    """
    if not ungraded:
        return None
    scope = "this session" if scope_known else "in the dispatches shown"
    ids = ", ".join(r.run_id for r in ungraded)
    return (f"{len(ungraded)} dispatch(es) terminated ungraded {scope} — {ids}. "
            "A claim was recorded and closed with no verdict; an absent grade "
            "is not a passing grade.")


def find_dispatch_by_agent(session_id: str | None, agent_id: str | None) -> DispatchRecord | None:
    """The newest non-terminal dispatch in ``session_id`` tracking ``agent_id``.

    This is how the SubagentStop gate finds the record its SubagentStart sibling
    created. The harness gives no correlation field between a spawn and a stop
    beyond the agent id, so that plus the session is the whole key — and it is
    scoped to non-terminal dispatches because an agent id can be reused after a
    dispatch is closed out.

    Returns None when there is no agent id (nothing to match on) or no match, and
    the caller is expected to back-fill a ``stop-only`` dispatch rather than treat
    the sighting as unrecorded.
    """
    if not agent_id:
        return None
    for record in list_dispatches(session_id=session_id, non_terminal_only=True):
        if record.agent_id == agent_id:
            return record
    return None


def _adopt_agent(record: DispatchRecord, agent_id: str | None, by: str) -> DispatchRecord:
    """Write a newly learned agent_id onto a name-matched dispatch.

    Not a state transition — the dispatch stays exactly as open as it was —
    but it is an audit event, so an entry carrying the unchanged state and an
    adoption detail is appended to the transitions list: the record must be
    able to say *how* it came to carry this id.
    """
    raw = _read_json(record.run_dir / DISPATCH_FILENAME) or {}
    agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    agent["agent_id"] = agent_id or None
    raw["agent"] = agent
    transitions = raw.get("transitions")
    if not isinstance(transitions, list):
        transitions = []
    transitions.append(_transition(
        record.state, by,
        detail=f"adopted agent_id {agent_id or 'unknown'} by name-match on "
               f"agent_type {agent.get('agent_type')!r}"))
    raw["transitions"] = transitions
    _write_json(record.run_dir / DISPATCH_FILENAME, raw)
    return load_dispatch(record.run_id) or record


def find_dispatch_for_stop(
    session_id: str | None,
    agent_id: str | None,
    agent_type: str | None,
) -> tuple[DispatchRecord | None, list[str]]:
    """The dispatch a SubagentStop should grade, plus notes for the caller to surface.

    Matching order:

    1. ``agent_id`` exact — today's key (:func:`find_dispatch_by_agent`).
    2. Name fallback: among *open* dispatches whose session equals the
       payload's exactly, the unique record whose ``agent.agent_type`` equals
       the payload's agent_type and whose ``agent_id`` is still null. That is
       the ``dispatch new --agent-name`` shape: the dispatcher recorded the
       spawn before the harness had an id, and this stop is where the id
       becomes known — so the record adopts it (written onto the agent block,
       noted in the transitions) and the CLI dispatch joins the hook
       lifecycle instead of forking the ledger.
    3. Two or more such records is not a guess this module gets to make:
       no match, with a note naming the candidates, and the caller falls
       through to the orphan path — a stop graded against the wrong contract
       is worse than a sighting.

    The session comparison here is exact (None matches only None), stricter
    than the agent-id path's documented no-session fallback: adoption mutates
    the record, and a cross-session guess is not evidence.
    """
    found = find_dispatch_by_agent(session_id, agent_id)
    if found is not None:
        return found, []
    if not agent_type:
        return None, []
    candidates = [
        r for r in list_dispatches(non_terminal_only=True)
        if r.is_open
        and (r.session_id or None) == (session_id or None)
        and isinstance(r.agent, dict)
        and r.agent.get("agent_type") == agent_type
        and not r.agent_id
    ]
    if not candidates:
        return None, []
    if len(candidates) > 1:
        listed = ", ".join(r.run_id for r in candidates)
        return None, [
            f"ambiguous name-match: {len(candidates)} open dispatches await an "
            f"agent of type {agent_type!r} ({listed}) — adopting none of them"]
    return _adopt_agent(candidates[0], agent_id, by="hook"), []


# === Intent inheritance ===

def carried_declared_intent(record: DispatchRecord) -> bool:
    """True when this dispatch's prompt/manifest came from the dispatcher, not a derivation.

    Two shapes qualify: a hook capture that consumed an intent sidecar
    (``intent_source`` is the marker — it is only ever written by a consumed
    sidecar), and a ``dispatch new --agent-name`` record whose manifest is
    not the derive-v1 placeholder (the CLI dispatcher supplied one, by file or
    by fenced block). A placeholder capture, or a CLI dispatch with nothing
    but a prompt, carried no contract worth inheriting — inheriting an empty
    manifest would reproduce exactly the ungraded stop this exists to stop.
    """
    if record.intent_source is not None:
        return True
    agent = record.agent if isinstance(record.agent, dict) else {}
    if agent.get("capture") != CAPTURE_CLI:
        return False
    notes = str((record.manifest or {}).get("notes") or "")
    return notes not in (DERIVED_V1_NO_MANIFEST, DERIVED_V1_MALFORMED)


def find_inheritable_intent(
    session_id: str | None, agent_type: str | None,
) -> DispatchRecord | None:
    """The newest dispatch in ``session_id`` for ``agent_type`` that carried a declared intent.

    Same session exactly, same ``agent.agent_type`` exactly, any state — the
    record being inherited from is usually terminal (the teammate finished,
    verified, and was then messaged again). A chain is fine: an inherited
    dispatch keeps the ``intent_source`` it inherited, so the third re-message
    inherits from the second, which inherited from the first. No session id
    means no inheritance: a session-less spawn has no "same session" to
    inherit within, and a cross-session guess is not a contract.
    """
    if not session_id or not agent_type:
        return None
    candidates = []
    for record in list_dispatches(session_id=session_id):
        agent = record.agent if isinstance(record.agent, dict) else {}
        if agent.get("agent_type") == agent_type and carried_declared_intent(record):
            candidates.append(record)
    if not candidates:
        return None
    # "Newest" by started_at (microsecond ISO on _root.json), not by run-id
    # order: run ids are second-resolution plus a hash, so two dispatches
    # in the same second sort by hash — and the wrong link in a chain of
    # re-messages would inherit a stale contract.
    return max(candidates, key=lambda r: (r.started_at or "", r.run_id))


# === Tier inference ===

def _parent_of(run_id: str) -> str | None:
    """The parent run id on a run's _root.json, or None if absent/unreadable."""
    root = _read_json(runs_dir() / run_id / ROOT_FILENAME)
    if not root:
        return None
    parent = root.get("parent_run_id")
    return str(parent) if parent else None


def _declared_tier(run_id: str) -> str | None:
    dispatch = _read_json(runs_dir() / run_id / DISPATCH_FILENAME)
    if not dispatch:
        return None
    tier = dispatch.get("tier")
    return str(tier) if tier else None


def _is_chain_root(run_id: str) -> bool:
    """True when ``run_id`` tops its own chain: no parent, or declared bridge tier."""
    if _declared_tier(run_id) == TIER_BRIDGE:
        return True
    return _parent_of(run_id) is None


def ancestor_chain(run_id: str | None) -> list[str]:
    """Run ids from ``run_id`` up to its chain root, inclusive, nearest first.

    Walks ``_root.json`` parent links. Guarded against cycles (a run that is its
    own ancestor) and against unbounded chains, because this runs inside a hook
    and a malformed run tree must cost bounded time, not a hang.
    """
    if not run_id:
        return []
    chain: list[str] = [run_id]
    seen = {run_id}
    current = run_id
    while len(chain) < MAX_CHAIN_WALK:
        if _is_chain_root(current):
            break
        parent = _parent_of(current)
        if parent is None or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


def infer_tier(parent_run_id: str | None) -> str:
    """Infer a dispatch's tier from how deep its parent chain goes.

    - no parent at all               -> ``bridge`` (nobody dispatched us)
    - parent tops its own chain      -> ``lane``   (a bridge dispatched us)
    - parent is itself dispatched    -> ``leaf``

    ``coordinator`` is never inferred: it is a role the operator assigns, not a
    shape visible in the run tree, so it only ever arrives declared.
    A parent whose record is missing or unreadable is treated as a chain root —
    the guess degrades to ``lane`` rather than failing the dispatch.
    """
    if not parent_run_id:
        return TIER_BRIDGE
    return TIER_LANE if len(ancestor_chain(parent_run_id)) <= 1 else TIER_LEAF


def _validate_tier(tier: str) -> str:
    """The tier string, or a LedgerError that names the legal values.

    Every path that bypasses argparse's ``choices`` lands here — a tier
    inside an intent sidecar, a manifest file, a library caller — so this is
    where an invented tier gets told what the vocabulary is.
    """
    if tier not in VALID_TIERS:
        raise LedgerError(unknown_tier_message(tier))
    return tier


# === Manifests ===

_JSON_FENCE_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def empty_manifest(notes: str = "") -> dict[str, Any]:
    """The enrichable manifest shape: three empty lists plus free-text notes."""
    manifest: dict[str, Any] = {key: [] for key in EMPTY_MANIFEST_KEYS}
    manifest["notes"] = notes
    return manifest


def _coerce_manifest(raw: Any) -> dict[str, Any]:
    """Normalize a manifest to the canonical shape. Raises ValueError if unusable.

    Unknown keys are preserved: the manifest is meant to be enriched over time,
    and this module is not the arbiter of what a future field means.
    """
    if not isinstance(raw, dict):
        raise ValueError("manifest must be an object")
    out = empty_manifest()
    for key in EMPTY_MANIFEST_KEYS:
        val = raw.get(key, [])
        if val is None:
            val = []
        if not isinstance(val, list):
            raise ValueError(f"manifest.{key} must be an array")
        out[key] = list(val)
    notes = raw.get("notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise ValueError("manifest.notes must be a string")
    out["notes"] = notes
    # Optional deliverable->check mapping: which declared checks stand as
    # evidence for which deliverable. Validated when present so coverage math
    # downstream never has to defend against a malformed shape, but never
    # added when absent — an older manifest keeps its exact shape, and absent
    # reads as empty.
    if "check_map" in raw and raw["check_map"] is not None:
        check_map = raw["check_map"]
        if not isinstance(check_map, dict):
            raise ValueError("manifest.check_map must be an object")
        for deliverable, check_ids in check_map.items():
            if not isinstance(deliverable, str):
                raise ValueError("manifest.check_map keys must be strings")
            if not isinstance(check_ids, list) or not all(
                isinstance(cid, str) for cid in check_ids
            ):
                raise ValueError(
                    f"manifest.check_map[{deliverable!r}] must be an array of check ids"
                )
        out["check_map"] = {k: list(v) for k, v in check_map.items()}
    for key, val in raw.items():
        if key not in out:
            out[key] = val
    return out


def derive_manifest(prompt: str) -> dict[str, Any]:
    """Derive a manifest from a dispatch prompt. Derivation template v1.

    v1 is deliberately dumb, and versioned so a later template can be told apart
    in the ledger: if the prompt contains a fenced ```json block whose object has
    a top-level "manifest" key, that manifest is used. Otherwise the empty
    enrichable manifest is returned, with a note saying so. No inference, no
    keyword scraping, no model call — a manifest the operator did not write is
    not evidence of anything.

    A fenced block that *claims* a manifest but is malformed degrades to the
    empty manifest with a different note, so the failure is visible in the record
    rather than swallowed.
    """
    saw_malformed = False
    for match in _JSON_FENCE_RE.finditer(prompt or ""):
        body = match.group(1).strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict) or "manifest" not in parsed:
            continue
        try:
            return _coerce_manifest(parsed["manifest"])
        except ValueError:
            saw_malformed = True
    return empty_manifest(DERIVED_V1_MALFORMED if saw_malformed else DERIVED_V1_NO_MANIFEST)


# === Dispatch-intent sidecar ===

def intents_dir() -> Path:
    """The ``.fleetproof/intents/`` directory, resolved beside the runs dir."""
    return runs_dir().parent / INTENTS_DIRNAME


def intent_path(agent_type: str | None) -> Path | None:
    """The sidecar path for ``agent_type``, or None when it cannot name a file."""
    if not agent_type or not _INTENT_NAME_RE.match(agent_type):
        return None
    return intents_dir() / f"{agent_type}.json"


def write_intent(
    agent_type: str,
    prompt: str,
    *,
    manifest: dict[str, Any] | None = None,
    tier: str | None = None,
    role: str | None = None,
) -> Path:
    """Write the dispatch-intent sidecar for the next spawn of ``agent_type``.

    The dispatcher calls this (via ``fleetproof dispatch intent``) *before*
    spawning, so the SubagentStart capture finds the real prompt waiting instead
    of recording a placeholder. Overwrites an existing intent for the same agent
    type — the newest declaration wins, matching how a re-issued dispatch prompt
    supersedes the one it replaces. Validation is strict here, on the write
    side, where an error still has someone to land on; the consume side (a hook
    that must not crash a spawn) degrades instead.

    ``role`` is the second match key: a spawn whose payload agent_type equals
    this string consumes the sidecar even when the filename does not match —
    exact string equality only, never a prefix, because prefix matching is how
    a 'build' intent lands on a 'build-docs' spawn. It exists for dispatchers
    that name spawns per-task rather than per-role.

    Trust note, stated honestly: the intents directory must be treated as
    write-restricted to the dispatcher. A hook cannot enforce that — an agent
    with filesystem access could forge or replace a sidecar mid-run — so the
    consumed sidecar's name and byte hash are recorded on the dispatch
    (``intent_source``), making a swap attributable post-hoc, not preventable.
    """
    path = intent_path(agent_type)
    if path is None:
        raise LedgerError(
            f"Agent type {agent_type!r} cannot name an intent file; use a plain "
            "name (letters, digits, dot, dash, underscore)."
        )
    if not isinstance(prompt, str) or not prompt.strip():
        raise LedgerError("An intent needs a prompt; refusing to record an empty one.")
    if role is not None and (not isinstance(role, str) or not role.strip()):
        raise LedgerError("An intent role must be a non-empty string or omitted.")
    intent: dict[str, Any] = {
        "agent_type": agent_type,
        "prompt": prompt,
        "created_at": _now_iso(),
    }
    if role is not None:
        intent["role"] = role
    if manifest is not None:
        try:
            intent["manifest"] = _coerce_manifest(manifest)
        except ValueError as e:
            raise LedgerError(f"Unusable manifest: {e}") from e
    if tier is not None:
        intent["tier"] = _validate_tier(tier)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, intent)
    return path


def _intent_by_role(agent_type: str | None) -> Path | None:
    """The first sidecar (sorted by name) whose ``role`` field equals ``agent_type``.

    Exact string equality only. The reverse direction — a payload name that
    merely *starts with* a sidecar's name or role — is deliberately not a
    match: prefix matching invites collisions between similarly named spawns.
    """
    if not agent_type:
        return None
    directory = intents_dir()
    if not directory.exists():
        return None
    for path in sorted(directory.glob("*.json")):
        raw = _read_json(path)
        if isinstance(raw, dict) and raw.get("role") == agent_type:
            return path
    return None


def consume_intent(agent_type: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    """Read, validate, and DELETE the intent sidecar for one spawning agent.

    Returns ``(fields, notes)``. ``fields`` is ``{"prompt", "manifest", "tier",
    "source"}`` — manifest coerced (or None), tier validated (or None), source
    the consumed file's name plus a SHA-256 of its bytes (the post-hoc
    attribution for a forged or replaced sidecar; see :func:`write_intent`) —
    or None when no usable intent exists. ``notes`` are human-readable
    degradation notes for the caller to surface; an intent that half-worked
    must be visible, not silent. A clean miss (no file matched at all) returns
    ``(None, [])`` and the caller announces it — a silent miss leaves a
    placeholder prompt and a defaulted tier with nobody the wiser.

    Matching order: the sidecar named ``<agent_type>.json``, then any sidecar
    whose ``role`` field equals the agent_type exactly.

    Consumption is unconditional once a matching file is found: one intent, one
    spawn, even when the intent turns out malformed. A malformed sidecar left in
    place would attach itself to the *next* spawn of the same agent type, which
    misattributes a prompt — worse than losing it, and the note says what was
    lost. The one exception is a file that cannot be deleted: an undeletable
    sidecar would replay onto every later spawn, so it is not trusted either.
    """
    notes: list[str] = []
    path = intent_path(agent_type)
    if path is None or not path.exists():
        path = _intent_by_role(agent_type)
    if path is None:
        return None, notes
    try:
        raw_bytes: bytes | None = path.read_bytes()
    except OSError:
        raw_bytes = None
    try:
        path.unlink()
    except OSError as e:
        notes.append(f"could not delete intent file {path}: {e}; ignoring it")
        return None, notes
    raw: dict[str, Any] | None = None
    if raw_bytes is not None:
        # utf-8-sig: a sidecar hand-written from a Windows shell arrives with
        # a BOM, which must not degrade a real intent to the placeholder
        # capture. The recorded intent_source hash stays over the raw bytes,
        # BOM included — attribution hashes what was on disk, not what it
        # decoded to.
        try:
            parsed = json.loads(raw_bytes.decode("utf-8-sig"))
            raw = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raw = None
    if raw is None:
        notes.append(
            f"intent file {path.name} was unreadable or not a JSON object; "
            "capturing with the placeholder prompt instead"
        )
        return None, notes
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        notes.append(
            f"intent file {path.name} has no usable prompt; "
            "capturing with the placeholder prompt instead"
        )
        return None, notes
    fields: dict[str, Any] = {
        "prompt": prompt,
        "manifest": None,
        "tier": None,
        "source": {
            "file": path.name,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        },
    }
    raw_manifest = raw.get("manifest")
    if raw_manifest is not None:
        try:
            fields["manifest"] = _coerce_manifest(raw_manifest)
        except ValueError as e:
            # Degrade to derive-from-prompt, with the failure written into the
            # manifest notes so it survives on the dispatch record itself.
            notes.append(
                f"intent manifest for {agent_type!r} was malformed ({e}); "
                "deriving from the intent prompt instead"
            )
            derived = derive_manifest(prompt)
            base = derived.get("notes") or ""
            derived["notes"] = (f"{base}; " if base else "") + INTENT_MANIFEST_MALFORMED
            fields["manifest"] = derived
    raw_tier = raw.get("tier")
    if raw_tier is not None:
        if isinstance(raw_tier, str) and raw_tier in VALID_TIERS:
            fields["tier"] = raw_tier
        else:
            notes.append(
                f"intent {unknown_tier_message(raw_tier)}; "
                "using the captured-subagent default"
            )
    return fields, notes


# === Orphan stops ===

def orphans_dir() -> Path:
    """The ``.fleetproof/orphans/`` directory, resolved beside the runs dir."""
    return runs_dir().parent / ORPHANS_DIRNAME


def record_orphan_stop(
    *,
    agent_id: str | None,
    agent_type: str | None,
    session_id: str | None,
    last_assistant_message: str = "",
) -> Path:
    """Record an unpaired SubagentStop sighting. Returns the file written.

    Never creates a dispatch: there is no contract to grade an unpaired stop
    against, and a verdict on a manufactured record reads back as a real one.
    The record keeps just enough to identify the agent post-hoc — ids, session,
    and the head of its last message.
    """
    orphans_dir().mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": session_id,
        "last_assistant_message": (last_assistant_message or "")[:ORPHAN_MESSAGE_LIMIT],
        "at": _now_iso(),
    }
    for _ in range(_RUN_ID_ATTEMPTS):
        path = orphans_dir() / f"{_generate_run_id()}.json"
        if not path.exists():
            _write_json(path, entry)
            return path
    raise LedgerError("Could not allocate a unique orphan-stop file name.")


def list_orphan_stops(session_id: str | None = None) -> list[dict[str, Any]]:
    """Every recorded orphan stop, newest first; unreadable files are skipped."""
    directory = orphans_dir()
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        raw = _read_json(path)
        if raw is None:
            continue
        if session_id is not None and (raw.get("session_id") or None) != session_id:
            continue
        out.append(raw)
    return out


# === Creating a dispatch ===

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _transition(
    state: str, by: str, detail: str = "", extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"state": state, "at": _now_iso(), "by": by or "cli"}
    if detail:
        entry["detail"] = detail
    # Extra keys (a terminate reason, a report hash) ride on the transition they
    # describe, so the audit trail carries them per attempt instead of a single
    # latest-wins field on the record.
    if extra:
        entry.update(extra)
    return entry


def _default_parent_run_id() -> str | None:
    """The run this dispatch hangs off: the dispatching process's own run id.

    Prefers FLEETPROOF_RUN_ID (the run the dispatching process is executing as)
    and falls back to FLEETPROOF_PARENT_RUN_ID, which is what a hook process gets
    handed when it has no run of its own yet.
    """
    return os.environ.get(RUN_ID_ENV) or os.environ.get(PARENT_RUN_ID_ENV) or None


def _fresh_run_dir() -> tuple[str, Path]:
    """Allocate an unused run id + its directory."""
    rd = runs_dir()
    for _ in range(_RUN_ID_ATTEMPTS):
        run_id = _generate_run_id()
        run_dir = rd / run_id
        if not run_dir.exists():
            run_dir.mkdir(parents=True)
            return run_id, run_dir
    raise LedgerError("Could not allocate a unique dispatch run id.")


def _coerce_agent(raw: Any) -> dict[str, Any]:
    """Normalize the optional harness-agent block. Raises LedgerError if unusable."""
    if not isinstance(raw, dict):
        raise LedgerError("agent must be an object.")
    out: dict[str, Any] = {}
    for key in ("agent_id", "agent_type"):
        val = raw.get(key)
        if val is not None and not isinstance(val, str):
            raise LedgerError(f"agent.{key} must be a string or omitted.")
        out[key] = val or None
    capture = raw.get("capture")
    if capture is not None and capture not in VALID_CAPTURE:
        raise LedgerError(f"agent.capture must be one of {sorted(VALID_CAPTURE)}.")
    out["capture"] = capture
    for key, val in raw.items():
        if key not in out:
            out[key] = val
    return out


def create_dispatch(
    prompt: str,
    *,
    tier: str | None = None,
    manifest: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    agent: dict[str, Any] | None = None,
    intent_source: dict[str, Any] | None = None,
    tier_source: str | None = None,
    by: str = "cli",
    spec_path: Path | None = None,
    session_id: str | None = None,
    inherited_from: str | None = None,
) -> str:
    """Record a dispatch at launch and return its run id.

    Creates a new top-level run directory with a ``_root.json`` whose
    ``parent_run_id`` points at the dispatching run, plus the ``dispatch.json``
    that makes it a dispatch. The current check spec is pinned onto the record
    twice — the spec bytes (``spec_sha256_pinned``) and the whole checks tree
    including the scripts under ``.fleetproof/checks/`` (``tree_sha256_pinned``)
    — so a later verdict can be told whether it was graded against the spec,
    and the graders, that were in force when the work was ordered.

    ``tier`` omitted means infer it (recorded as ``tier_source="inferred"``);
    passing one records ``"declared"`` — unless ``tier_source`` overrides the
    attribution explicitly, which is how a capture that fell back to a default
    tier records ``"defaulted"`` instead of a declaration nobody made.
    ``manifest`` omitted means derive one from
    the prompt. ``agent`` records the harness subagent this dispatch stands for
    (``agent_id``/``agent_type``/``capture``), which is what lets a later
    SubagentStop find this record again. ``intent_source`` attributes the
    prompt to the consumed intent sidecar (file name + byte hash) so a forged
    sidecar is traceable post-hoc. ``spec_path`` overrides which spec file
    gets hashed (hooks/tests); by default the project's ``.fleetproof/checks.json``
    is used, and an unreadable spec pins ``null`` rather than failing the dispatch.
    ``session_id`` stamps the session explicitly; omitted, it is read from
    ``FLEETPROOF_SESSION_ID`` (a hook process always has it, a bare CLI shell
    usually does not — and a session-less dispatch is one no live session can
    ever adopt, see :func:`find_dispatch_for_stop`). ``inherited_from`` names
    the dispatch this one's intent was taken from and is only legal with
    ``tier_source="inherited"`` (and vice versa) — a record must not claim
    inheritance without naming its source, or name one without claiming it.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise LedgerError("A dispatch needs a prompt; refusing to record an empty one.")

    resolved_parent = parent_run_id if parent_run_id is not None else _default_parent_run_id()

    if tier is None:
        resolved_tier = infer_tier(resolved_parent)
        resolved_tier_source = TIER_SOURCE_INFERRED
    else:
        resolved_tier = _validate_tier(tier)
        resolved_tier_source = TIER_SOURCE_DECLARED
    if tier_source is not None:
        if tier_source not in VALID_TIER_SOURCES:
            raise LedgerError(
                f"Unknown tier_source {tier_source!r}; expected one of "
                f"{sorted(VALID_TIER_SOURCES)} or omit it."
            )
        resolved_tier_source = tier_source
    if (resolved_tier_source == TIER_SOURCE_INHERITED) != bool(inherited_from):
        raise LedgerError(
            "tier_source 'inherited' and inherited_from go together: pass both "
            "(naming the source dispatch) or neither."
        )

    if manifest is None:
        resolved_manifest = derive_manifest(prompt)
    else:
        try:
            resolved_manifest = _coerce_manifest(manifest)
        except ValueError as e:
            raise LedgerError(f"Unusable manifest: {e}") from e

    resolved_agent = _coerce_agent(agent) if agent is not None else None

    run_id, run_dir = _fresh_run_dir()
    if session_id is None:
        session_id = os.environ.get(SESSION_ID_ENV) or None

    # Same shape runlog writes for a root record, so `fleetproof list`/`show` and
    # the HTML report read dispatch runs back without knowing about the ledger.
    _write_json(run_dir / ROOT_FILENAME, {
        "run_id": run_id,
        "parent_run_id": resolved_parent,
        "session_id": session_id,
        "root_tool": DISPATCH_ROOT_TOOL,
        "started_at": _now_iso(),
        "host": socket.gethostname(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "pid": os.getpid(),
    })
    dispatch: dict[str, Any] = {
        "prompt": prompt,
        "tier": resolved_tier,
        "tier_source": resolved_tier_source,
        "manifest": resolved_manifest,
        "spec_sha256_pinned": spec_hash(spec_path),
        "tree_sha256_pinned": checks_tree_hash(spec_path),
        "transitions": [_transition(STATE_DISPATCHED, by)],
    }
    # Telemetry-era membership is stamped at creation from deployment config —
    # a property of the dispatch record itself, never inferred later from the
    # presence of a telemetry file (file absence is deletable; this stamp is
    # not). Omitted entirely pre-cutover, so older-shaped records stay older-shaped.
    era = telemetry_era_stamp()
    if era is not None:
        dispatch["telemetry_era"] = era
    # Omitted entirely when there is no agent, so a hand-made dispatch record
    # stays the shape Phase A wrote and readers keep treating absent as None.
    if resolved_agent is not None:
        dispatch["agent"] = resolved_agent
    # Same posture for the intent-sidecar attribution: only a consumed sidecar
    # puts it on the record.
    if intent_source is not None:
        dispatch["intent_source"] = dict(intent_source)
    if inherited_from:
        dispatch["inherited_from"] = inherited_from
    _write_json(run_dir / DISPATCH_FILENAME, dispatch)
    return run_id


# === State transitions ===

def _require_dispatch(run_id: str) -> DispatchRecord:
    record = load_dispatch(run_id)
    if record is None:
        raise LedgerError(f"No dispatch record for run {run_id!r}.")
    return record


def _append_transition(
    run_id: str,
    state: str,
    by: str,
    detail: str = "",
    extra: dict[str, Any] | None = None,
) -> DispatchRecord:
    """Append a transition after checking it is legal from the current state."""
    record = _require_dispatch(run_id)
    current = record.state
    allowed = _ALLOWED_NEXT.get(current, frozenset())
    if state not in allowed:
        raise LedgerError(
            f"Illegal transition {current!r} -> {state!r} for dispatch {run_id}; "
            f"legal next states: {sorted(allowed) or 'none (terminal)'}."
        )
    raw = _read_json(record.run_dir / DISPATCH_FILENAME) or {}
    transitions = raw.get("transitions")
    if not isinstance(transitions, list):
        transitions = []
    transitions.append(_transition(state, by, detail, extra))
    raw["transitions"] = transitions
    _write_json(record.run_dir / DISPATCH_FILENAME, raw)
    record.transitions = transitions
    return record


def _validate_report(report: Any) -> dict[str, Any]:
    """Loosely validate a dispatched agent's report. Additive: unknown keys pass.

    The one hard rule: a report with neither a summary nor any deliverable is not
    a report, and accepting it would let "I did nothing" register as "I reported".
    """
    if not isinstance(report, dict):
        raise LedgerError("A report must be a JSON object.")
    summary = report.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise LedgerError("report.summary must be a string.")
    deliverables = report.get("deliverables", [])
    if deliverables is None:
        deliverables = []
    if not isinstance(deliverables, list):
        raise LedgerError("report.deliverables must be an array.")
    if not (summary or "").strip() and not deliverables:
        raise LedgerError(
            "Empty report: needs a summary or at least one deliverable."
        )
    for i, item in enumerate(deliverables):
        if not isinstance(item, dict):
            raise LedgerError(f"report.deliverables[{i}] must be an object.")
        confidence = item.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise LedgerError(
                    f"report.deliverables[{i}].confidence must be a number 0.0-1.0."
                )
            if not 0.0 <= float(confidence) <= 1.0:
                raise LedgerError(
                    f"report.deliverables[{i}].confidence {confidence} is outside 0.0-1.0."
                )
        evidence = item.get("evidence")
        if evidence is not None and evidence not in VALID_EVIDENCE:
            raise LedgerError(
                f"report.deliverables[{i}].evidence must be one of "
                f"{sorted(VALID_EVIDENCE)}."
            )
    return report


def report_content_hash(report: dict[str, Any]) -> str:
    """SHA-256 (hex) over a report's canonical JSON serialization.

    Canonical (sorted keys, fixed separators) so the same claim hashes the same
    regardless of key order. This is the work-product identity a retry gets
    compared against: an unchanged hash across a contradicted->reported->verified
    chain says the *contradiction* changed its mind, not the work.
    """
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_report(run_id: str, report: dict[str, Any], by: str = "cli") -> DispatchRecord:
    """Store what the dispatched agent claimed, then mark it ``reported``.

    The report is written verbatim: it is the agent's own claim, and the whole
    point of keeping it is being able to diff a claim against a verdict later.

    Legal from ``dispatched`` and from ``contradicted`` (the gate-blocked retry).
    A retry overwrites ``report.json`` with the newer claim and *appends* the
    same payload as ``reports/NNN.json``, so every attempt's body survives —
    the transition list keeps the per-attempt hash, and the numbered file is
    the body that hash is of. (Before this, two of an agent's three reports
    were gone from disk — observed in a field deployment on Windows.)
    """
    record = _require_dispatch(run_id)
    validated = _validate_report(report)
    # Check the transition is legal *before* writing report.json, so a report
    # against a terminated dispatch cannot leave a claim on disk with no
    # transition explaining where it came from.
    allowed = _ALLOWED_NEXT.get(record.state, frozenset())
    if STATE_REPORTED not in allowed:
        raise LedgerError(
            f"Illegal transition {record.state!r} -> {STATE_REPORTED!r} for "
            f"dispatch {run_id}; legal next states: {sorted(allowed) or 'none (terminal)'}."
        )
    _write_json(record.run_dir / REPORT_FILENAME, validated)
    reports = record.run_dir / REPORTS_DIRNAME
    reports.mkdir(exist_ok=True)
    _write_json(reports / (_next_sequence_name(reports, ".json") + ".json"), validated)
    return _append_transition(
        run_id, STATE_REPORTED, by,
        extra={"report_sha256": report_content_hash(validated)},
    )


def record_block(
    run_id: str, text: str, checker_run_id: str | None = None,
) -> Path:
    """Persist the verbatim block text an agent was handed. Returns the .txt path.

    Not a transition: a block is the gate *refusing* a stop, and the state it
    refuses from (``contradicted``, or unchanged for a no-report block) is
    already on the transitions. What was missing was the message itself —
    the exact text the agent read, which decides whether a wedged agent was
    wedged by a bad check or by bad wording. ``checker_run_id`` names the
    evidence run the text was composed from, when there was one (a pin-drift
    or no-report block runs no checker).
    """
    record = _require_dispatch(run_id)
    blocks = record.run_dir / BLOCKS_DIRNAME
    blocks.mkdir(exist_ok=True)
    seq = _next_sequence_name(blocks, BLOCK_TEXT_SUFFIX)
    path = blocks / (seq + BLOCK_TEXT_SUFFIX)
    path.write_text(text, encoding="utf-8")
    _write_json(blocks / (seq + BLOCK_META_SUFFIX), {
        "at": _now_iso(),
        "checker_run_id": checker_run_id,
    })
    return path


def record_verdict(
    run_id: str,
    verdict: str,
    detail: str = "",
    by: str = "checker",
) -> DispatchRecord:
    """Append the checker's verdict on a reported dispatch.

    Only ``verified``, ``contradicted``, or ``advisory`` (a blocking failure
    under a disarmed gate — see :data:`STATE_ADVISORY`), and only after a
    report — grading a dispatch that never reported anything would be grading
    nothing.
    """
    if verdict not in VERDICT_STATES:
        raise LedgerError(
            f"Verdict must be one of {sorted(VERDICT_STATES)}; got {verdict!r}."
        )
    return _append_transition(run_id, verdict, by, detail)


def close_dispatch(
    run_id: str, by: str = "cli", reason: str | None = None,
) -> DispatchRecord:
    """Terminate a dispatch. Legal from any non-terminal state.

    ``reason`` is one of :data:`VALID_TERMINATE_REASONS` — or a parked reason,
    ``"parked: <text>"`` with a non-blank remainder — and says *why* work was
    terminated, which, for a dispatch that never reported, is the only thing
    that separates an idle agent swept off the board from an operator closing
    shop. Omitting it stays legal (older callers) and reads downstream as
    "unclassified"; passing anything else is an error, because a free-text
    reason would be underivable in exactly the way the vocabulary exists to
    prevent — the parked prefix is the one namespaced exception, classifiable
    by its prefix alone.
    """
    extra: dict[str, Any] | None = None
    if reason is not None:
        if reason not in VALID_TERMINATE_REASONS and not is_parked_reason(reason):
            raise LedgerError(
                f"Unknown terminate reason {reason!r}; expected one of "
                f"{sorted(VALID_TERMINATE_REASONS)}, "
                f"'{REASON_PARKED_PREFIX}<text>', or omit it."
            )
        extra = {"reason": reason}
    return _append_transition(run_id, STATE_TERMINATED, by, extra=extra)
