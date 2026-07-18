"""Run-id propagation and durable run recording.

FleetProof records every recorded invocation to a run log that a *different*
process reads back. That separation is the point: the process that does the
work writes the record; the process that verifies the work (the checker, the
report) reads it. Nothing grades its own homework.

Records are written under ``<project-root>/.fleetproof/runs/<run-id>/<tool>-<timestamp>/``.
Per invocation:
    _root.json       — root invocation summary (one per top-level run)
    invocation.json  — {tool, subcmd, args, env_filtered, cwd, parent_run, started_at}
    result.json      — {exit_code, duration_ms, ended_at, exception}
    output.json      — structured output if the function returned a dict

Public API:
    current_run_id() -> str
    child_run_id() -> str
    @recorded                          decorator for any function
    record(tool, subcmd, args)         context manager for ad-hoc recording
    project_root() -> Path             nearest .fleetproof/ or .git/ ancestor
    runs_dir() -> Path                 the .fleetproof/runs/ directory
    set_runs_dir(path)                 override (tests; hook init)
    list_run_records()                 returns RunRecord summaries
    load_run(run_id)                   returns RunRecord with sub-invocations
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import socket
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


# === Constants ===

RUN_ID_ENV = "FLEETPROOF_RUN_ID"
RUNS_DIR_ENV = "FLEETPROOF_RUNS_DIR"  # tests + hook init override
PARENT_RUN_ID_ENV = "FLEETPROOF_PARENT_RUN_ID"
NO_RECORD_ENV = "FLEETPROOF_NO_RECORD"

# Directory the tool owns inside the user's repo.
PROJECT_MARKER = ".fleetproof"

# Env-var name patterns redacted from invocation.env_filtered
SENSITIVE_ENV_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"token", r"secret", r"password", r"passwd", r"api[_-]?key",
        r"auth", r"credential", r"private[_-]?key", r"session",
    )
]

# Env vars always passed through (whitelist)
SAFE_ENV_KEYS = frozenset({
    "PATH", "PYTHONPATH", "PWD", "HOME", "USER", "USERNAME",
    RUN_ID_ENV, RUNS_DIR_ENV, "OS", "LANG", "LC_ALL", "TZ",
})


# === Run-id machinery ===

def _generate_run_id() -> str:
    """Generate <YYYYMMDD>-<HHMMSS>-<short-hash> (sortable, readable, unique)."""
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    seed = f"{stamp}-{os.getpid()}-{time.perf_counter_ns()}"
    short = hashlib.sha256(seed.encode()).hexdigest()[:6]
    return f"{stamp}-{short}"


def current_run_id() -> str:
    """Return the run-id from env, generating + setting one if absent."""
    rid = os.environ.get(RUN_ID_ENV)
    if rid:
        return rid
    rid = _generate_run_id()
    os.environ[RUN_ID_ENV] = rid
    return rid


def child_run_id() -> str:
    """Generate a child run-id extending the current one with a sub-index.

    First child becomes <parent>.1, then <parent>.2, etc. Tracks index per
    parent process using a side file in the run directory.
    """
    parent = current_run_id()
    parent_dir = runs_dir() / parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    counter_file = parent_dir / ".child_counter"
    n = 1
    if counter_file.exists():
        try:
            n = int(counter_file.read_text(encoding="utf-8").strip()) + 1
        except ValueError:
            n = 1
    counter_file.write_text(str(n), encoding="utf-8")
    return f"{parent}.{n}"


# === Where records live ===

_runs_dir_override: Path | None = None


def set_runs_dir(path: Path | None) -> None:
    """Override the runs directory (tests + hook init)."""
    global _runs_dir_override
    _runs_dir_override = path


def project_root(start: Path | None = None) -> Path:
    """Return the nearest ancestor that looks like a project root.

    Walks up from ``start`` (default: cwd) and returns the first directory
    containing a ``.fleetproof/`` marker directory or a ``.git`` entry. Falls
    back to ``start`` itself when no marker is found (orphan runs).

    This is the whole point of the resolver: FleetProof has to work in *any*
    repo, so it anchors on ordinary version-control markers rather than on any
    project-specific convention.
    """
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / PROJECT_MARKER).is_dir() or (candidate / ".git").exists():
            return candidate
    return here


def runs_dir() -> Path:
    """Return the ``.fleetproof/runs/`` directory.

    Resolution order:
        1. Programmatic override (set_runs_dir)
        2. FLEETPROOF_RUNS_DIR env var
        3. <project-root>/.fleetproof/runs/  (walk up for .fleetproof/ or .git)
        4. <cwd>/.fleetproof/runs/  fallback (orphan runs)
    """
    if _runs_dir_override is not None:
        return _runs_dir_override
    env_val = os.environ.get(RUNS_DIR_ENV)
    if env_val:
        return Path(env_val)
    return project_root() / PROJECT_MARKER / "runs"


# === Env filtering ===

def filter_env(env: dict[str, str]) -> dict[str, str]:
    """Return env with sensitive values redacted.

    Keys matching SENSITIVE_ENV_PATTERNS are replaced with '<redacted>'.
    Keys in SAFE_ENV_KEYS pass through unchanged regardless of name.
    Everything else passes through (no redaction).
    """
    out: dict[str, str] = {}
    for k, v in env.items():
        if k in SAFE_ENV_KEYS:
            out[k] = v
            continue
        if any(p.search(k) for p in SENSITIVE_ENV_PATTERNS):
            out[k] = "<redacted>"
        else:
            out[k] = v
    return out


# === Record writing ===

@dataclass
class RunHandle:
    """Live handle to an in-progress recording."""
    run_id: str
    tool: str
    subcmd: str
    record_dir: Path
    started_at: datetime
    started_perf: float
    _result_written: bool = field(default=False)

    def set_output(self, payload: Any) -> None:
        """Write structured output (must be JSON-serializable)."""
        try:
            (self.record_dir / "output.json").write_text(
                json.dumps(payload, default=str, indent=2),
                encoding="utf-8",
            )
        except (TypeError, ValueError):
            # Non-serializable output is logged but doesn't fail the run
            (self.record_dir / "output.txt").write_text(
                repr(payload), encoding="utf-8",
            )

    def write_result(self, exit_code: int = 0, exception: BaseException | None = None) -> None:
        """Write result.json. Called automatically by @recorded and record()."""
        if self._result_written:
            return
        duration_ms = (time.perf_counter() - self.started_perf) * 1000.0
        ended_at = datetime.now(timezone.utc)
        result = {
            "exit_code": exit_code,
            "duration_ms": round(duration_ms, 3),
            "ended_at": ended_at.isoformat(),
        }
        if exception is not None:
            result["exception"] = {
                "type": type(exception).__name__,
                "message": str(exception),
                "traceback": traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                ),
            }
        (self.record_dir / "result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8",
        )
        self._result_written = True


def _write_invocation(
    record_dir: Path,
    run_id: str,
    tool: str,
    subcmd: str,
    args: dict | None,
    parent_run: str | None,
    started_at: datetime,
) -> None:
    inv = {
        "run_id": run_id,
        "parent_run_id": parent_run,
        "tool": tool,
        "subcmd": subcmd,
        "args": args or {},
        "cwd": str(Path.cwd()),
        "env_filtered": filter_env(dict(os.environ)),
        "started_at": started_at.isoformat(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    }
    (record_dir / "invocation.json").write_text(
        json.dumps(inv, indent=2), encoding="utf-8",
    )


def _ensure_root_record(run_id: str, tool: str) -> None:
    """Write _root.json if this is a brand-new top-level run."""
    parent_dir = runs_dir() / run_id
    root_file = parent_dir / "_root.json"
    if root_file.exists():
        return
    parent_dir.mkdir(parents=True, exist_ok=True)
    parent_run = os.environ.get(PARENT_RUN_ID_ENV)
    root = {
        "run_id": run_id,
        "parent_run_id": parent_run,
        "root_tool": tool,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        "pid": os.getpid(),
    }
    root_file.write_text(json.dumps(root, indent=2), encoding="utf-8")


@contextlib.contextmanager
def record(
    tool: str,
    subcmd: str = "",
    args: dict | None = None,
) -> Iterator[RunHandle]:
    """Context manager for recording an arbitrary block of work.

    Usage:
        with record("my-tool", "do") as h:
            ...work...
            h.set_output({"result": "ok"})
        # result.json written automatically on exit
    """
    run_id = current_run_id()
    _ensure_root_record(run_id, tool)
    started = datetime.now(timezone.utc)
    started_perf = time.perf_counter()
    stamp = started.strftime("%Y%m%d-%H%M%S-%f")
    suffix = subcmd or "_"
    record_dir = runs_dir() / run_id / f"{tool}-{suffix}-{stamp}"
    record_dir.mkdir(parents=True, exist_ok=True)
    parent_run = os.environ.get(PARENT_RUN_ID_ENV)
    _write_invocation(record_dir, run_id, tool, subcmd, args, parent_run, started)
    handle = RunHandle(
        run_id=run_id,
        tool=tool,
        subcmd=subcmd,
        record_dir=record_dir,
        started_at=started,
        started_perf=started_perf,
    )
    try:
        yield handle
        handle.write_result(exit_code=0)
    except BaseException as e:
        handle.write_result(exit_code=1, exception=e)
        raise


# === Decorator ===

def recorded(func: Callable | None = None, *, tool: str | None = None, subcmd: str | None = None):
    """Decorator that records the invocation, output, and result of a function.

    Usage:
        @recorded
        def do_thing(...):
            ...

        @recorded(tool="my-tool", subcmd="do")
        def do_thing(...):
            ...

    If `tool` is omitted, the function's module-derived name is used.
    If `subcmd` is omitted, the function's name is used.
    The decorated function's return value (if a dict) is written as output.json.
    """
    def decorate(f: Callable) -> Callable:
        resolved_tool = tool or _infer_tool_name(f)
        resolved_subcmd = subcmd or f.__name__

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            # Skip recording if explicitly disabled (e.g., from inside the CLI itself)
            if os.environ.get(NO_RECORD_ENV) == "1":
                return f(*args, **kwargs)
            with record(resolved_tool, resolved_subcmd, _safe_args(args, kwargs)) as h:
                result = f(*args, **kwargs)
                if isinstance(result, dict):
                    h.set_output(result)
                return result
        return wrapper

    # Allow both @recorded and @recorded(tool=..., subcmd=...)
    if func is not None and callable(func):
        return decorate(func)
    return decorate


def _infer_tool_name(f: Callable) -> str:
    mod = getattr(f, "__module__", "") or ""
    parts = mod.split(".")
    return parts[-1] if parts else "unknown"


def _safe_args(args: tuple, kwargs: dict) -> dict:
    """Reduce args/kwargs to a JSON-serializable shape; drop unserializable."""
    out: dict[str, Any] = {}
    if args:
        out["positional"] = [_safe_repr(a) for a in args]
    if kwargs:
        out["keyword"] = {k: _safe_repr(v) for k, v in kwargs.items()}
    return out


def _safe_repr(v: Any) -> Any:
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return f"<{type(v).__name__}>"


# === Reading runs back ===

@dataclass
class SubInvocation:
    record_dir: Path
    tool: str
    subcmd: str
    started_at: str | None
    exit_code: int | None
    duration_ms: float | None
    exception_type: str | None

    def load_output(self) -> Any | None:
        """Load this invocation's output.json, or None if absent/unreadable."""
        out_file = self.record_dir / "output.json"
        if not out_file.exists():
            return None
        try:
            return json.loads(out_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


@dataclass
class RunRecord:
    run_id: str
    root_dir: Path
    root_tool: str | None
    started_at: str | None
    sub_invocations: list[SubInvocation] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        """Sub-invocations that exited non-zero or raised — the 'what actually failed' count."""
        return sum(
            1 for s in self.sub_invocations
            if (s.exit_code is not None and s.exit_code != 0) or s.exception_type
        )


def list_run_records() -> list[RunRecord]:
    """Return all run records in the runs directory, newest first."""
    rd = runs_dir()
    if not rd.exists():
        return []
    out: list[RunRecord] = []
    for run_dir in sorted(rd.iterdir(), reverse=True):
        if not run_dir.is_dir() or run_dir.name.startswith("."):
            continue
        record_obj = _load_run_record(run_dir)
        if record_obj is not None:
            out.append(record_obj)
    return out


def load_run(run_id: str) -> RunRecord | None:
    """Load a specific run by id, or None if not found."""
    rd = runs_dir() / run_id
    if not rd.exists():
        return None
    return _load_run_record(rd)


def _load_run_record(run_dir: Path) -> RunRecord | None:
    root_file = run_dir / "_root.json"
    root_data: dict[str, Any] = {}
    if root_file.exists():
        try:
            root_data = json.loads(root_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    subs: list[SubInvocation] = []
    for sub_dir in sorted(run_dir.iterdir()):
        if not sub_dir.is_dir() or sub_dir.name.startswith("."):
            continue
        subs.append(_load_sub_invocation(sub_dir))

    return RunRecord(
        run_id=run_dir.name,
        root_dir=run_dir,
        root_tool=root_data.get("root_tool"),
        started_at=root_data.get("started_at"),
        sub_invocations=subs,
    )


def _load_sub_invocation(sub_dir: Path) -> SubInvocation:
    inv: dict[str, Any] = {}
    res: dict[str, Any] = {}
    inv_file = sub_dir / "invocation.json"
    res_file = sub_dir / "result.json"
    if inv_file.exists():
        try:
            inv = json.loads(inv_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    if res_file.exists():
        try:
            res = json.loads(res_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    exc = res.get("exception") or {}
    return SubInvocation(
        record_dir=sub_dir,
        tool=inv.get("tool", "?"),
        subcmd=inv.get("subcmd", ""),
        started_at=inv.get("started_at"),
        exit_code=res.get("exit_code"),
        duration_ms=res.get("duration_ms"),
        exception_type=exc.get("type") if isinstance(exc, dict) else None,
    )
