"""Shared fixtures. Runs stdlib + pytest only; no third-party imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleetproof import runlog


@pytest.fixture
def tmp_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate the run log to a tmp directory and clear inherited run-id state."""
    monkeypatch.delenv(runlog.RUN_ID_ENV, raising=False)
    monkeypatch.delenv(runlog.NO_RECORD_ENV, raising=False)
    rd = tmp_path / "runs"
    runlog.set_runs_dir(rd)
    yield rd
    runlog.set_runs_dir(None)


@pytest.fixture
def tmp_project(tmp_path: Path):
    """A throwaway working directory to run checks in."""
    return tmp_path
