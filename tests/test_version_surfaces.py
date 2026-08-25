"""Every surface that carries the release version agrees.

Observed in the field on the 0.5.0 release day: ``pyproject.toml`` and
``__version__`` were bumped, PyPI and the GitHub release went out, and
``.claude-plugin/plugin.json`` stayed at 0.4.0 — so ``/plugin update`` was a
silent no-op for every deployment while pip reported 0.5.0. A version that
lives in three files is three chances to ship a skew; this test makes the
skew a red suite instead of a field report.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import fleetproof

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml has no version line"
    return m.group(1)


def _plugin_manifest_version() -> str:
    data = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8-sig"))
    return data["version"]


def test_pyproject_matches_package_version():
    assert _pyproject_version() == fleetproof.__version__


def test_plugin_manifest_matches_package_version():
    assert _plugin_manifest_version() == fleetproof.__version__, (
        ".claude-plugin/plugin.json is what /plugin update reads — a stale value "
        "there makes the plugin upgrade a silent no-op while pip reports the new version"
    )


def test_changelog_has_an_entry_for_the_package_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {fleetproof.__version__}" in text
