"""FleetProof command-line interface.

Stdlib only (argparse) — a verification tool should add as little dependency
surface as it can. Subcommands:

    init      write a starter .fleetproof/checks.json
    check     run the independent checker, record the verdict, exit non-zero on block
    list      list recorded runs, newest first
    show      show one run and its sub-invocations
    report    render the self-contained HTML run report
    cleanup   delete run records older than N days
    stop-gate Stop-hook entry: emit a Claude Code block decision on a false "done"
    record    PostToolUse-hook entry: append an evidence record from hook stdin
    subagent-start SubagentStart-hook entry: put a spawning subagent on the ledger
    subagent-stop  SubagentStop-hook entry: the per-subagent report-and-verify gate
    arm       arm the bridge Stop gate (the default state)
    disarm    set the bridge Stop gate to advisory; requires --note
    dispatch  ledger verbs: new / report / close / park a dispatch; intent
              writes the sidecar the SubagentStart capture consumes
    fleet     the dispatch board — every dispatch, its state, and whether it reported
    telemetry summary (local-only aggregates) / export (allowlisted bundle) /
              anchor (chain head) / loss (the one raw-loss question per failure)

Output is plain ASCII on purpose: these commands get read in cp1252 consoles on
Windows, where a stray unicode glyph is a UnicodeEncodeError, not a nicer table.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# The CLI must not record its own invocations.
os.environ.setdefault("FLEETPROOF_NO_RECORD", "1")

from . import __version__
from .checker import format_report_text, run_checks, spec_drifted
from .checks import (
    CheckSpecError,
    SPEC_DRIFT_NOTE,
    STARTER_SPEC,
    VALID_TIERS,
    default_checks_path,
    load_checks,
    short_spec_hash,
)
from .hookgate import (
    BRIDGE_ADVISORY,
    BRIDGE_ARMED,
    load_arming,
    record_tool_main,
    set_arming,
    stop_gate_main,
    subagent_start_main,
    subagent_stop_main,
)
from .ledger import (
    CAPTURE_CLI,
    REASON_ABANDONED,
    REASON_OPERATOR_CLOSE,
    REASON_PARKED_PREFIX,
    VALID_TERMINATE_REASONS,
    LedgerError,
    close_dispatch,
    create_dispatch,
    list_dispatches,
    list_orphan_stops,
    load_dispatch,
    record_report,
    write_intent,
)
# The board and the HTML report describe a dispatch with the same words, defined
# once in report.py. Two surfaces disagreeing about what a state is called is how
# an operator ends up unsure whether they are looking at the same dispatch twice.
from .report import (
    agent_label,
    state_label,
    tier_label,
    verdict_label,
    write_report,
)
from .runlog import (
    PROJECT_MARKER,
    SESSION_ID_ENV,
    list_run_records,
    load_run,
    project_root,
    runs_dir,
)
from .telemetry import TelemetryError, build_telemetry, record_failure_loss


# === Liveness beacon ===
#
# The harness registers hooks at session start, so a plugin installed or
# enabled mid-session is a gate that looks installed and grades nothing until
# the session restarts (observed in a field deployment on Windows). The read
# surfaces — fleet, check, report — are where an operator would look, so each
# says plainly when the current session has left no hook-produced record.

_LIVENESS_WARNING = (
    "WARNING: no hook has fired for this session — if you installed or "
    "enabled the plugin mid-session, the gate is INERT until the session "
    "restarts.")
_LIVENESS_UNKNOWN = "hooks-liveness unknown (not running under a hook session)."


def _session_has_hook_evidence(session_id: str) -> bool:
    """True when this session left any record only a hook could have written."""
    for run in list_run_records():
        if (run.session_id or None) != session_id:
            continue
        if run.root_tool == "claude-tool":
            return True
    for dispatch in list_dispatches(session_id=session_id):
        if any(t.get("by") == "hook" for t in dispatch.transitions
               if isinstance(t, dict)):
            return True
    return False


def _print_liveness_line(soft_when_unknown: bool = False) -> None:
    """One stderr line about hook liveness; stderr so json output stays clean."""
    session_id = os.environ.get(SESSION_ID_ENV)
    if not session_id:
        if soft_when_unknown:
            print(_LIVENESS_UNKNOWN, file=sys.stderr)
        return
    if not _session_has_hook_evidence(session_id):
        print(_LIVENESS_WARNING, file=sys.stderr)


def _cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else default_checks_path()
    if path.exists() and not args.force:
        print(f"Refusing to overwrite existing {path} (use --force).", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(STARTER_SPEC, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote starter check spec to {path}")
    print("Edit it to declare what 'done' means for this repo, then run `fleetproof check`.")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    _print_liveness_line()
    spec_path = Path(args.spec) if args.spec else None
    try:
        checks = load_checks(spec_path)
    except CheckSpecError as e:
        _emit_error("check_spec_error", str(e), args.format)
        return 2
    try:
        report = run_checks(checks, record_to_log=not args.no_record,
                            spec_path=spec_path, tier=args.tier)
    except ValueError as e:  # unknown tier
        _emit_error("bad_tier", str(e), args.format)
        return 2

    # Best-effort drift note: a bare CLI run usually has no session context (so
    # baseline is unresolvable and nothing is flagged), but when it runs inside a
    # Claude Code session the env carries the id and we can flag drift here too.
    session_id = os.environ.get(SESSION_ID_ENV)
    drifted, baseline = spec_drifted(report.spec_sha256, session_id)

    if args.format == "json":
        payload = report.to_dict()
        payload["run_id"] = report.run_id
        payload["spec_drift"] = drifted
        payload["session_baseline_sha256"] = baseline
        print(json.dumps(payload, indent=2))
    else:
        print(format_report_text(report))
        if drifted:
            print(SPEC_DRIFT_NOTE)
            print(f"  session baseline: {short_spec_hash(baseline)}")
    return 1 if report.verdict == "fail" else 0


def _cmd_list(args: argparse.Namespace) -> int:
    records = list_run_records()
    rows = []
    for r in records:
        failed = r.failed_count
        if args.status == "ok" and failed > 0:
            continue
        if args.status == "fail" and failed == 0:
            continue
        rows.append(r)
        if args.limit and len(rows) >= args.limit:
            break
    if args.format == "json":
        print(json.dumps({
            "runs": [
                {
                    "run_id": r.run_id,
                    "root_tool": r.root_tool,
                    "started_at": r.started_at,
                    "session_id": r.session_id,
                    "sub_count": len(r.sub_invocations),
                    "failed_count": r.failed_count,
                }
                for r in rows
            ]
        }, indent=2))
        return 0
    if not rows:
        print("No runs found.")
        return 0
    print(f"{'run_id':<24} {'root_tool':<16} {'started':<20} {'subs':>5} {'failed':>7} {'session':<14}")
    for r in rows:
        print(f"{r.run_id:<24} {(r.root_tool or '-'):<16} "
              f"{(r.started_at or '-')[:19]:<20} {len(r.sub_invocations):>5} {r.failed_count:>7} "
              f"{_short_session(r.session_id):<14}")
    return 0


def _short_session(session_id: str | None) -> str:
    """A compact session id for the list column; '-' when a run has none."""
    if not session_id:
        return "-"
    return session_id if len(session_id) <= 14 else session_id[:11] + "..."


def _cmd_show(args: argparse.Namespace) -> int:
    r = load_run(args.run_id)
    if r is None:
        _emit_error("run_not_found", f"No run record for {args.run_id!r}", args.format)
        return 1
    if args.format == "json":
        print(json.dumps({
            "run_id": r.run_id,
            "root_tool": r.root_tool,
            "started_at": r.started_at,
            "session_id": r.session_id,
            "sub_invocations": [
                {
                    "tool": s.tool,
                    "subcmd": s.subcmd,
                    "started_at": s.started_at,
                    "exit_code": s.exit_code,
                    "duration_ms": s.duration_ms,
                    "exception_type": s.exception_type,
                    "record_dir": str(s.record_dir),
                }
                for s in r.sub_invocations
            ],
        }, indent=2))
        return 0
    print(f"{r.run_id}  root_tool={r.root_tool or '-'}  started={(r.started_at or '-')[:19]}"
          f"  session={r.session_id or '-'}")
    for s in r.sub_invocations:
        if s.exit_code is None:
            badge = "?"
        elif s.exit_code == 0 and not s.exception_type:
            badge = "ok"
        else:
            badge = "fail"
        dur = f"{s.duration_ms:.1f}ms" if s.duration_ms is not None else "?ms"
        print(f"  [{badge}] {s.tool} {s.subcmd} ({dur})  {s.record_dir}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    _print_liveness_line()
    out = Path(args.output) if args.output else project_root() / PROJECT_MARKER / "fleetproof-report.html"
    written = write_report(out)
    print(f"Wrote report to {written}")
    return 0


def _cmd_cleanup(args: argparse.Namespace) -> int:
    rd = runs_dir()
    deleted, skipped = [], []
    if rd.exists():
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
        for run_dir in sorted(rd.iterdir()):
            if not run_dir.is_dir() or run_dir.name.startswith("."):
                continue
            started = _run_started_at(run_dir)
            if started is None or started >= cutoff:
                skipped.append(run_dir.name)
                continue
            if not args.dry_run:
                shutil.rmtree(run_dir, ignore_errors=True)
            deleted.append(run_dir.name)
    prefix = "DRY RUN " if args.dry_run else ""
    print(f"{prefix}Deleted {len(deleted)} runs, skipped {len(skipped)}.")
    return 0


def _run_started_at(run_dir: Path) -> datetime | None:
    record = load_run(run_dir.name)
    if record and record.started_at:
        try:
            return datetime.fromisoformat(record.started_at)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _cmd_stop_gate(args: argparse.Namespace) -> int:
    return stop_gate_main()


def _cmd_record(args: argparse.Namespace) -> int:
    # PostToolUse recorder must be allowed to write records.
    os.environ["FLEETPROOF_NO_RECORD"] = "0"
    return record_tool_main()


def _cmd_subagent_start(args: argparse.Namespace) -> int:
    return subagent_start_main()


def _cmd_subagent_stop(args: argparse.Namespace) -> int:
    return subagent_stop_main()


def _resolve_prompt(args: argparse.Namespace) -> str:
    """The dispatch prompt from --prompt or --prompt-file (exactly one).

    A prompt file exists because real dispatch prompts are multi-paragraph and
    shell-quoting one on Windows is how you end up recording a mangled prompt.
    """
    if bool(args.prompt) == bool(args.prompt_file):
        raise ValueError("Pass exactly one of --prompt or --prompt-file.")
    if args.prompt:
        return args.prompt
    try:
        return Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Could not read prompt file {args.prompt_file}: {e}") from e


def _load_json_file(path: str, label: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"Could not read {label} {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{label} {path} must contain a JSON object.")
    return data


def _cmd_dispatch_new(args: argparse.Namespace) -> int:
    try:
        prompt = _resolve_prompt(args)
    except ValueError as e:
        _emit_error("bad_prompt", str(e), args.format)
        return 2

    manifest = None
    if args.manifest:
        try:
            manifest = _load_json_file(args.manifest, "manifest")
        except ValueError as e:
            _emit_error("bad_manifest", str(e), args.format)
            return 2
        # Accept either a bare manifest object or a wrapper with a "manifest" key,
        # so the same file shape works whether it came from a prompt or by hand.
        inner = manifest.get("manifest")
        if isinstance(inner, dict):
            manifest = inner

    # --agent-name makes the dispatch joinable: the agent block records the
    # spawn name the harness will report as agent_type, with a null agent_id
    # the first matching SubagentStop adopts (find_dispatch_for_stop). The
    # session id rides in from FLEETPROOF_SESSION_ID via create_dispatch, so
    # a dispatch recorded inside a hook session lands in that session.
    agent = None
    if getattr(args, "agent_name", None):
        agent = {"agent_type": args.agent_name, "agent_id": None,
                 "capture": CAPTURE_CLI}

    try:
        run_id = create_dispatch(prompt, tier=args.tier, manifest=manifest,
                                 agent=agent)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1

    record = load_dispatch(run_id)
    if args.format == "json":
        print(json.dumps(record.to_dict() if record else {"run_id": run_id}, indent=2))
    else:
        print(run_id)
    return 0


def _cmd_dispatch_intent(args: argparse.Namespace) -> int:
    """Write the dispatch-intent sidecar the next SubagentStart capture consumes.

    Exists so dispatchers never hand-write the JSON: the prompt comes from a
    file (same rationale as ``dispatch new --prompt-file`` — shell-quoting a
    real prompt on Windows is how prompts get mangled), the manifest is
    validated here where an error still has someone to land on, and the printed
    path is the audit surface: the file that will vanish when the spawn
    consumes it.
    """
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    except OSError as e:
        _emit_error("bad_prompt",
                    f"Could not read prompt file {args.prompt_file}: {e}", args.format)
        return 2

    manifest = None
    if args.manifest:
        try:
            manifest = _load_json_file(args.manifest, "manifest")
        except ValueError as e:
            _emit_error("bad_manifest", str(e), args.format)
            return 2
        # Same tolerance as dispatch new: a bare manifest object or a wrapper
        # with a "manifest" key both work.
        inner = manifest.get("manifest")
        if isinstance(inner, dict):
            manifest = inner

    try:
        path = write_intent(args.agent, prompt, manifest=manifest, tier=args.tier,
                            role=args.role)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1
    if args.format == "json":
        print(json.dumps({"ok": True, "agent_type": args.agent,
                          "intent_path": str(path)}))
    else:
        print(str(path))
    return 0


def _cmd_dispatch_report(args: argparse.Namespace) -> int:
    try:
        payload = _load_json_file(args.report, "report")
    except ValueError as e:
        _emit_error("bad_report", str(e), args.format)
        return 2
    try:
        record = record_report(args.run_id, payload)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1
    if args.format == "json":
        print(json.dumps(record.to_dict(), indent=2))
    else:
        print(f"{record.run_id} -> {record.state}")
    return 0


def _cmd_dispatch_close(args: argparse.Namespace) -> int:
    try:
        record = close_dispatch(args.run_id, reason=args.reason)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1
    # The close finalizes the lifecycle, so the telemetry record derives here.
    # Best-effort: a telemetry failure must not turn a successful close into a
    # failed command — the summary surfaces the missing record as an integrity
    # defect instead.
    try:
        build_telemetry(record.run_id)
    except Exception as e:
        print(f"[fleetproof] telemetry build failed for {record.run_id}: {e}",
              file=sys.stderr)
    if args.format == "json":
        print(json.dumps(record.to_dict(), indent=2))
    else:
        print(f"{record.run_id} -> {record.state}")
    return 0


def _cmd_dispatch_park(args: argparse.Namespace) -> int:
    """Terminate a dispatch as parked: closed on purpose, with the reason kept.

    Parking is the dispatcher's exit for work that cannot be satisfied from
    its seat (an unsatisfiable check, a wedged or abandoned agent). The reason
    is required and travels on the terminate transition under the ``parked:``
    namespace; the parked dispatch is terminal, so a later stop from its agent
    lands on the orphan path instead of re-grading it.
    """
    reason = (args.reason or "").strip()
    if not reason:
        _emit_error("bad_reason", "park needs a non-empty --reason.", args.format)
        return 2
    try:
        record = close_dispatch(args.run_id, reason=REASON_PARKED_PREFIX + reason)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1
    # Same posture as dispatch close: the lifecycle just finished, so the
    # telemetry record derives here, best-effort.
    try:
        build_telemetry(record.run_id)
    except Exception as e:
        print(f"[fleetproof] telemetry build failed for {record.run_id}: {e}",
              file=sys.stderr)
    if args.format == "json":
        print(json.dumps(record.to_dict(), indent=2))
    else:
        print(f"{record.run_id} -> parked: {reason}")
    return 0


def _format_age(started_at: str | None) -> str:
    """Compact ASCII age of a dispatch: '42s', '17m', '3h05m', '2d04h', or '?'."""
    if not started_at:
        return "?"
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return "?"
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"


def _truncate(value: str, width: int) -> str:
    """Clip a display cell to ``width``, marking that it was clipped."""
    return value if len(value) <= width else value[:width - 3] + "..."


def _cmd_arm(args: argparse.Namespace) -> int:
    path = set_arming(BRIDGE_ARMED, note=(args.note or "").strip())
    if args.format == "json":
        print(json.dumps({"ok": True, "bridge": BRIDGE_ARMED, "path": str(path)}))
    else:
        print("bridge gate: armed. Blocking check failures block the stop again.")
    return 0


def _cmd_disarm(args: argparse.Namespace) -> int:
    # Asymmetric with arm on purpose: switching the gate OFF requires a reason,
    # and a whitespace note is no reason.
    note = (args.note or "").strip()
    if not note:
        _emit_error(
            "bad_note",
            "disarm needs a non-empty --note; switching the gate off requires "
            "a reason.", args.format)
        return 2
    path = set_arming(BRIDGE_ADVISORY, note=note)
    if args.format == "json":
        print(json.dumps({"ok": True, "bridge": BRIDGE_ADVISORY, "note": note,
                          "path": str(path)}))
    else:
        print(f"bridge gate: ADVISORY (disarmed): {note}")
        print("Checks still run and render; check failures no longer block the "
              "bridge's stop. The ledger sweep still blocks on stalled "
              "dispatches, and subagents are still graded. Re-arm: fleetproof arm")
    return 0


def _arming_board_line(arming: dict) -> str:
    """The fleet board's echo of a disarmed gate — the note travels with it."""
    note = str(arming.get("note") or "no note recorded")
    set_at = str(arming.get("set_at") or "?")[:19]
    by = str(arming.get("by") or "?")
    return (f"bridge gate: ADVISORY (disarmed): {note} "
            f"[set {set_at} by {by}; re-arm: fleetproof arm]")


