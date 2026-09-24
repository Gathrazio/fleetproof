"""Static HTML report: what did my fleet actually do — and what did it lie about.

Reads the run log (written by one process) and the checker verdicts stored inside
it (written by another), and renders a single self-contained HTML file: per run,
what the fleet claimed vs. what the independent checker found. No CDN, no JS, no
external assets — one file an operator can hand to a compliance function.

Dispatch runs (v0.2) are first-class here. A dispatch is not an ordinary run with
a verdict attached: its grade lives on the ledger, appended by a process the
graded agent did not control, so it renders with its own row — state, tier,
verdict, agent type, what it was told to do, what it claimed back — plus the
transition trail that is the provenance for that verdict. A dispatch nobody
graded reads as ``ungraded``, never as a blank, because on a page full of
verdicts an empty cell reads as "fine".

The inline-CSS discipline here is carried over from the run-inspection tooling this
package grew out of; the body — claimed-vs-verified, not database tables — is new.
"""

from __future__ import annotations

import html as html_lib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checks import short_spec_hash
from .ledger import (
    CAPTURE_STOP_ONLY,
    DISPATCH_FILENAME,
    DISPATCH_ROOT_TOOL,
    REASON_ABANDONED,
    REASON_NO_REPORT,
    ROOT_FILENAME,
    STATE_ADVISORY,
    STATE_CONTRADICTED,
    STATE_DISPATCHED,
    STATE_TERMINATED,
    STATE_VERIFIED,
    TIER_SOURCE_DECLARED,
    TIER_SOURCE_DEFAULTED,
    TIER_SOURCE_INHERITED,
    DispatchRecord,
    is_parked_reason,
)
from .runlog import RunRecord, list_run_records

# How deep the tree indents before it stops. Past a few levels the indentation
# has stopped carrying information and started eating the page, and a malformed
# parent chain must not be able to indent a row off the right-hand edge.
MAX_TREE_DEPTH = 5

# How much of a prompt or a claimed summary a row shows before the operator has
# to open the record itself. The path to that record is printed alongside.
FIRST_LINE_LIMIT = 240

# How a row is classified for the operator.
STATUS_CONTRADICTED = "contradicted"   # claimed done, a blocking check disagreed
STATUS_UNGRADED = "ungraded"           # a dispatch with no verdict on record
STATUS_VERIFIED = "verified"           # an independent checker passed it
STATUS_ADVISORY = "advisory"           # a blocking check failed under a disarmed gate
STATUS_UNVERIFIED = "unverified"       # an ordinary run with no checker verdict

# Worst first. A container (session, tree) takes the status of the most alarming
# thing inside it, because what the operator does next is driven by the worst row,
# not the average one. ``ungraded`` outranks ``verified``: an absent grade is not a
# passing grade, and it is the second most important thing on the page.
# ``advisory`` sits beside it: a failed check whose block was switched off is
# closer to ungraded than to verified.
_STATUS_RANK = (STATUS_CONTRADICTED, STATUS_UNGRADED, STATUS_ADVISORY,
                STATUS_VERIFIED, STATUS_UNVERIFIED)


# === Shared dispatch vocabulary ===
#
# Both audit surfaces — this report and the `fleetproof fleet` board — describe a
# dispatch with these exact words. One definition, because an operator who sees
# "done" on the board and "terminated" in the report has to work out whether they
# are looking at the same thing, and that is the bug this avoids.

def state_label(dispatch: DispatchRecord) -> str:
    """Explicit English for a dispatch state.

    The raw state names are accurate and useless to skim: "dispatched" reads like
    a finished action, and "verified" on a dispatch nobody ever closed reads like
    everything went fine. A dispatch that terminated without ever reporting says
    so, rather than sharing the plain "done" a completed one gets.
    """
    state = dispatch.state
    if state == STATE_DISPATCHED:
        return "in-fleet (awaiting report)"
    if state == STATE_TERMINATED:
        reason = dispatch.terminate_reason
        if reason == REASON_ABANDONED:
            # Terminal after three contradicted stops. Loud on purpose: the
            # one thing this row must never read as is a clean "done" — the
            # work was never verified, and the dispatcher owes it a decision.
            return "abandoned!"
        if reason == REASON_NO_REPORT:
            # Terminal after three empty-message stops: the agent went idle
            # without ever reporting. Loud for the same reason abandoned! is —
            # this row must never skim as a clean "done".
            return "no-report!"
        if is_parked_reason(reason):
            # The --unsatisfiable marker is the operator-accepted escalation
            # exit; it must read differently from an ordinary park because
            # the dispatcher owes the two different follow-ups.
            if dispatch.park_unsatisfiable:
                # An escalation that reclassified a real failure is still an
                # escalation for the metric (decision 0012 keeps the
                # precedence), but the board must not hide which face it wears:
                # a contradicted verdict in the trail, or no report at all, is
                # a penalized failure the marker dropped out of the aggregate
                # rates (0.6.0 escalated red-team #3/#5/#6). A plain principled
                # escalation stays the unqualified label.
                if dispatch.escalated_over_contradiction:
                    return "parked (unsatisfiable, over contradiction)"
                if dispatch.escalated_unreported:
                    return "parked (unsatisfiable, unreported)"
                return "parked (unsatisfiable)"
            return "parked"
        return "done" if dispatch.has_report else "done (no report)"
    return f"{state} (stalled)"


