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


# === argv-form run (B5: no shell unless the spec asked for one) ===

def test_argv_run_parsed_and_recorded_verbatim(tmp_path):
    p = _write(tmp_path, {"checks": [
        {"id": "a", "run": ["python", "-m", "pytest", "-q"]},
    ]})
    check = load_checks(p)[0]
    assert check.run == ["python", "-m", "pytest", "-q"]
    assert check.describe_run() == "python -m pytest -q"


def test_string_run_describes_verbatim(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "pytest -q && echo done"}]})
    assert load_checks(p)[0].describe_run() == "pytest -q && echo done"


def test_empty_argv_run_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": []}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_string_argv_element_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": ["python", 3]}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_multiline_argv_element_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": ["python", "-c", "x\ny"]}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_string_non_list_run_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": 42}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


# === optional owner field (B4: who can actually satisfy this check) ===

def test_owner_absent_reads_back_as_none(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x"}]})
    assert load_checks(p)[0].owner is None


def test_owner_field_parsed(tmp_path):
    p = _write(tmp_path, {"checks": [
        {"id": "a", "run": "x", "owner": "leaf"},
        {"id": "b", "run": "x", "owner": "lane"},
        {"id": "c", "run": "x", "owner": "coordinator"},
        {"id": "d", "run": "x", "owner": "bridge"},
        {"id": "e", "run": "x", "owner": "operator"},
    ]})
    assert {c.id: c.owner for c in load_checks(p)} == {
        "a": "leaf", "b": "lane", "c": "coordinator", "d": "bridge",
        "e": "operator",
    }


def test_unknown_owner_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "owner": "the-vendor"}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_string_owner_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "owner": 3}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


# === per-check redact patterns (B6: redaction before persistence) ===

def test_redact_absent_reads_back_as_empty(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x"}]})
    assert load_checks(p)[0].redact == ()


def test_redact_patterns_parsed(tmp_path):
    p = _write(tmp_path, {"checks": [
        {"id": "a", "run": "x", "redact": [r"internal-id-\d+", r"host=\S+"]},
    ]})
    assert load_checks(p)[0].redact == (r"internal-id-\d+", r"host=\S+")


def test_uncompilable_redact_pattern_raises(tmp_path):
    # Validated at spec load, where the error still has someone to land on —
    # a pattern that first fails to compile inside the checker would silently
    # skip the redaction it promised.
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "redact": ["(unclosed"]}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_list_redact_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "redact": "secret"}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


def test_non_string_redact_element_raises(tmp_path):
    p = _write(tmp_path, {"checks": [{"id": "a", "run": "x", "redact": [1]}]})
    with pytest.raises(CheckSpecError):
        load_checks(p)


# === checks-tree hash (B1: the graders are part of the spec) ===

def test_tree_hash_without_a_scripts_dir_differs_from_the_spec_hash(tmp_path):
    from fleetproof.checks import checks_tree_hash, spec_hash
    p = _write(tmp_path, STARTER_SPEC)
    tree = checks_tree_hash(p)
    assert tree is not None
    # Domain-separated on purpose: a repo with no checks/ directory must not
    # produce a tree hash that collides with the plain spec hash, or a reader
    # could mistake one pin for the other.
    assert tree != spec_hash(p)


