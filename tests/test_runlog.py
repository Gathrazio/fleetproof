"""Tests for the extracted run-log spine, including the project-agnostic resolver."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.runlog import (
    RUN_ID_ENV,
    _generate_run_id,
    child_run_id,
    current_run_id,
    filter_env,
    list_run_records,
    load_run,
    project_root,
    record,
    recorded,
    runs_dir,
    set_runs_dir,
)


# === run-id generation ===

def test_generate_run_id_format():
    parts = _generate_run_id().split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 8 and len(parts[1]) == 6 and len(parts[2]) == 6


def test_run_ids_unique():
    assert len({_generate_run_id() for _ in range(50)}) == 50


def test_current_run_id_uses_env(monkeypatch):
    monkeypatch.setenv(RUN_ID_ENV, "fixed-xyz")
    assert current_run_id() == "fixed-xyz"


def test_child_run_id_extends_parent(tmp_runs, monkeypatch):
    monkeypatch.setenv(RUN_ID_ENV, "parent-id")
    assert child_run_id() == "parent-id.1"
    assert child_run_id() == "parent-id.2"


# === project-agnostic resolver (the one real rewrite) ===

def test_project_root_finds_git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert project_root(nested) == tmp_path.resolve()


def test_project_root_finds_fleetproof_marker(tmp_path):
    (tmp_path / ".fleetproof").mkdir()
    nested = tmp_path / "deep"
    nested.mkdir()
    assert project_root(nested) == tmp_path.resolve()


def test_project_root_falls_back_to_start_when_no_marker(tmp_path):
    # A directory with neither .git nor .fleetproof anywhere up the chain that we own.
    # (tmp_path itself has no marker; parents won't either within the tmp tree.)
    lonely = tmp_path / "orphan"
    lonely.mkdir()
    assert project_root(lonely) == lonely.resolve()


def test_runs_dir_uses_marker_root(tmp_path, monkeypatch):
    set_runs_dir(None)
    monkeypatch.delenv(runlog.RUNS_DIR_ENV, raising=False)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    assert runs_dir() == tmp_path.resolve() / ".fleetproof" / "runs"


def test_runs_dir_env_override(tmp_path, monkeypatch):
    set_runs_dir(None)
    monkeypatch.setenv(runlog.RUNS_DIR_ENV, str(tmp_path))
    try:
        assert runs_dir() == tmp_path
    finally:
        monkeypatch.delenv(runlog.RUNS_DIR_ENV, raising=False)


def test_set_runs_dir_takes_precedence(tmp_runs):
    assert runs_dir() == tmp_runs


# === env filtering ===

def test_filter_env_redacts_secrets():
    out = filter_env({
        "MY_API_TOKEN": "s", "AUTH_HEADER": "b", "PASSWORD": "p",
        "SECRET_KEY": "k", "USERNAME": "someuser",
    })
    assert out["MY_API_TOKEN"] == "<redacted>"
    assert out["AUTH_HEADER"] == "<redacted>"
    assert out["PASSWORD"] == "<redacted>"
    assert out["SECRET_KEY"] == "<redacted>"
    assert out["USERNAME"] == "someuser"  # in SAFE_ENV_KEYS


# === record / read-back (written by one path, read by another) ===

def test_record_creates_files_and_reads_back(tmp_runs):
    with record("my-tool", "do", {"arg": "value"}) as h:
        h.set_output({"result": "ok"})

    run_dirs = [p for p in tmp_runs.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    sub = next(p for p in run_dirs[0].iterdir() if p.is_dir())
    assert (sub / "invocation.json").exists()
    assert (sub / "result.json").exists()
    assert (sub / "output.json").exists()

    inv = json.loads((sub / "invocation.json").read_text(encoding="utf-8"))
    assert inv["tool"] == "my-tool" and inv["args"] == {"arg": "value"}

    # Read back via the public reader — the "different process" path in-process.
    runs = list_run_records()
    assert len(runs) == 1
    assert runs[0].sub_invocations[0].subcmd == "do"
    assert runs[0].sub_invocations[0].load_output() == {"result": "ok"}


def test_record_captures_exception_and_marks_failed(tmp_runs):
    with pytest.raises(RuntimeError):
        with record("my-tool", "fail"):
            raise RuntimeError("kaboom")
    run = list_run_records()[0]
    assert run.failed_count == 1
    assert run.sub_invocations[0].exit_code == 1
    assert run.sub_invocations[0].exception_type == "RuntimeError"


def test_recorded_decorator_records(tmp_runs):
    @recorded
    def add(a, b):
        return {"sum": a + b}

    assert add(2, 3) == {"sum": 5}
    run = list_run_records()[0]
    assert run.sub_invocations[0].subcmd == "add"
    assert run.sub_invocations[0].exit_code == 0


def test_recorded_respects_no_record_flag(tmp_runs, monkeypatch):
    monkeypatch.setenv(runlog.NO_RECORD_ENV, "1")

    @recorded
    def f():
        return "x"

    f()
    assert list_run_records() == []


def test_list_newest_first(tmp_runs, monkeypatch):
    monkeypatch.setenv(RUN_ID_ENV, "20260101-100000-aaaaaa")
    with record("t1", "a"):
        pass
    monkeypatch.setenv(RUN_ID_ENV, "20260201-100000-bbbbbb")
    with record("t2", "b"):
        pass
    assert [r.run_id for r in list_run_records()] == [
        "20260201-100000-bbbbbb", "20260101-100000-aaaaaa",
    ]


def test_load_run_missing_returns_none(tmp_runs):
    assert load_run("nope") is None


def test_root_record_captures_session_id_from_env(tmp_runs, monkeypatch):
    # The session id is carried in via env (set by the hook from stdin) and must
    # land on the root record so the report can group by it.
    monkeypatch.setenv(runlog.SESSION_ID_ENV, "sess-xyz")
    with record("t", "a"):
        pass
    run = list_run_records()[0]
    assert run.session_id == "sess-xyz"


def test_root_record_session_id_none_without_env(tmp_runs, monkeypatch):
    monkeypatch.delenv(runlog.SESSION_ID_ENV, raising=False)
    with record("t", "a"):
        pass
    run = list_run_records()[0]
    assert run.session_id is None


def test_env_constants_use_fleetproof_namespace():
    # Guardrail: every env var this package reads is namespaced.
    for name in (RUN_ID_ENV, runlog.RUNS_DIR_ENV, runlog.PARENT_RUN_ID_ENV,
                 runlog.NO_RECORD_ENV, runlog.SESSION_ID_ENV):
        assert name.startswith("FLEETPROOF_")
