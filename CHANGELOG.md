# Changelog

Releases before 0.4.0 predate this file; their stories are in the README's
version-marked sections and the git history.

## 0.6.0 — 2026-08-26

Honest-bookkeeping release. Every item below answers the third field trial
at the same Windows deployment — a graded sitting under an armed coordinator
gate, twelve asks and thirteen findings, every one verified against source
before a line was written — collective credit to that deployment's operators,
including for the three findings they disclosed against their own authoring.
The trial's headline was not a false claim (their ledger is still empty after
three trials); it was a lane *correctly refusing* unsatisfiable work and
being scored as the fleet's only liar. 0.6.0 gives the escalation a class of
its own, makes the advisory stop a real stop, and makes every surface say
what it actually wrote.

### Classification honesty

- `escalated`, a twelfth outcome class: `dispatch park --unsatisfiable`
  records that the agent reported the work cannot be satisfied from its seat
  and the operator agreed. Counted (`escalated_rate`), in no success and no
  failure numerator, no incident or severity attach — a lane that correctly
  refuses must never score worse than one that guesses. A plain park after a
  contradiction stays `contradicted`; only the flag reclasses
  (derivation_version 5). The board renders `parked (unsatisfiable)`.
- The advisory stop is a stop: a blocking failure under a disarmed
  coordinator gate records `advisory`, closes the dispatch, and no longer
  resumes the agent with the failure text — that resume left the graded seat
  editing the failed artifact after its own dispatch was closed. The failure
  detail reaches the dispatcher instead: the fleet board footer and the
  bridge Stop gate's stderr, via the same plumbing as the ungraded announce.
- `no_verdict_rate` prints beside `ungraded_rate` with its split shown
  (`ungraded` + `unverifiable`) — the plain-English total, so the word
  "ungraded" stops meaning both.
- Coverage tells the truth about the unmeasured half: deliverables with no
  `check_map` derive `outcome.coverage: null`, never `0.0` — an absent join
  is an absence of measurement, not a zero score.
- `output.json`'s all-advisory flag is named `all_advisory` (`advisory` also
  names an arming state and a dispatch verdict in the same record);
  the 0.4.0 `advisory` key is dual-written for one release, removed in 0.7.0.

### Strictness where the author still sits

- Closed check-key sets, reified (`SPEC_CHECK_KEYS` / `MANIFEST_CHECK_KEYS`):
  an unknown key — `expects` for `expect` — is refused at authoring time
  (`dispatch intent`, `dispatch new`, `--preflight`) naming the key and the
  legal set, and warned about loudly at the gate while the check runs as its
  known keys declare — a manifest pinned under 0.5.0 must not start failing
  mid-flight.
- Per-sample control provenance, with `pending-capture` as the pass
  direction's honest third state for creation work (recorded with no value at
  all). Strict controls are satisfied by a captured pass OR a captured fail
  beside a pending pass; `check control --upgrade` promotes the pass
  direction from the work's real emission at closeout, and the gate's
  verified transition names every control still pending with that remedy.
- `fleetproof phase preflight`, the vacuous-successor tripwire: runs every
  `succeeded_by` successor NOW against the current tree, recording nothing,
  and exits non-zero naming each successor that already passes — a successor
  must assert something the predecessor's completion causes, not something
  its starting state already satisfies. The rule is in the README's *Grader
  integrity* section verbatim, as rule 5.
- `check_map` — parsed and validated since 0.3, named by no surface — is
  documented (README manifest section with an example, `--manifest` help),
  and `dispatch intent` / `dispatch new` warn when a manifest declares
  deliverables AND checks but no map: coverage will read null.

### Surfaces that say what happened

- `show` badges every checker row by its verdict, not its process exit —
  a checker that blocked a lane under a disarmed gate exits 0, and `[ok]`
  off the exit code read as a pass. The row carries `verdict: fail
  (blocking_failed: N)` in both formats; non-checker rows wear their exit
  plainly (`[exit:0]`).
- Every persisted check entry records `cmd`, the exact string/argv the
  runner executed — local `output.json` only, excluded from telemetry and
  export like check ids; redaction does not cover a command line, and the
  record says so.
- `dispatch intent` echoes the recorded tier and its provenance on every
  write (`tier recorded: lane (defaulted — pass --tier to declare)`); JSON
  carries `tier` + `tier_source`. The board splits the defaulted glyph:
  `lane=` (sidecar matched, tier undeclared) vs `lane?` (no intent matched).
- `--preflight` stops underselling its write: the sidecar line announces
  `sidecar written (next spawn of X consumes it):` before the path, and the
  help text names the write. New `--dry-run` parses, validates, and (with
  `--preflight`) runs the checks while writing nothing at all.
- Inheritance stamps `inherited_from_state` / `inherited_from_verdict` —
  the predecessor as loaded at inherit time; a message-vs-resume trigger is
  not observable from the SubagentStart payload, so no field pretends it is.
  A no-verdict terminal predecessor warns on stderr and sharpens the board
  glyph to `tier~!`.
