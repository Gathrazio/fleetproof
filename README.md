# FleetProof

Independent, out-of-band verification for agent fleets — *what did my agents actually do, and what did they claim was done when it wasn't?*

FleetProof is a Claude Code plugin plus a small stdlib-only Python package. It
records what your agents do to a durable run log, and — critically — grades that
work with a checker that runs in a separate process from the agent that did
it. An agent may author the checks; it never executes and grades its own work.

> **Install and enable the plugin *before* the session starts.** Hooks
> register at session start, so a mid-session install is a **silently inert
> gate**: nothing fires, nothing blocks, and every "your stop will be gated"
> belief is false — the exact failure mode this tool exists to prevent, in the
> tool itself. Preflight every gated session: run one trivial tool call, then
> confirm a `claude-tool-*` run record with a non-null session id appeared
> under `.fleetproof/runs/`. No record means not live. The CLI backstops this:
> `fleetproof fleet`, `check`, and `report` each warn when the current session
> has no hook-produced record.

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
| `fleetproof telemetry` | v0.3: per-dispatch outcome records, a local reliability summary, and a strict-allowlist export. |
| `fleetproof arm` / `disarm`, `dispatch park`, `fleet --orphans` | v0.4: the fleet-operating surface — phase arming, deliberate termination, and the orphan-stop view. |

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
from a guess — and a tier that is neither declared nor inferred but *defaulted*
(a captured spawn no intent matched) renders as `lane?`, because a guess
wearing a declared tier's clothes is how a leaf gets graded as a lane.

### The two new hooks

SubagentStart is context-only by contract (it cannot block), so its whole job is
getting the dispatch on record before the subagent does any work.

SubagentStop is the gate. Each step is its own reason to refuse the stop:

1. Report-before-idle. No final message means no report: an agent that went idle
   saying nothing has not reported, and writing that down as `reported` would
   launder silence into a claim. Blocked without transitioning.
2. Pin drift. Every dispatch pins the SHA-256 of the check spec that was in
   force when the work was ordered — and, as of 0.4.0, a tree hash over the
   check scripts under `.fleetproof/checks/`, because the graders are part of
   the spec and a rewritten grader is drift the same as a rewritten spec. If
   either changed mid-flight, the stop is blocked rather than graded against a
   spec the agent could have edited itself, and the block names which one
   drifted. The one exception: if the agent's tier selects nothing runnable,
   there is nothing to mis-grade — the drift is noted and the stop allowed,
   ungraded.
3. That agent's tier of the spec, unioned with any checks declared on the
   dispatch's own manifest (`{"id", "cmd"}` entries — blocking, expect-exit0).
   A blocking failure records `contradicted` and blocks; a pass records
   `verified` and closes the dispatch. If nothing is runnable — the tier selects
   no repo checks and the manifest declares none — no verdict is recorded: an
   absent grade must never read as a passing one.

Two guardrails around the gate. A SubagentStop with no dispatch to join —
harness-internal helper agents emit these every session — is recorded as an
**orphan**: a sighting, never a graded dispatch, because a ledger that
manufactures dispatches out of unpaired stops manufactures verdicts too. And a
dispatch that keeps failing the same gate does not loop forever: after three
contradicted stops it is terminated as `abandoned` — never `verified` — and
the dispatcher is told to park it, fix the spec or tier, or re-dispatch (see
*Operating a fleet* below).

The Stop hook keeps its v0.1 job and adds a ledger sweep. A dispatch that reported
and then never terminated blocks the bridge from stopping: something started
closing it out and stopped halfway, which is exactly how work goes missing. A
dispatch still in state `dispatched` is a legitimately running background agent, so
it is reported and not blocked on.

### Working with it

