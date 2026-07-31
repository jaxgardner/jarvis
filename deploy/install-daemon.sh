#!/bin/bash
# Install the Jarvis LaunchDaemons. Run with sudo:
#
#     sudo ./deploy/install-daemon.sh
#
# Safe to re-run.
#
# Two things this has to get right, both learned the hard way:
#
#   1. `launchctl bootout` returns before the service is actually gone. Calling
#      bootstrap immediately races the still-registered label and fails with
#      "Bootstrap failed: 5: Input/output error". So we poll until the label
#      really disappears before bootstrapping.
#
#   2. One failing label must not abort the run. With `set -e`, a failed
#      bootstrap on the first daemon left the machine with that service booted
#      out and the remaining daemons never installed — strictly worse than
#      before the script ran. Each label is now independent and the script
#      reports a summary.

set -uo pipefail   # deliberately NOT -e; see note 2 above

LABELS=(com.jarvis.api com.jarvis.scheduler com.jarvis.worker)
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="/Users/jaxongardner/Library/Logs/jarvis"

if [ "$EUID" -ne 0 ]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

install -d -o jaxongardner -g staff -m 755 "$LOGDIR"

# Wait for a label to be fully unregistered. Returns 1 if it never clears.
wait_gone() {
    local label=$1
    for _ in $(seq 1 60); do
        launchctl print "system/${label}" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    return 1
}

FAILED=()

for LABEL in "${LABELS[@]}"; do
    SRC="${HERE}/${LABEL}.plist"
    DEST="/Library/LaunchDaemons/${LABEL}.plist"

    if [ ! -f "$SRC" ]; then
        echo "!! ${LABEL}: no plist at ${SRC}"
        FAILED+=("$LABEL")
        continue
    fi

    launchctl bootout "system/${LABEL}" 2>/dev/null
    if ! wait_gone "$LABEL"; then
        echo "!! ${LABEL}: still loaded after bootout; skipping to avoid a race"
        FAILED+=("$LABEL")
        continue
    fi

    # launchd refuses to load a plist that isn't root-owned.
    install -o root -g wheel -m 644 "$SRC" "$DEST"

    if launchctl bootstrap system "$DEST" 2>/tmp/jarvis-bootstrap.err; then
        launchctl enable "system/${LABEL}"
        echo "ok  ${LABEL}"
    else
        echo "!! ${LABEL}: $(cat /tmp/jarvis-bootstrap.err)"
        FAILED+=("$LABEL")
    fi
done

echo
echo "state:"
for LABEL in "${LABELS[@]}"; do
    STATE=$(launchctl print "system/${LABEL}" 2>/dev/null \
              | awk '/^\tstate = /{print $3; exit}')
    printf '  %-24s %s\n' "$LABEL" "${STATE:-not loaded}"
done

if [ ${#FAILED[@]} -gt 0 ]; then
    echo
    echo "FAILED: ${FAILED[*]}"
    echo "logs:   tail -20 ${LOGDIR}/*.err.log"
    exit 1
fi

cat <<EOF

verify:  curl -s http://127.0.0.1:8000/health
logs:    tail -f ${LOGDIR}/api.err.log ${LOGDIR}/scheduler.err.log ${LOGDIR}/worker.err.log
stop:    sudo launchctl bootout system/com.jarvis.api
EOF
