#!/usr/bin/env python
"""Stop-hook entry point (type: "command", never an LLM).

Deterministic, and — because Claude Code runs it as its own OS process — separate
from the agent whose "done" claim it is judging. It runs the independent checker
and, if a blocking check failed, emits a Claude Code Stop-hook block decision so
the agent is not allowed to stop on a false "done".

Kept to a thin shim; the logic lives in ``fleetproof.hookgate`` where it is tested.
"""

import os
import sys


def _import_hookgate():
    """Import fleetproof.hookgate, falling back to the copy the plugin ships.

    The plugin bundles its own package under ``<plugin root>/src``, so the gate
    works even when the user never ran ``pip install fleetproof``. A verifier
    must never silently look absent just because of a missing install step.
    """
    try:
        from fleetproof import hookgate
        return hookgate
    except ImportError:
        pass
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    src = os.path.join(plugin_root, "src")
    if os.path.isdir(src) and src not in sys.path:
        sys.path.insert(0, src)
    try:
        from fleetproof import hookgate
        return hookgate
    except ImportError:
        return None


def _main() -> int:
    hookgate = _import_hookgate()
    if hookgate is None:
        # Truly not importable — fail open, but say so on stderr so the operator
        # can see the gate is not actually running.
        sys.stderr.write(
            "[fleetproof] package not importable; Stop gate is a no-op. "
            "Run `pip install fleetproof`.\n"
        )
        return 0
    return hookgate.stop_gate_main()


if __name__ == "__main__":
    sys.exit(_main())