```
fleetproof dispatch new --prompt "..."        # --prompt-file for a real, long one
fleetproof dispatch new --agent-name worker \
    --prompt-file p.md                        # joinable: the next SubagentStop of
                                              # that spawn name adopts this record
fleetproof dispatch report <run-id> --report report.json
fleetproof dispatch close <run-id>
fleetproof dispatch park <run-id> --reason "..."  # terminate on purpose, reason kept

fleetproof dispatch intent --agent recon --prompt-file p.md \
    [--manifest m.json] [--tier lane] [--role tester]
                                              # declare the NEXT spawn of an agent
                                              # type; the start capture consumes it

fleetproof fleet                              # the board, newest first
fleetproof fleet --open                       # only what has not terminated
fleetproof fleet --orphans                    # unpaired stops: sightings, not dispatches
fleetproof fleet --format json                # full records, for an agent to read

fleetproof check --tier lane                  # grade one rung of the spec

fleetproof arm                                # bridge Stop gate blocks (the default)
fleetproof disarm --note "why"                # bridge gate advisory; note required
```

The board never prints a bare dash where a verdict goes. A dispatch nothing graded
reads `ungraded`, because in a column of verdicts a blank cell reads as "fine".
`fleetproof report` says the same thing at more length: dispatches render as their
own rows, indented under the run that ordered them, with the prompt, the claim, the
transition trail, and which process wrote each transition.

### What the ledger does not do yet

- The spawn prompt is in neither subagent hook payload, so an undeclared spawn
  still records a placeholder saying so, rather than inventing a prompt it never
  saw. To get the real prompt (and manifest, and tier) onto a captured dispatch,
  write an intent sidecar first: `fleetproof dispatch intent --agent <type>
  --prompt-file <file>` writes `.fleetproof/intents/<agent_type>.json`, and the
  next SubagentStart of that agent type consumes it — one intent, one spawn.
- Captured subagents without a declared intent tier are recorded at `lane`
  tier, marked `defaulted` — `lane?` on the board — and the miss is loud: the
  start capture writes a stderr line naming the spawn that matched nothing and
  listing the sidecars that were present. Real nesting depth is not visible
  from the payload — there is no parent-agent field — so declaring a tier
  beats inferring it wrongly.
- An agent killed mid-turn can reach SubagentStop with an empty final message,
  which report-before-idle blocks. Blocking the stop of an agent that is already
  gone is not useful; how often it happens is under observation.
- There is no correlation field between the Task call that spawned an agent and
  that agent's stop, so `agent_id` plus `session_id` is the primary join key.
  A dispatch recorded from the CLI with `--agent-name` has no agent id yet; the
  first SubagentStop in the same session whose spawn name matches it uniquely
  adopts it (the adoption is written into the transitions). An ambiguous name
  match — two open dispatches, same name — joins nothing: the stop lands as an
  orphan with a stderr note naming the ambiguity, because guessing which
  dispatch to grade is worse than grading neither. The lookup is scoped to
  non-terminal dispatches: an agent id can be reused once a dispatch is closed
  out.

## Verification telemetry (v0.3)

The ledger records what happened to each dispatch. v0.3 turns those records into
reliability data an operator can actually read — and, when they choose to, share.

Enable it per repo with `.fleetproof/config.json`:

```json
{ "telemetry_era": "2026-08-21" }
```

From that date, every dispatch gains a `telemetry.json`, written on the checker/hook
side at verdict or close time — never by the agent being measured. Runs from before
the date read back as pre-telemetry and are never back-filled or guessed.

Each finished dispatch derives one of nine **outcome classes** from its transition
history — bookkeeping, not judgment:

- `verified` — the claim survived the checks.
- `near_miss` — a gate-blocked retry whose work product *changed* before passing:
  a real failure, caught before acceptance.
- `verifier_flake` — the retry passed with the work product *unchanged*: the
  contradiction was wrong, not the work. Counted separately so flaky checks can't
  inflate the near-miss number.
- `contradicted` — the claim did not survive.
- `ungraded` — checks existed but no verdict ever landed. This is a control
  failure and every summary says so; it is never folded into a benign class.
- `unverifiable` — reported, but nothing in the claim was checkable. Never counts
  as success.
- `silent_idle` / `terminated_unreported` / `terminated_unclassified` — died
  without reporting, split by the recorded terminate reason.

The commands:

