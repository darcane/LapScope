# TODO / backlog

Open items for going public and beyond. **Workflow:** pick one item, cut a
`feat/…` or `fix/…` branch, open a PR, get CI green, merge. Once the repo is on
GitHub these become issues; this file is the pre-GitHub backlog. Prune items when
they land and move any lasting behavioral knowledge into
[AGENTS.md](AGENTS.md) rather than leaving it here.

Rough priority: **Release blockers → Distribution → Settings/Car DB → CI/Docs →
Ideas.**

---

## Release blockers (do before announcing publicly)

- **LICENSE.** ✅ **MIT**, `LICENSE` file added; the README now has a `## License`
  line and a Third-party assets section crediting Rajdhani (OFL) and uPlot (MIT).
- **Public README rewrite.** ✅ Rewritten public-facing: hero block (logo + tagline
  + badges + animated `hero.gif`), condensed features with real screenshots, exe-first
  install, in-game Data Out table front and centre (with a settings screenshot), brief
  troubleshooting with the deep material pushed to the Wiki.
- **Screenshots + GIFs.** ✅ Done — all wired into the README:
  - `hero.gif` — live dashboard in motion (RACE MODE).
  - `analysis-compare.png` — real Koenigsegg CCGT @ Hokubu Track, 12-lap A/B compare.
  - `track-map.png` (2D speed) + `track-map-3d.png` (3D, drag-to-rotate, contact ✦).
  - `session-list.png` (12-lap list with 💥/⏪ flags) + `session-sidebar.png` (class/PI +
    drivetrain + track-type + conditions ribbons).
  - `fh6-settings.png` — the in-game FH6 Data Out settings screen.
  Analysis/session shots are from real recorded sessions; the hero was captured live.
- **CI on pull requests** (see Testing & CI) — required before opening the repo
  to outside PRs.
- **Repo hygiene files:** ✅ Added `CONTRIBUTING.md` (workflow + branch-protection
  rules + squash-merge), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), a PR
  template, and issue forms (bug / feature / "unrecognized event" capture) with
  an `ISSUE_TEMPLATE/config.yml`. README's Contributing section links them.

## Distribution & packaging (make it plug & play)

✅ Decided: ship **both** — a single-click `.exe` as the headline path for normal
users, Docker documented for power/cross-platform users.

- ✅ **Windows build (PyInstaller).** Shipped as an **onedir** build (fewer AV
  heuristics than onefile): [run_desktop.py](run_desktop.py) entry +
  [LapScope.spec](LapScope.spec) bundle `app/` + static assets + fonts +
  `car_ordinals.json`; the exe binds UDP 9999, serves `127.0.0.1:8000`, and
  auto-opens the browser. DB location: `%LOCALAPPDATA%\LapScope`.
- ✅ **GitHub Releases pipeline.** [.github/workflows/release.yml](.github/workflows/release.yml)
  builds the exe on a `v*` tag and attaches a zip + SHA256 `checksums.txt` to a
  Release, with a VirusTotal note in the body (transparency vs. SmartScreen/AV
  false positives).
- **SmartScreen / antivirus trust.** Unsigned PyInstaller binaries get flagged.
  Onedir is already shipped (fewer heuristics than onefile) and checksums are in
  the release notes. Remaining: evaluate Azure Trusted Signing or SignPath (free
  for OSS) for code signing, and publish reproducible-build instructions.
- **Version / update check.** Exe users don't get `git pull`. Add a lightweight
  "newer version available" notice (checks the GitHub Releases API, dismissible,
  no auto-download). `app.__version__` is already stamped from the tag at build
  time — the check can compare against it.
- ✅ **Keep the Docker path** for advanced/cross-platform users; both are now
  documented in the README (exe for normal users, Docker for power users). Note:
  a native exe also sidesteps the Docker IPv6-proxy port bug and one layer of
  UWP-loopback pain, so it is arguably *more* reliable for the common case.

## Settings page (user preferences)

Today a couple of preferences are ad-hoc: the live dashboard has an mph/km/h
toggle in `localStorage` (`fc_mph`) but the analysis page is hard-coded to km/h.
Consolidate into one Settings page and apply preferences across both pages.

- **Units:** speed (mph / km/h), tire temp (°F / °C — packet is Fahrenheit),
  distance (mi / km). Make the analysis charts honor the choice too (currently
  km/h only).
- **Free-roam map:** optional toggle to draw the live track map in free roam,
  not only race mode.
- **Analysis map layers:** show/hide overlays — contact spikes, 2D/3D default,
  color-by (speed vs. slip) default.
