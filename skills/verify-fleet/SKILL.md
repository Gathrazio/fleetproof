---
name: verify-fleet
description: Run FleetProof's independent checker on demand and open the run report. Use when you want to verify — out of band — what an agent actually did, rather than trusting its "done" claim.
---

# Verify fleet

FleetProof verifies agent work in a process separate from the agent that did it.
You may **author** checks; you must not grade your own work against them.

## Run the independent checker

```
fleetproof check
```

This loads `.fleetproof/checks.json`, runs each declared check in its own
subprocess, records the verdict to `.fleetproof/runs/`, and exits non-zero if a
blocking check failed. It is the same command the Stop hook runs automatically.

If there is no check spec yet:

```
fleetproof init      # writes a starter .fleetproof/checks.json to edit
```

## Read what the fleet actually did

```
fleetproof list                 # recorded runs, newest first
fleetproof show <run-id>        # one run and its recorded invocations
fleetproof report               # self-contained fleetproof-report.html
```

The report marks each run **verified**, **contradicted** (claimed done, but a
blocking check failed), or **unverified** (no out-of-band checker verdict on record).

## The one rule

When asked whether work is done, do not answer from your own belief. Author or
update the checks, then let `fleetproof check` — a separate process — decide.