def tier_label(dispatch: DispatchRecord) -> str:
    """Tier, with a trailing '!' when declared, '?'/'=' when defaulted, '~' when inherited.

    Inference is a guess off the shape of the run tree, and it degrades to ``lane``
    when a parent record is unreadable — so which provenance produced this tier
    changes how much the tier is worth. A defaulted tier splits in two, on
    ``intent_source`` (only ever written by a consumed sidecar): '=' means an
    intent DID match but declared no tier — the prompt and manifest are the
    dispatcher's, only the tier fell back; pass ``--tier`` to ``dispatch
    intent`` to declare it. '?' means no intent matched at all and the whole
    capture fell back — the visible trace of a missed intent. The two used to
    share '?', so a dispatcher who omitted ``--tier`` read their own matched
    sidecar as a miss (observed in a field deployment on Windows).
    An inherited tier ('~') means no sidecar matched but an earlier dispatch of
    the same agent type in the same session had one, and this dispatch is under
    that contract — distinct from '!' because nobody declared it *for this
    spawn*, and from '?' because it is graded. '~!' sharpens that: the stamped
    predecessor had already terminated with NO verdict when this dispatch
    inherited from it, so nothing ever validated the inherited configuration
    (a harness resume copied a never-graded, wrong-tier contract this way,
    observed in a field deployment on Windows). A record written before the
    stamp existed stays plain '~' — an absent stamp is not evidence the
    verdict was absent. One or two characters keep the column skimmable.
    """
    if not dispatch.tier:
        return "-"
    if dispatch.tier_source == TIER_SOURCE_DECLARED:
        marker = "!"
    elif dispatch.tier_source == TIER_SOURCE_DEFAULTED:
        marker = "=" if dispatch.intent_source is not None else "?"
    elif dispatch.tier_source == TIER_SOURCE_INHERITED:
        if (dispatch.inherited_from_state == STATE_TERMINATED
                and dispatch.inherited_from_verdict is None):
            marker = "~!"
        else:
            marker = "~"
    else:
        marker = ""
    return f"{dispatch.tier}{marker}"


def verdict_label(dispatch: DispatchRecord) -> str:
    """'verified', 'contradicted', or an explicit 'ungraded' — never a blank.

    A dispatch nobody graded is the single thing on an audit surface that must not
    be mistakable for a pass, and a dash in a column of verdicts reads as "fine".
    'verified*' marks a verified verdict recorded while one or more advisory
    (block:false) checks FAILED — the verdict stands, but a reader skimming the
    column must not mistake it for an unqualified pass (finding 1a: with only
    advisory checks a false "done" rendered plain 'verified' everywhere).
    """
    if dispatch.verdict == STATE_VERIFIED and dispatch.advisory_failures:
        return "verified*"
    return dispatch.verdict or STATUS_UNGRADED


def agent_label(dispatch: DispatchRecord) -> str:
    """The harness agent type, flagged when we only ever saw the agent stop.

    ``stop-only`` means the SubagentStart hook never fired for this agent and the
    dispatch was back-filled from its own stop. That is a hole in the capture
    surface rather than a normal case, so it travels with the agent type.
    """
    agent = dispatch.agent if isinstance(dispatch.agent, dict) else {}
    agent_type = agent.get("agent_type") or "-"
    if agent.get("capture") == CAPTURE_STOP_ONLY:
        return f"{agent_type} (stop-only)"
    return str(agent_type)


# === Reading records back ===

def _read_json_file(path: Path) -> dict[str, Any] | None:
    """Read a JSON object off disk, or None when missing/unreadable/not an object.

    Fail-open on purpose: one corrupt record must cost that one row its detail,
    never take down the report the operator is using to audit everything else.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dispatch_for(run: RunRecord, root: dict[str, Any]) -> DispatchRecord | None:
    """The ledger record for a dispatch run, read out of that run's own directory.

    Deliberately not :func:`ledger.load_dispatch`, which resolves paths through the
    process-global runs directory: :func:`build_report` accepts an arbitrary list of
    runs, and a report must describe the records it was actually handed.
    """
    raw = _read_json_file(run.root_dir / DISPATCH_FILENAME)
    if raw is None:
        return None
    transitions = raw.get("transitions")
    manifest = raw.get("manifest")
    agent = raw.get("agent")
    return DispatchRecord(
        run_id=run.run_id,
        run_dir=run.root_dir,
        parent_run_id=root.get("parent_run_id"),
        session_id=root.get("session_id"),
        prompt=str(raw.get("prompt") or ""),
        tier=raw.get("tier"),
        tier_source=raw.get("tier_source"),
        manifest=manifest if isinstance(manifest, dict) else {},
        spec_sha256_pinned=raw.get("spec_sha256_pinned"),
        transitions=transitions if isinstance(transitions, list) else [],
        started_at=root.get("started_at"),
        agent=agent if isinstance(agent, dict) else None,
        inherited_from=str(raw.get("inherited_from") or "") or None,
        inherited_from_state=str(raw.get("inherited_from_state") or "") or None,
        inherited_from_verdict=str(raw.get("inherited_from_verdict") or "") or None,
    )


def _find_check_verdicts(run: RunRecord) -> list[dict[str, Any]]:
    """Pull every recorded checker verdict out of a run's sub-invocations."""
    verdicts = []
    for sub in run.sub_invocations:
        if sub.tool == "fleetproof" and sub.subcmd == "check":
            payload = sub.load_output()
            if isinstance(payload, dict) and "checks" in payload:
                verdicts.append(payload)
    return verdicts


