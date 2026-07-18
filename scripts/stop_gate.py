#!/usr/bin/env python
"""Stop-hook entry point (type: "command", never an LLM).

Deterministic, and — because Claude Code runs it as its own OS process — separate
from the agent whose "done" claim it is judging. It runs the independent checker
and, if a blocking check failed, emits a Claude Code Stop-hook block decision so
the agent is not allowed to stop on a false "done".

Kept to a thin shim; the logic lives in ``fleetproof.hookgate`` where it is tested.
"""

import sys


def _main() -> int:
    try:
        from fleetproof.hookgate import stop_gate_main
    except ImportError:
        # Package not importable — fail open, but say so on stderr so the operator
        # can see the gate is not actually running. A verifier must never look like
        # it verified when it did not.
        sys.stderr.write(
            "[fleetproof] package not importable; Stop gate is a no-op. "
            "Run `pip install fleetproof`.\n"
        )
        return 0
    return stop_gate_main()


if __name__ == "__main__":
    sys.exit(_main())
