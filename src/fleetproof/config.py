"""Deployment-level telemetry configuration.

Verification telemetry needs a handful of facts that are properties of the
*deployment*, not of any single run: when telemetry capture was turned on, what
kind of work this deployment does, and how the operator wants it identified in
aggregates. Asking per run would violate the "automatic or it won't exist"
posture, so they live in one JSON file the operator edits once:

    .fleetproof/config.json
    {
      "telemetry_era": "2026-08-20",
      "run_context": "production",
      "deployment_id": "my-fleet",
      "operator_id": "me",
      "eval_suite_id": null
    }

Everything is optional and the file itself is optional: an absent or unreadable
config reads back as empty, and every consumer treats a missing value as "not
configured" rather than inventing one.

``telemetry_era`` is the dated cutover that marks when this deployment started
capturing telemetry. It is stamped into each dispatch record *at creation* so
era membership is a property of the dispatch itself, never inferred from
whether a telemetry file happens to exist later — file absence is deletable,
a stamp written at dispatch time is on the record the aggregate reads.

The config file is resolved next to the runs directory (the ``.fleetproof/``
project marker), so tests that override the runs dir get an isolated config
for free, the same way the drift marker does.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .runlog import runs_dir

CONFIG_FILENAME = "config.json"

# The kinds of work a deployment can declare itself to be doing. An aggregate
# that cannot tell real work from drills or synthetic load is worth less than
# one that can, so the value is per-deployment config — never per-run prompting,
# and never guessed from the work itself.
RUN_CONTEXT_PRODUCTION = "production"
RUN_CONTEXT_DEVELOPMENT = "development"
RUN_CONTEXT_DRILL = "drill"
RUN_CONTEXT_SYNTHETIC = "synthetic"
VALID_RUN_CONTEXTS = frozenset({
    RUN_CONTEXT_PRODUCTION,
    RUN_CONTEXT_DEVELOPMENT,
    RUN_CONTEXT_DRILL,
    RUN_CONTEXT_SYNTHETIC,
})


def config_path() -> Path:
    return runs_dir().parent / CONFIG_FILENAME


def load_config() -> dict[str, Any]:
    """The deployment config, or an empty dict when absent/unreadable.

    A malformed config must not take down a hook, so every failure mode reads
    as "not configured" — the consequence is a pre-telemetry-shaped record,
    which is the honest description of a deployment whose config is broken.
    """
    try:
        data = json.loads(config_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def telemetry_era_stamp(config: dict[str, Any] | None = None) -> str | None:
    """The era value to stamp onto a dispatch created now, or None.

    Returns the configured cutover date string once the cutover date has
    arrived; a future-dated cutover (or no cutover at all) yields None, and the
    dispatch is created pre-telemetry. The comparison is by date, matching the
    granularity the config declares.
    """
    cfg = config if config is not None else load_config()
    raw = cfg.get("telemetry_era")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        cutover = date.fromisoformat(raw.strip())
    except ValueError:
        return None
    if datetime.now(timezone.utc).date() < cutover:
        return None
    return raw.strip()


def run_context(config: dict[str, Any] | None = None) -> str:
    """The deployment's declared run context; ``production`` when unconfigured.

    Defaulting to ``production`` is deliberate: a deployment that never touched
    the config is doing its real work, and letting real work default into a
    discounted bucket would under-count the corpus that matters. Drills and
    synthetic load are the exceptional cases, so they are the ones that require
    an explicit declaration. An unknown value degrades to the default rather
    than crashing a hook on a typo.
    """
    cfg = config if config is not None else load_config()
    value = cfg.get("run_context")
    if isinstance(value, str) and value in VALID_RUN_CONTEXTS:
        return value
    return RUN_CONTEXT_PRODUCTION


def _config_str(key: str, config: dict[str, Any] | None = None) -> str | None:
    cfg = config if config is not None else load_config()
    value = cfg.get(key)
    return value if isinstance(value, str) and value.strip() else None


def deployment_id(config: dict[str, Any] | None = None) -> str | None:
    return _config_str("deployment_id", config)


def operator_id(config: dict[str, Any] | None = None) -> str | None:
    return _config_str("operator_id", config)


def eval_suite_id(config: dict[str, Any] | None = None) -> str | None:
    return _config_str("eval_suite_id", config)
