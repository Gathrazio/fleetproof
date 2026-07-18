#!/usr/bin/env python
"""PostToolUse-hook entry point (type: "command", never an LLM).

Appends an evidence record for the tool call that just ran, so the report has a
per-tool trail. Non-blocking by contract — the tool already executed. A crash here
must never break the agent's turn, so everything is best-effort.
"""

import os
import sys


def _main() -> int:
    # This hook is the one place recording must be on.
    os.environ["FLEETPROOF_NO_RECORD"] = "0"
    try:
        from fleetproof.hookgate import record_tool_main
    except ImportError:
        return 0
    try:
        return record_tool_main()
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(_main())
