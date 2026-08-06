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

# Prefer a real identity automatically. Ad-hoc signing regenerates the code
# identity on every build, which silently invalidates the Full Disk Access
# grant — macOS then records the binary as DENIED, and the failure reads as
# "the grant never worked" rather than "you rebuilt". That cost an hour once;
# defaulting to a Developer ID is what stops it costing another.
if [ -z "${TCCREAD_IDENTITY:-}" ]; then
  IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
               | awk -F'"' '/Developer ID Application/{print $2; exit}')
  : "${IDENTITY:=-}"
else
  IDENTITY="$TCCREAD_IDENTITY"
fi

codesign --force --sign "$IDENTITY" tccread

if [ "$IDENTITY" = "-" ]; then
  echo "WARNING: ad-hoc signed — no Developer ID Application identity found." >&2
  echo "         Full Disk Access must be re-granted after EVERY rebuild."    >&2
  echo "         Set TCCREAD_IDENTITY to a stable identity to avoid that."    >&2
else
  echo "signed with: $IDENTITY" >&2
fi

echo "built: $(pwd)/tccread"
