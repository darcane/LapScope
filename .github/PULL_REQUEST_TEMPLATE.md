<!--
  Thanks for contributing to LapScope!
  Because we squash-merge, this PR's TITLE becomes the commit on main —
  make it a clear, imperative, prefixed summary, e.g.
  "fix: don't crash-exit when the UDP telemetry port is busy".
-->

## Summary

<!-- What does this PR do, and why? Link any related issue: "Closes #123". -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Event-detection change (recorder / `laps.py`)
- [ ] Docs only
- [ ] Refactor / chore

## How was it tested?

<!--
  Which simulator command(s) and/or tests cover this?
  e.g. `python tools/simulator.py --race 3 --duration 200`
-->

## Checklist

- [ ] Branched off `main` as `feat/…` or `fix/…`.
- [ ] `ruff check .` passes.
- [ ] `python app/telemetry/packet.py` (packet self-test) passes.
- [ ] `pytest -q` passes.
- [ ] Docs updated where relevant (AGENTS.md for detection changes,
      ARCHITECTURE.md for structural/schema/API changes, README.md for
      user-visible features).
- [ ] For a new detection scenario: added a `tools/simulator.py` flag **and** a
      matching assertion in `tests/test_scenarios.py`.
- [ ] Cross-file invariants kept in sync (see ARCHITECTURE.md "Cross-file
      invariants").
