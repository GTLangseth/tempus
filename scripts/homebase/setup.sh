#!/usr/bin/env bash
# Provision this Mac as the tempus home base. Safe to re-run: every step checks first.
# Runbook: docs/HOMEBASE.md. Usage: mise run homebase:setup [-- --with-sync]
set -euo pipefail

WITH_SYNC=0
for arg in "$@"; do
  case "$arg" in
    --with-sync) WITH_SYNC=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${TEMPUS_PORT:-8765}"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/tempus"
TODO=()

step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
todo() { printf '  ! %s\n' "$*"; TODO+=("$*"); }

[[ "$(uname)" == "Darwin" ]] || { echo "This script is for macOS." >&2; exit 1; }
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh (see docs/HOMEBASE.md step 0)" >&2; exit 1; }
BREW_PREFIX="$(brew --prefix)"

step "Asking for your password once (power settings, Remote Login, Tailscale daemon)"
sudo -v

# --- packages ---------------------------------------------------------------------------
step "Homebrew packages"
for pkg in tailscale colima docker docker-compose tmux gh; do
  if brew list --formula "$pkg" >/dev/null 2>&1; then ok "$pkg"; else brew install "$pkg"; ok "$pkg installed"; fi
done

# Let `docker compose` find Homebrew's compose plugin.
python3 - "$BREW_PREFIX/lib/docker/cli-plugins" <<'PY'
import json, pathlib, sys
cfg = pathlib.Path.home() / ".docker" / "config.json"
cfg.parent.mkdir(exist_ok=True)
data = json.loads(cfg.read_text()) if cfg.exists() else {}
dirs = data.setdefault("cliPluginsExtraDirs", [])
if sys.argv[1] not in dirs:
    dirs.append(sys.argv[1])
    cfg.write_text(json.dumps(data, indent=2))
PY
ok "docker compose plugin registered"

# --- always on --------------------------------------------------------------------------
step "Power: never sleep, restart after a power failure"
sudo pmset -a sleep 0 disksleep 0 autorestart 1 womp 1
ok "pmset applied (displays may still sleep)"

step "Remote Login (macOS SSH server, reached over the tailnet)"
if sudo systemsetup -getremotelogin 2>/dev/null | grep -qi ": on"; then
  ok "already on"
elif sudo systemsetup -setremotelogin on >/dev/null 2>&1; then
  ok "turned on"
else
  todo "Turn on System Settings → General → Sharing → Remote Login (Terminal lacks Full Disk Access to do it)"
fi

if fdesetup status 2>/dev/null | grep -q "On"; then
  todo "FileVault is on: after a power cut this Mac waits at the unlock screen until someone logs in (docs/HOMEBASE.md → Unattended restarts)"
fi

# --- tailscale ----------------------------------------------------------------------------
step "Tailscale (tailscaled as a system daemon: up before login, survives reboots)"
if sudo launchctl print system/com.tailscale.tailscaled >/dev/null 2>&1; then
  ok "tailscaled daemon installed"
else
  sudo "$BREW_PREFIX/bin/tailscaled" install-system-daemon
  ok "tailscaled daemon installed"
  sleep 3
fi
if tailscale status >/dev/null 2>&1; then
  ok "joined tailnet as $(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
else
  echo "  Opening Tailscale login (approve it in the browser link below)…"
  sudo tailscale up --operator="$USER"   # so later tailscale commands work without sudo
  ok "joined tailnet"
fi

# --- containers ---------------------------------------------------------------------------
step "Colima (headless Docker runtime), started at login"
if colima status >/dev/null 2>&1; then ok "colima running"; else brew services start colima; sleep 5; colima status >/dev/null 2>&1 || colima start; ok "colima started"; fi

# --- tempus -------------------------------------------------------------------------------
step "tempus checkout at $REPO"
cd "$REPO"
command -v mise >/dev/null || { echo "Install mise first (docs/HOMEBASE.md step 0)" >&2; exit 1; }
mise trust --quiet "$REPO" >/dev/null 2>&1 || true
mise install
test -f .env || cp .env.example .env
for key in FRED_API_KEY TYPESAFE_API_KEY; do
  if grep -Eq "^${key}=.+" .env; then ok "$key set"; else todo "Add $key to $REPO/.env"; fi
done
mise exec -- uv sync --quiet
mise run db:up
mise exec -- uv run tempus db init
ok "database ready"

UV="$(mise which uv)"
ENV_PATH="$(dirname "$UV"):$BREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin"
mkdir -p "$AGENTS" "$LOGS"

write_agent() { # label, interval-or-keepalive, args...
  local label="$1" mode="$2"; shift 2
  local plist="$AGENTS/$label.plist" args=""
  for a in "$@"; do args+="<string>$a</string>"; done
  local schedule
  if [[ "$mode" == keepalive ]]; then
    schedule="<key>KeepAlive</key><true/>"
  else
    schedule="<key>StartInterval</key><integer>$mode</integer>"
  fi
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$ENV_PATH</string></dict>
  <key>RunAtLoad</key><true/>
  $schedule
  <key>StandardOutPath</key><string>$LOGS/$label.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$label.log</string>
</dict></plist>
PLIST
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
  ok "$label loaded (logs: $LOGS/$label.log)"
}

step "launchd: keep tempus serve running (localhost:$PORT only)"
write_agent com.tempus.serve keepalive "$UV" run tempus serve --host 127.0.0.1 --port "$PORT"

if [[ $WITH_SYNC == 1 ]]; then
  step "launchd: tempus sync every 6 hours"
  write_agent com.tempus.sync 21600 "$UV" run tempus sync
else
  launchctl bootout "gui/$(id -u)/com.tempus.sync" >/dev/null 2>&1 || true
fi

step "Publish tempus to your tailnet only (tailscale serve → HTTPS)"
for _ in 1 2 3 4 5 6 7 8 9 10; do curl -sf "http://127.0.0.1:$PORT/api/cards" >/dev/null && break; sleep 1; done
if sudo tailscale serve --bg "$PORT" >/dev/null 2>&1; then
  ok "$(tailscale serve status 2>/dev/null | head -1)"
else
  todo "Enable HTTPS certificates in the Tailscale admin console (DNS page), then re-run: sudo tailscale serve --bg $PORT"
fi

step "Claude Code (for agents you reach from your phone)"
if command -v claude >/dev/null; then ok "claude $(claude --version 2>/dev/null | head -1)"; else todo "Install Claude Code: https://docs.claude.com/en/docs/claude-code/setup, then run \`claude\` once to sign in"; fi
gh auth status >/dev/null 2>&1 && ok "gh signed in" || todo "Run: gh auth login"

printf '\n\033[1mDone.\033[0m Check everything with: mise run homebase:status\n'
if ((${#TODO[@]})); then
  printf '\nStill to do by hand:\n'
  for t in "${TODO[@]}"; do printf '  • %s\n' "$t"; done
fi
