# Changelog

Releases before 0.4.0 predate this file; their stories are in the README's
version-marked sections and the git history.

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
