#!/usr/bin/env python
"""worktree_landed — the work landed: tree clean, HEAD ahead of base.

ASSERTS
    The working tree at --worktree (default: the current directory, which
    under FleetProof is the resolved project root) has no uncommitted or
    untracked changes, AND its HEAD is at least one commit ahead of --base.
    This is the "bridge-worktrees-landed" shape: a phase whose deliverable is
    a merge is done when the merge is on the branch and nothing is left
    behind in the tree.

SOURCE OF TRUTH
    git itself, two commands, both argv (no shell):
        git -C <worktree> status --porcelain        -> clean iff empty output
        git -C <worktree> rev-list --count <base>..HEAD  -> ahead iff >= 1
    Nothing is inferred from file names or timestamps.

EXIT CODES
    0  clean and ahead            2  tree is dirty (output printed)
    3  HEAD is 0 commits ahead    4  git failed (not a repo, unknown base)
    5  usage

CONTROL SAMPLE (FLEETPROOF_CONTROL_SAMPLE)
    When set, the file it names is read INSTEAD of running git, so a captured
    real emission can be a grader control (`fleetproof check control`). The
    sample is JSON with the two raw outputs, captured from a real repo state:
        {"status_porcelain": "<stdout of git status --porcelain>",
         "ahead_count": <int from git rev-list --count base..HEAD>}
    Capture it, do not write it:
        python -c "import json,subprocess as s;print(json.dumps({
          'status_porcelain': s.check_output(['git','status','--porcelain'],text=True),
          'ahead_count': int(s.check_output(['git','rev-list','--count','main..HEAD'],text=True))}))" > pass.json
    Run that once on a repo that HAS landed (pass sample) and once on one
    that has not (fail sample), then register both:
        fleetproof check control worktree-landed --pass-sample pass.json \\
            --fail-sample fail.json --provenance captured
    A positive control must be a captured real emission, never authored by
    the check's author.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def _git(worktree: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", worktree, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def observe(worktree: str, base: str) -> tuple[str, int]:
    """``(status_porcelain, ahead_count)`` from git, or from the control sample."""
    sample = os.environ.get("FLEETPROOF_CONTROL_SAMPLE")
    if sample:
        with open(sample, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return str(data.get("status_porcelain") or ""), int(data.get("ahead_count") or 0)
    porcelain = _git(worktree, "status", "--porcelain")
    ahead = int(_git(worktree, "rev-list", "--count", f"{base}..HEAD").strip() or "0")
    return porcelain, ahead


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="The ref HEAD must be ahead of (e.g. main).")
    parser.add_argument("--worktree", default=".", help="Path of the tree to grade (default: cwd).")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 5
    try:
        porcelain, ahead = observe(args.worktree, args.base)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as e:
        print(f"FAIL: could not observe the tree: {e}")
        return 4
    if porcelain.strip():
        print("FAIL: working tree is dirty:")
        print(porcelain.rstrip())
        return 2
    if ahead < 1:
        print(f"FAIL: HEAD has 0 commits ahead of {args.base} -- nothing landed")
        return 3
    print(f"PASS: tree clean, HEAD is {ahead} commit(s) ahead of {args.base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