def test_tree_hash_changes_when_a_check_script_changes(tmp_path):
    from fleetproof.checks import checks_tree_hash
    p = _write(tmp_path, STARTER_SPEC)
    scripts = tmp_path / "checks"
    scripts.mkdir()
    (scripts / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    before = checks_tree_hash(p)
    (scripts / "verify.py").write_text("raise SystemExit(0)  # weakened\n",
                                       encoding="utf-8")
    assert checks_tree_hash(p) != before


def test_tree_hash_changes_when_a_script_appears_at_all(tmp_path):
    from fleetproof.checks import checks_tree_hash
    p = _write(tmp_path, STARTER_SPEC)
    before = checks_tree_hash(p)
    scripts = tmp_path / "checks"
    scripts.mkdir()
    (scripts / "new-grader.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    assert checks_tree_hash(p) != before


def test_tree_hash_covers_file_names_not_just_bytes(tmp_path):
    from fleetproof.checks import checks_tree_hash
    p = _write(tmp_path, STARTER_SPEC)
    scripts = tmp_path / "checks"
    scripts.mkdir()
    (scripts / "a.py").write_text("same bytes\n", encoding="utf-8")
    before = checks_tree_hash(p)
    (scripts / "a.py").rename(scripts / "b.py")
    # Same content under a different name is a different tree: which file a
    # spec's run line resolves to is part of what the pin vouches for.
    assert checks_tree_hash(p) != before


def test_tree_hash_is_recursive_and_deterministic(tmp_path):
    from fleetproof.checks import checks_tree_hash
    p = _write(tmp_path, STARTER_SPEC)
    scripts = tmp_path / "checks"
    (scripts / "nested").mkdir(parents=True)
    flat_only = checks_tree_hash(p)
    (scripts / "nested" / "deep.py").write_text("x = 1\n", encoding="utf-8")
    with_nested = checks_tree_hash(p)
    assert with_nested != flat_only
    assert checks_tree_hash(p) == with_nested  # stable across reads


def test_tree_hash_none_when_spec_unreadable(tmp_path):
    from fleetproof.checks import checks_tree_hash
    assert checks_tree_hash(tmp_path / "absent.json") is None


def test_spec_with_an_invented_tier_names_the_legal_tiers(tmp_path):
    from fleetproof.checks import CheckSpecError, load_checks
    spec = tmp_path / "checks.json"
    spec.write_text(json.dumps({"checks": [
        {"id": "x", "run": "true", "expect": "exit0", "tier": "lead"},
    ]}), encoding="utf-8")
    with pytest.raises(CheckSpecError) as excinfo:
        load_checks(spec)
    assert "unknown tier 'lead'" in str(excinfo.value)
    assert "legal tiers: bridge, coordinator, lane, leaf" in str(excinfo.value)


def test_checker_tier_rejection_names_the_legal_tiers():
    from fleetproof.checker import select_checks
    with pytest.raises(ValueError) as excinfo:
        select_checks([], "lead")
    assert "legal tiers: bridge, coordinator, lane, leaf" in str(excinfo.value)



# === parse_manifest_check: the manifest shares the spec's parser (C4) ===

def test_parse_manifest_check_accepts_run_as_an_alias_for_cmd():
    from fleetproof.checks import parse_manifest_check
    a = parse_manifest_check({"id": "x", "cmd": "echo a"}, "m[0]")
    b = parse_manifest_check({"id": "x", "run": "echo a"}, "m[0]")
    assert a.run == b.run == "echo a"
    # cmd wins when both are present; run is dropped, not merged.
    c = parse_manifest_check({"id": "x", "cmd": "echo a", "run": "echo b"}, "m[0]")
    assert c.run == "echo a"


def test_parse_manifest_check_names_the_entry_and_the_cmd_key_in_errors():
    from fleetproof.checks import CheckSpecError, parse_manifest_check
    with pytest.raises(CheckSpecError, match=r"m\[2\] \(x\): 'cmd' must be a single line"):
        parse_manifest_check({"id": "x", "cmd": "a\nb"}, "m[2]")
    with pytest.raises(CheckSpecError, match="'cmd' must not be blank"):
        parse_manifest_check({"id": "x", "cmd": "   "}, "m[2]")
    with pytest.raises(CheckSpecError, match="requires a 'cmd' command"):
        parse_manifest_check({"id": "x"}, "m[2]")
    with pytest.raises(CheckSpecError, match="is not an object"):
        parse_manifest_check("junk", "m[2]")
    with pytest.raises(CheckSpecError, match="'tier' is not a manifest field"):
        parse_manifest_check({"id": "x", "cmd": "echo", "tier": "lane"}, "m[2]")


def test_parse_manifest_check_file_exists_needs_no_cmd():
    from fleetproof.checks import parse_manifest_check
    c = parse_manifest_check({"id": "x", "expect": {"file_exists": "out.txt"}}, "m[0]")
    assert c.run is None and c.expect == {"kind": "file_exists", "path": "out.txt"}


# === strict key validation: the legal sets are reified (ask 3 / #30) ===

def test_unknown_manifest_key_refused_at_authoring_names_key_and_legal_set():
    from fleetproof.checks import CheckSpecError, parse_manifest_check
    with pytest.raises(CheckSpecError, match="unknown key 'expects'") as exc:
        parse_manifest_check({"id": "x", "cmd": "echo", "expects": "exit0"},
                             "manifest check [0]", on_unknown="refuse")
    message = str(exc.value)
    assert "legal keys:" in message and "expect" in message


def test_unknown_manifest_key_warns_and_runs_unchanged_at_gate_default(capsys):
    # The gate's mode: a manifest pinned under 0.5.0 must not start failing
    # mid-flight after an upgrade. The unknown key is named loudly, then
    # dropped, and the check parses exactly as its known keys declare —
    # expect falls back to exit0, which is the observed #30 shape (a content
    # assertion silently converted into an exit-code assertion).
    from fleetproof.checks import parse_manifest_check
    check = parse_manifest_check(
        {"id": "x", "cmd": "echo", "expects": {"regex": "hi"}},
        "manifest check [0]")
    err = capsys.readouterr().err
    assert "unknown key 'expects'" in err and "legal keys:" in err
    assert check.expect == {"kind": "exit0"}
    assert check.block is True


def test_legacy_id_cmd_manifest_shape_is_untouched(capsys):
    from fleetproof.checks import parse_manifest_check
    check = parse_manifest_check({"id": "m", "cmd": "echo hi"}, "m[0]",
                                 on_unknown="refuse")
    assert capsys.readouterr().err == ""
    assert check.block is True and check.expect == {"kind": "exit0"}


def test_unknown_spec_key_warns_but_the_spec_still_loads(tmp_path, capsys):
    # The spec has no authoring command in front of it, and a stray key in a
    # pinned checks.json turning into a hard error would wedge every
    # in-flight dispatch on the fail-closed pin path — so the spec side
    # always warns, never refuses.
    checks = load_checks(_write(tmp_path, {"checks": [
        {"id": "a", "run": "echo", "expects": "exit0"}]}))
    err = capsys.readouterr().err
    assert "unknown key 'expects'" in err and "legal keys:" in err
    assert [c.id for c in checks] == ["a"]
    assert checks[0].expect == {"kind": "exit0"}


def test_spec_only_keys_keep_their_dedicated_manifest_refusals():
    # tier/succeeded_by are not "unknown" — their refusals say where the
    # field actually belongs, which the generic message cannot.
    from fleetproof.checks import parse_manifest_check
    with pytest.raises(CheckSpecError, match="'tier' is not a manifest field"):
        parse_manifest_check({"id": "x", "cmd": "echo", "tier": "lane"}, "m[0]")
    with pytest.raises(CheckSpecError, match="'succeeded_by' is not a manifest field"):
        parse_manifest_check({"id": "x", "cmd": "echo", "succeeded_by": "y"}, "m[0]")


# === phase succession: succeeded_by (C13) ===

def _succ(tmp_path, checks):
    return load_checks(_write(tmp_path, {"checks": checks}))


def test_succeeded_by_parses_and_reads_back_none_when_absent(tmp_path):
    checks = _succ(tmp_path, [
        {"id": "worktree-ahead", "run": "x", "succeeded_by": "merge-landed"},
        {"id": "merge-landed", "run": "y"},
    ])
    assert checks[0].succeeded_by == "merge-landed"
    assert checks[1].succeeded_by is None


def test_succeeded_by_must_name_a_check_in_the_spec(tmp_path):
    with pytest.raises(CheckSpecError, match="not a check in this spec"):
        _succ(tmp_path, [{"id": "a", "run": "x", "succeeded_by": "ghost"}])


def test_succeeded_by_cannot_name_itself(tmp_path):
    with pytest.raises(CheckSpecError, match="must name a different check"):
        _succ(tmp_path, [{"id": "a", "run": "x", "succeeded_by": "a"}])


def test_succeeded_by_chain_cannot_cycle(tmp_path):
    with pytest.raises(CheckSpecError, match="chain cycles: a -> b -> a"):
        _succ(tmp_path, [
            {"id": "a", "run": "x", "succeeded_by": "b"},
            {"id": "b", "run": "y", "succeeded_by": "a"},
        ])


def test_succeeded_by_must_share_the_predecessors_tier(tmp_path):
    with pytest.raises(CheckSpecError, match="must be selected wherever its predecessor is"):
        _succ(tmp_path, [
            {"id": "a", "run": "x", "tier": "bridge", "succeeded_by": "b"},
            {"id": "b", "run": "y", "tier": "lane"},
        ])
    # Untiered -> untiered is the same tier (None == None).
    checks = _succ(tmp_path, [
        {"id": "a", "run": "x", "succeeded_by": "b"}, {"id": "b", "run": "y"}])
    assert checks[0].succeeded_by == "b"


def test_succeeded_by_must_be_a_non_blank_string(tmp_path):
    with pytest.raises(CheckSpecError, match="'succeeded_by' must be a check id string"):
        _succ(tmp_path, [{"id": "a", "run": "x", "succeeded_by": ""}, {"id": "b", "run": "y"}])


def test_manifest_check_refuses_succeeded_by():
    from fleetproof.checks import parse_manifest_check
    with pytest.raises(CheckSpecError, match="'succeeded_by' is not a manifest field"):
        parse_manifest_check({"id": "m", "cmd": "x", "succeeded_by": "n"}, "manifest check [0]")
