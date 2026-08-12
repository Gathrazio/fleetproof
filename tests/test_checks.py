"""Tests for the check-spec loader and its validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleetproof.checks import Check, CheckSpecError, STARTER_SPEC, load_checks


def _write(tmp_path: Path, spec: dict) -> Path:
    p = tmp_path / "checks.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


def test_starter_spec_loads(tmp_path):
    p = _write(tmp_path, STARTER_SPEC)
    checks = load_checks(p)
    assert [c.id for c in checks] == ["tests-pass", "build-succeeds", "changelog-updated"]
    assert checks[0].expect == {"kind": "exit0"}
    assert checks[0].block is True
    assert checks[2].expect == {"kind": "file_exists", "path": "CHANGELOG.md"}
    assert checks[2].block is False


def test_missing_spec_raises(tmp_path):
    with pytest.raises(CheckSpecError):
        load_checks(tmp_path / "absent.json")


def test_bad_json_raises(tmp_path):
    p = tmp_path / "checks.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_expect_variants(tmp_path):
    p = _write(tmp_path, {"checks": [
        {"id": "a", "run": "x", "expect": "exit0"},
        {"id": "b", "run": "x", "expect": {"exit": 2}},
        {"id": "c", "run": "x", "expect": {"regex": "ok"}},
        {"id": "d", "expect": {"file_exists": "out.txt"}},
    ]})
    checks = load_checks(p)
    kinds = {c.id: c.expect["kind"] for c in checks}
    assert kinds == {"a": "exit0", "b": "exit", "c": "regex", "d": "file_exists"}


def test_duplicate_id_raises(tmp_path):
    p = _write(tmp_path, {"checks": [
        {"id": "dup", "run": "x"}, {"id": "dup", "run": "y"},
    ]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_file_exists_without_run_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "expect": "exit0"}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_unknown_expect_kind_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "expect": {"bogus": 1}}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_missing_id_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"run": "x"}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


# === optional tier field (additive; a v0.1 spec has none) ===

def test_tier_absent_reads_back_as_none(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x"}]})
    assert load_checks(p)[0].tier is None


def test_tier_field_parsed(tmp_path):
    p = _write(tmp_path, {"checks": [
        {"id": "a", "run": "x", "tier": "leaf"},
        {"id": "b", "run": "x", "tier": "lane"},
        {"id": "c", "run": "x", "tier": "coordinator"},
        {"id": "d", "run": "x", "tier": "bridge"},
    ]})
    assert {c.id: c.tier for c in load_checks(p)} == {
        "a": "leaf", "b": "lane", "c": "coordinator", "d": "bridge",
    }


def test_unknown_tier_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "tier": "middle-management"}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_string_tier_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "tier": 3}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)
