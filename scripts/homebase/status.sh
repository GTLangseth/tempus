#!/usr/bin/env bash
# One screen of home-base health. Runbook: docs/HOMEBASE.md.
set -uo pipefail
PORT="${TEMPUS_PORT:-8765}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

check() { # label, command...
  local label="$1"; shift
  if out="$("$@" 2>&1)"; then printf '  ✓ %-22s %s\n' "$label" "$(head -1 <<<"$out")"
  else printf '  ✗ %-22s %s\n' "$label" "$(head -1 <<<"$out")"; fi
}

echo "tempus home base — $(hostname -s)"
check "tailnet name"      sh -c "tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"Self\"][\"DNSName\"].rstrip(\".\"))'"
check "tailnet IP"        tailscale ip -4
check "tailscale serve"   sh -c "tailscale serve status | grep -m1 https"
check "remote login"      sh -c "nc -z 127.0.0.1 22 2>/dev/null && echo 'port 22 open' || (echo 'off'; false)"
check "sleep disabled"    sh -c "pmset -g | awk '\$1==\"sleep\"{print \"sleep \"\$2; exit (\$2!=0)}'"
check "docker engine"     sh -c "docker info --format '{{.OperatingSystem}}' 2>/dev/null || (echo 'not running'; false)"
check "database"          sh -c "cd '$REPO' && mise exec -- uv run tempus db status | head -3 | tr '\n' ' '"
check "launchd: serve"    sh -c "launchctl print gui/$(id -u)/com.tempus.serve 2>/dev/null | grep -m1 'state =' || (echo 'not loaded'; false)"
check "launchd: sync"     sh -c "launchctl print gui/$(id -u)/com.tempus.sync 2>/dev/null | grep -m1 'state =' || (echo 'not scheduled (opt-in: --with-sync)'; false)"
check "app responds"      sh -c "body=\$(curl -sf http://127.0.0.1:$PORT/api/cards) || { echo 'no answer on port $PORT'; exit 1; }; printf '%s' \"\$body\" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)), \"cards\")'"
check "claude code"       claude --version