```
fleetproof telemetry summary                  # local-only reliability read
fleetproof telemetry loss <run-id>            # one question per failure: hours or dollars
fleetproof telemetry export --recipient X     # allowlist extract for sharing
fleetproof telemetry anchor                   # record the current chain head
```

`summary` prints outcome and severity distributions with their numerators and
denominators spelled out, override rates, and the corpus's own integrity rates
(stop-only captures, missing telemetry files) — a dataset that can't measure its
own holes isn't worth reading.

`export` is built the opposite way from most exports: a strict per-field
**allowlist** — closed enums, counts, bands, versions, and salted identifiers.
Free text, prompts, paths, and every name you chose yourself never leave the
machine; timestamps coarsen to dates. Each export carries a completeness manifest
(runs per day per class, gaps visible) and a methodology note that says plainly
what `verified` means here: conformance to *your own declared checks* at a
measured coverage — not a judgment of work quality.

The honest trust model, stated rather than implied: local records are
operator-attested. The checker writes verdicts from a separate process, and v0.3
chains a rolling hash over the records as they're written (`anchor` gives you a
head you can sign or store elsewhere) — but a local ledger on a shared filesystem
is not tamper-proof against everything that can write to it, and FleetProof will
not pretend otherwise. The dispatch-intent sidecar (0.3.1) sits in the same
trust class as `checks.json` itself: operator-attested, exactly as forgeable as
the rest of `.fleetproof/`, no worse — anything that can write the repo can
write an intent, and the manifest checks it carries run as shell commands at
the stop gate the same way repo checks do.

## Operating a fleet (v0.4)

v0.4 is the field-hardening release: everything in it was shaped by running a
multi-lane fleet under the gate in a field deployment on Windows. The
mechanics are FleetProof's; the discipline below is yours. It is what keeps a
verification gate from decaying into either wallpaper or a wedge.

**Declare intent before every spawn, and verify the capture.** Intent sidecars
are keyed by the *exact spawn name* — the `agent_type` the harness reports —
not by what you think of the role as. If the names in your head and your spawn
calls drift apart, key the sidecar to the role instead with
`dispatch intent --role`; matching is exact string equality on filename OR
role, never a prefix guess. Then look at `fleetproof fleet` right after each
spawn: the row should show the real prompt and a `tier!`. A `lane?` means no
intent matched — the dispatch is running with a placeholder prompt at a
defaulted tier, and the capture you thought you declared didn't happen.

**Never dispatch an agent against a blocking check its seat cannot satisfy.**
A lane gated on something only the bridge or the operator can do will fail,
retry, and fail again — the gate is working; the dispatch was wrong. Declare
`"owner"` on such checks (`leaf|lane|coordinator|bridge|operator`): at any
other tier the check still runs and renders, but as advisory — it blocks only
at the seat that can actually satisfy it. `operator`-owned checks are advisory
at every agent tier; they keep a criterion visible without wedging anyone, and
the operator closes them out of band.

**Expensive checks belong where they run once.** The gate runs a tier's checks
at every graded stop on that tier. A slow suite gated on every leaf stop taxes
the whole fleet; tier it to the rung where it runs once, and demote what
doesn't need to block to `block: false`.

**Edit specs between phases, not under pinned dispatches.** Every dispatch
pins the spec and the check-script tree; an edit while agents are in flight
drift-blocks each of them at stop. Propose spec edits as text mid-phase, land
them at the phase boundary, then dispatch. And if the temptation is to flip
checks to `block: false` because a phase can't pass them yet, that flip is
itself a spec edit — use arming instead.

**Arming is the phase switch; disarming is an operator action.**
`fleetproof disarm --note "why"` sets the bridge Stop gate to advisory:
checks still run and render their failures, but the bridge can end its turn —
for build phases whose release checks can only pass at publish.
`fleetproof arm` turns blocking back on, and the armed state is the default —
no file, a corrupt file, or an unknown value all read as armed. The note is
required on disarm by design, and the file records who set it and when; the
board echoes a disarmed gate on every render. A bridge disarming its own gate
is therefore visible and attributed, not silent: anything that can write the
repo can flip the switch, but it cannot flip it quietly.

