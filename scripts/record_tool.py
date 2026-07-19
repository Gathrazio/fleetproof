#!/usr/bin/env python
"""PostToolUse-hook entry point (type: "command", never an LLM).

Appends an evidence record for the tool call that just ran, so the report has a
per-tool trail. Non-blocking by contract — the tool already executed. A crash here
must never break the agent's turn, so everything is best-effort.
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
    # This hook is the one place recording must be on.
    os.environ["FLEETPROOF_NO_RECORD"] = "0"
    hookgate = _import_hookgate()
    if hookgate is None:
        return 0
    try:
        return hookgate.record_tool_main()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(_main())
