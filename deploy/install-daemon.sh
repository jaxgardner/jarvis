#!/bin/bash
# Install the Jarvis LaunchDaemons. Run with sudo:
#
#     sudo ./deploy/install-daemon.sh
#
# Re-running is safe — each job is unloaded before the new plist is loaded.
set -euo pipefail

LABELS=(com.jarvis.api com.jarvis.scheduler com.jarvis.worker)
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="/Users/jaxongardner/Library/Logs/jarvis"

if [ "$EUID" -ne 0 ]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

# The daemons run as jaxongardner, so the log dir must be writable by them.
install -d -o jaxongardner -g staff -m 755 "$LOGDIR"

for LABEL in "${LABELS[@]}"; do
    SRC="${HERE}/${LABEL}.plist"
    DEST="/Library/LaunchDaemons/${LABEL}.plist"

    # launchd refuses to load a plist that isn't root-owned.
    install -o root -g wheel -m 644 "$SRC" "$DEST"

    launchctl bootout "system/${LABEL}" 2>/dev/null || true
    launchctl bootstrap system "$DEST"
    launchctl enable "system/${LABEL}"
    echo "installed ${DEST}"
done

echo
for LABEL in "${LABELS[@]}"; do
    printf '%s: ' "$LABEL"
    launchctl print "system/${LABEL}" 2>/dev/null \
        | grep -E "^\s+state = " | head -1 | tr -d '\t' || echo "(not loaded)"
done

cat <<EOF

verify:  curl -s http://127.0.0.1:8000/health
logs:    tail -f ${LOGDIR}/api.err.log ${LOGDIR}/scheduler.err.log
stop:    sudo launchctl bootout system/com.jarvis.api
         sudo launchctl bootout system/com.jarvis.scheduler
EOF