Arming is per tier. `--tier coordinator` (on either verb) does the same for
coordinator-tier dispatches in the subagent gate: a coordinator ends many
turns per task too, and a blocking coordinator-tier check that can only pass
at the end would otherwise wedge every mid-task stop — the field's workaround
was authoring such checks `block: false`, which left the coordinator's own
deliverable ungated. A disarmed coordinator's failing check is recorded
(verdict `verified` with an `advisory:` detail naming the failures), rendered
in full as context, never blocks, and never counts as a contradiction. Each
tier carries its own note. **Lanes are never disarmable** — `--tier lane` is
refused with that sentence; a lane's stop is the claim being verified. The
ledger sweep still blocks on stalled dispatches regardless of arming, and the
abandonment ladder neither sees nor is reset by it.

Every checker run a gate orders stamps the arming that governed it into its
`output.json` — `"arming": {"tier": "bridge", "state": "advisory", "note":
"publish phase"}` — and `fleetproof show <run>` renders it, so a `verdict:
fail` in the evidence is readable as blocked-or-not without the arming file as
it was at the time. A bare `fleetproof check` has no gate and records
`"state": "n/a"`.

**The graders are part of the spec.** The tree hash pins every file under
`.fleetproof/checks/` alongside `checks.json` itself. A check whose grading
logic lives in a script is only as trustworthy as that script's bytes, so a
rewritten grader now trips the same drift block as a rewritten spec.

**Orphans are sightings, never verdicts.** Harness-internal helper agents emit
SubagentStops that never had a start; a parked or abandoned dispatch's agent
may stop again after its dispatch is terminal. These land in the orphan list
(`fleet --orphans`), not on the board, and are never graded — a stop that
joins no dispatch must not be allowed to manufacture one, because once lane
checks are green a manufactured dispatch grades itself `verified`.

**A wedge must terminate, not loop.** The escalation ladder: a blocking
failure blocks; the third contradicted stop on the same dispatch terminates it
as `abandoned` instead — the agent is released, the board shows `abandoned!`
(never `verified`), and the dispatcher is told to park, fix, or re-dispatch.
For a dispatch you can see is wedged before the ladder runs out — or one whose
check its seat can't satisfy — `fleetproof dispatch park <run-id> --reason`
closes it on purpose with the reason kept. A parked dispatch is terminal; its
agent, if it stops again, lands as an orphan.

**Treat `.fleetproof/runs/` as secret-class, and keep secrets off command
lines.** Check stdout and stderr are persisted into the run log — that is the
point, and the risk. Builtin redactions (connection-string keys, SAS-style
signatures, private-key blocks, JWTs, bearer tokens) plus per-check `redact`
patterns run before anything is written, and the env snapshot is
denylist-filtered — but redaction is a backstop, not a permission slip. The
PostToolUse hook records `tool_input` *verbatim by design*: redacting
arbitrary code before recording it would be false comfort about what your
agents ran, so a secret typed on a command line is a secret in the run log,
full stop. Never type one. And add `.fleetproof/runs/` to `.gitignore` — it
is evidence, not source.

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

Beyond `{ "id", "run", "expect", "block" }`, a check can declare a `tier`
(which rung grades it — see the ledger section), an `owner` (which rung can
*satisfy* it — see *Operating a fleet*), and `redact` patterns applied to its
captured output before anything is persisted. `run` may also be a JSON array
of argv strings, executed with `shell=False` — no shell at all, which on
Windows means no cmd.exe quoting hazards; the string form keeps shell
semantics for compatibility. Manifest checks — the `checks` array of a
dispatch manifest — carry the same shape with `cmd` in place of `run` (`run`
is accepted as an alias): `expect` of every kind, `block`, `owner`, `redact`,
`description`. Only `tier` is refused there, because a manifest is graded at
its own dispatch's tier. A bare `{ "id", "cmd" }` entry still means blocking,
exit-0. Checks execute from the resolved project root, not from
wherever the hook's shell happened to be `cd`'d, and every rendered verdict
prints the cwd it ran from.

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
counts, API health, diffs, wordcounts, link resolution.

