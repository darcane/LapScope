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
- **SmartScreen / antivirus trust.** ✅ Chose **SignPath Foundation** (free for
  OSS). The release workflow now has a signing step that stays **inert until the
  grant lands** (runs only when `SIGNPATH_API_TOKEN` is set), checksums the final
  (signed) zip, and templates the release notes on signed vs. unsigned. Build
  inputs are pinned for reproducibility (`requirements-build.lock` with hashes +
  a pinned Python patch), and reproducible-build / verify instructions are in
  [docs/BUILDING.md](docs/BUILDING.md). Remaining (owner, out of code): apply to
  the SignPath OSS program, then add the secret/variable in repo settings.
- **Version / update check.** ✅ Done. `app.__version__` is surfaced at
  `GET /api/version` (and in `/status` + the console banner); the frontend
  (`common.js`) compares it client-side against the latest GitHub Release and
  shows a dismissible `.update-banner` (24 h cached, fail-soft/offline-safe, no
  auto-download, skipped on the `0.0.0` dev build).
- ✅ **Keep the Docker path** for advanced/cross-platform users; both are now
  documented in the README (exe for normal users, Docker for power users). Note:
  a native exe also sidesteps the Docker IPv6-proxy port bug and one layer of
  UWP-loopback pain, so it is arguably *more* reliable for the common case.

## Settings page (user preferences)

✅ **Shipped** a shared ⚙ Settings panel (`app/static/js/settings.js`, opened
from the header on both pages), `localStorage`-backed under one `ls_settings`
key, migrating the old `fc_mph` / `fc_mapmode` keys. Delivered:

- ✅ **Units:** speed (mph / km/h), tire temp (°F / °C), distance (mi / km) —
  applied on both the live dashboard and the analysis charts/map/readouts.
- ✅ **Free-roam map:** toggle to draw the live track map in free roam, not only
  race mode.
- ✅ **Analysis map layers:** contact-spike show/hide, plus default 2D/3D and
  default color-by (speed vs. slip).
- ✅ **Persistence:** `localStorage` (per-browser, zero backend) — the right home
  since every conversion is display-time and never touches stored packets.

Remaining / follow-ups:

- **More unit choices:** power (kW / hp / PS), boost (psi / bar).
- **Theme / accent** (optional, low priority; the theme is already centralized in
  CSS custom props in `style.css`).

## Car database (community ordinal list)

`app/car_ordinals.json` (638 entries) maps car name → ordinal and is baked into
the build. Playground Games ships new cars → new ordinals, so it goes stale.

- ✅ **Auto-refresh mechanism.** `app/cars.py`: the canonical list is this repo's
  `app/car_ordinals.json` on `main` (raw URL, `LS_CAR_LIST_URL` overridable).
  `POST /api/cars/refresh` downloads + validates it into `DATA_DIR/car_ordinals.json`
  and hot-swaps the in-memory dict; layers are DB override > downloaded > bundled
  (offline fallback). The browser triggers a refresh at most once a day (same
  fail-soft pattern as the update banner) and Settings has a "Car list" row with
  count / last-updated / Refresh-now.
- ✅ **Surface unknown cars.** Sessions expose `car_known`; the analysis header
  shows an "unknown car — help name it" button (sidebar car line goes amber) that
  opens the rename prompt with a link to a pre-filled `unknown_car.yml` GitHub
  issue; the live-dashboard chip gets an amber name + tooltip. Merged name
  reports reach every install on its next daily refresh — no release needed.

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

- ✅ **Wiki** shipped, authored in-repo: pages live in [docs/wiki/](docs/wiki/)
  (Home, Troubleshooting, Capturing-an-Unrecognized-Event, FH6-Data-Out-Packet,
  Event-Detection, plus `_Sidebar`/`_Footer`) and are mirrored to the GitHub
  wiki by [.github/workflows/wiki.yml](.github/workflows/wiki.yml) on every
  merge to main. `docs/wiki/` is the source of truth — never edit the wiki
  directly. Convention recorded in AGENTS.md (documentation map) and
  CONTRIBUTING.md; the unrecognized-event issue template deep-links the
  capture page.
- ✅ README kept basic and polished: troubleshooting/how-it-works now deep-link
  the specific wiki pages, the jump-glyph screenshot and a Settings caption
  were added, and the simulator section notes it needs a source checkout.

## Contact & lap-invalidation detection (accuracy)

The `contact` 💥 flag is a ground-plane accel spike (`IMPACT_ACCEL` = 45 m/s²).
Real cross-country data (session 55) showed it was both too eager and too blind:

- ✅ **False positives: hard jump-landings flagged as contact.** Fixed: a spike
  while airborne (all wheels at full droop + zero tire force for ≥ 0.12 s) or
  within 0.35 s of touchdown is classified as a landing — no `contact` flag,
  amber (not red) marker on the analysis map, excluded from the Contacts count.
  Calibrated on session 55 (5 of its 12 spikes were landings) and verified to
  leave circuit sessions untouched; old recordings pick it up via Reprocess.
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