def _run_status(run: RunRecord, verdicts: list[dict[str, Any]]) -> str:
    """Classify an ordinary run: 'contradicted', 'verified', or 'unverified'."""
    if not verdicts:
        return STATUS_UNVERIFIED
    if any(v.get("verdict") == "fail" for v in verdicts):
        return STATUS_CONTRADICTED
    return STATUS_VERIFIED


def _dispatch_status(dispatch: DispatchRecord | None) -> str:
    """Classify a dispatch by its ledger verdict, not by checker sub-invocations.

    A dispatch run holds no checker records of its own: the hook that graded it ran
    as a different process and appended its verdict to the ledger transitions. So
    "ungraded" here means the transitions never reached a verdict — which gets its
    own status instead of being folded into the 'unverified' bucket ordinary runs
    use, because the two mean different things and only one of them is a fleet hole.
    """
    if dispatch is None:
        return STATUS_UNGRADED
    verdict = dispatch.verdict
    if verdict == STATE_CONTRADICTED:
        return STATUS_CONTRADICTED
    if verdict == STATE_VERIFIED:
        return STATUS_VERIFIED
    if verdict == STATE_ADVISORY:
        return STATUS_ADVISORY
    return STATUS_UNGRADED


@dataclass
class _Item:
    """One run, enriched with everything the renderer needs to classify it."""
    run: RunRecord
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_UNVERIFIED
    is_dispatch: bool = False
    dispatch: DispatchRecord | None = None
    parent_run_id: str | None = None


def _enrich(runs: list[RunRecord]) -> list[_Item]:
    """Read each run's root record once and classify it."""
    items: list[_Item] = []
    for run in runs:
        root = _read_json_file(run.root_dir / ROOT_FILENAME) or {}
        parent = root.get("parent_run_id")
        parent = str(parent) if parent else None
        if run.root_tool == DISPATCH_ROOT_TOOL:
            dispatch = _dispatch_for(run, root)
            items.append(_Item(
                run=run,
                status=_dispatch_status(dispatch),
                is_dispatch=True,
                dispatch=dispatch,
                parent_run_id=parent,
            ))
            continue
        verdicts = _find_check_verdicts(run)
        items.append(_Item(
            run=run,
            verdicts=verdicts,
            status=_run_status(run, verdicts),
            parent_run_id=parent,
        ))
    return items


def _worst_status(statuses: list[str]) -> str:
    for candidate in _STATUS_RANK:
        if candidate in statuses:
            return candidate
    return STATUS_UNVERIFIED


# === Tree ordering ===

# One positioned row: the item and how far to indent it.
_Placed = tuple[_Item, int]


def _tree_order(items: list[_Item]) -> list[_Placed]:
    """Order items parent-before-child, each paired with an indentation depth.

    A run nests under another only when its ``parent_run_id`` names a run in this
    same list. A parent outside the list — filtered out, cleaned up, or in another
    session — leaves the child at top level rather than dropping it.

    Cycle- and depth-safe by construction. Every item is emitted exactly once; an
    item whose ancestry loops is emitted at top level once no root can reach it.
    Losing a row is not an option on a surface whose whole job is showing the
    operator everything. The walk is iterative because a long or malformed parent
    chain must cost bounded stack, not a RecursionError inside a reporting tool.
    """
    by_id = {item.run.run_id: item for item in items}
    children: dict[str, list[_Item]] = {}
    roots: list[_Item] = []
    for item in items:
        parent = item.parent_run_id
        if parent and parent != item.run.run_id and parent in by_id:
            children.setdefault(parent, []).append(item)
        else:
            roots.append(item)

    ordered: list[_Placed] = []
    emitted: set[str] = set()
    stack: list[_Placed] = [(item, 0) for item in reversed(roots)]
    while True:
        while stack:
            item, depth = stack.pop()
            run_id = item.run.run_id
            if run_id in emitted:
                continue
            emitted.add(run_id)
            ordered.append((item, min(depth, MAX_TREE_DEPTH)))
            stack.extend(
                (child, depth + 1) for child in reversed(children.get(run_id, []))
            )
        remaining = [it for it in items if it.run.run_id not in emitted]
        if not remaining:
            return ordered
        # Nothing left is reachable from a root, so these parent links form a
        # cycle. Promote one to top level and drain again: a cycle costs its rows
        # their nesting and nothing else.
        stack.append((remaining[0], 0))


# === Block partitioning ===

def _session_baseline_hash(placed: list[_Placed]) -> str | None:
    """The earliest verdict's spec hash across a session's runs.

    Within a session block runs are ordered oldest-first, so the first verdict
    carrying a hash is the baseline every later verdict is diffed against in the
    report — the same rule the live gate uses.
    """
    for item, _depth in placed:
        for v in item.verdicts:
            sha = v.get("spec_sha256")
            if sha:
                return sha
    return None