**A check knows which dispatch it is grading.** Every check a gate runs sees
four variables in its environment: `FLEETPROOF_RUN_ID` (the dispatch's run
id), `FLEETPROOF_AGENT_TYPE` (the spawn name), `FLEETPROOF_TIER`, and
`FLEETPROOF_SESSION_ID` — each the empty string when unknown, never absent.
The bridge Stop gate has no dispatch, so its checks see an empty run id and
agent type with tier `bridge`; the bare `fleetproof check` sets tier and
session only. So a single spec check can branch per lane — a repo with four
lanes no longer needs four manifests to grade them differently:

```python
# .fleetproof/checks/per-lane.py — one spec check, graded per lane
import os, subprocess, sys
lane = os.environ.get("FLEETPROOF_AGENT_TYPE", "")
suite = {"docs": "tests/docs", "api": "tests/api"}.get(lane, "tests")
sys.exit(subprocess.call([sys.executable, "-m", "pytest", suite, "-q"]))
```

Note that `FLEETPROOF_RUN_ID` is the same variable the run log uses for
run-id propagation, on purpose: a check that itself records lands under the
dispatch it graded. FleetProof's claim is
never that the predicate language is rich — it's that *whatever predicate you
choose runs outside the agent's process*.

Guidance that keeps the gate honest: keep blocking checks fast and
deterministic (flaky checks flap the gate; slow ones tax every stop) and demote
heavy suites to `block: false`. A spec whose selected checks are *all*
advisory renders its verdict as `ADVISORY - 0 blocking; ...`, never the bare
word PASS — green with nothing blocking is a fact worth labelling. For fleets doing varied tasks in one repo,
the working convention is to have the agent author task-specific checks at task
start — authoring is a feature — and let the spec-drift flag make any later
revision loud. Per-task check scoping as a first-class mechanism is on the
roadmap.

## Scope and limitations

Honest boundaries:

- **`python` must be on `PATH`.** The plugin hooks invoke the checker via
  `python "${CLAUDE_PLUGIN_ROOT}/scripts/..."`. Environments where the interpreter
  is only reachable as `python3`, or not on `PATH`, need a shim — any wrapper on
  `PATH` named `python` works, e.g. `sudo ln -s $(which python3) /usr/local/bin/python`.
- **Checks are commands you chose.** Their portability is your responsibility —
  a check that shells out to `grep` won't behave identically on every OS
  (the argv form of `run` sidesteps the shell-quoting half of that problem).
  And because a repo's `checks.json` runs commands on the Stop event, treat a
  cloned repo's `checks.json` with the same trust you give its `Makefile` or
  pre-commit config: it is executable code. FleetProof adds no network surface of
  its own, and first-run consent-per-spec-hash is on the roadmap.
- **The gate is only as good as the checks.** FleetProof enforces that an
  independent process runs your checks; it cannot know whether your checks capture
  what "done" really means. Weak checks give false confidence.
- **An agent can weaken its own checks — but not quietly.** Authoring
  `checks.json` is a feature, so nothing forbids an agent editing it
  mid-session to slip the gate. What stands in the way: every dispatch pins
  the spec hash *and* the tree hash over the check scripts under
  `.fleetproof/checks/`, and a pinned agent whose spec or graders drifted is
  blocked at its stop rather than graded against the edited version. At the
  session level, drift from the session's first spec is flagged loudly — in
  the Stop-gate output, the CLI, and the report. What remains open: an agent
  that edits the spec *before* any dispatch pins it is drift-invisible;
  consent-on-change is on the roadmap.
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
- **Blocking can repeat — but not unboundedly.** If a blocking check keeps
  failing, the gate keeps blocking: don't stop on a false done. Two escape
  hatches keep that from becoming a wedge. A subagent's dispatch is terminated
  as `abandoned` after three contradicted stops — the agent is released, the
  work is marked unverified, and the dispatcher is told. And the bridge's Stop
  gate can be set to advisory with `fleetproof disarm --note` when a phase
  legitimately cannot pass its checks yet — an operator decision, recorded and
  echoed on the board.

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
