"""LapScope - Forza Horizon 6 telemetry dashboard."""

import os

# Overwritten with the git tag at release-build time (see release.yml). "0.0.0"
# marks an unversioned dev/source run.
__version__ = "0.0.0"

# Airplane mode. LapScope makes three outbound calls, all optional and all
# fail-soft: the update check (browser -> GitHub Releases) and the two
# reference-list refreshes (server -> raw.githubusercontent.com). The Settings
# toggle covers a person at a browser; this covers the install - a headless
# container, an air-gapped box, or anyone who would rather the process simply
# never reached the network. Read at import time, like LS_KEEP_DISCARDED, so it
# is process-global and needs a restart; reported on /api/version so the
# frontend stops asking rather than being refused (issue #76).
OFFLINE = os.environ.get("LS_OFFLINE", "0").lower() not in ("", "0", "false", "no")
