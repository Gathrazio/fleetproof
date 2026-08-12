# FleetProof

Independent, out-of-band verification for agent fleets — *what did my agents actually do, and what did they claim was done when it wasn't?*

FleetProof is a Claude Code plugin plus a small stdlib-only Python package. It
records what your agents do to a durable run log, and — critically — grades that
work with a checker that runs in a separate process from the agent that did
it. An agent may author the checks; it never executes and grades its own work.

## Why this exists

The failure mode this targets is the false "done": an agent reports a task
complete when it isn't. A recent characterization study
([arXiv:2606.09863](https://arxiv.org/abs/2606.09863)) measured it directly:
45–48% of failures in single-control tau2-bench domains are exactly this —
the agent confidently claims success. Among self-assessing coding-agent
trajectories on AppWorld, it's 75.8%. And in the study's one *dual-control*
setting — where a second, independent control point contradicts the agent's
claims — false success collapses to 3%.

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
- a different process — the plugin's Stop hook, a `type: "command"` hook,
  never an LLM — executes those checks and grades them;
- if a blocking check fails, the hook returns `{"decision": "block", ...}` and
  Claude Code refuses to let the agent stop on the false "done".

No language model sits in the grading path. Grading is comparison.

> Verifying one machine is free, forever. A hosted team tier — shared
> evidence chains, approval queues, compliance export — is coming:
> [join the waitlist](https://forms.gle/FcuBTYyoV4x2z8Hm8).

## What's in the box

| Piece | What it does |
|---|---|
| `@recorded` / `record()` | Durable per-invocation run records under `.fleetproof/runs/`. |
| `fleetproof check` | The independent checker: runs each declared check in its own subprocess, records the verdict. |
| Stop hook | Runs the checker when an agent claims done; blocks on a blocking-check failure, and on dispatches left half-closed. |
| PostToolUse hook | Accretes a per-tool evidence trail into the run log. |
| SubagentStart hook | Puts a spawning subagent on the dispatch ledger before it does any work. |
| SubagentStop hook | The per-subagent gate: records what the agent claimed, grades it at that agent's tier, blocks a false "done". |
| `fleetproof fleet` | The dispatch board: every dispatch, its state, its tier, and whether anything graded it. |
| `fleetproof report` | One self-contained HTML file: per run, claimed-done vs. independently-verified. |

Runtime dependencies: none (Python standard library only). A tool whose job is
being trustworthy should add as little dependency and supply-chain surface as it can.

## The Dispatch Ledger (v0.2)

v0.1 could answer "did this agent's claim survive an independent check?" It could
not answer the question a fleet operator actually has: of everything I dispatched,
what came back, and did any of it check out? A subagent that was launched, wrote
nothing, and died leaves no trace at all in a log that only records tool calls.

So v0.2 records a dispatch at launch — before any work happens — as its own run:

```
.fleetproof/runs/<run-id>/
    _root.json      root_tool="dispatch", parent_run_id -> the dispatching run
    dispatch.json   the prompt, the tier, the manifest, the transitions
    report.json     what the dispatched agent claimed (written when it reports)
```

Nothing grades itself here either. The dispatching process writes `dispatch.json`;
the report is the dispatched agent's own claim; the `verified` / `contradicted`
transition is appended by the checker, from a different process.

### The lifecycle

```
dispatched -> reported -> verified ------------------> terminated
     |            ^          contradicted -> terminated
     |            |               |
     |            +---------------+   (the gate blocked, the agent fixed it, the
     |                                 SAME dispatch reports again)
     +----------------------------------------------> terminated
```

A dispatch's state is its last transition, and `terminated` is the only terminal
one. Two things in that diagram are deliberate:

- `terminated` is reachable from everywhere, including straight from `dispatched`.
  An agent killed before it ever reported is a thing that happens; a ledger that
  refused to record it would be lying to keep its state machine tidy.
- `contradicted -> reported` is legal. When the gate blocks a subagent's stop, the
  harness hands that same agent its turn back, so the retry lands on the same
  dispatch by construction. The transition list is the audit trail of how many
  tries it took.

### Tiers

A check can declare which rung of the fleet it governs — `leaf`, `lane`,
`coordinator`, or `bridge`:

```json
{ "id": "tests-pass", "run": "python -m pytest -q", "expect": "exit0", "tier": "bridge" }
```

A check with no `tier` fires at **every** rung. That is deliberate: an untiered
v0.1 spec keeps gating everything it ever gated, at every gate — the earlier
policy of scoping untiered checks to the bridge alone left the default subagent
gate grading nothing at all while the bridge's green made the system look alive.
Narrowing a check to one rung is an explicit act: write the tier. A *tiered*
check is graded on its own rung and nothing else, which is why a failing
leaf-tier check does not block the bridge from stopping: the bridge has no way
to fix a leaf's work from its own turn. And if every blocking check in the spec
is tiered below the bridge, the Stop gate blocks rather than grading nothing —
an absent grade is never a passing grade.

A dispatch's tier is either declared or inferred, and the ledger records which one.
Inference reads the shape of the run tree: no parent means `bridge`, a parent that
tops its own chain means `lane`, anything deeper means `leaf`. `coordinator` is
never inferred — it is a role you assign, not a shape that shows up in a run tree.
On the board a declared tier carries a trailing `!`, so you can tell a decision
from a guess.

### The two new hooks

SubagentStart is context-only by contract (it cannot block), so its whole job is
getting the dispatch on record before the subagent does any work.

SubagentStop is the gate. Each step is its own reason to refuse the stop:

1. Report-before-idle. No final message means no report: an agent that went idle
   saying nothing has not reported, and writing that down as `reported` would
   launder silence into a claim. Blocked without transitioning.
2. Pin drift. Every dispatch pins the SHA-256 of the check spec that was in force
   when the work was ordered. If the spec changed mid-flight, the stop is blocked
   rather than graded against a spec the agent could have edited itself.
3. That agent's tier of the spec. A blocking failure records `contradicted` and
   blocks; a pass records `verified` and closes the dispatch. If the tier selects
   no checks at all, no verdict is recorded — an absent grade must never read as a
   passing one.

The Stop hook keeps its v0.1 job and adds a ledger sweep. A dispatch that reported
and then never terminated blocks the bridge from stopping: something started
closing it out and stopped halfway, which is exactly how work goes missing. A
dispatch still in state `dispatched` is a legitimately running background agent, so
it is reported and not blocked on.

### Working with it

```
fleetproof dispatch new --prompt "..."        # --prompt-file for a real, long one
fleetproof dispatch report <run-id> --report report.json
fleetproof dispatch close <run-id>

fleetproof fleet                              # the board, newest first
fleetproof fleet --open                       # only what has not terminated
fleetproof fleet --format json                # full records, for an agent to read

fleetproof check --tier lane                  # grade one rung of the spec
```

The board never prints a bare dash where a verdict goes. A dispatch nothing graded
reads `ungraded`, because in a column of verdicts a blank cell reads as "fine".
`fleetproof report` says the same thing at more length: dispatches render as their
own rows, indented under the run that ordered them, with the prompt, the claim, the
transition trail, and which process wrote each transition.

### What the ledger does not do yet

- The spawn prompt is in neither subagent hook payload. A captured dispatch records
  a placeholder saying so, rather than inventing a prompt it never saw. To get the
  real prompt on the record, dispatch through `fleetproof dispatch new`.
- Captured subagents are recorded as `lane` tier. Real nesting depth is not visible
  from the payload — there is no parent-agent field — so declaring one beats
  inferring it wrongly. What that costs in practice is a pilot question.
- An agent killed mid-turn can reach SubagentStop with an empty final message,
  which report-before-idle blocks. Blocking the stop of an agent that is already
  gone is not useful; how often it happens is under observation.
- There is no correlation field between the Task call that spawned an agent and
  that agent's stop, so `agent_id` plus `session_id` is the entire join key. That
  is also why the lookup is scoped to non-terminal dispatches: an agent id can be
  reused once a dispatch is closed out.

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
3. Run any Claude Code agent task as usual. When it claims done, the Stop hook
   runs `fleetproof check` in its own process. If a blocking check fails, Claude is
   blocked from stopping and told exactly which check disagreed.
4. See what actually happened:
   ```
   fleetproof list                 # runs, newest first
   fleetproof report               # writes .fleetproof/fleetproof-report.html
   ```
   The report marks each run *verified*, *contradicted* (claimed done, but a
   blocking check failed), or *unverified* (no out-of-band verdict on record).

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

## Writing richer checks

The grading vocabulary is deliberately small — exit codes, a regex, a file
existing — but `run` is an arbitrary command, so the *predicate* can be any
program. The pattern: encode "done" as a script that exits non-zero when it
isn't, and let FleetProof gate on the exit code.

```json
{
  "checks": [
    { "id": "dataset-valid",
      "run": "python scripts/validate_dataset.py out/rows.csv --min-rows 1000",
      "expect": "exit0", "block": true,
      "description": "Output CSV exists, parses, has >=1000 rows, no nulls in key columns." },
    { "id": "citations-resolve",
      "run": "python scripts/check_links.py report/draft.md",
      "expect": "exit0", "block": true },
    { "id": "full-suite-advisory",
      "run": "python -m pytest tests/slow -q",
      "expect": "exit0", "block": false,
      "description": "Slow suite is advisory: recorded, never gates the stop." }
  ]
}
```

Anything a program can decide, a check can gate: schema conformance, row
counts, API health, diffs, wordcounts, link resolution. FleetProof's claim is
never that the predicate language is rich — it's that *whatever predicate you
choose runs outside the agent's process*.

Guidance that keeps the gate honest: keep blocking checks fast and
deterministic (flaky checks flap the gate; slow ones tax every stop) and demote
heavy suites to `block: false`. For fleets doing varied tasks in one repo,
the working convention is to have the agent author task-specific checks at task
start — authoring is a feature — and let the spec-drift flag make any later
revision loud. Per-task check scoping as a first-class mechanism is on the
roadmap.

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
  every verdict and flags mid-session *spec drift* loudly — in the Stop-gate
  output, the CLI, and the report — when a verdict's spec hash differs from the
  session's first. Drift does not by itself block a passing verdict in v0.1; spec
  pinning and consent-on-change are on the roadmap.
- **The local ledger is forgeable by design.** An agent with write access to the
  repo can, in principle, forge or tamper with records in its own `.fleetproof/`
  — verdicts, transitions, reports. The gates make honest mistakes loud and
  cheap to catch; they are not a security boundary against an adversary that
  owns the working tree. A second control point outside the agent's reach (the
  hosted tier) is the structural answer, and that is why it exists on the
  roadmap.
- **Fail-open only when nothing was promised.** With no `.fleetproof/checks.json`
  and no history of one, the Stop hook does nothing (and says so on stderr)
  rather than blocking every task. But once a spec has graded work this session
  — or a dispatch has pinned one — a missing, emptied, or unreadable spec
  *blocks*: a promised gate cannot be switched off by deleting its spec.
- **Blocking can repeat.** If a blocking check keeps failing, the Stop hook keeps
  blocking. That is intended (don't stop on a false done), but it means a check
  that can never pass needs operator intervention.

## FleetProof for teams (coming — waitlist open)

The plugin verifies one machine. The hosted tier turns those local verdicts into
shared, durable evidence chains, team approval queues for gating agent
work a human signs off on, and a compliance export a non-engineer can
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
