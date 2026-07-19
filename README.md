# FleetProof

Independent, out-of-band verification for agent fleets — *what did my agents actually do, and what did they claim was done when it wasn't?*

FleetProof is a Claude Code plugin plus a small stdlib-only Python package. It
records what your agents do to a durable run log, and — critically — grades that
work with a checker that runs in a **separate process from the agent that did
it**. An agent may author the checks; it never executes and grades its own work.

## Why this exists

The failure mode this targets is the false "done": an agent reports a task
complete when it isn't. A recent characterization study
([arXiv:2606.09863](https://arxiv.org/abs/2606.09863)) measured it directly:
**45–48% of failures** in single-control tau2-bench domains are exactly this —
the agent confidently claims success. Among **self-assessing** coding-agent
trajectories on AppWorld, it's **75.8%**. And in the study's one *dual-control*
setting — where a second, independent control point contradicts the agent's
claims — false success collapses to **3%**.

FleetProof is built on that comparison: it gives any repo a second, independent
control point. (To be precise: the 3% is a measured property of that
benchmark setting, not a measured result of installing this tool — the study
motivates the design; it didn't test it.)

The word doing the work there is *independent*. A verifier that is the same agent
(or a sub-agent prompted by it) inherits the same blind spots and the same
incentive to declare victory. The only way to escape that is to move verification
out of the agent's own process and make it deterministic. That is the entire
design of FleetProof:

- the agent writes the run records and authors `checks.json`;
- a **different process** — the plugin's Stop hook, a `type: "command"` hook,
  never an LLM — executes those checks and grades them;
- if a blocking check fails, the hook returns `{"decision": "block", ...}` and
  Claude Code refuses to let the agent stop on the false "done".

No language model sits in the grading path. Grading is comparison.

> Verifying one machine is free, forever. A hosted **team tier** — shared
> evidence chains, approval queues, compliance export — is coming:
> [join the waitlist](https://forms.gle/FcuBTYyoV4x2z8Hm8).

## What's in the box

| Piece | What it does |
|---|---|
| `@recorded` / `record()` | Durable per-invocation run records under `.fleetproof/runs/`. |
| `fleetproof check` | The independent checker: runs each declared check in its own subprocess, records the verdict. |
| Stop hook | Runs the checker when an agent claims done; blocks on a blocking-check failure. |
| PostToolUse hook | Accretes a per-tool evidence trail into the run log. |
| `fleetproof report` | One self-contained HTML file: per run, claimed-done vs. independently-verified. |

Runtime dependencies: **none** (Python standard library only). A tool whose job is
being trustworthy should add as little dependency and supply-chain surface as it can.

## Install (each line is one command in Claude Code)

```
/plugin marketplace add Gathrazio/fleetproof
/plugin install fleetproof@fleetproof
```

Then install the Python package so the CLI and hooks can run:

```
pip install fleetproof
```

Requires Python 3.10+. The plugin's hook commands invoke `python`, so `python`
must be on your `PATH` (see *Scope and limitations*).

## 10-minute quickstart

1. Install the plugin and the package (three commands above).
2. Declare what "done" means for your repo:
   ```
   fleetproof init
   ```
   This writes a starter `.fleetproof/checks.json`. Edit it — each entry is
   `{ "id", "run", "expect", "block" }`:
   ```json
   {
     "checks": [
       { "id": "tests-pass", "run": "python -m pytest -q", "expect": "exit0", "block": true },
       { "id": "artifact-built", "expect": { "file_exists": "dist/app.zip" }, "block": true }
     ]
   }
   ```
   `expect` is one of `"exit0"`, `{ "exit": N }`, `{ "regex": "..." }`, or
   `{ "file_exists": "path" }`. `block: true` gates "done"; `block: false` is advisory.
3. Run any Claude Code agent task as usual. When it claims done, the **Stop hook**
   runs `fleetproof check` in its own process. If a blocking check fails, Claude is
   blocked from stopping and told exactly which check disagreed.
4. See what actually happened:
   ```
   fleetproof list                 # runs, newest first
   fleetproof report               # writes .fleetproof/fleetproof-report.html
   ```
   The report marks each run **verified**, **contradicted** (claimed done, but a
   blocking check failed), or **unverified** (no out-of-band verdict on record).

You can also run the checker yourself at any time — `fleetproof check` — or on
demand via the bundled `/fleetproof:verify-fleet` skill.

## The check-spec format

`.fleetproof/checks.json` is deliberately small and declarative. It is a contract
you (or an agent, under your review) write *before* work is graded, kept in the
repo where it is versioned and diffable. JSON, not YAML, on purpose: there is no
third-party parser in the runtime.

An agent is welcome to propose or edit checks. The guarantee FleetProof makes is
narrower and firmer than "the agent verified its work": it is that *something
other than the agent* ran the checks and recorded the result.

## Scope and limitations (v0.1)

This is an early release. Honest boundaries:

- **`python` must be on `PATH`.** The plugin hooks invoke the checker via
  `python "${CLAUDE_PLUGIN_ROOT}/scripts/..."`. Environments where the interpreter
  is only reachable as `python3`, or not on `PATH`, need a shim — any wrapper on
  `PATH` named `python` works, e.g. `sudo ln -s $(which python3) /usr/local/bin/python`.
- **Checks are shell commands.** Their portability is your responsibility — a
  check that shells out to `grep` won't behave identically on every OS. And
  because a repo's `checks.json` runs shell commands on the Stop event, treat a
  cloned repo's `checks.json` with the same trust you give its `Makefile` or
  pre-commit config: it is executable code. FleetProof adds no network surface of
  its own, and first-run consent-per-spec-hash is on the roadmap.
- **The gate is only as good as the checks.** FleetProof enforces that an
  independent process runs your checks; it cannot know whether your checks capture
  what "done" really means. Weak checks give false confidence.
- **An agent can weaken its own checks.** Authoring `checks.json` is a feature, so
  nothing stops an agent from editing it mid-session to slip the gate. FleetProof
  does not (yet) forbid spec edits; instead it records the SHA-256 of the spec on
  every verdict and flags mid-session **spec drift** loudly — in the Stop-gate
  output, the CLI, and the report — when a verdict's spec hash differs from the
  session's first. Drift does not by itself block a passing verdict in v0.1; spec
  pinning and consent-on-change are on the roadmap.
- **Fail-open when unconfigured.** With no `.fleetproof/checks.json`, the Stop hook
  does nothing (and says so on stderr) rather than blocking every task. A missing
  spec is not a passing grade — it is an absent one.
- **Blocking can repeat.** If a blocking check keeps failing, the Stop hook keeps
  blocking. That is intended (don't stop on a false done), but it means a check
  that can never pass needs operator intervention.

## FleetProof for teams (coming — waitlist open)

The plugin verifies one machine. The hosted tier turns those local verdicts into
shared, durable **evidence chains**, **team approval queues** for gating agent
work a human signs off on, and a **compliance export** a non-engineer can
inspect — relevant as EU AI Act Article 14 human-oversight obligations become
enforceable (2026-08-02).

**[Join the waitlist →](https://forms.gle/FcuBTYyoV4x2z8Hm8)** — and tell us
which piece you'd want first.

The free plugin never sends anything anywhere: no telemetry, no network calls in
the runtime, your run data stays on your disk. The hosted tier is opt-in and
separate.

## Development

```
pip install -e ".[dev]"
python -m pytest -q
```

## License

MIT. Author: Gathrazio.
