#!/usr/bin/env python
"""py_tests_pinned — python -m pytest with PYTHONPATH pinned to the graded tree.

ASSERTS
    `python -m pytest <args>` exits 0 when run from --root (default: the
    current directory, which under FleetProof is the resolved project root)
    with PYTHONPATH set to that root FIRST — so the tests import the code in
    the graded tree, not whatever copy happens to be installed in the
    interpreter's site-packages. A green suite against an installed, older
    copy of the package is the classic false pass this pin prevents.

SOURCE OF TRUTH
    pytest's own exit code, from a subprocess launched argv-form with no
    shell: [sys.executable, "-m", "pytest", *args]. Every argument after the
    script's own options is passed to pytest verbatim.

EXIT CODES
    pytest's exit code (0 = all passed; 1 = failures; 2 = interrupted;
    3 = internal error; 4 = usage; 5 = no tests collected — which is a FAIL
    here on purpose: a suite that collected nothing verified nothing).
    98  --root is not a directory.

CONTROL SAMPLE (FLEETPROOF_CONTROL_SAMPLE)
    When set, the file it names is read INSTEAD of running pytest, so a
    captured real run can be registered as a control. The sample is JSON:
        {"pytest_exit": <int>}
    Capture it from a real run, never type it:
        python -m pytest -q; python -c "import json;print(json.dumps({'pytest_exit': $LASTEXITCODE}))" > pass.json   (PowerShell)
        python -m pytest -q; python -c "import json;print(json.dumps({'pytest_exit': $?}))" > pass.json             (POSIX)
    Then: fleetproof check control py-tests-pinned --pass-sample pass.json --provenance captured
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="The graded tree root (default: cwd).")
    args, pytest_args = parser.parse_known_args(argv)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"FAIL: --root {root} is not a directory")
        return 98

    sample = os.environ.get("FLEETPROOF_CONTROL_SAMPLE")
    if sample:
        try:
            with open(sample, encoding="utf-8-sig") as fh:
                code = int(json.load(fh).get("pytest_exit"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            print(f"FAIL: unreadable control sample: {e}")
            return 98
        print(f"{'PASS' if code == 0 else 'FAIL'}: control sample pytest_exit={code}")
        return code

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    proc = subprocess.run([sys.executable, "-m", "pytest", *pytest_args], cwd=root, env=env)
    print(f"{'PASS' if proc.returncode == 0 else 'FAIL'}: pytest exit {proc.returncode} "
          f"(PYTHONPATH pinned to {root})")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
