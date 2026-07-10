# Contributing to LapScope

Thanks for wanting to help! LapScope is a self-hosted Forza Horizon 6 telemetry
dashboard and lap analyzer. Issues and pull requests are welcome — especially
**captures of event types that aren't detected yet** and **car-ordinal
additions**.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Where to start

- **Found a bug?** Open a [bug report](../../issues/new?template=bug_report.yml).
- **An event type isn't recorded correctly?** This is the most valuable kind of
  report — use the
  [unrecognized event capture](../../issues/new?template=unrecognized_event.yml)
  template and follow the capture workflow it links to.
- **Have an idea?** Open a
  [feature request](../../issues/new?template=feature_request.yml).
- **Want to code?** Comment on an existing issue so we don't duplicate work, then
  follow the workflow below.

## Project layout & docs

Read these before making non-trivial changes:

- **[AGENTS.md](AGENTS.md)** — dev workflow, the hard-won FH6 packet facts, and
  the event-detection model. The *why* behind the recorder.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — what lives where: file
  responsibilities, data flow, DB schema, API surface, and the cross-file
  invariants that must stay in sync.
- **[README.md](README.md)** — user-facing setup, in-game settings, and quick
  troubleshooting.
- **The [Wiki](../../wiki)** — user-facing deep dives (troubleshooting, the
  capture workflow, packet internals, event detection). Authored in
  [docs/wiki/](docs/wiki/) and mirrored to the GitHub wiki on merge —
  `docs/wiki/` is the source of truth, so wiki changes go through normal PRs;
  never edit the wiki directly on GitHub.

## Development setup

You don't need the game or a real Xbox to work on most of LapScope — a built-in
simulator replays realistic telemetry.

```bash
# 1. Install runtime + dev tooling
pip install -r requirements.txt -r requirements-dev.txt

# 2. Run the app (Docker is the reference environment)
docker compose up --build -d          # http://localhost:8000

# 3. Feed it synthetic telemetry (no game needed)
python tools/simulator.py --race 3 --duration 200
```

> Static files are **baked into the image** — any change under `app/` needs a
> `docker compose build` + restart to take effect. There is no bind mount for
> code.

## Making a change

We use a **branch → PR → squash-merge** workflow. `main` is protected; nothing
lands on it except a green, reviewed PR.

1. **Cut a branch off `main`.** Name it `feat/<short-desc>` for features or
   `fix/<short-desc>` for fixes.
2. **Make your change**, keeping the docs in step:
   - a detection change usually touches the AGENTS.md model section **and** the
     wiki's Event-Detection page,
   - a new endpoint/table belongs in ARCHITECTURE.md,
   - a new user-visible feature belongs in the README,
   - changed packet facts, troubleshooting steps, or capture workflow update
     the matching page under `docs/wiki/` (published to the GitHub wiki on
     merge).
3. **Add or update tests.** New detection behavior needs a scenario in
   `tools/simulator.py` and a matching headless assertion in
   `tests/test_scenarios.py`.
4. **Run the checks locally** (CI runs the exact same ones):

   ```bash
   ruff check .
   python app/telemetry/packet.py     # packet round-trip self-test
   pytest -q
   ```

5. **Open a pull request** against `main` and fill in the template. Keep PRs
   focused — one logical change per PR is much easier to review.

### Commit & PR style

- Write imperative, prefixed commit subjects matching the existing history:
  `feat:`, `fix:`, `docs:`, `test:`, `chore:` (e.g.
  `fix: don't crash-exit when the UDP telemetry port is busy`).
- Because we **squash-merge**, the *PR title* becomes the commit on `main` — make
  it a good one-line summary in the same style.
- Don't force-push to `main` (it's protected anyway). Never push directly to
  `main`.

## Branch protection rules (enforced on `main`)

These are enforced in the GitHub repo settings; contributors just need to know
what they imply:

- **No direct pushes to `main`** — all changes go through a pull request (rules
  apply to admins too).
- **A pull request is required**, with all review conversations resolved before
  merge. Required approvals are currently **0** (LapScope is solo-maintained, and
  GitHub won't let you approve your own PR); this will move to **≥1** once there
  are other reviewers.
- **Status checks must pass** before merging — the required check is **`test`**
  (the CI workflow: `ruff`, the `packet.py` self-test, and `pytest`).
- **Branches must be up to date with `main`** before merging (so CI runs against
  the final merged state), and **linear history** is required.
- **Squash and merge is the only allowed merge method** — merge commits and
  rebase merges are disabled, so `main` stays one commit per PR. Head branches
  are auto-deleted after merge.

## Reporting security issues

Please **do not** open a public issue for a security problem. Report it privately
via GitHub's [security advisory form](../../security/advisories/new) instead.

---

LapScope is an unofficial, fan-made tool and is not affiliated with or endorsed
by Playground Games, Turn 10, or Microsoft.
