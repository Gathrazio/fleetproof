# Changelog

Releases before 0.4.0 predate this file; their stories are in the README's
version-marked sections and the git history.

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
