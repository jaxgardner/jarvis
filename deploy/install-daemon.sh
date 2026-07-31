#!/bin/bash
# Install the Jarvis API LaunchDaemon. Run with sudo:
#
#     sudo ./deploy/install-daemon.sh
#
# Re-running is safe — it unloads the old job before loading the new one.
set -euo pipefail

LABEL="com.jarvis.api"
SRC="$(cd "$(dirname "$0")" && pwd)/${LABEL}.plist"
DEST="/Library/LaunchDaemons/${LABEL}.plist"
LOGDIR="/Users/jaxongardner/Library/Logs/jarvis"

if [ "$EUID" -ne 0 ]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

# The daemon runs as jaxongardner, so the log dir must be writable by them.
install -d -o jaxongardner -g staff -m 755 "$LOGDIR"

# launchd refuses to load a plist that isn't root-owned.
install -o root -g wheel -m 644 "$SRC" "$DEST"

launchctl bootout system "$DEST" 2>/dev/null || true
launchctl bootstrap system "$DEST"
launchctl enable "system/${LABEL}"

echo "installed ${DEST}"
echo
echo "status:"
launchctl print "system/${LABEL}" 2>/dev/null \
    | grep -E "^\s+(state|pid|last exit code) " || true
echo
echo "verify:  curl -s http://127.0.0.1:8000/health"
echo "logs:    tail -f ${LOGDIR}/api.err.log"
echo "stop:    sudo launchctl bootout system/${LABEL}"