def _cmd_fleet_orphans(args: argparse.Namespace) -> int:
    """The orphan-stop view: unpaired stops, which are sightings, not dispatches."""
    orphans = list_orphan_stops(session_id=args.session)
    if args.format == "json":
        print(json.dumps({"orphans": orphans}, indent=2))
        return 0
    if not orphans:
        print("No orphan stops recorded.")
        return 0
    print(f"{'at':<20} {'agent_type':<18} {'agent_id':<18} {'session':<14} message")
    for o in orphans:
        print(f"{str(o.get('at') or '-')[:19]:<20} "
              f"{_truncate(str(o.get('agent_type') or '-'), 18):<18} "
              f"{_truncate(str(o.get('agent_id') or '-'), 18):<18} "
              f"{_short_session(o.get('session_id')):<14} "
              f"{_truncate(str(o.get('last_assistant_message') or ''), 60)}")
    print(f"{len(orphans)} orphan stop(s): a SubagentStop with no dispatch to "
          "join. Not graded, not counted as dispatches.")
    return 0


def _orphan_count_line(orphans: list[dict]) -> str:
    return (f"{len(orphans)} orphan stop(s) this session — not graded "
            "(view: fleetproof fleet --orphans).")


def _cmd_fleet(args: argparse.Namespace) -> int:
    _print_liveness_line(soft_when_unknown=True)
    if args.orphans:
        return _cmd_fleet_orphans(args)
    records = list_dispatches(session_id=args.session, non_terminal_only=args.open)
    orphans = list_orphan_stops(session_id=args.session)
    # The board echoes a disarmed bridge gate whenever it is advisory — the
    # armed default stays quiet. Silence here is what makes the echo a signal.
    arming = load_arming()
    advisory = arming.get("bridge") == BRIDGE_ADVISORY
    if args.format == "json":
        payload = {
            "dispatches": [
                dict(r.to_dict(), age=_format_age(r.started_at)) for r in records
            ],
            "orphan_stop_count": len(orphans),
        }
        if advisory:
            payload["arming"] = arming
        print(json.dumps(payload, indent=2))
        return 0
    if advisory:
        print(_arming_board_line(arming))
    if not records:
        print("No dispatches found.")
        if orphans:
            print(_orphan_count_line(orphans))
        return 0
    print(f"{'run_id':<24} {'state':<26} {'tier':<12} {'agent':<18} "
          f"{'verdict':<13} {'age':>7} {'session':<14}")
    for r in records:
        print(f"{r.run_id:<24} {state_label(r):<26} {tier_label(r):<12} "
              f"{_truncate(agent_label(r), 18):<18} {verdict_label(r):<13} "
              f"{_format_age(r.started_at):>7} {_short_session(r.session_id):<14}")
    open_count = sum(1 for r in records if r.is_open)
    ungraded = sum(1 for r in records if r.verdict is None)
    print(f"{len(records)} dispatch(es), {open_count} awaiting a report, "
          f"{ungraded} ungraded.")
    # The legend is not decoration: '!' and '(stalled)' are load-bearing and an
    # unexplained marker on an audit surface is worse than no marker.
    print("tier! = declared, not inferred.  tier? = defaulted (no intent "
          "matched).  (stalled) = reported or graded but never closed.")
    print("ungraded = no verdict on record; an absent grade is not a passing grade.")
    # Only explained when present, like the orphan count: these two labels are
    # rare enough that an always-on legend line would drown the common ones.
    labels = {state_label(r) for r in records}
    if "abandoned!" in labels:
        print("abandoned! = terminated after 3 contradicted stops; the work "
              "was never verified. Park it, fix the spec/tier, or re-dispatch.")
    if "parked" in labels:
        print("parked = terminated on purpose with a recorded reason; not "
              "graded further.")
    if orphans:
        print(_orphan_count_line(orphans))
    return 0