def _session_blocks(items: list[_Item]) -> list[tuple[str, str | None, list[_Placed]]]:
    """Partition enriched runs into render blocks, newest activity first.

    Runs sharing a ``session_id`` collapse into one ``("session", id, [...])``
    block, tree-ordered oldest-first inside. Runs without a session id — including
    every record written before session grouping existed — render as their own
    ``("run", None, [...])`` block as before, except that a parent and its
    descendants now share one block so the relationship is visible instead of
    scattered across unrelated sections. Blocks are ordered by their most-recent
    run so the newest activity leads.
    """
    by_sid: dict[str | None, list[_Item]] = {}
    for item in items:
        by_sid.setdefault(item.run.session_id or None, []).append(item)

    blocks: list[tuple[str, str | None, list[_Placed]]] = []
    for sid, group in by_sid.items():
        if sid is None:
            continue
        group_sorted = sorted(group, key=lambda it: it.run.started_at or "")
        blocks.append(("session", sid, _tree_order(group_sorted)))

    loose = by_sid.get(None, [])
    if loose:
        chunk: list[_Placed] = []
        for placed in _tree_order(loose):
            if placed[1] == 0 and chunk:
                blocks.append(("run", None, chunk))
                chunk = []
            chunk.append(placed)
        if chunk:
            blocks.append(("run", None, chunk))

    def _recency(block: tuple[str, str | None, list[_Placed]]) -> str:
        return max((item.run.started_at or "" for item, _d in block[2]), default="")

    blocks.sort(key=_recency, reverse=True)
    return blocks


# === Rendering ===

def build_report(runs: list[RunRecord] | None = None) -> str:
    """Render the full HTML report for the given runs (default: all runs)."""
    if runs is None:
        runs = list_run_records()

    items = _enrich(runs)

    n_total = len(items)
    n_contradicted = sum(1 for it in items if it.status == STATUS_CONTRADICTED)
    n_verified = sum(1 for it in items if it.status == STATUS_VERIFIED)
    n_unverified = sum(1 for it in items if it.status == STATUS_UNVERIFIED)
    n_dispatches = sum(1 for it in items if it.is_dispatch)
    n_ungraded = sum(1 for it in items if it.status == STATUS_UNGRADED)

    blocks = _session_blocks(items)
    n_sessions = sum(1 for kind, _, _ in blocks if kind == "session")

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parts = [_HTML_HEAD.format(title=html_lib.escape("FleetProof — Run Report"))]
    parts.append("<h1>FleetProof — Run Report</h1>")
    session_meta = f" &nbsp; <b>Sessions:</b> {n_sessions}" if n_sessions else ""
    dispatch_meta = f" &nbsp; <b>Dispatches:</b> {n_dispatches}" if n_dispatches else ""
    parts.append(
        f"<p class='meta'><b>Generated:</b> {html_lib.escape(generated)} &nbsp; "
        f"<b>Runs:</b> {n_total}{session_meta}{dispatch_meta}</p>"
    )

    parts.append("<div class='cards'>")
    if n_sessions:
        parts.append(_stat_card("Sessions", str(n_sessions), "neutral"))
    parts.append(_stat_card("Runs", str(n_total), "neutral"))
    parts.append(_stat_card("Independently verified", str(n_verified), "ok"))
    parts.append(_stat_card("Claim contradicted", str(n_contradicted), "bad"))
    parts.append(_stat_card("Unverified", str(n_unverified), "warn"))
    if n_dispatches:
        parts.append(_stat_card("Dispatches", str(n_dispatches), "neutral"))
        parts.append(_stat_card("Dispatch ungraded", str(n_ungraded), "warn"))
    parts.append("</div>")

    if n_contradicted:
        parts.append(
            "<p class='lead bad-text'>"
            f"{n_contradicted} run(s) reported done, but the independent checker "
            "found a blocking failure. Those are below, marked "
            "<span class='badge bad'>contradicted</span>.</p>"
        )
    elif n_total == 0:
        parts.append("<p class='lead'>No runs recorded yet.</p>")
    else:
        parts.append(
            "<p class='lead ok-text'>No contradicted claims in this window.</p>"
        )
    if n_ungraded:
        parts.append(
            "<p class='lead warn-text'>"
            f"{n_ungraded} dispatch(es) carry no verdict at all, marked "
            "<span class='badge warn'>ungraded</span>. Nothing independently "
            "checked what they claimed: an absent grade is not a passing grade.</p>"
        )
    if n_total:
        parts.append(_LEGEND)

    for kind, sid, placed in blocks:
        if kind == "session":
            parts.append(_render_session(sid, placed))
        else:
            parts.append(_render_tree(placed))

    parts.append(_HTML_FOOT)
    return "\n".join(parts)


def _render_tree(placed: list[_Placed], baseline_hash: str | None = None) -> str:
    """Render a tree-ordered run list; each item carries its own indent depth."""
    return "\n".join(_render_item(item, baseline_hash, depth) for item, depth in placed)


def _render_session(session_id: str | None, placed: list[_Placed]) -> str:
    """Render one session as a container: a header with time range, then its runs."""
    starts = sorted(item.run.started_at for item, _d in placed if item.run.started_at)
    lo = starts[0][:19] if starts else "-"
    hi = starts[-1][:19] if starts else "-"
    status = _worst_status([item.status for item, _d in placed])
    n_dispatches = sum(1 for item, _d in placed if item.is_dispatch)

    parts = [f"<section class='session {status}'>"]
    parts.append(
        f"<h2 class='session-h'>Session <code>{html_lib.escape(str(session_id))}</code> "
        f"{_badge(status)}</h2>"
    )
    dispatch_meta = (f" &nbsp; <b>dispatches:</b> {n_dispatches}"
                     if n_dispatches else "")
    parts.append(
        "<p class='meta'>"
        f"<b>runs in session:</b> {len(placed)} &nbsp; "
        f"<b>time range:</b> {html_lib.escape(lo)} → {html_lib.escape(hi)}"
        f"{dispatch_meta}"
        "</p>"
    )
    baseline = _session_baseline_hash(placed)
    parts.append("<div class='session-body'>")
    parts.append(_render_tree(placed, baseline))
    parts.append("</div>")
    parts.append("</section>")
    return "\n".join(parts)


