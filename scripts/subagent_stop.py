#!/usr/bin/env python
"""SubagentStop-hook entry point (type: "command", never an LLM).

The per-subagent gate: records what the subagent claimed in its final message,
grades that claim against the subagent's tier of the check spec, and emits a
SubagentStop block decision when the claim does not survive. Runs as its own OS
process, so the agent being graded is not the one grading.

Kept to a thin shim; the logic lives in ``fleetproof.hookgate`` where it is tested.
"""

import os
import sys


def _import_hookgate():
    """Import fleetproof.hookgate, falling back to the copy the plugin ships."""
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
        # Fail open, but visibly: a gate that is silently absent is worse than one
        # that is loudly absent.
        sys.stderr.write(
            "[fleetproof] package not importable; subagent gate is a no-op. "
            "Run `pip install fleetproof`.\n"
        )
        return 0
    return hookgate.subagent_stop_main()


if __name__ == "__main__":
    sys.exit(_main())
