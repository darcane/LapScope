# Configuration

LapScope runs with no configuration at all. This page is for when you want to
change a port, move the database, run it offline, or put it behind a proxy.

## Environment variables

| Env var              | Default          | Meaning                                    |
|----------------------|------------------|--------------------------------------------|
| `TELEMETRY_UDP_PORT` | `9999`           | UDP port the listener binds                |
| `DATA_DIR`           | `/app/data`      | Where `telemetry.db` is written            |
| `LS_KEEP_DISCARDED`  | `0`              | `1` = keep sessions with no completed laps |
| `LS_CAR_LIST_URL`    | this repo's list | Where "Refresh car names" downloads from   |
| `LS_TRACK_LIST_URL`  | this repo's list | Where "Refresh tracks" downloads from      |
| `LS_OFFLINE`         | `0`              | `1` = never contact the internet ([below](#turning-the-online-requests-off)) |
| `LS_ALLOWED_HOSTS`   | *(empty)*        | Extra `Host` names to answer to ([below](#host-names)) |

With Docker, add them under `environment:` in `docker-compose.yml`
(`LS_KEEP_DISCARDED` is already passed through, so a repo-root `.env` file or the
host environment is enough for that one). On the exe, set them as Windows
environment variables before starting it.

`LS_KEEP_DISCARDED`, `LS_OFFLINE` and `LS_ALLOWED_HOSTS` are read once when the app
starts, so they have to be set before launch. On the exe that means closing the
LapScope window and opening it again: the **Restart** button restarts the
*server*, which is enough for `DATA_DIR` and `TELEMETRY_UDP_PORT` but not for these
three. The two list URLs point at this repo's `main` branch by default and only
exist so a fork can serve its own lists.

`LS_KEEP_DISCARDED=1` is the capture path for event types LapScope doesn't
recognize yet: see [Capturing an Unrecognized Event](Capturing-an-Unrecognized-Event).

## Turning the online requests off

LapScope makes three optional requests, all to GitHub, all fail-soft, none of them
more than once a day: the browser asks `api.github.com` whether there is a newer
release, and the server fetches the community car-name list and the official-route
catalogue from `raw.githubusercontent.com`. They are plain GETs for public files,
so the only thing they reveal is that some IP asked for one.

- **Settings → Privacy** turns all three off for that browser.
- **`LS_OFFLINE=1`** turns them off for the whole install (a headless container,
  an air-gapped box). The server then also refuses to serve the refreshes at all.

The bundled car and track lists keep working either way, and **Refresh now** stays
a manual button you can press whenever you like, unless `LS_OFFLINE` is set, in
which case nothing reaches out at all.

## Host names

LapScope answers to `localhost`, any `.local` name, and any IP address. Loopback,
the exe, and reaching a Docker host by its LAN IP all work untouched. Other
hostnames get a `400 Invalid host header`, which is what stops a web page you
happen to visit from pointing its own domain at your machine and driving the API
from there. If you front LapScope with a reverse proxy or reach it by some other
name, list those names in `LS_ALLOWED_HOSTS` (comma-separated).

## Network exposure (Docker)

The bundled `docker-compose.yml` publishes port 8000 on **every** interface, and
LapScope has no accounts or passwords: anyone who can reach that port can read
your sessions and delete them. That's fine on a home LAN; don't publish it to an
untrusted network or forward it through your router. To keep it on the machine
itself, change the port mapping to `"127.0.0.1:8000:8000"`. The Windows exe only
ever listens on `127.0.0.1`.

## Storage

Recordings are raw 324-byte packets, about **70 MB per hour of driving**. Deleting
a session from the Analysis page frees that space *inside* the database file for
future recordings, but the file itself never shrinks on its own. **Settings →
Storage → Compact now** hands the space back to the drive. It needs as much free
disk space as the database currently uses, and can't run while a session is
recording.

## Where things live

| | Windows exe | Docker |
|---|---|---|
| Database | `%LOCALAPPDATA%\LapScope\telemetry.db` | `./data/telemetry.db` (bind mount) |
| Log | `%LOCALAPPDATA%\LapScope\logs\lapscope.log` (rotated, 2 MB × 4) | `docker compose logs` |
| Dashboard | `http://127.0.0.1:8000` | `http://localhost:8000` |

The exe's log file is the one to attach to a bug report.