def _parse_date(value: str | None, label: str) -> "date | None":
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"--{label} must be YYYY-MM-DD: {e}") from e


def _cmd_telemetry_summary(args: argparse.Namespace) -> int:
    from .telemetry_export import summarize
    summary = summarize()
    if args.format == "json":
        print(json.dumps(summary, indent=2))
        return 0
    print("Telemetry summary (LOCAL-ONLY: clear-text names and exact times;")
    print("anything shareable goes through `fleetproof telemetry export`).")
    for name, block in summary["windows"].items():
        counts = block["counts"]
        shown = {k: v for k, v in counts.items() if v}
        print(f"\n[{name}] {block['total_dispatches']} dispatch(es), "
              f"{block['classifiable_dispatches']} classifiable")
        print(f"  outcomes: {shown if shown else 'none'}")
        for metric in ("delivery_failure_rate", "false_claim_rate",
                       "abandoned_rate",
                       "near_miss_rate", "verifier_flake_rate",
                       "ungraded_rate", "unverifiable_rate",
                       "telemetry_missing_rate", "stop_only_fraction"):
            m = block[metric]
            rate = f"{m['rate']:.3f}" if m["rate"] is not None else "n/a"
            print(f"  {metric}: {m['numerator']}/{m['denominator']} = {rate}")
        sev = block["severity_distribution"]
        print(f"  severity: {({k: v for k, v in sev.items() if v}) or 'no failures'}")
        override = block["override_rate_by_source"]
        for source in ("hook", "operator"):
            m = override[source]
            rate = f"{m['rate']:.3f}" if m["rate"] is not None else "n/a"
            print(f"  override_rate[{source}]: {m['numerator']}/{m['denominator']} = {rate}")
    timeline = summary["spec_hash_timeline"]
    if timeline:
        print("\nspec-hash timeline:")
        for entry in timeline:
            print(f"  {entry['first_seen']}  {short_spec_hash(entry['spec_sha256'])}")
    return 0


