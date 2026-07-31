#!/usr/bin/env python
"""SubagentStart-hook entry point (type: "command", never an LLM).

Records a dispatch the moment a subagent spawns, so the ledger knows the agent
exists *before* it does any work. Context-only by contract: this hook cannot block,
and a failure here must never break the spawn.

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
        sys.stderr.write(
            "[fleetproof] package not importable; subagent capture is a no-op. "
            "Run `pip install fleetproof`.\n"
        )
        return 0
    try:
        return hookgate.subagent_start_main()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(_main())
