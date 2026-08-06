#!/usr/bin/env bash
# Build and sign tccread.
#
# The signature must be STABLE across rebuilds, or macOS treats each build as
# a new binary and the Full Disk Access grant silently stops applying — the
# grant is keyed on the code signature, not the path. Ad-hoc signing (`-`)
# regenerates the identity every time, so a real signing identity is required
# if you rebuild often. Set TCCREAD_IDENTITY to use one.
set -euo pipefail

cd "$(dirname "$0")"
swiftc -O -o tccread main.swift

IDENTITY="${TCCREAD_IDENTITY:--}"
codesign --force --sign "$IDENTITY" tccread

if [ "$IDENTITY" = "-" ]; then
  echo "WARNING: ad-hoc signed. Re-granting Full Disk Access will be needed" >&2
  echo "         after every rebuild. Set TCCREAD_IDENTITY to avoid that."   >&2
fi

echo "built: $(pwd)/tccread"