def _stat_card(label: str, value: str, tone: str) -> str:
    return (
        f"<div class='card {tone}'><div class='card-val'>{html_lib.escape(value)}</div>"
        f"<div class='card-lbl'>{html_lib.escape(label)}</div></div>"
    )


def _badge(status: str) -> str:
    tone = {
        STATUS_CONTRADICTED: "bad",
        STATUS_VERIFIED: "ok",
        STATUS_ADVISORY: "warn",
        STATUS_UNVERIFIED: "warn",
        STATUS_UNGRADED: "warn",
    }.get(status, "neutral")
    return f"<span class='badge {tone}'>{html_lib.escape(status)}</span>"


def _depth_class(depth: int) -> str:
    """The indentation class for a nested run, or '' at top level."""
    return f" d{depth}" if depth else ""


def _first_line(text: str, limit: int = FIRST_LINE_LIMIT) -> str:
    """The first non-empty line of ``text``, clipped and marked when clipped."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > limit:
                return stripped[:limit] + " ..."
            return stripped
    return ""


def _evidence_link(path: Path) -> str:
    """The on-disk record, as a local file link wrapped around the literal path.

    The path text stays visible and copy-pasteable either way: a ``file://`` link
    is a convenience for an operator reading the report where it was generated, and
    it degrades to plain text everywhere else. No external request is involved, so
    the report stays self-contained.
    """
    literal = f"<code>{html_lib.escape(str(path))}</code>"
    try:
        uri = path.as_uri()
    except ValueError:  # relative path: nothing to link to, show the text
        return literal
    return f"<a href='{html_lib.escape(uri, quote=True)}'>{literal}</a>"


def _parent_meta(item: _Item) -> str:
    """The parent-run pointer, so nesting is readable without relying on indent."""
    if not item.parent_run_id:
        return ""
    return (" &nbsp; <b>parent run:</b> "
            f"<code>{html_lib.escape(item.parent_run_id)}</code>")


def _render_item(item: _Item, baseline_hash: str | None = None, depth: int = 0) -> str:
    if item.is_dispatch:
        return _render_dispatch(item, depth)
    return _render_run(item, baseline_hash, depth)


def _render_run(item: _Item, baseline_hash: str | None = None, depth: int = 0) -> str:
    run, verdicts, status = item.run, item.verdicts, item.status
    parts = [f"<section class='run {status}{_depth_class(depth)}'>"]
    parts.append(
        f"<h2>{html_lib.escape(run.run_id)} {_badge(status)}</h2>"
    )
    parts.append(
        "<p class='meta'>"
        f"<b>root tool:</b> {html_lib.escape(run.root_tool or '-')} &nbsp; "
        f"<b>started:</b> {html_lib.escape((run.started_at or '-')[:19])} &nbsp; "
        f"<b>sub-invocations:</b> {len(run.sub_invocations)} &nbsp; "
        f"<b>failed sub-invocations:</b> {run.failed_count}"
        f"{_parent_meta(item)}"
        "</p>"
    )

    if not verdicts:
        parts.append(
            "<p class='note'>No independent checker verdict recorded for this run. "
            "The fleet's claim here has not been verified out-of-band.</p>"
        )
    for v in verdicts:
        parts.append(_render_verdict(v, baseline_hash))

    if run.sub_invocations:
        parts.append(_render_sub_table(run))

    parts.append("</section>")
    return "\n".join(parts)


def _render_dispatch(item: _Item, depth: int = 0) -> str:
    """Render a dispatch: who was told to do what, and how it was graded.

    The shape follows how an operator reads a board — the state/tier/verdict row
    they scan, the prompt and claimed report they spot-check, the transition trail
    and evidence path they drill into.
    """
    run, status, dispatch = item.run, item.status, item.dispatch
    parts = [f"<section class='run dispatch {status}{_depth_class(depth)}'>"]
    parts.append(
        f"<h2>{html_lib.escape(run.run_id)} "
        f"<span class='badge kind'>dispatch</span> {_badge(status)}</h2>"
    )
    if dispatch is None:
        # Recorded as a dispatch, but the record that says what it was for is gone.
        parts.append(
            "<p class='note bad-text'>This run is recorded as a dispatch, but its "
            "<code>dispatch.json</code> is missing or unreadable, so what it was "
            "told to do and whether anything graded it cannot be shown. Treat it "
            "as ungraded.</p>"
        )
        parts.append(
            "<p class='meta'>"
            f"<b>started:</b> {html_lib.escape((run.started_at or '-')[:19])}"
            f"{_parent_meta(item)} &nbsp; "
            f"<b>evidence:</b> {_evidence_link(run.root_dir)}"
            "</p>"
        )
        parts.append("</section>")
        return "\n".join(parts)

    parts.append(
        "<p class='meta'>"
        f"<b>started:</b> {html_lib.escape((run.started_at or '-')[:19])} &nbsp; "
        f"<b>pinned spec:</b> "
        f"<span class='spec'>{html_lib.escape(short_spec_hash(dispatch.spec_sha256_pinned))}</span>"
        f"{_parent_meta(item)}"
        "</p>"
    )
    parts.append("<table class='dispatch-row'><thead><tr>")
    parts.append("<th>state</th><th>tier</th><th>verdict</th><th>agent type</th>"
                 "<th>report on record</th>")
    parts.append("</tr></thead><tbody><tr>")
    parts.append(f"<td>{html_lib.escape(state_label(dispatch))}</td>")
    parts.append(f"<td>{html_lib.escape(tier_label(dispatch))}</td>")
    parts.append(f"<td>{_badge(verdict_label(dispatch))}</td>")
    parts.append(f"<td>{html_lib.escape(agent_label(dispatch))}</td>")
    parts.append(f"<td>{'yes' if dispatch.has_report else 'no'}</td>")
    parts.append("</tr></tbody></table>")
    parts.append(_render_dispatch_claim(dispatch))
    parts.append(_render_transitions(dispatch))
    parts.append("</section>")
    return "\n".join(parts)


def _render_dispatch_claim(dispatch: DispatchRecord) -> str:
    """What it was told to do and what it claimed back, first lines only.

    First lines because the operator is scanning many dispatches and the full text
    is in the record; the record's path is printed so drilling in does not require
    guessing where it lives.
    """
    parts = ["<table class='claim'><tbody>"]
    prompt_line = _first_line(dispatch.prompt)
    prompt_cell = html_lib.escape(prompt_line) if prompt_line else (
        "<span class='note'>no prompt recorded</span>")
    if dispatch.prompt.lstrip().startswith("[uncaptured]"):
        # The harness does not put a spawn prompt in the subagent payload, so this
        # dispatch never had one. Say that on the row rather than letting a
        # placeholder read as the real instruction.
        prompt_cell += " <span class='badge warn'>prompt not captured</span>"
    parts.append(f"<tr><th>told to do</th><td>{prompt_cell}</td></tr>")

    report = dispatch.load_report()
    if report is None:
        parts.append(
            "<tr><th>claimed back</th><td class='note'>No report on record. "
            "Nothing was claimed, so there is nothing that could have been "
            "verified.</td></tr>"
        )
    else:
        summary = _first_line(str(report.get("summary") or ""))
        source = report.get("source")
        cell = html_lib.escape(summary) if summary else (
            "<span class='note'>report on record with no summary</span>")
        if isinstance(source, str) and source:
            cell += f" <span class='count'>(via {html_lib.escape(source)})</span>"
        parts.append(f"<tr><th>claimed back</th><td>{cell}</td></tr>")

    parts.append(
        f"<tr><th>evidence</th><td>{_evidence_link(dispatch.run_dir)}</td></tr>"
    )
    parts.append("</tbody></table>")

    deliverables = report.get("deliverables") if isinstance(report, dict) else None
    if isinstance(deliverables, list) and deliverables:
        parts.append(_render_deliverables(deliverables))
    return "\n".join(parts)


def _render_deliverables(deliverables: list[Any]) -> str:
    """Per-deliverable confidence and evidence kind, exactly as claimed.

    Both columns read "not stated" when the report omitted them, rather than
    defaulting to anything reassuring: a deliverable with no evidence kind on
    record is one nobody checked, and that has to be visible.
    """
    parts = ["<h3>Claimed deliverables</h3>",
             "<table class='rows'><thead><tr>",
             "<th>deliverable</th><th>confidence</th><th>evidence</th><th>pointer</th>",
             "</tr></thead><tbody>"]
    for entry in deliverables:
        if not isinstance(entry, dict):
            parts.append("<tr class='row-warn'><td colspan='4'>"
                         f"{html_lib.escape(repr(entry))} (malformed deliverable)"
                         "</td></tr>")
            continue
        name = str(entry.get("name") or entry.get("id") or "-")
        confidence = entry.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence_text = "not stated"
        else:
            confidence_text = f"{float(confidence):.2f}"
        evidence = entry.get("evidence")
        pointer = entry.get("pointer") or entry.get("path") or "-"
        parts.append("<tr>")
        parts.append(f"<td>{html_lib.escape(name)}</td>")
        parts.append(f"<td>{html_lib.escape(confidence_text)}</td>")
        parts.append(f"<td>{html_lib.escape(str(evidence) if evidence else 'not stated')}</td>")
        parts.append(f"<td>{html_lib.escape(str(pointer))}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_transitions(dispatch: DispatchRecord) -> str:
    """The transition trail: every state change, when, and which process wrote it.

    This is the provenance for the verdict above. ``by`` is the column that matters:
    a verdict written by ``checker-via-hook`` came out of a process the graded agent
    did not control, which is the entire claim this tool makes.
    """
    if not dispatch.transitions:
        return ("<p class='note bad-text'>This dispatch has no transitions on record "
                "at all — a damaged record whose state cannot be trusted.</p>")
    parts = ["<h3>Transitions</h3>", "<table class='rows'><thead><tr>",
             "<th>state</th><th>at</th><th>by</th><th>detail</th>",
             "</tr></thead><tbody>"]
    for entry in dispatch.transitions:
        if not isinstance(entry, dict):
            parts.append("<tr class='row-warn'><td colspan='4'>"
                         f"{html_lib.escape(repr(entry))} (malformed transition)"
                         "</td></tr>")
            continue
        state = str(entry.get("state") or "?")
        row_cls = "row-fail" if state == STATE_CONTRADICTED else ""
        parts.append(f"<tr class='{row_cls}'>")
        parts.append(f"<td>{html_lib.escape(state)}</td>")
        parts.append(f"<td>{html_lib.escape(str(entry.get('at') or '-')[:19])}</td>")
        parts.append(f"<td>{html_lib.escape(str(entry.get('by') or '-'))}</td>")
        parts.append(f"<td>{html_lib.escape(str(entry.get('detail') or '-'))}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_verdict(v: dict[str, Any], baseline_hash: str | None = None) -> str:
    summary = v.get("summary", {})
    verdict = v.get("verdict", "?")
    # An all-advisory pass renders as 'advisory', never a green 'pass': zero
    # blocking checks means nothing was at stake in this verdict. The stored
    # verdict field keeps its pass/fail vocabulary; only the badge changes.
    # ``all_advisory`` is the current key; ``advisory`` is the 0.4.0 name,
    # still read for records written before the dual-write.
    advisory = bool(v.get("all_advisory", v.get("advisory"))) and verdict == "pass"
    if advisory:
        verdict, tone = "advisory", "warn"
    else:
        tone = "bad" if verdict == "fail" else "ok"
    sha = v.get("spec_sha256")
    tier = v.get("tier")
    drifted = bool(baseline_hash and sha and sha != baseline_hash)
    drift_badge = " <span class='badge drift'>spec drift</span>" if drifted else ""
    # Only rendered when the verdict was tier-scoped, so a v0.1 record reads as before.
    tier_note = (f" <span class='count'>tier {html_lib.escape(str(tier))}</span>"
                 if tier else "")
    parts = [
        f"<h3>Checker verdict: <span class='badge {tone}'>{html_lib.escape(verdict)}</span> "
        f"<span class='count'>({summary.get('passed', 0)}/{summary.get('total', 0)} passed, "
        f"{summary.get('blocking_failed', 0)} blocking)</span> "
        f"<span class='spec'>spec {html_lib.escape(short_spec_hash(sha))}</span>"
        f"{tier_note}{drift_badge}</h3>"
    ]
    if drifted:
        parts.append(
            "<p class='drift-note'>.fleetproof/checks.json was modified during this "
            "session (spec drift): this verdict was graded against "
            f"<code>{html_lib.escape(short_spec_hash(sha))}</code>, but the session "
            f"baseline is <code>{html_lib.escape(short_spec_hash(baseline_hash))}</code>. "
            "Review the diff before trusting this verdict.</p>"
        )
    parts.append("<table class='checks'><thead><tr>")
    parts.append("<th>check</th><th>expected</th><th>result</th><th>detail</th><th>blocking</th>")
    parts.append("</tr></thead><tbody>")
    for c in v.get("checks", []):
        passed = c.get("passed")
        blocking = c.get("blocking")
        row_cls = "row-pass" if passed else ("row-fail" if blocking else "row-warn")
        result = "pass" if passed else ("FAIL" if blocking else "warn")
        parts.append(f"<tr class='{row_cls}'>")
        parts.append(f"<td><code>{html_lib.escape(str(c.get('id', '?')))}</code></td>")
        parts.append(f"<td>{html_lib.escape(str(c.get('expectation', '-')))}</td>")
        parts.append(f"<td>{html_lib.escape(result)}</td>")
        parts.append(f"<td>{html_lib.escape(str(c.get('detail', '-')))}</td>")
        parts.append(f"<td>{'yes' if blocking else 'no'}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_sub_table(run: RunRecord) -> str:
    parts = ["<h3>Recorded invocations</h3>"]
    parts.append("<table class='rows'><thead><tr>")
    parts.append("<th>tool</th><th>subcmd</th><th>started</th><th>exit</th>"
                 "<th>duration</th><th>evidence</th>")
    parts.append("</tr></thead><tbody>")
    for s in run.sub_invocations:
        if s.exit_code is None:
            outcome = "?"
        elif s.exit_code == 0 and not s.exception_type:
            outcome = "0"
        else:
            outcome = f"{s.exit_code}{' / ' + s.exception_type if s.exception_type else ''}"
        dur = f"{s.duration_ms:.1f}ms" if s.duration_ms is not None else "-"
        parts.append("<tr>")
        parts.append(f"<td>{html_lib.escape(s.tool)}</td>")
        parts.append(f"<td>{html_lib.escape(s.subcmd)}</td>")
        parts.append(f"<td>{html_lib.escape((s.started_at or '-')[:19])}</td>")
        parts.append(f"<td>{html_lib.escape(outcome)}</td>")
        parts.append(f"<td>{html_lib.escape(dur)}</td>")
        parts.append(f"<td><code>{html_lib.escape(s.record_dir.name)}</code></td>")
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def write_report(output: Path, runs: list[RunRecord] | None = None) -> Path:
    """Render and write the report to ``output``; returns the path written."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_report(runs), encoding="utf-8")
    return output


