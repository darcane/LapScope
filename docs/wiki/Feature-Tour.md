# Feature Tour

Everything LapScope does, one screen at a time. The
[README](https://github.com/darcane/LapScope#readme) has the one-line version;
every screenshot here is regenerated from real Forza Horizon 6 drives by
[`tools/capture_media.py`](https://github.com/darcane/LapScope/blob/main/docs/MEDIA.md).

## Live dashboard

The Live page is one **cockpit**, laid out like a car's instrument cluster: a
tach with shift lights and the gear, speed, power and boost, a friction circle
(G-forces — an impact past full scale pins to the rim and flashes red), per-tire
grip (green = grip, red = sliding) with an understeer/oversteer indicator,
throttle/brake/steering bars, and a lap timer with a **live delta vs. your
session-best lap**. Below it, a **track map that draws itself as you drive** and
a 12-second input trace. The map survives a mid-race pause (photo mode included)
and marks contacts and jumps as they happen. The whole scene scales to fit the
window and re-stacks itself on a portrait screen.

The cockpit draws with WebGL, which every current desktop browser has. If yours
can't start it, the page says so and links to the **Classic** dashboard (the
link at the end of the header), the previous separate-panels layout, which is
also there if you simply prefer it.

An optional **raw telemetry panel** (off by default, ⚙ Settings) shows every
packet field live in game-native units, with a ⏸ Hold button to freeze the values
mid-drive.

![LapScope live dashboard](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/hero.gif)

**When the game pauses.** FH6 stops sending telemetry the moment it loses focus,
so alt-tabbing to a second screen just shows an amber **paused** chip instead of
covering the page:

![The dashboard's amber paused chip after the game stops sending](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/paused-chip.png)

## Lap analysis

Every timed drive is stored. Browse sessions, see lap times, and **compare laps**
with distance-aligned charts: time delta, speed, inputs, steering, and slip.
Overlay up to 6 laps, even from different sessions, each in its own color.
**Drag-zoom any chart** and every chart follows while the map **highlights exactly
that stretch of road** (double-click to reset). Turn on **raw data at cursor** in
Settings and hovering any chart reads every channel of every compared lap at that
exact spot.

![Analysis lap comparison](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/analysis-compare.png)

## Track map, 2D and 3D

Your **racing line colored by speed or tire slip**. The 3D view uses the packet's
elevation and is drag-to-rotate; ✦ markers are detected contacts.

![Track map in 2D, colored by speed](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/track-map.png)

![Track map in 3D, drag-to-rotate](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/track-map-3d-animated.gif)

## Jumps, drawn as jumps

Airborne stretches are detected from suspension + tire load and drawn as an
explicit **takeoff ○ → touchdown ▸ flight line** on both maps. A hard landing gets
an amber glow + impact ring instead of being mistaken for a crash; a red ✦ is a
real contact. How the landing classifier works:
[Event Detection](Event-Detection).

![Jump glyphs on the 3D track map](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/track-map-3d-jumps.png)

## Dirty-lap flags, and the final say

The packet has no "lap invalidated" field, so LapScope infers it: ⏪ **rewind**
(the lap clock ran backwards) and 💥 **contact** (a G-spike with the shape of an
impact, beyond what tires can generate). Rewound stretches are trimmed from the
charts and map.

![Lap list with dirty-lap flags](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/session-list.png)

Detection wrong on a recording? Right-click a contact marker on the map to dismiss
it, edit a lap's flags, or exclude junk laps from the bests. All of it is
reversible (**Reset edits**) and kept across a **Reprocess**.

## Sessions, routes and cars

Every drive gets Forza-colored class/PI ribbons (including the new **R class**),
a drivetrain badge, its car name (a bundled community list plus your own
overrides), auto-detected wet conditions, and a track-type tag that fills itself
in: road / dirt / cross-country / WTC, read from the telemetry. Your own tag always
wins, and can be applied to a whole route in one click.

The game never sends route names, so courses are fingerprinted from lap geometry.
LapScope ships a catalogue of the official courses, so most routes name themselves
on your first completed lap. Anything it doesn't recognize (a blueprint, a custom
route, a course added after your build) you name once, and every past and future
session on it picks it up. Your name always wins over the catalogue.

![Session list with ribbons](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/session-sidebar.png)

The browse bar searches, filters and sorts the list; the ⋯ menu holds the
session's actions, each with a line saying what it reaches:

![Browse bar and the session header menu](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/browse-bar-menu.png)

## Automatic event detection

Races, Rivals, sprints, drags, touge, cross-country and **World Time Attack** are
each detected and timed correctly, even though the game never labels them (it
doesn't even count the last lap of a race). Free-roam cruising is discarded
automatically, so the list stays clean. The rules, and the real captures that
proved each one: [Event Detection](Event-Detection).

## Take it with you

- **CSV export:** any lap (⬇ in the lap table) or the whole session (**Export
  CSV…**) as full-rate telemetry for spreadsheets or MoTeC-style tools. Each
  download asks which columns: the 19 curated channels, or **every packet field**
  (all 107, tire temps and per-wheel slip and suspension included, exactly as the
  game sends them, about 5× the file size).
- **Import CSV** (above the session list) closes the loop: send a friend your
  lap's CSV and they can open it like a recording, with laps, charts and map
  rebuilt from the file.
- **Save PNG** turns the track map as drawn (colors, 2D/3D, A + B overlay) into a
  shareable image with a title / car / lap-time caption.

![CSV export column choice dialog](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/csv-export-choice.png)

## Settings and themes

A ⚙ **Settings** panel (top-right on both pages) switches units (**speed** km/h ↔
mph, **tire temp** °C ↔ °F, **distance** km ↔ mi, **power** kW / hp / PS, **boost**
psi / bar), picks an **accent theme**, and holds the map preferences (draw the live
map in free roam, show/hide contact markers, default 2D/3D view and color-by), the
raw-data toggles, privacy and storage. Preferences are saved in your browser.

![Settings panel](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/settings.png)

Six curated accent presets; every gauge, map and chart follows along. Here, the
live cockpit in Sunset:

![Live dashboard in the Sunset accent](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/accent-live-sunset.png)

## The Windows launcher

The exe opens a small control panel, not the dashboard: one line telling you what
the recorder is doing (*Waiting for telemetry*, *Recording — session 12*,
*Telemetry port blocked*), a live log, and four buttons: **Open Dashboard**,
**Stop**, **Restart**, **Open Data Folder**. Closing it stops the server cleanly
and saves the lap you were on.

![The LapScope launcher window](https://raw.githubusercontent.com/darcane/LapScope/main/docs/media/launcher-window.png)

## Accessibility

Every control is reachable by keyboard, and looping animations (including the
shift lights on the limiter) stop if your system asks for reduced motion.
