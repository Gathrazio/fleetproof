"""FleetProof command-line interface.

Stdlib only (argparse) — a verification tool should add as little dependency
surface as it can. Subcommands:

    init      write a starter .fleetproof/checks.json
    check     run the independent checker, record the verdict, exit non-zero on block
    check control  record a grader control (pass/fail samples + provenance) for one check
    list      list recorded runs, newest first
    show      show one run and its sub-invocations
    report    render the self-contained HTML run report
    cleanup   delete run records older than N days
    stop-gate Stop-hook entry: emit a Claude Code block decision on a false "done"
    record    PostToolUse-hook entry: append an evidence record from hook stdin
    subagent-start SubagentStart-hook entry: put a spawning subagent on the ledger
    subagent-stop  SubagentStop-hook entry: the per-subagent report-and-verify gate
    arm       arm a gate (the default state); --tier bridge|coordinator
    disarm    set a gate to advisory; requires --note; --tier bridge|coordinator
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
from .checker import (
    CHECK_ENV_AGENT_TYPE,
    CHECK_ENV_RUN_ID,
    CHECK_ENV_SESSION_ID,
    CHECK_ENV_TIER,
    check_env,
    format_arming_stamp,
    format_report_text,
    run_check,
    run_checks,
    spec_drifted,
)
from .checks import (
    Check,
    CheckSpecError,
    SPEC_DRIFT_NOTE,
    STARTER_SPEC,
    VALID_TIERS,
    default_checks_path,
    load_checks,
    parse_manifest_check,
    short_spec_hash,
)
from .controls import (
    CONTROL_SAMPLE_ENV,
    VALID_PROVENANCE,
    ControlError,
    control_warnings,
    record_control,
)
from .hookgate import (
    ADVISORY,
    ARMABLE_TIERS,
    ARMED,
    DEFAULT_ARMING_TIER,
    LANE_NEVER_DISARMED,
    load_arming,
    tier_arming,
    tier_set_meta,
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
    list_ungraded_terminations,
    load_dispatch,
    record_report,
    ungraded_termination_line,
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

# The arming stamp a bare `fleetproof check` writes: no gate, so no arming.
ARMING_NOT_APPLICABLE = "n/a"


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
    # The bare CLI has no dispatch: checks see the tier and session only
    # (empty when unknown); run id and agent type are left as inherited.
    identity = {CHECK_ENV_TIER: args.tier or "",
                CHECK_ENV_SESSION_ID: os.environ.get(SESSION_ID_ENV) or ""}
    # No gate ordered this run, so no arming governed it: state "n/a" on the
    # record, distinct from both "armed" and "advisory".
    arming = {"tier": args.tier, "state": ARMING_NOT_APPLICABLE, "note": None}
    try:
        report = run_checks(checks, record_to_log=not args.no_record,
                            spec_path=spec_path, tier=args.tier, identity=identity,
                            arming=arming)
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


def _cmd_check_control(args: argparse.Namespace) -> int:
    """Record a grader control: the samples a check was exercised against.

    The check's command is resolved from ``--manifest`` when given (the
    manifest checks are where lane grading lives), else from the repo spec;
    when neither names the id the control is recorded without an observed
    run and says so — the samples' hashes and provenance are still the
    record that matters. See :mod:`fleetproof.controls`.
    """
    check = None
    source = None
    if args.manifest:
        try:
            manifest = _load_json_file(args.manifest, "manifest")
        except ValueError as e:
            _emit_error("bad_manifest", str(e), args.format)
            return 2
        inner = manifest.get("manifest")
        if isinstance(inner, dict):
            manifest = inner
        try:
            found = [c for c in _manifest_checks_strict(manifest) if c.id == args.check_id]
        except CheckSpecError as e:
            _emit_error("bad_manifest_check", str(e), args.format)
            return 2
        if found:
            check, source = found[0], "manifest"
    if check is None:
        try:
            found = [c for c in load_checks(Path(args.spec) if args.spec else None)
                     if c.id == args.check_id]
        except CheckSpecError:
            found = []
        if found:
            check, source = found[0], "spec"
    try:
        record = record_control(
            args.check_id,
            pass_sample=Path(args.pass_sample),
            fail_sample=Path(args.fail_sample) if args.fail_sample else None,
            provenance=args.provenance,
            note=(args.note or "").strip(),
            check=check, check_source=source, cwd=project_root())
    except ControlError as e:
        _emit_error("control_error", str(e), args.format)
        return 2
    if args.format == "json":
        print(json.dumps(record, indent=2))
        return 0
    print(f"control recorded for '{args.check_id}' (provenance: {record['provenance']})")
    if check is None:
        print(f"  check not found in a manifest or the spec; samples hashed, "
              f"no run observed. Pass --manifest <file> to run it.")
    for direction in ("pass_sample", "fail_sample"):
        rec = record[direction]
        if rec is None:
            continue
        line = f"  {direction}: {rec['path']} sha256 {rec['sha256'][:12]}"
        if rec.get("observed_exit") is not None or rec.get("observed_pass") is not None:
            verdict = "agrees" if rec.get("agrees") else "DISAGREES"
            line += (f"; observed exit {rec['observed_exit']} -> "
                     f"{'pass' if rec['observed_pass'] else 'fail'} ({verdict})")
        print(line)
    if record["provenance"] != "captured":
        print("  WARNING: an authored pass sample shares its author's beliefs. "
              "Replace it with a captured real emission before pinning.")
    if record["fail_sample"] is None:
        print("  no fail sample: the control shows the check can pass, not that it "
              "can fail.")
    print(f"  the sample path reaches the check as ${CONTROL_SAMPLE_ENV}.")
    return 0


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
                    "arming": _checker_arming(s),
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
        # A checker verdict says which gate's arming governed it, every
        # time — a reader six months on must not need arming.json as it was.
        stamp = format_arming_stamp(_checker_arming(s))
        if stamp:
            print(f"        {stamp}")
    return 0


def _checker_arming(sub) -> dict | None:
    """The arming stamp on a checker sub-invocation's output.json, or None
    (older records, non-checker invocations, unreadable output)."""
    if sub.tool != "fleetproof" or sub.subcmd != "check":
        return None
    payload = sub.load_output()
    if not isinstance(payload, dict):
        return None
    arming = payload.get("arming")
    return arming if isinstance(arming, dict) else None


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
        # utf-8-sig: a prompt file written by PowerShell redirection carries a
        # BOM, and a plain utf-8 read embeds it at the start of the recorded
        # prompt (same rationale as _load_json_file).
        return Path(args.prompt_file).read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ValueError(f"Could not read prompt file {args.prompt_file}: {e}") from e


def _load_json_file(path: str, label: str) -> dict:
    # utf-8-sig: PowerShell redirection writes UTF-8 with a BOM, and a
    # BOM-prefixed manifest or report must not be rejected as invalid JSON
    # (same rationale as the spec loader's utf-8-sig read of checks.json).
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise ValueError(f"Could not read {label} {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"{label} {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{label} {path} must contain a JSON object.")
    return data


# === Preflight: see the grader run before the work is pinned to it ===
#
# A blocking check demanded a health field that had never existed, its
# positive control was a sample its own author wrote, and the lane renamed a
# production field to satisfy it (observed in a field deployment on Windows).
# Nothing in the tool showed the author what the command actually was and
# what the target actually emitted before the intent was pinned. Preflight
# does exactly that and nothing else: it runs each manifest check now, from
# the project root, with the runner the gate uses, and prints the resolved
# command line, the exit code, the expectation, PASS/FAIL, and a redacted
# output tail. It records nothing under runs/ — the work has not happened
# yet, most checks are expected to fail, and a preview is not evidence.

_PREFLIGHT_CLOSING = (
    "A positive control must be a captured real emission, never authored by "
    "the check's author.")
_PREFLIGHT_TAIL_LINES = 8


def _manifest_checks_strict(manifest: dict | None) -> list[Check]:
    """Every manifest check parsed with the gate's parser; the first malformed
    entry raises :class:`CheckSpecError` naming it. Authoring time is where
    a malformed check still has someone to land on — the gate would skip it
    with a stderr line nobody reads."""
    entries = (manifest or {}).get("checks") or []
    if not isinstance(entries, list):
        raise CheckSpecError("manifest.checks must be an array")
    out: list[Check] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        check = parse_manifest_check(entry, f"manifest check [{i}]")
        if check.id in seen:
            raise CheckSpecError(f"manifest check [{i}]: duplicate id {check.id!r}")
        seen.add(check.id)
        out.append(check)
    return out


def _describe_command(check: Check) -> tuple[str, str]:
    """``(form, rendered)``: the exact command as the runner will see it.
    Argv form is rendered as a JSON array — the one rendering that is
    re-parseable into the argv it came from."""
    if check.run is None:
        return "none", "(no command; file_exists only)"
    if isinstance(check.run, str):
        return "shell", check.run
    return "argv", json.dumps(check.run)


def _tail_lines(text: str, limit: int = _PREFLIGHT_TAIL_LINES) -> list[str]:
    lines = text.rstrip("\r\n").splitlines()
    if len(lines) <= limit:
        return lines
    return ["...(" + str(len(lines) - limit) + " earlier line(s) omitted)"] + lines[-limit:]


def _preflight_manifest(checks: list[Check], *, tier: str | None,
                        agent_type: str | None) -> list[dict]:
    """Run every manifest check now, as the gate would, recording nothing.

    Same runner (:func:`fleetproof.checker.run_check`), same cwd (the
    resolved project root), same argv-or-shell as declared, same identity
    environment the gate would set — except the run id, which does not
    exist yet and is the empty string. Output tails are the checker's own
    redacted tails, so a preview leaks nothing a verdict would not.
    """
    root = project_root()
    env = check_env({
        CHECK_ENV_RUN_ID: "",
        CHECK_ENV_AGENT_TYPE: agent_type or "",
        CHECK_ENV_TIER: tier or "",
        CHECK_ENV_SESSION_ID: os.environ.get(SESSION_ID_ENV) or "",
    })
    results = []
    for check in checks:
        form, rendered = _describe_command(check)
        result = run_check(check, root, env=env)
        results.append({
            "id": check.id,
            "blocking": check.block,
            "owner": check.owner,
            "form": form,
            "command": rendered,
            "expectation": result.expectation,
            "returncode": result.returncode,
            "passed": result.passed,
            "detail": result.detail,
            "stdout_tail": result.stdout_tail,
            "stderr_tail": result.stderr_tail,
            "duration_ms": result.duration_ms,
        })
    return results


def _render_preflight(results: list[dict]) -> str:
    root = project_root()
    lines = [f"preflight: {len(results)} manifest check(s), run now from {root} "
             "as the gate would; nothing recorded"]
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        kind = "blocking" if r["blocking"] else "advisory"
        if r["owner"]:
            kind += f", owner: {r['owner']}"
        lines.append(f"[{mark}] {r['id']}  ({kind})")
        lines.append(f"  {r['form'] + ':':<8}{r['command']}")
        lines.append(f"  expect: {r['expectation']}")
        code = "none (did not complete)" if r["returncode"] is None else str(r["returncode"])
        lines.append(f"  exit:   {code}  -> {r['detail']}")
        for stream in ("stdout", "stderr"):
            tail = _tail_lines(r[f"{stream}_tail"])
            if not tail:
                lines.append(f"  {stream}: (empty)")
                continue
            lines.append(f"  {stream}: | {tail[0]}")
            lines.extend(f"          | {line}" for line in tail[1:])
    passed = sum(1 for r in results if r["passed"])
    lines.append(f"preflight: {passed} passed, {len(results) - passed} failed of "
                 f"{len(results)} - a preview only; nothing was recorded under runs/.")
    lines.append(_PREFLIGHT_CLOSING)
    return "\n".join(lines)


def _controls_ok(manifest: dict | None, *, strict: bool, fmt: str) -> bool:
    """Warn — or, when ``strict``, refuse — per blocking manifest check with
    no grader control or an authored-only pass sample.

    Said on stderr before anything is written, one loud line per check, so a
    json consumer keeps clean stdout and a human cannot miss it. Checks that
    do not parse are not warned about here: the gate will skip them and say
    so, and preflight refuses them. With ``strict`` the first offence ends
    the command with nothing written: a blocking check whose grader was
    never shown a real emission is a blocking check with an agent, a deploy
    path, and a deadline pointed at it (observed in a field deployment on
    Windows).
    """
    entries = (manifest or {}).get("checks") or []
    parsed: list[Check] = []
    if isinstance(entries, list):
        for i, entry in enumerate(entries):
            try:
                parsed.append(parse_manifest_check(entry, f"manifest check [{i}]"))
            except CheckSpecError:
                continue
    warnings = control_warnings(parsed)
    for line in warnings:
        print(f"{'REFUSED' if strict else 'WARNING'}: {line}", file=sys.stderr)
    if warnings and strict:
        _emit_error(
            "uncontrolled_checks",
            f"{len(warnings)} blocking manifest check(s) lack a captured grader "
            "control; nothing written (--strict-controls).", fmt)
        return False
    return True


# `dispatch new` from a shell with no session id. The dispatch is still
# recorded — the ledger records what happened — but the operator is told what
# that record can and cannot do.
_SESSION_LESS_WARNING = (
    "WARNING: dispatch recorded session-less (no --session-id and no "
    f"{SESSION_ID_ENV} in the environment). A hook stop from a live session "
    "can never adopt it: adoption requires an exact session match. Pass "
    f"--session-id <id> or export {SESSION_ID_ENV} before `dispatch new`.")


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

    if manifest is not None and not _controls_ok(
            manifest, strict=getattr(args, "strict_controls", False), fmt=args.format):
        return 1

    preflight = None
    if getattr(args, "preflight", False):
        if manifest is None:
            _emit_error("bad_preflight", "--preflight needs --manifest: there are no "
                        "manifest checks to run.", args.format)
            return 2
        try:
            checks = _manifest_checks_strict(manifest)
        except CheckSpecError as e:
            _emit_error("bad_manifest_check", str(e), args.format)
            return 2
        preflight = _preflight_manifest(
            checks, tier=args.tier, agent_type=getattr(args, "agent_name", None))

    # --agent-name makes the dispatch joinable: the agent block records the
    # spawn name the harness will report as agent_type, with a null agent_id
    # the first matching SubagentStop adopts (find_dispatch_for_stop). The
    # session id rides in from --session-id or FLEETPROOF_SESSION_ID, so a
    # dispatch recorded inside a hook session lands in that session.
    agent = None
    if getattr(args, "agent_name", None):
        agent = {"agent_type": args.agent_name, "agent_id": None,
                 "capture": CAPTURE_CLI}

    session_id = (getattr(args, "session_id", None) or "").strip() or None
    if session_id is None:
        session_id = os.environ.get(SESSION_ID_ENV) or None
    if session_id is None:
        # Said before the record is written, on stderr, so a json consumer
        # still gets clean stdout. A bare CLI shell has no session id, the
        # record stamps session_id null, and adoption needs an exact session
        # match — so the joinable dispatch the operator thought they made is
        # a second, unjoinable ledger row, silently (observed in a field
        # deployment on Windows: the first CLI dispatch of a lane had to be
        # closed and recreated with the id exported).
        print(_SESSION_LESS_WARNING, file=sys.stderr)

    try:
        run_id = create_dispatch(prompt, tier=args.tier, manifest=manifest,
                                 agent=agent, session_id=session_id)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1

    record = load_dispatch(run_id)
    if args.format == "json":
        payload = record.to_dict() if record else {"run_id": run_id}
        if preflight is not None:
            payload["preflight"] = preflight
        print(json.dumps(payload, indent=2))
    else:
        print(run_id)
        if preflight is not None:
            print(_render_preflight(preflight))
    return 0


def _cmd_dispatch_intent(args: argparse.Namespace) -> int:
    """Write the dispatch-intent sidecar the next SubagentStart capture consumes.

    Exists so dispatchers never hand-write the JSON: the prompt comes from a
    file (same rationale as ``dispatch new --prompt-file`` — shell-quoting a
    real prompt on Windows is how prompts get mangled), the manifest is
    validated here where an error still has someone to land on, and the printed
    path is the audit surface: the file that will vanish when the spawn
    consumes it.

    ``--preflight`` additionally parses every manifest check with the gate's
    parser (a malformed one is an error here, not a skipped line in a hook's
    stderr) and, after the sidecar is written, runs each check now and prints
    what the gate will see — see :func:`_preflight_manifest`. Nothing is
    recorded; the exit code is 0 whatever the checks did, because a preview
    of graders that mostly cannot pass yet is the point, not a failure.
    """
    try:
        # utf-8-sig for the same reason as _resolve_prompt: strip a
        # PowerShell-redirection BOM before it lands in the sidecar.
        prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig")
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

    checks: list[Check] = []
    if args.preflight:
        # Parsed before the sidecar is written: a malformed check is refused
        # here, where the author is still in the seat to fix it.
        try:
            checks = _manifest_checks_strict(manifest)
        except CheckSpecError as e:
            _emit_error("bad_manifest_check", str(e), args.format)
            return 2

    if not _controls_ok(manifest, strict=args.strict_controls, fmt=args.format):
        return 1

    try:
        path = write_intent(args.agent, prompt, manifest=manifest, tier=args.tier,
                            role=args.role)
    except LedgerError as e:
        _emit_error("ledger_error", str(e), args.format)
        return 1

    preflight = None
    if args.preflight:
        preflight = _preflight_manifest(checks, tier=args.tier, agent_type=args.agent)
    if args.format == "json":
        payload = {"ok": True, "agent_type": args.agent, "intent_path": str(path)}
        if preflight is not None:
            payload["preflight"] = preflight
        print(json.dumps(payload, indent=2 if preflight is not None else None))
    else:
        print(str(path))
        if preflight is not None:
            print(_render_preflight(preflight))
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


def _armable_tier(value: str) -> str:
    """argparse type for ``--tier`` on arm/disarm: bridge or coordinator only.

    A lane is refused with the reason, not a bare choices list: an operator
    typing ``--tier lane`` is asking a real question, and "invalid choice" is
    not the answer.
    """
    if value in ARMABLE_TIERS:
        return value
    if value in VALID_TIERS:
        raise argparse.ArgumentTypeError(
            f"tier {value!r} cannot be armed or disarmed: {LANE_NEVER_DISARMED}")
    raise argparse.ArgumentTypeError(
        f"unknown tier {value!r}; armable tiers: {', '.join(ARMABLE_TIERS)}")


def _cmd_arm(args: argparse.Namespace) -> int:
    tier = args.tier
    path = set_arming(ARMED, note=(args.note or "").strip(), tier=tier)
    if args.format == "json":
        print(json.dumps({"ok": True, "tier": tier, tier: ARMED, "path": str(path)}))
    else:
        print(f"{tier} gate: armed. Blocking check failures block the stop again.")
    return 0


def _cmd_disarm(args: argparse.Namespace) -> int:
    # Asymmetric with arm on purpose: switching the gate OFF requires a reason,
    # and a whitespace note is no reason.
    tier = args.tier
    note = (args.note or "").strip()
    if not note:
        _emit_error(
            "bad_note",
            "disarm needs a non-empty --note; switching the gate off requires "
            "a reason.", args.format)
        return 2
    path = set_arming(ADVISORY, note=note, tier=tier)
    if args.format == "json":
        print(json.dumps({"ok": True, "tier": tier, tier: ADVISORY, "note": note,
                          "path": str(path)}))
    else:
        print(f"{tier} gate: ADVISORY (disarmed): {note}")
        whose = ("the bridge's stop" if tier == DEFAULT_ARMING_TIER
                 else f"a {tier}-tier dispatch's stop")
        print(f"Checks still run and render; check failures no longer block "
              f"{whose}. The ledger sweep still blocks on stalled dispatches, "
              "lanes are always graded, and the abandonment ladder is "
              f"unaffected. Re-arm: fleetproof arm --tier {tier}")
    return 0


def _arming_board_lines(arming: dict) -> list[str]:
    """The fleet board's echo of every disarmed gate — one line per tier,
    each with its own note. The armed default stays quiet."""
    lines = []
    for tier in ARMABLE_TIERS:
        state, note = tier_arming(arming, tier)
        if state != ADVISORY:
            continue
        meta = tier_set_meta(arming, tier)
        set_at = str(meta.get("set_at") or "?")[:19]
        by = str(meta.get("by") or "?")
        lines.append(f"{tier} gate: ADVISORY (disarmed): {note or 'no note recorded'} "
                     f"[set {set_at} by {by}; re-arm: fleetproof arm --tier {tier}]")
    return lines


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
    # "This session" is the --session filter, else the hook session the CLI
    # is running inside (FLEETPROOF_SESSION_ID); with neither, the count runs
    # over everything on the board and the wording says so. Counted off the
    # ledger directly, not off `records`, so --open cannot hide it.
    scope_session = args.session or os.environ.get(SESSION_ID_ENV) or None
    ungraded = list_ungraded_terminations(scope_session)
    ungraded_line = ungraded_termination_line(ungraded, scope_known=bool(scope_session))
    # The board echoes every disarmed gate (bridge, coordinator) whenever it
    # is advisory — the armed default stays quiet. Silence here is what makes
    # the echo a signal.
    arming = load_arming()
    arming_lines = _arming_board_lines(arming)
    advisory = bool(arming_lines)
    if args.format == "json":
        payload = {
            "dispatches": [
                dict(r.to_dict(), age=_format_age(r.started_at)) for r in records
            ],
            "orphan_stop_count": len(orphans),
            "terminated_ungraded_count": len(ungraded),
            "terminated_ungraded": [r.run_id for r in ungraded],
        }
        if advisory:
            payload["arming"] = arming
        print(json.dumps(payload, indent=2))
        return 0
    for line in arming_lines:
        print(line)
    if not records:
        print("No dispatches found.")
        if orphans:
            print(_orphan_count_line(orphans))
        if ungraded_line:
            print(ungraded_line)
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
          "matched).  tier~ = inherited from an earlier dispatch of the same "
          "agent type this session (no sidecar matched; see inherited_from).  "
          "(stalled) = reported or graded but never closed.")
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
    if ungraded_line:
        print(ungraded_line)
    return 0


def _parse_date(value: str | None, label: str) -> "date | None":
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"--{label} must be YYYY-MM-DD: {e}") from e


# === Telemetry off-state ===
#
# A summary over a repo with no telemetry_era rendered "54 dispatch(es), 0
# classifiable ... severity: no failures" on a run that produced a real
# abandoned and two real contradictions — correct (nothing was ever era-
# stamped, and pre-telemetry is never back-filled) but an off-state that
# reads exactly like an on-state with nothing to report (observed in a field
# deployment on Windows). The first line now says which it is.

_TELEMETRY_OFF_LINE = (
    "telemetry is OFF for this repo: no telemetry_era in .fleetproof/config.json "
    "— set it (YYYY-MM-DD) to begin classifying dispatches created from that "
    "date; pre-telemetry dispatches are never back-filled.")
_TELEMETRY_ALL_PRE_LINE = (
    "  all {n} dispatch(es) in this window predate telemetry_era {era} — "
    "pre-telemetry, never back-filled; nothing here is classifiable yet.")
_SEVERITY_NONE_CLASSIFIABLE = "n/a (0 classifiable)"


def _cmd_telemetry_summary(args: argparse.Namespace) -> int:
    from .telemetry_export import BUCKET_PRE_TELEMETRY, summarize
    summary = summarize()
    era = summary.get("telemetry_era")
    if args.format == "json":
        print(json.dumps(summary, indent=2))
        return 0
    if era is None:
        # Said FIRST, before any number: every rate below is n/a because the
        # layer is off, not because the fleet was clean.
        print(_TELEMETRY_OFF_LINE)
    print("Telemetry summary (LOCAL-ONLY: clear-text names and exact times;")
    print("anything shareable goes through `fleetproof telemetry export`).")
    for name, block in summary["windows"].items():
        counts = block["counts"]
        shown = {k: v for k, v in counts.items() if v}
        total = block["total_dispatches"]
        print(f"\n[{name}] {total} dispatch(es), "
              f"{block['classifiable_dispatches']} classifiable")
        if era is not None and total and counts.get(BUCKET_PRE_TELEMETRY) == total:
            print(_TELEMETRY_ALL_PRE_LINE.format(n=total, era=era))
        print(f"  outcomes: {shown if shown else 'none'}")
        for metric in ("delivery_failure_rate", "false_claim_rate",
                       "abandoned_rate",
                       "near_miss_rate", "verifier_flake_rate",
                       "ungraded_rate", "unverifiable_rate", "advisory_rate",
                       "telemetry_missing_rate", "stop_only_fraction"):
            m = block[metric]
            rate = f"{m['rate']:.3f}" if m["rate"] is not None else "n/a"
            print(f"  {metric}: {m['numerator']}/{m['denominator']} = {rate}")
        sev = block["severity_distribution"]
        sev_shown = {k: v for k, v in sev.items() if v}
        if block["classifiable_dispatches"] == 0:
            # "no failures" over zero classifiable dispatches is a claim about
            # a fleet that was never measured. Never print it.
            print(f"  severity: {_SEVERITY_NONE_CLASSIFIABLE}")
        else:
            print(f"  severity: {sev_shown or 'no failures'}")
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
    # `fleetproof check` stays the checker; `fleetproof check control ...` is
    # the one verb beneath it. The nested subparser is optional so every
    # existing `check --spec/--tier/...` invocation parses exactly as before.
    csub = p_check.add_subparsers(dest="check_command")
    p_ctl = csub.add_parser(
        "control",
        help="Record a grader control for one check: the pass (and fail) samples "
             "it was exercised against, their hashes, the provenance of the pass "
             "sample, and the observed exit per direction. Written to "
             ".fleetproof/controls/<check-id>.json, outside the hashed tree.")
    p_ctl.add_argument("check_id")
    p_ctl.add_argument("--pass-sample", required=True,
                       help="A file the check must grade as PASS.")
    p_ctl.add_argument("--fail-sample", default=None,
                       help="A file the check must grade as FAIL.")
    p_ctl.add_argument("--provenance", required=True, choices=list(VALID_PROVENANCE),
                       help="Where the pass sample came from: captured (a real "
                            "emission of the target) or authored (written by a "
                            "person). Authored controls are warned about at "
                            "dispatch intent.")
    p_ctl.add_argument("--note", default=None,
                       help="Where and when the sample was captured, for the record.")
    p_ctl.add_argument("--manifest", default=None,
                       help="Manifest file whose check with this id should be run "
                            "against each sample (the sample path is passed as "
                            f"${CONTROL_SAMPLE_ENV}). Falls back to the repo spec.")
    p_ctl.add_argument("--spec", default=None, help="Spec to resolve the check from.")
    _add_format(p_ctl)
    p_ctl.set_defaults(func=_cmd_check_control)

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

    tier_help = ("Which gate: bridge (the Stop gate; default) or coordinator "
                 "(coordinator-tier dispatches in the subagent gate). Lanes "
                 "are always graded and cannot be named here.")
    p_arm = sub.add_parser(
        "arm",
        help="Arm a gate (the default state): blocking check failures block "
             "the stop again. --tier bridge (default) or coordinator.")
    p_arm.add_argument("--note", default=None,
                       help="Optional note recorded with the arming state.")
    p_arm.add_argument("--tier", type=_armable_tier, default=DEFAULT_ARMING_TIER,
                       metavar="{bridge,coordinator}", help=tier_help)
    _add_format(p_arm)
    p_arm.set_defaults(func=_cmd_arm)

    p_disarm = sub.add_parser(
        "disarm",
        help="Set a gate to advisory: checks still run and render, but check "
             "failures no longer block that tier's stop. --tier bridge "
             "(default) or coordinator; lanes are always graded. The ledger "
             "sweep still blocks on stalled dispatches and the abandonment "
             "ladder is unaffected. Requires --note.")
    p_disarm.add_argument(
        "--note", required=True,
        help="Why the gate is coming down (required; recorded per tier in "
             ".fleetproof/arming.json and echoed by `fleetproof fleet`).")
    p_disarm.add_argument("--tier", type=_armable_tier, default=DEFAULT_ARMING_TIER,
                          metavar="{bridge,coordinator}", help=tier_help)
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
    p_dnew.add_argument("--session-id", default=None,
                        help="Stamp this session id on the dispatch (default: "
                             f"{SESSION_ID_ENV} from the environment). Without "
                             "one the dispatch is recorded session-less, which "
                             "no hook stop can ever adopt; a warning says so.")
    p_dnew.add_argument("--strict-controls", action="store_true", help="Refuse (exit 1, nothing written) instead of warning when a blocking "
                             "manifest check has no grader control or an authored-only pass "
                             "sample (see `fleetproof check control`).")
    p_dnew.add_argument("--preflight", action="store_true",
                        help="Run every manifest check now, from the project root, exactly as the "
                             "gate would, and print the resolved command, exit code, "
                             "expectation, PASS/FAIL, and an output tail per check. Records "
                             "nothing; exits 0 whatever the checks did (it is a preview), "
                             "non-zero on a malformed check." + " Needs --manifest.")
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
    p_dint.add_argument("--strict-controls", action="store_true", help="Refuse (exit 1, nothing written) instead of warning when a blocking "
                             "manifest check has no grader control or an authored-only pass "
                             "sample (see `fleetproof check control`).")
    p_dint.add_argument("--preflight", action="store_true",
                        help="Run every manifest check now, from the project root, exactly as the "
                             "gate would, and print the resolved command, exit code, "
                             "expectation, PASS/FAIL, and an output tail per check. Records "
                             "nothing; exits 0 whatever the checks did (it is a preview), "
                             "non-zero on a malformed check.")
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