# === HTML chrome (inline; no external deps) ===

_LEGEND = """<p class='legend'><b>How to read this:</b>
<span class='badge ok'>verified</span> an independent checker passed it &middot;
<span class='badge bad'>contradicted</span> it claimed done and a blocking check
disagreed &middot; <span class='badge warn'>advisory</span> a blocking check
failed but that tier's gate was disarmed, so nothing blocked &middot;
<span class='badge warn'>ungraded</span> a dispatch with no
verdict on record at all &middot; <span class='badge warn'>unverified</span> an
ordinary run with no checker verdict. Indented sections are runs dispatched by the
run above them. Every section prints the on-disk record it was built from.</p>"""

_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                       Helvetica, Arial, sans-serif;
          margin: 2em auto; max-width: 1100px; padding: 0 1em;
          color: #222; background: #fdfdfd; line-height: 1.45; }}
  h1, h2, h3 {{ color: #1a1a1a; }}
  h1 {{ border-bottom: 2px solid #888; padding-bottom: 0.3em; }}
  h2 {{ margin-top: 0.2em; font-size: 1.1em; }}
  h3 {{ margin-top: 1em; color: #555; font-size: 1em; }}
  .meta {{ color: #666; font-size: 0.9em; }}
  .count {{ color: #888; font-weight: normal; font-size: 0.9em; }}
  .lead {{ font-size: 1.05em; margin: 1em 0; }}
  .legend {{ color: #555; font-size: 0.85em; margin: 1em 0 1.4em;
             padding: 0.6em 0.9em; border: 1px solid #e2e2e2;
             border-radius: 6px; background: #fff; }}
  .note {{ color: #777; font-style: italic; }}
  .bad-text {{ color: #a11; }}
  .ok-text {{ color: #171; }}
  .warn-text {{ color: #a60; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 1em; margin: 1.2em 0; }}
  .card {{ flex: 1 1 140px; border: 1px solid #ddd; border-radius: 6px;
           padding: 0.8em 1em; background: #fff; }}
  .card-val {{ font-size: 1.8em; font-weight: 700; }}
  .card-lbl {{ color: #666; font-size: 0.85em; }}
  .card.ok .card-val {{ color: #171; }}
  .card.bad .card-val {{ color: #a11; }}
  .card.warn .card-val {{ color: #a60; }}
  .badge {{ display: inline-block; font-size: 0.7em; font-weight: 700;
            text-transform: uppercase; letter-spacing: 0.04em;
            padding: 2px 7px; border-radius: 10px; vertical-align: middle; }}
  .badge.ok {{ background: #e3f5e3; color: #171; }}
  .badge.bad {{ background: #fbe3e3; color: #a11; }}
  .badge.warn {{ background: #fbf1dd; color: #a60; }}
  .badge.neutral {{ background: #eee; color: #555; }}
  .badge.drift {{ background: #efe0fb; color: #6b21a8; }}
  .badge.kind {{ background: #e6eefb; color: #24478f; }}
  .spec {{ font-family: ui-monospace, "SF Mono", Consolas, monospace;
           font-size: 0.8em; color: #777; font-weight: normal; }}
  .drift-note {{ margin: 0.3em 0 0.6em; padding: 0.5em 0.8em;
                 border-left: 4px solid #8b3fd1; background: #f6edfc;
                 color: #6b21a8; font-size: 0.9em; }}
  section.session {{ margin-top: 1.8em; padding: 1em 1.2em 1.2em;
                     border-radius: 8px; border: 1px solid #d3d3d3;
                     background: #f4f5f7; }}
  section.session > .session-h {{ margin-top: 0; font-size: 1.15em; }}
  section.session.contradicted {{ border-left: 6px solid #c33; }}
  section.session.verified {{ border-left: 6px solid #3a3; }}
  section.session.unverified {{ border-left: 6px solid #d9a441; }}
  section.session.ungraded {{ border-left: 6px solid #d9a441; }}
  .session-body section.run {{ margin-top: 1em; }}
  section.run {{ margin-top: 1.6em; padding: 1em 1.2em; border-radius: 6px;
                 border: 1px solid #e2e2e2; background: #fff; }}
  section.run.contradicted {{ border-left: 5px solid #c33; }}
  section.run.verified {{ border-left: 5px solid #3a3; }}
  section.run.unverified {{ border-left: 5px solid #d9a441; }}
  /* Ungraded outranks unverified: same amber, but tinted so it does not read as
     just another run nobody happened to check. */
  section.run.ungraded {{ border-left: 5px solid #d9a441; background: #fffbf2; }}
  section.run.advisory {{ border-left: 5px solid #d9a441; background: #fffbf2; }}
  section.session.advisory {{ border-left: 6px solid #d9a441; }}
  section.run.dispatch {{ border-top: 1px solid #cddafc; }}
  /* One indent level per nesting depth, capped in the renderer at 5. */
  section.run.d1 {{ margin-left: 2em; }}
  section.run.d2 {{ margin-left: 4em; }}
  section.run.d3 {{ margin-left: 6em; }}
  section.run.d4 {{ margin-left: 8em; }}
  section.run.d5 {{ margin-left: 10em; }}
  table {{ border-collapse: collapse; margin: 0.5em 0 1em 0;
           font-size: 0.9em; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: left;
            vertical-align: top; }}
  th {{ background: #f0f0f0; font-weight: 600; }}
  tr.row-fail td {{ background: #fdecec; }}
  tr.row-warn td {{ background: #fdf6e8; }}
  code {{ font-family: ui-monospace, "SF Mono", Consolas, monospace;
          background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }}
  a {{ color: #24478f; }}
  a code {{ background: #eef2fb; }}
  table.rows td {{ font-family: ui-monospace, "SF Mono", Consolas, monospace;
                   overflow-wrap: anywhere; }}
  table.claim th {{ width: 9em; white-space: nowrap; }}
  table.claim td {{ overflow-wrap: anywhere; }}
</style>
</head>
<body>
"""

_HTML_FOOT = """
</body>
</html>
"""
