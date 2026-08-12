"""Regressions for the 2026-07-31 adversarial pass (findings C1-C4, H1, H3, H5)
and for the stop-hook continuation loop observed live on 2026-08-11.

Each test names the finding it pins down. The adversarial findings doc lives in
the GCC's domains/recon/orchestration-reliability/ knowledge base.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.checker import run_check, run_checks, select_checks
from fleetproof.checks import (
    STARTER_SPEC,
    Check,
    CheckSpecError,
    load_checks,
    short_spec_hash,
)
from fleetproof.hookgate import stop_gate, stop_gate_main

OK_CMD = f'"{sys.executable}" -c "raise SystemExit(0)"'
FAIL_CMD = f'"{sys.executable}" -c "raise SystemExit(1)"'


@pytest.fixture(autouse=True)
def _reset_runs():
    yield
    runlog.set_runs_dir(None)
    os.environ.pop(runlog.SESSION_ID_ENV, None)


def _setup_project(tmp_path: Path, checks: list[dict], monkeypatch) -> Path:
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    spec = marker / "checks.json"
    spec.write_text(json.dumps({"checks": checks}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    return spec


# --- C1: the default (untiered) spec must grade subagents -------------------

def test_starter_spec_selects_checks_below_bridge_tier(tmp_path):
    # The defect that shipped: `fleetproof init` wrote untiered checks, captured
    # subagents graded at lane, and lane selected nothing — the default subagent
    # gate was a no-op. The gate must fire on the spec init actually writes.
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps(STARTER_SPEC), encoding="utf-8")
    checks = load_checks(spec)
    assert select_checks(checks, "lane"), "starter spec selects nothing at lane (C1)"
    assert select_checks(checks, "leaf"), "starter spec selects nothing at leaf (C1)"


# --- C2: file_exists must not pass on an unrunnable command or junk target --

def test_file_exists_fails_when_command_fails(tmp_path):
    (tmp_path / "stub.js").write_text("// TODO: implement", encoding="utf-8")
    check = Check(id="c2", run="definitely-not-a-real-command-xyz --build",
                  expect={"kind": "file_exists", "path": "stub.js"}, block=True)
    result = run_check(check, tmp_path)
    assert not result.passed
    assert "not consulting" in result.detail


def test_file_exists_rejects_directory_empty_file_and_escapes(tmp_path):
    (tmp_path / "adir").mkdir()
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("content", encoding="utf-8")
    try:
        cases = {
            "adir": "directory",
            "empty.txt": "empty",
            str(outside): "absolute",
            f"../{outside.name}": "escapes",
        }
        for raw_path, why in cases.items():
            check = Check(id=f"c2-{why}", run=None,
                          expect={"kind": "file_exists", "path": raw_path}, block=True)
            assert not run_check(check, tmp_path).passed, f"{why} case passed (C2)"
    finally:
        outside.unlink()


def test_file_exists_still_passes_on_a_real_file(tmp_path):
    (tmp_path / "artifact.txt").write_text("built", encoding="utf-8")
    check = Check(id="c2-ok", run=OK_CMD,
                  expect={"kind": "file_exists", "path": "artifact.txt"}, block=True)
    assert run_check(check, tmp_path).passed


# --- H1: regex must not match the output of a failing command ---------------

def test_regex_requires_exit_zero(tmp_path):
    code = "import sys; sys.stderr.write('ALL_TESTS_PASSED is not a flag'); sys.exit(1)"
    check = Check(id="h1", run=f'"{sys.executable}" -c "{code}"',
                  expect={"kind": "regex", "pattern": "ALL_TESTS_PASSED"}, block=True)
    result = run_check(check, tmp_path)
    assert not result.passed
    assert "exit 1" in result.detail


def test_regex_still_matches_on_success(tmp_path):
    code = "print('ALL_TESTS_PASSED')"
    check = Check(id="h1-ok", run=f'"{sys.executable}" -c "{code}"',
                  expect={"kind": "regex", "pattern": "ALL_TESTS_PASSED"}, block=True)
    assert run_check(check, tmp_path).passed


# --- C3: BOM tolerance; fail closed once a spec was promised ----------------

def test_bom_prefixed_spec_still_loads(tmp_path):
    spec = tmp_path / "checks.json"
    body = json.dumps({"checks": [{"id": "x", "run": OK_CMD, "expect": "exit0"}]})
    spec.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert [c.id for c in load_checks(spec)] == ["x"]


def test_utf16_spec_raises_cleanly(tmp_path):
    spec = tmp_path / "checks.json"
    spec.write_bytes(json.dumps({"checks": []}).encode("utf-16"))
    with pytest.raises(CheckSpecError):
        load_checks(spec)


def test_stop_gate_blocks_when_promised_spec_vanishes(tmp_path, monkeypatch):
    spec = _setup_project(tmp_path, [
        {"id": "ok", "run": OK_CMD, "expect": "exit0", "block": True},
    ], monkeypatch)
    os.environ[runlog.SESSION_ID_ENV] = "sess-c3"
    decision, _ = stop_gate()  # records the session baseline verdict
    assert decision is None
    spec.unlink()
    decision, _ = stop_gate()
    assert decision is not None and decision["decision"] == "block"
    assert "missing or unreadable" in decision["reason"]


def test_stop_gate_blocks_when_promised_spec_is_emptied(tmp_path, monkeypatch):
    spec = _setup_project(tmp_path, [
        {"id": "ok", "run": OK_CMD, "expect": "exit0", "block": True},
    ], monkeypatch)
    os.environ[runlog.SESSION_ID_ENV] = "sess-c3b"
    decision, _ = stop_gate()
    assert decision is None
    spec.write_text(json.dumps({"checks": []}), encoding="utf-8")
    decision, _ = stop_gate()
    assert decision is not None and decision["decision"] == "block"
    assert "zero checks" in decision["reason"]


def test_stop_gate_still_fails_open_when_nothing_was_promised(tmp_path, monkeypatch):
    marker = tmp_path / ".fleetproof"
    marker.mkdir()
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(marker / "runs")
    os.environ[runlog.SESSION_ID_ENV] = "sess-unconfigured"
    decision, code = stop_gate()
    assert decision is None and code == 0


# --- C4: one word per check must not disable the bridge gate ----------------

def test_stop_gate_blocks_when_every_blocking_check_is_tiered_below_bridge(
        tmp_path, monkeypatch):
    _setup_project(tmp_path, [
        {"id": "retiered", "run": FAIL_CMD, "expect": "exit0", "block": True,
         "tier": "leaf"},
    ], monkeypatch)
    decision, _ = stop_gate()
    assert decision is not None and decision["decision"] == "block"
    assert "bridge" in decision["reason"]


def test_stop_gate_allows_advisory_only_lower_tier_spec(tmp_path, monkeypatch):
    # No blocking check anywhere: nothing was ever enforced, so nothing to defend.
    _setup_project(tmp_path, [
        {"id": "advisory", "run": FAIL_CMD, "expect": "exit0", "block": False,
         "tier": "leaf"},
    ], monkeypatch)
    decision, _ = stop_gate()
    assert decision is None


# --- H5: a multi-line run command must refuse to parse ----------------------

def test_multiline_run_command_rejected(tmp_path):
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "h5", "run": "echo one\necho two", "expect": "exit0"},
    ]}), encoding="utf-8")
    with pytest.raises(CheckSpecError):
        load_checks(spec)


# --- H3 (partial): a poisoned record must not crash the display path --------

def test_short_spec_hash_tolerates_non_string():
    assert short_spec_hash(123) == "unknown"          # type: ignore[arg-type]
    assert short_spec_hash({"a": 1}) == "unknown"     # type: ignore[arg-type]
    assert short_spec_hash(None) == "unknown"


# --- Stop-hook loop (2026-08-11, live): drift said once; retries stay quiet -

def _drift_the_spec(spec: Path) -> None:
    body = json.loads(spec.read_text(encoding="utf-8"))
    body["checks"][0]["description"] = "edited mid-session"
    spec.write_text(json.dumps(body), encoding="utf-8")


def test_drift_note_emitted_once_per_session(tmp_path, monkeypatch):
    spec = _setup_project(tmp_path, [
        {"id": "ok", "run": OK_CMD, "expect": "exit0", "block": True},
    ], monkeypatch)
    os.environ[runlog.SESSION_ID_ENV] = "sess-drift"
    assert stop_gate()[0] is None  # baseline
    _drift_the_spec(spec)

    first, _ = stop_gate()
    assert first is not None and "decision" not in first
    assert "spec drift" in first["hookSpecificOutput"]["additionalContext"]

    second, _ = stop_gate()
    assert second is None, "the same drift must not be re-announced every stop"


def test_stop_hook_active_suppresses_context_only_output(
        tmp_path, monkeypatch, capsys):
    spec = _setup_project(tmp_path, [
        {"id": "ok", "run": OK_CMD, "expect": "exit0", "block": True},
    ], monkeypatch)
    os.environ[runlog.SESSION_ID_ENV] = "sess-loop"
    assert stop_gate()[0] is None  # baseline
    _drift_the_spec(spec)

    payload = {"session_id": "sess-loop", "stop_hook_active": True}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert stop_gate_main() == 0
    assert capsys.readouterr().out == "", (
        "context-only output on a stop_hook_active continuation re-engages the "
        "agent and loops to the harness block cap"
    )


def test_stop_hook_active_never_suppresses_a_real_block(
        tmp_path, monkeypatch, capsys):
    _setup_project(tmp_path, [
        {"id": "must-pass", "run": FAIL_CMD, "expect": "exit0", "block": True},
    ], monkeypatch)
    payload = {"session_id": "sess-loop2", "stop_hook_active": True}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert stop_gate_main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block", (
        "a false done must not become passable by simply stopping twice"
    )
