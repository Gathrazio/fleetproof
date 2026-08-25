"""Tests for the shipped check library (fleetproof init --library).

The scripts are exercised the way the gate runs them: as argv subprocesses,
from a project root, with FLEETPROOF_CONTROL_SAMPLE where a sample stands in
for the live target.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fleetproof import runlog
from fleetproof.cli import main
from fleetproof.library import SCRIPT_NAMES, SUGGESTED_ENTRIES, library_files


@pytest.fixture(autouse=True)
def _reset_runs():
    yield
    runlog.set_runs_dir(None)


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".fleetproof").mkdir()
    monkeypatch.chdir(tmp_path)
    runlog.set_runs_dir(tmp_path / ".fleetproof" / "runs")
    return tmp_path


def _run(script: Path, *args: str, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.pop("FLEETPROOF_CONTROL_SAMPLE", None)
    full.update(env or {})
    return subprocess.run([sys.executable, str(script), *args], cwd=str(cwd), env=full,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


# === install ===

def test_init_library_writes_scripts_and_fixtures(project, capsys):
    assert main(["init", "--library"]) == 0
    out = capsys.readouterr().out
    lib = project / ".fleetproof" / "checks" / "lib"
    for name in SCRIPT_NAMES:
        assert (lib / name).exists()
        header = (lib / name).read_text(encoding="utf-8")
        for word in ("ASSERTS", "SOURCE OF TRUTH", "CONTROL SAMPLE"):
            assert word in header, (name, word)
    assert (lib / "fixtures" / "http_json_field.sample.json").exists()
    readme = (lib / "fixtures" / "README.md").read_text(encoding="utf-8")
    assert "replace it with your own captured emission" in readme
    assert "fleetproof check control" in readme
    # The three suggested entries are printed, one JSON object per line,
    # each a valid argv-form check that names the installed script.
    printed = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [e["id"] for e in printed] == ["worktree-landed", "py-tests-pinned", "http-json-field"]
    for entry in printed:
        assert entry["run"][0] == "python"
        assert entry["run"][1].startswith(".fleetproof/checks/lib/")
        assert (project / entry["run"][1]).exists()
    assert printed == SUGGESTED_ENTRIES


def test_init_library_keeps_existing_files_unless_forced(project, capsys):
    assert main(["init", "--library"]) == 0
    script = project / ".fleetproof" / "checks" / "lib" / "http_json_field.py"
    script.write_text("# edited by the repo\n", encoding="utf-8")
    assert main(["init", "--library"]) == 0
    assert "5 left as-is (use --force to overwrite)" in capsys.readouterr().out
    assert script.read_text(encoding="utf-8") == "# edited by the repo\n"
    assert main(["init", "--library", "--force", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert str(script) in payload["written"]
    assert "ASSERTS" in script.read_text(encoding="utf-8")


def test_init_library_lands_inside_the_tree_pin(project, capsys):
    from fleetproof.checks import checks_tree_hash
    spec = project / ".fleetproof" / "checks.json"
    spec.write_text(json.dumps({"checks": []}), encoding="utf-8")
    before = checks_tree_hash()
    assert main(["init", "--library"]) == 0
    assert checks_tree_hash() != before  # the graders are part of the spec


def test_init_without_library_still_writes_the_spec(project, capsys):
    assert main(["init"]) == 0
    assert (project / ".fleetproof" / "checks.json").exists()
    assert not (project / ".fleetproof" / "checks" / "lib").exists()


def test_suggested_entries_parse_as_spec_checks(tmp_path):
    from fleetproof.checks import load_checks
    p = tmp_path / "checks.json"
    p.write_text(json.dumps({"checks": SUGGESTED_ENTRIES}), encoding="utf-8")
    assert [c.id for c in load_checks(p)] == [e["id"] for e in SUGGESTED_ENTRIES]


# === http_json_field ===

def _installed(project) -> Path:
    lib = project / ".fleetproof" / "checks" / "lib"
    if not lib.exists():
        main(["init", "--library"])
    return lib


def test_http_json_field_grades_a_sample_file(project, capsys):
    lib = _installed(project)
    script = lib / "http_json_field.py"
    sample = lib / "fixtures" / "http_json_field.sample.json"
    env = {"FLEETPROOF_CONTROL_SAMPLE": str(sample)}
    base = ["--url", "http://unused.invalid/health"]
    present = _run(script, *base, "--field", "sati_cert.days_remaining", "--type", "number",
                   "--min", "0", cwd=project, env=env)
    assert present.returncode == 0, present.stdout
    assert present.stdout.startswith("PASS: sati_cert.days_remaining=67.1")
    missing = _run(script, *base, "--field", "sati_cert.days_until_expiry", cwd=project, env=env)
    assert missing.returncode == 2
    assert "FAIL: sati_cert.days_until_expiry missing" in missing.stdout
    wrong_type = _run(script, *base, "--field", "sati_cert.checked", "--type", "number",
                      cwd=project, env=env)
    assert wrong_type.returncode == 3
    out_of_range = _run(script, *base, "--field", "sati_cert.days_remaining", "--max", "30",
                        cwd=project, env=env)
    assert out_of_range.returncode == 4
    as_bool = _run(script, *base, "--field", "sati_cert.checked", "--type", "bool",
                   cwd=project, env=env)
    assert as_bool.returncode == 0


def test_http_json_field_unreachable_url_is_a_fetch_failure(project):
    script = _installed(project) / "http_json_field.py"
    proc = _run(script, "--url", "http://127.0.0.1:9/nothing", "--field", "x",
                "--timeout", "1", cwd=project)
    assert proc.returncode == 5
    assert "FAIL: could not load JSON" in proc.stdout


# === worktree_landed ===

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x"))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_worktree_landed_against_a_real_repo(project, tmp_path):
    script = _installed(project) / "worktree_landed.py"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")

    zero_ahead = _run(script, "--base", "main", "--worktree", str(repo), cwd=project)
    assert zero_ahead.returncode == 3
    assert "0 commits ahead of main" in zero_ahead.stdout

    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "work")
    one_ahead = _run(script, "--base", "main", "--worktree", str(repo), cwd=project)
    assert one_ahead.returncode == 0, one_ahead.stdout
    assert "PASS: tree clean, HEAD is 1 commit(s) ahead of main" in one_ahead.stdout

    (repo / "c.txt").write_text("dirty\n", encoding="utf-8")
    dirty = _run(script, "--base", "main", "--worktree", str(repo), cwd=project)
    assert dirty.returncode == 2
    assert "FAIL: working tree is dirty" in dirty.stdout and "c.txt" in dirty.stdout

    not_a_repo = _run(script, "--base", "main", "--worktree", str(tmp_path / "nope"), cwd=project)
    assert not_a_repo.returncode == 4


def test_worktree_landed_honours_a_control_sample(project, tmp_path):
    script = _installed(project) / "worktree_landed.py"
    sample = tmp_path / "landed.json"
    sample.write_text(json.dumps({"status_porcelain": "", "ahead_count": 2}), encoding="utf-8")
    proc = _run(script, "--base", "main", cwd=project,
                env={"FLEETPROOF_CONTROL_SAMPLE": str(sample)})
    assert proc.returncode == 0
    sample.write_text(json.dumps({"status_porcelain": " M a.txt\n", "ahead_count": 2}),
                      encoding="utf-8")
    proc = _run(script, "--base", "main", cwd=project,
                env={"FLEETPROOF_CONTROL_SAMPLE": str(sample)})
    assert proc.returncode == 2


# === py_tests_pinned ===

def test_py_tests_pinned_runs_pytest_with_the_root_first_on_pythonpath(project, tmp_path):
    script = _installed(project) / "py_tests_pinned.py"
    root = tmp_path / "graded"
    (root / "tests").mkdir(parents=True)
    (root / "mymod.py").write_text("VALUE = 'graded-tree'\n", encoding="utf-8")
    (root / "tests" / "test_it.py").write_text(
        "import mymod\n\ndef test_value():\n    assert mymod.VALUE == 'graded-tree'\n",
        encoding="utf-8")
    ok = _run(script, "--root", str(root), "-q", "-p", "no:cacheprovider", cwd=project)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert f"PASS: pytest exit 0 (PYTHONPATH pinned to {root})" in ok.stdout

    (root / "tests" / "test_it.py").write_text(
        "def test_value():\n    assert False\n", encoding="utf-8")
    bad = _run(script, "--root", str(root), "-q", "-p", "no:cacheprovider", cwd=project)
    assert bad.returncode == 1
    assert "FAIL: pytest exit 1" in bad.stdout

    (root / "tests" / "test_it.py").unlink()
    none = _run(script, "--root", str(root), "-q", "-p", "no:cacheprovider", cwd=project)
    assert none.returncode == 5  # collected nothing: verified nothing

    missing_root = _run(script, "--root", str(tmp_path / "absent"), cwd=project)
    assert missing_root.returncode == 98


def test_py_tests_pinned_honours_a_control_sample(project, tmp_path):
    script = _installed(project) / "py_tests_pinned.py"
    sample = tmp_path / "run.json"
    sample.write_text(json.dumps({"pytest_exit": 0}), encoding="utf-8")
    proc = _run(script, cwd=project, env={"FLEETPROOF_CONTROL_SAMPLE": str(sample)})
    assert proc.returncode == 0 and "control sample pytest_exit=0" in proc.stdout


def test_library_files_are_the_packaged_bytes():
    files = library_files()
    assert set(files) == set(SCRIPT_NAMES) | {"fixtures/http_json_field.sample.json",
                                              "fixtures/README.md"}
    for name in SCRIPT_NAMES:
        assert b"FLEETPROOF_CONTROL_SAMPLE" in files[name]
        assert b"import subprocess" in files[name] or b"urllib" in files[name]
        assert b"shell=True" not in files[name]