- **Persistence:** decide `localStorage` (per-browser, zero backend) vs. a
  server-side settings row (shared across devices). Start with `localStorage`.
- **Theme / accent** (optional, low priority).

## Car database (community ordinal list)

`app/car_ordinals.json` (638 entries) maps car name → ordinal and is baked into
the build. Playground Games ships new cars → new ordinals, so it goes stale.

- **Auto-refresh mechanism.** Fetch an updated community list from a canonical
  source (a maintained file in this repo / a pinned gist) on demand or on a
  schedule, with the bundled copy as offline fallback and user overrides winning.
- **Surface unknown cars.** When an ordinal isn't in the list, make it obvious in
  the UI ("Car #1234 — help us name it") and provide a one-click "report unknown
  car" that pre-fills a GitHub issue, so the community list self-heals.
- Interim: the maintainer triggers refreshes manually for the first months.

## Testing & CI

- ✅ **Automated test harness** (`tests/`): `pytest` covers the packet
  round-trip / `FIELDS`↔`_STRUCT` invariant and the full AGENTS.md
  event-detection matrix, driven headlessly through `SessionTracker` by a
  fake-socket harness that reuses the simulator's scenarios — no container, no
  game, no wall-clock wait (~2 s total).
- ✅ **CI on PRs** (`.github/workflows/ci.yml`): ruff + the `packet.py`
  self-test + pytest on every push and pull request.
- ✅ **Build the exe on version tags** — [.github/workflows/release.yml](.github/workflows/release.yml)
  builds and publishes the exe on `v*` tags only.
- **Branch protection** on `main`: require a PR + review, the passing `test` CI
  check, up-to-date branches, and no direct pushes; allow **squash-merge only**.
  Rules documented in [CONTRIBUTING.md](CONTRIBUTING.md); apply them in the GitHub
  repo settings (Settings → Branches / General — see the checklist handed over
  when this landed).

## Docs

- **Wiki** for advanced material (packet internals, event-detection deep dive,
  capture-diagnosis workflow, the FH6 behavioral facts). README links to it.
- Keep README basic: what it is, features, install, in-game setup, a little
  troubleshooting.

## Contact & lap-invalidation detection (accuracy)

The `contact` 💥 flag is a ground-plane accel spike (`IMPACT_ACCEL` = 45 m/s²).
Real cross-country data (session 55) shows it is both too eager and too blind:

- **False positives: hard jump-landings flagged as contact.** Cross-country is
  full of big jumps; landing hard trips the ground-plane threshold even though
  it isn't a wall/car hit. On session 55 roughly half of ~12 contact markers
  were landings, not the AI bumps they looked like. Idea: detect the airborne
  phase preceding a spike (all wheels unloaded / suspension at full droop / no
  tire load for several frames) and classify the following spike as a landing,
  not contact.
- **Manual session editing.** Let the user curate a recording — dismiss/ignore
  specific contact markers on the map, re-tag, or trim — for when the auto
  detector is wrong. Pairs with the analysis-map layer toggles in the settings
  work.
- **False negatives: light wall scrapes (Rivals).** In Rivals the faintest wall
  touch invalidates the lap in-game, but the lateral force is far below
  `IMPACT_ACCEL`, so the flag misses it — and the packet has no lap-invalidated
  field to read directly. Would need a subtler signature (a small sharp lateral
  jolt + speed drop with no brake input, or a sustained scrape). Hard; tracked
  as a known gap.

## Ideas / nice-to-have (unscheduled)

- Auto-suggest track type from geometry instead of a manual dropdown.
- Export a session/lap (CSV or image of the racing line) for sharing.
- Multiple-session overlay (more than A/B) on the analysis map.

---

## In flight (carried over)

- _(none)_ — all known FH6 event types are recognized. Cross-country was the
  last unconfirmed one: **verified 2026-07-05 on a real race (session 55)** — it
  uses the touge signature (gridded, `CurrentLap` counts, stream cut dead at the
  line at 67 m/s) and is already recovered by the existing gridded cut-dead path,
  no new code. Capture mode (`LS_KEEP_DISCARDED`) is now **off**.

## Known accepted trade-offs (not bugs; revisit only with new signal data)

- A fresh-boot free-roam session starting at `DistanceTraveled` 0 that loops over
  its own start point without teleporting can produce one false geometric lap
  (AGENTS.md, WTA section).
- A mid-run quit at speed is indistinguishable from a stream-cut point-to-point
  finish; such runs carry the `cutoff` 🏁 flag rather than being dropped.
