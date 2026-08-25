"""The shipped check library: three graders with fixtures, installed by
``fleetproof init --library`` into ``.fleetproof/checks/lib/``.

Every script here is stdlib-only Python, invoked in argv form
(``["python", ".fleetproof/checks/lib/<name>.py", ...]``) so it runs with no
shell on Windows, and honours the grader-control convention: when
``FLEETPROOF_CONTROL_SAMPLE`` names a file, the script grades that captured
emission instead of the live target, so a real emission can be registered as
a positive control with ``fleetproof check control``. Each script's header
states what it asserts, its source of truth, and how to capture a fixture
for it. A field deployment hand-wrote all three at 08:20 in a hurry, and
that is how a grader ends up demanding a field its target never emitted
(observed in a field deployment on Windows).

The files are copied, not imported: once under ``.fleetproof/checks/`` they
are inside the tree pin, and the repo owns them from then on.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any

# Where init --library writes, relative to the checks tree directory.
LIBRARY_DIRNAME = "lib"
FIXTURES_DIRNAME = "fixtures"

# Script name -> the suggested checks.json entry, printed after install.
# ``run`` is argv form on purpose (no cmd.exe quoting); the paths are
# repo-relative because checks execute from the resolved project root.
SUGGESTED_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "worktree-landed",
        "run": ["python", ".fleetproof/checks/lib/worktree_landed.py", "--base", "main"],
        "expect": "exit0",
        "block": True,
        "tier": "bridge",
        "description": "Tree clean and HEAD >= 1 commit ahead of main (the work landed).",
    },
    {
        "id": "py-tests-pinned",
        "run": ["python", ".fleetproof/checks/lib/py_tests_pinned.py", "-q"],
        "expect": "exit0",
        "block": True,
        "description": "python -m pytest with PYTHONPATH pinned to the graded tree root.",
    },
    {
        "id": "http-json-field",
        "run": ["python", ".fleetproof/checks/lib/http_json_field.py",
                "--url", "http://localhost:8000/health",
                "--field", "sati_cert.days_remaining", "--type", "number", "--min", "0"],
        "expect": "exit0",
        "block": True,
        "description": "A JSON field exists at the endpoint with the declared type and range. "
                       "Quote the field name from the emitter's source, never from memory.",
    },
]

SCRIPT_NAMES = ("worktree_landed.py", "py_tests_pinned.py", "http_json_field.py")
FIXTURE_NAMES = ("http_json_field.sample.json", "README.md")


def _package_files():
    return importlib.resources.files(__name__)


def library_files() -> dict[str, bytes]:
    """``{relative path under lib/: bytes}`` for everything the library ships."""
    root = _package_files()
    out: dict[str, bytes] = {}
    for name in SCRIPT_NAMES:
        out[name] = root.joinpath(name).read_bytes()
    for name in FIXTURE_NAMES:
        out[f"{FIXTURES_DIRNAME}/{name}"] = root.joinpath(FIXTURES_DIRNAME, name).read_bytes()
    return out


def install_library(checks_tree: Path, force: bool = False) -> tuple[list[Path], list[Path]]:
    """Write the library under ``checks_tree / lib``. Returns ``(written, skipped)``.

    An existing file is skipped unless ``force`` — the repo owns these files
    once they are inside the tree pin, and an install must not silently
    overwrite a grader someone edited.
    """
    lib_dir = Path(checks_tree) / LIBRARY_DIRNAME
    written: list[Path] = []
    skipped: list[Path] = []
    for rel, data in library_files().items():
        target = lib_dir / rel
        if target.exists() and not force:
            skipped.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(target)
    return written, skipped