def _cmd_telemetry_export(args: argparse.Namespace) -> int:
    from .telemetry_export import export_telemetry
    try:
        since = _parse_date(args.since, "since")
        until = _parse_date(args.until, "until")
    except ValueError as e:
        _emit_error("bad_date", str(e), args.format)
        return 2
    try:
        out = export_telemetry(
            args.recipient,
            out_dir=Path(args.output) if args.output else None,
            since=since, until=until)
    except TelemetryError as e:
        _emit_error("telemetry_error", str(e), args.format)
        return 1
    if args.format == "json":
        print(json.dumps({"ok": True, "export_dir": str(out)}))
    else:
        print(f"Wrote export bundle to {out}")
        print("Label: identified, minimized (single-party exports are "
              "identified by construction).")
    return 0


def _cmd_telemetry_anchor(args: argparse.Namespace) -> int:
    from .telemetry_export import anchor_chain
    anchor = anchor_chain()
    if args.format == "json":
        print(json.dumps(anchor, indent=2))
    else:
        print(f"chain head: {anchor['chain_head']}")
        print(f"entries:    {anchor['chain_entries']}")
        print(anchor["note"])
    return 0


def _cmd_telemetry_loss(args: argparse.Namespace) -> int:
    try:
        telemetry = record_failure_loss(
            args.run_id, args.value, args.unit,
            default_accepted=args.default_accepted or None)
    except TelemetryError as e:
        _emit_error("telemetry_error", str(e), args.format)
        return 1
    if args.format == "json":
        print(json.dumps(telemetry, indent=2))
    else:
        print(f"{args.run_id}: severity {telemetry['failure.severity_band']} "
              f"(floor {telemetry['failure.severity_floor']}), "
              f"loss {telemetry['failure.estimated_loss']}")
    return 0