- One scope rule for every fleet footer count (orphans, ungraded, advisory):
  ledger-scoped, never listing-scoped — "this session" with a session-scoped
  count when a session is known, "on the board" over the whole ledger when
  not. The orphan line no longer says "this session" from a session-less
  shell over a full-disk count.
- `arming.json`'s top-level 0.4.0 trio (`note`/`set_at`/`by`) is
  bridge-pinned as one coherent view: setting the coordinator no longer
  stamps the bridge's note with the coordinator's time and actor. Per-tier
  truth stays in `notes`/`set`.

## 0.5.0 — 2026-08-25

Confidence release. Every item below was shaped by a second field deployment
on Windows — four lanes, a coordinator, and a deliberate probe under the
0.4.0 gate, evaluated from the ledger — collective credit to that
deployment's operators. The run caught no false claim of done, because none
was made; it caught a grader that was confidently wrong and an agent that
obeyed it into a deployed product. 0.5.0 makes grader authorship
self-doubting, closes the silent fail-open that two correct 0.4.0 fixes
composed into, and keeps every word the gate said.

### Confidence

- A re-message of a live teammate no longer grades nothing: a SubagentStart
  that matches no sidecar inherits the prompt, manifest, and tier of the
  newest same-session dispatch of the same agent type that carried a declared
  intent — `tier_source: "inherited"`, `lane~` on the board, `inherited_from`
  on the record, a stderr line naming the source.
- A claim closed with no verdict is announced: `fleet` prints
  `N dispatch(es) terminated ungraded this session — <ids>` after the orphan
  count (counted off the ledger, so `--open` cannot hide it; JSON carries
  `terminated_ungraded_count` and a per-row flag), and the bridge Stop gate
  writes the same line to stderr on every stop while it holds.
- Arming is per tier: `arm` / `disarm --note --tier bridge|coordinator`
  (default bridge; a 0.4.0 `arming.json` reads unchanged with the coordinator
  armed). `--tier lane` is refused with the reason — lanes are always graded.
- A blocking failure under a disarmed coordinator gate records the new
  verdict `advisory` — never `verified` with a note — renders in full as
  context, never blocks, and never strikes the abandonment ladder. The board
  and the HTML report show `advisory`; telemetry classes it `advisory`
  (derivation_version 4, an eleventh outcome class, its own rate, never
  counted as success).
- Phase succession: a spec check may declare `succeeded_by: "<check-id>"`
  (same spec, same tier, no cycles). Once the successor has passed in a
  persisted checker run of this session — or in the very run being graded —
  the predecessor is listed as `[retired] <id>: retired (succeeded by ...)`
  and never counted as failed. `fleetproof phase advance --retire <id>
  --note "..."` retires checks by hand in `.fleetproof/phase.json`, outside
  the hashed spec and checks tree, so the swap trips no drift pin;
  `phase status` and `phase reset` complete the verb. Every verdict's
  `output.json` carries `retired`.
- An invented tier is rejected with the legal tiers named, from every surface
  that validates one by value.

### Grader integrity

- `dispatch intent --preflight` (and `dispatch new --manifest --preflight`)
  runs every manifest check now, from the project root, with the gate's
  runner and identity environment, and prints per check the exact command
  line, expectation, exit code, PASS/FAIL, and a redacted output tail.
  Nothing is recorded; the exit is 0 whatever the checks did; a malformed
  check is an error here, not a skipped hook line.
- The grader control ledger: `fleetproof check control <id> --pass-sample
  <file> [--fail-sample <file>] --provenance captured|authored [--manifest]`
  records, under `.fleetproof/controls/<id>.json`, sample hashes, the pass
  sample's provenance, who and when, and the observed exit per direction; the
  sample reaches the check as `FLEETPROOF_CONTROL_SAMPLE`. `dispatch intent`
  and `dispatch new --manifest` warn once per blocking manifest check with no
  control or an authored-only pass sample; `--strict-controls` refuses with
  nothing written.
- Manifest checks carry the full spec-check shape — `expect` of every kind,
  `block`, `owner`, `redact`, `description` — parsed by the spec's own
  parser (`cmd`, with `run` as an alias; `tier` and `succeeded_by` refused).
  A bare `{"id", "cmd"}` entry parses exactly as before. `owner` now governs
  the checks that actually grade lanes.
- Every gate-run check sees `FLEETPROOF_RUN_ID`, `FLEETPROOF_AGENT_TYPE`,
  `FLEETPROOF_TIER`, and `FLEETPROOF_SESSION_ID` in its environment, each
  the empty string when unknown and never absent, so one spec check can branch
  per lane.
