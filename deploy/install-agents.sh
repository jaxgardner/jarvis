#!/bin/bash
# Install the Jarvis LaunchAgents. Run WITHOUT sudo:
#
#     ./deploy/install-agents.sh
#
# Safe to re-run.
#
# Everything else in this directory is a LaunchDaemon installed by
# install-daemon.sh. These two are agents, and the difference is not a style
# choice: they read chat.db and CallHistoryDB through helpers/tccread, whose
# Full Disk Access grant is per-user and lives in the GUI login session. A
# system daemon runs outside that session and does not inherit it, so the
# identical code fails under launchd with `tcc-denied` and no other clue.
#
# Running this with sudo would install them into root's agent domain, which is
# the same mistake wearing a different hat — hence the guard below.
#
# The bootout/bootstrap race and the per-label independence are handled the
# same way install-daemon.sh handles them, and for the same reasons; read the
# notes at the top of that script.

set -uo pipefail   # deliberately NOT -e; one bad label must not abort the run

LABELS=(
    com.jarvis.messages
    com.jarvis.calls
)
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="${HOME}/Library/Logs/jarvis"
AGENTDIR="${HOME}/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

if [ "$EUID" -eq 0 ]; then
    echo "do NOT run this as root: $0" >&2
    echo "these are per-user agents; under sudo they land in root's domain" >&2
    echo "and the Full Disk Access grant will not apply to them." >&2
    exit 1
fi

mkdir -p "$LOGDIR" "$AGENTDIR"

HELPER="${HERE}/../helpers/tccread/tccread"
if [ ! -x "$HELPER" ]; then
    echo "!! helpers/tccread/tccread is not built — run helpers/tccread/build.sh" >&2
    echo "   installing anyway; both importers will report stale until it is." >&2
fi

# Wait for a label to be fully unregistered. Returns 1 if it never clears.
wait_gone() {
    local label=$1
    for _ in $(seq 1 60); do
        launchctl print "${DOMAIN}/${label}" >/dev/null 2>&1 || return 0
        sleep 0.25
    done
    return 1
}

FAILED=()

for LABEL in "${LABELS[@]}"; do
    SRC="${HERE}/${LABEL}.plist"
    DEST="${AGENTDIR}/${LABEL}.plist"

    if [ ! -f "$SRC" ]; then
        echo "!! ${LABEL}: no plist at ${SRC}"
        FAILED+=("$LABEL")
        continue
    fi

    launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null
    if ! wait_gone "$LABEL"; then
        echo "!! ${LABEL}: still loaded after bootout; skipping to avoid a race"
        FAILED+=("$LABEL")
        continue
    fi

    install -m 644 "$SRC" "$DEST"

    if launchctl bootstrap "$DOMAIN" "$DEST" 2>/tmp/jarvis-agent-bootstrap.err; then
        launchctl enable "${DOMAIN}/${LABEL}"
        echo "ok  ${LABEL}"
    else
        echo "!! ${LABEL}: $(cat /tmp/jarvis-agent-bootstrap.err)"
        FAILED+=("$LABEL")
    fi
done

echo
echo "state:"
for LABEL in "${LABELS[@]}"; do
    # Same wording problem install-daemon.sh solved: these run briefly on an
    # interval and are "not running" almost always, which reads as a failure
    # if printed verbatim.
    STATE=$(launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null \
              | awk '/^\tstate = /{sub(/^\tstate = /, ""); print; exit}')
    case "$STATE" in
        running)       STATE="running" ;;
        "not running") STATE="loaded · idle until its next run" ;;
        "")            STATE="NOT LOADED" ;;
    esac
    printf '  %-24s %s\n' "$LABEL" "${STATE:-not loaded}"
done

if [ ${#FAILED[@]} -gt 0 ]; then
    echo
    echo "FAILED: ${FAILED[*]}"
    echo "logs:   tail -20 ${LOGDIR}/messages.err.log ${LOGDIR}/calls.err.log"
    exit 1
fi

cat <<EOF

verify:  curl -s http://127.0.0.1:8000/health -H "Authorization: Bearer \$JARVIS_TOKEN"
         (look for "messages" and "calls" in the ingest block, with
          last_run_at equal to last_ok_at)
logs:    tail -f ${LOGDIR}/messages.err.log ${LOGDIR}/calls.err.log
stop:    launchctl bootout ${DOMAIN}/com.jarvis.messages
EOF