def _emit_error(code: str, message: str, fmt: str) -> None:
    if fmt == "json":
        print(json.dumps({"ok": False, "error_code": code, "message": message}), file=sys.stderr)
    else:
        print(f"error ({code}): {message}", file=sys.stderr)


def _add_format(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=["human", "json"], default="human",
                   help="Output format. Use json for agent consumption.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleetproof",
        description="Independent, out-of-band verification for agent fleets.",
    )
    parser.add_argument("--version", action="version", version=f"fleetproof {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Write a starter .fleetproof/checks.json.")
    p_init.add_argument("--path", default=None, help="Where to write the spec.")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing spec.")
    p_init.set_defaults(func=_cmd_init)

    p_check = sub.add_parser("check", help="Run the independent checker and record the verdict.")
    p_check.add_argument("--spec", default=None, help="Path to a check spec (default: .fleetproof/checks.json).")
    p_check.add_argument("--no-record", action="store_true", help="Do not write to the run log.")
    p_check.add_argument("--tier", choices=sorted(VALID_TIERS), default=None,
                         help="Only run checks for this tier (untiered checks count "
                              "as bridge). Omit to run every check.")
    _add_format(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_list = sub.add_parser("list", help="List recorded runs, newest first.")
    p_list.add_argument("--status", choices=["ok", "fail"], default=None)
    p_list.add_argument("--limit", type=int, default=20)
    _add_format(p_list)
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show a run and its sub-invocations.")
    p_show.add_argument("run_id")
    _add_format(p_show)
    p_show.set_defaults(func=_cmd_show)

    p_report = sub.add_parser("report", help="Render the self-contained HTML run report.")
    p_report.add_argument("-o", "--output", default=None, help="Output HTML path.")
    p_report.set_defaults(func=_cmd_report)

    p_cleanup = sub.add_parser("cleanup", help="Delete run records older than N days.")
    p_cleanup.add_argument("--older-than-days", type=int, required=True)
    p_cleanup.add_argument("--dry-run", action="store_true")
    p_cleanup.set_defaults(func=_cmd_cleanup)

    p_stop = sub.add_parser("stop-gate", help="Stop-hook entry: emit a block decision on a false 'done'.")
    p_stop.set_defaults(func=_cmd_stop_gate)

    p_rec = sub.add_parser("record", help="PostToolUse-hook entry: record a tool call from hook stdin.")
    p_rec.set_defaults(func=_cmd_record)

    p_sstart = sub.add_parser(
        "subagent-start",
        help="SubagentStart-hook entry: record a spawning subagent as a dispatch.")
    p_sstart.set_defaults(func=_cmd_subagent_start)

    p_sstop = sub.add_parser(
        "subagent-stop",
        help="SubagentStop-hook entry: record the report, grade it, block a false 'done'.")
    p_sstop.set_defaults(func=_cmd_subagent_stop)

    p_arm = sub.add_parser(
        "arm",
        help="Arm the bridge Stop gate (the default): blocking check failures "
             "block the bridge's stop.")
    p_arm.add_argument("--note", default=None,
                       help="Optional note recorded with the arming state.")
    _add_format(p_arm)
    p_arm.set_defaults(func=_cmd_arm)

    p_disarm = sub.add_parser(
        "disarm",
        help="Set the bridge Stop gate to advisory: checks still run and "
             "render, but check failures no longer block the bridge's stop. "
             "The ledger sweep still blocks on stalled dispatches; subagent "
             "gating is unaffected. Requires --note.")
    p_disarm.add_argument(
        "--note", required=True,
        help="Why the gate is coming down (required; recorded in "
             ".fleetproof/arming.json and echoed by `fleetproof fleet`).")
    _add_format(p_disarm)
    p_disarm.set_defaults(func=_cmd_disarm)

    p_dispatch = sub.add_parser("dispatch", help="Dispatch-ledger verbs.")
    dsub = p_dispatch.add_subparsers(dest="dispatch_command", required=True)

    p_dnew = dsub.add_parser("new", help="Record a dispatch at launch; prints its run id.")
    p_dnew.add_argument("--prompt", default=None, help="The dispatch prompt, verbatim.")
    p_dnew.add_argument("--prompt-file", default=None,
                        help="Read the prompt from a file (use for multi-line prompts).")
    p_dnew.add_argument("--tier", choices=sorted(VALID_TIERS), default=None,
                        help="Declare the tier. Omit to infer it from the run tree.")
    p_dnew.add_argument("--manifest", default=None,
                        help="JSON file with the dispatch manifest. Omit to derive from the prompt.")
    p_dnew.add_argument("--agent-name", default=None,
                        help="The Task-tool spawn name the harness will report "
                             "as agent_type. Makes the dispatch joinable: the "
                             "first matching SubagentStop adopts it and grades "
                             "it, instead of the CLI record and the hook stop "
                             "forking into two ledgers.")
    _add_format(p_dnew)
    p_dnew.set_defaults(func=_cmd_dispatch_new)

    p_dint = dsub.add_parser(
        "intent",
        help="Write the dispatch-intent sidecar for the next spawn of one agent type.")
    p_dint.add_argument("--agent", required=True,
                        help="The agent_type the harness will report for the spawn.")
    p_dint.add_argument("--prompt-file", required=True,
                        help="File holding the dispatch prompt, verbatim.")
    p_dint.add_argument("--manifest", default=None,
                        help="JSON file with the dispatch manifest "
                             "(bare object or a {'manifest': ...} wrapper).")
    p_dint.add_argument("--tier", choices=sorted(VALID_TIERS), default=None,
                        help="Declare the tier. Omit to record the captured-subagent "
                             "default (lane).")
    p_dint.add_argument("--role", default=None,
                        help="Second match key: a spawn whose agent_type equals "
                             "this exactly consumes the intent even when the "
                             "filename does not match.")
    _add_format(p_dint)
    p_dint.set_defaults(func=_cmd_dispatch_intent)

    p_drep = dsub.add_parser("report", help="Record what a dispatched agent claimed.")
    p_drep.add_argument("run_id")
    p_drep.add_argument("--report", required=True, help="JSON file holding the report.")
    _add_format(p_drep)
    p_drep.set_defaults(func=_cmd_dispatch_report)

    p_dclose = dsub.add_parser("close", help="Terminate a dispatch.")
    p_dclose.add_argument("run_id")
    # Defaulted, not required: the CLI is the operator's close path, so the
    # honest machine-set reason for it is operator-close. Scripted sweepers and
    # session-teardown callers override it to say what they actually are. The
    # abandonment reason is excluded: it is the gate ladder's attestation of
    # three contradicted stops, not a label an operator may pick.
    p_dclose.add_argument(
        "--reason", choices=sorted(VALID_TERMINATE_REASONS - {REASON_ABANDONED}),
        default=REASON_OPERATOR_CLOSE,
        help="Why this dispatch is being terminated (default: operator-close).")
    _add_format(p_dclose)
    p_dclose.set_defaults(func=_cmd_dispatch_close)

    p_dpark = dsub.add_parser(
        "park",
        help="Terminate a dispatch as parked: work that cannot be satisfied "
             "from its seat, closed on purpose with the reason kept. A parked "
             "dispatch is terminal and is never re-graded.")
    p_dpark.add_argument("run_id")
    p_dpark.add_argument(
        "--reason", required=True,
        help="Why this dispatch is being parked (required; recorded on the "
             "terminate transition as 'parked: <reason>').")
    _add_format(p_dpark)
    p_dpark.set_defaults(func=_cmd_dispatch_park)

    p_tel = sub.add_parser("telemetry", help="Verification-telemetry surfaces.")
    tsub = p_tel.add_subparsers(dest="telemetry_command", required=True)

    p_tsum = tsub.add_parser(
        "summary",
        help="Local-only outcome/severity/integrity summary with trailing windows.")
    _add_format(p_tsum)
    p_tsum.set_defaults(func=_cmd_telemetry_summary)

    p_texp = tsub.add_parser(
        "export",
        help="Write the allowlisted, date-bucketed export bundle for one recipient.")
    p_texp.add_argument("--recipient", required=True,
                        help="Counterparty label; selects (or mints) the export salt.")
    p_texp.add_argument("-o", "--output", default=None, help="Output directory.")
    p_texp.add_argument("--since", default=None, help="Window start, YYYY-MM-DD.")
    p_texp.add_argument("--until", default=None, help="Window end, YYYY-MM-DD.")
    _add_format(p_texp)
    p_texp.set_defaults(func=_cmd_telemetry_export)

    p_tanc = tsub.add_parser(
        "anchor",
        help="Record the telemetry chain head as an anchorable value.")
    _add_format(p_tanc)
    p_tanc.set_defaults(func=_cmd_telemetry_anchor)

    p_tloss = tsub.add_parser(
        "loss",
        help="Record the one raw loss quantity for a failed run (hours or usd).")
    p_tloss.add_argument("run_id")
    p_tloss.add_argument("--value", type=float, required=True,
                         help="The raw quantity; the band is derived, never chosen.")
    p_tloss.add_argument("--unit", choices=["hours", "usd"], required=True)
    p_tloss.add_argument("--default-accepted", action="store_true",
                         help="The suggested default was accepted unchanged.")
    _add_format(p_tloss)
    p_tloss.set_defaults(func=_cmd_telemetry_loss)

    p_fleet = sub.add_parser("fleet", help="The dispatch board, newest first.")
    p_fleet.add_argument("--open", action="store_true",
                         help="Only dispatches that have not terminated.")
    p_fleet.add_argument("--session", default=None, help="Filter to one session id.")
    p_fleet.add_argument("--orphans", action="store_true",
                         help="List orphan stops (unpaired SubagentStops; "
                              "sightings, never graded) instead of dispatches.")
    _add_format(p_fleet)
    p_fleet.set_defaults(func=_cmd_fleet)

    return parser


def main(argv: list[str] | None = None) -> int:
    # A cp1252 console must degrade output, not crash it: a fleet board that dies
    # on one non-ASCII agent_type is a board the operator stops trusting.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