- `fleetproof init --library` installs three stdlib-only, argv-form graders
  under `.fleetproof/checks/lib/` — `worktree_landed.py`,
  `py_tests_pinned.py`, `http_json_field.py` — each headed by what it
  asserts, its source of truth, its exit codes, and how to capture a control
  sample; all honour `FLEETPROOF_CONTROL_SAMPLE`; `fixtures/` ships one
  example emission with a README saying to replace it with a captured one.
  The three suggested `checks.json` entries are printed after install.
  Existing files are kept unless `--force`.
- The README gains a *Grader integrity* section: a wrong blocking check is
  not neutral, and the four rules — cite the source of truth by `file:line`,
  quote identifiers from code, capture positive controls, escalate when the
  prompt contradicts the code.

### Audit record

- Every checker run a gate orders stamps `"arming": {"tier", "state",
  "note"}` into its `output.json` (`n/a` for the bare CLI); `fleetproof show`
  renders it, and the text verdict prints it when the gate was advisory.
- Every report is kept (`reports/NNN.json`; `report.json` stays the latest)
  and every block message the agent received is kept verbatim
  (`blocks/NNN.txt`, with `NNN.meta.json` naming the checker run).
- `telemetry summary` says when telemetry is off: with no well-formed
  `telemetry_era` the first line is `telemetry is OFF for this repo: ...`;
  with an era set and every dispatch in a window predating it, the window
  says so with the date; `severity: n/a (0 classifiable)` replaces
  `no failures` over zero classifiable dispatches. The JSON form carries
  `telemetry_era`.

### Ergonomics

- `dispatch new` warns on stderr when it records a session-less dispatch (no
  `--session-id` and no `FLEETPROOF_SESSION_ID`), because a hook stop can
  never adopt it; the new `--session-id` flag stamps one.
- The README documents that the stalled-dispatch sweep and the abandonment
  ladder are unaware of each other: a lane mid-ladder reads as stalled to the
  bridge's sweep.
- The README's *Operating a fleet (v0.5)* section covers all of the above,
  and the telemetry section documents `telemetry_era`, the off-state, and
  `run_context` defaulting to `production`.

## 0.4.0 — 2026-08-24

Field-hardening release. Every item below was observed in a field deployment
on Windows, running a multi-lane fleet under the gate — collective credit to
that deployment's operators.

### Trust hardening

- An unpaired SubagentStop is recorded as an orphan sighting, never
  manufactured into a graded dispatch — harness helper agents can no longer
  produce phantom `verified` verdicts.
- `fleet`, `check`, and `report` warn when the current session has no
  hook-produced record, so a mid-session install can no longer impersonate a
  live gate.
- An intent miss is loud: a spawn that matches no sidecar gets a stderr line
  naming it, and sidecars can match by `--role` as well as by exact filename.
- A defaulted tier says so: `tier_source` distinguishes `defaulted` from
  `declared`, and the board renders it as `lane?`, not a bare tier.
- Pin drift no longer blocks a tier with nothing runnable to grade — the stop
  is allowed ungraded, drift noted.
- A verdict over zero blocking checks renders `ADVISORY - 0 blocking; ...`,
  never the bare word PASS.
- Checks execute from the resolved project root regardless of the hook's cwd,
  and every rendered verdict prints the cwd it ran from.
- Block messages lead with `[FLEETPROOF GATE — AUTOMATED BLOCK, NOT A USER
  MESSAGE]`, carry the full breakdown in the reason payload, and name the
  exits — fix and stop again, or report the check unsatisfiable from your
  seat.

### Fleet-grade

- Tree-hash pinning: every dispatch pins the check scripts under
  `.fleetproof/checks/` alongside `checks.json` — the graders are part of the
  spec, and the drift block names which one changed.
- Arming: `fleetproof arm` / `disarm --note` set the bridge Stop gate to
  blocking or advisory outside the hashed spec; disarm requires a reason,
  records who set it, and is echoed on the board.
- Escalation ladder: the third contradicted stop terminates a dispatch as
  `abandoned` instead of blocking forever, and `dispatch park` closes a wedged
  dispatch on purpose with the reason kept.
- Check ownership: `"owner"` declares which seat can satisfy a check; at any
  other tier it grades advisory instead of wedging an agent that cannot fix
  it.
- Argv-form runner: `run` (and manifest `cmd`) may be a JSON array executed
  with no shell — no cmd.exe quoting hazards on Windows.
- Redaction before persistence: builtin patterns (connection-string keys,
  SAS-style signatures, private-key blocks, JWTs, bearer tokens) plus
  per-check `redact` patterns run over check output before it is written.
- CLI dispatches are joinable: `dispatch new --agent-name` records the spawn
  name, and the first matching SubagentStop adopts the record instead of
  forking the ledger.
- Env-snapshot denylist extended (`*_KEY`, `SAS`, `PWD`, `PFX`, `DSN`,
  `CERT`, and connection-string names with or without underscores), and
  BOM-prefixed JSON and prompt files read cleanly on Windows.
