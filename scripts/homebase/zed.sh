#!/usr/bin/env bash
# Open the home base's checkout in Zed over SSH (Tailscale), from the MacBook. Runbook: docs/HOMEBASE.md.
# TEMPUS_HOMEBASE_HOST: an SSH host or ~/.ssh/config alias (default: homebase).
# TEMPUS_HOMEBASE_DIR: the checkout, relative to the mini's home (default: tempus, then Projects/tempus).
set -euo pipefail
HOST="${TEMPUS_HOMEBASE_HOST:-homebase}"
DIRS="${TEMPUS_HOMEBASE_DIR:-tempus Projects/tempus}"

command -v zed >/dev/null || { echo "zed CLI not found: in Zed, run 'cli: install' from the command palette" >&2; exit 1; }

# One round trip: find the checkout, print its absolute path and branch state.
if ! info="$(ssh -o ConnectTimeout=8 "$HOST" "for d in $DIRS; do
    if [ -d \"\$HOME/\$d/.git\" ]; then cd \"\$HOME/\$d\" && pwd && git status -sb | head -1; exit 0; fi
  done; exit 3")"; then
  echo "no tempus checkout found on $HOST (tried: $DIRS), or $HOST is unreachable." >&2
  echo "Is Tailscale up and Remote Login on? Set TEMPUS_HOMEBASE_HOST / TEMPUS_HOMEBASE_DIR to override." >&2
  exit 1
fi
path="$(sed -n 1p <<<"$info")"
echo "Opening $HOST:$path in Zed ($(sed -n 2p <<<"$info" | sed 's/^## //'))"
echo "This checkout serves the live app: edits take effect when com.tempus.serve restarts."
exec zed "ssh://$HOST$path"
