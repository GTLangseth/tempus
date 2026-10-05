#!/usr/bin/env bash
# Share tempus with people outside the tailnet, behind a password, only while it's on.
# Runbook: docs/HOMEBASE.md → Share with colleagues.
#
#   share.sh on [--for 8h|90m|2d|none] [--new-password]
#   share.sh off
#   share.sh status
#
# Funnel publishes ONLY https port 8443, to a separate read-only guest app on 127.0.0.1:8766
# (`tempus serve --share`). The tailnet app on 443 → 8765 is never touched. Never use
# `tailscale funnel reset` here: it would wipe the tailnet serve config too.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
FUNNEL_PORT=8443                          # Funnel allows 443, 8443 or 10000
GUEST_PORT="${TEMPUS_SHARE_PORT:-8766}"
LABEL=com.tempus.share
# Outside ~/Library/LaunchAgents on purpose: launchd forgets it at reboot, so sharing never
# comes back on by itself.
STATE="$HOME/Library/Application Support/tempus"
PLIST="$STATE/$LABEL.plist"
UNTIL_FILE="$STATE/share.until"
LOGS="$HOME/Library/Logs/tempus"
DOMAIN="gui/$(id -u)"

ok()  { printf '  ✓ %s\n' "$*"; }
die() { printf '  ✗ %s\n' "$*" >&2; exit 1; }

ts_name() { tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'; }
funnel_on() { tailscale funnel status 2>/dev/null | grep -q ":$FUNNEL_PORT (Funnel on)"; }

duration_seconds() { # 8h | 90m | 2d | none
  case "$1" in
    none) echo 0 ;;
    *m) echo $(( ${1%m} * 60 )) ;;
    *h) echo $(( ${1%h} * 3600 )) ;;
    *d) echo $(( ${1%d} * 86400 )) ;;
    *) die "--for takes e.g. 90m, 8h, 2d or none (got '$1')" ;;
  esac
}

ensure_password() { # $1 = 1 to replace it
  cd "$REPO"
  local current; current="$(sed -n 's/^TEMPUS_SHARE_PASSWORD=//p' .env | tail -1)"
  if [[ -n "$current" && "$1" != 1 ]]; then
    ((${#current} >= 12)) || die "TEMPUS_SHARE_PASSWORD in .env is shorter than 12 characters"
    ok "password: from .env (TEMPUS_SHARE_PASSWORD)"
    return
  fi
  local pw; pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
  grep -v '^TEMPUS_SHARE_PASSWORD=' .env > .env.tmp || true
  printf 'TEMPUS_SHARE_PASSWORD=%s\n' "$pw" >> .env.tmp
  mv .env.tmp .env && chmod 600 .env
  ok "new password saved to .env: $pw"
}

cmd_on() {
  local dur=8h new_pw=0
  while (($#)); do
    case "$1" in
      --for) dur="${2:?--for needs a value}"; shift 2 ;;
      --new-password) new_pw=1; shift ;;
      *) die "unknown option: $1" ;;
    esac
  done
  local secs until_arg=() until=0
  secs="$(duration_seconds "$dur")"
  if ((secs > 0)); then until=$(( $(date +%s) + secs )); until_arg=(--until "$until"); fi

  tailscale status >/dev/null 2>&1 || die "Tailscale isn't up"
  ensure_password "$new_pw"

  local uv; uv="$(cd "$REPO" && mise which uv)"
  mkdir -p "$STATE" "$LOGS"
  local args="" a
  for a in "$uv" run tempus serve --share --host 127.0.0.1 --port "$GUEST_PORT" ${until_arg[@]+"${until_arg[@]}"}; do
    args+="<string>$a</string>"
  done
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$(dirname "$uv"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/$LABEL.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$LABEL.log</string>
</dict></plist>
PLIST
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true   # restart = new signing key
  launchctl bootstrap "$DOMAIN" "$PLIST"
  local _
  for _ in $(seq 1 20); do curl -sf "http://127.0.0.1:$GUEST_PORT/login" >/dev/null && break; sleep 0.5; done
  curl -sf "http://127.0.0.1:$GUEST_PORT/login" >/dev/null || die "guest app didn't start; see $LOGS/$LABEL.log"
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$GUEST_PORT/api/cards")" == 401 ]] \
    || die "guest app answered without a password; not publishing it"
  ok "guest app on 127.0.0.1:$GUEST_PORT (read-only, password-gated)"

  tailscale funnel --bg --https="$FUNNEL_PORT" "http://127.0.0.1:$GUEST_PORT" >/dev/null \
    || die "Funnel refused. Check that the tailnet policy grants this machine the 'funnel' attribute (docs/HOMEBASE.md)"
  ok "funnel: https://$(ts_name):$FUNNEL_PORT → 127.0.0.1:$GUEST_PORT"

  echo "$until" > "$UNTIL_FILE"
  if ((until > 0)); then
    # Belt and braces: the guest app refuses everything after --until on its own; this also
    # takes the Funnel down. A newer `share:on` writes a new until, so old timers do nothing.
    nohup bash "$0" expire "$until" >/dev/null 2>&1 &
    disown
    ok "turns itself off at $(date -r "$until" '+%a %H:%M')"
  else
    ok "no time limit: run \`mise run share:off\` when you're done"
  fi
  printf '\nShare this link: https://%s:%s  (password in .env: TEMPUS_SHARE_PASSWORD)\n' "$(ts_name)" "$FUNNEL_PORT"
}

cmd_off() {
  tailscale funnel --https="$FUNNEL_PORT" off >/dev/null 2>&1 || true   # fine if already off
  ok "funnel :$FUNNEL_PORT off"
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  local _   # bootout returns before the job is gone
  for _ in $(seq 1 20); do launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break; sleep 0.25; done
  rm -f "$PLIST" "$UNTIL_FILE"
  ok "guest app stopped (every guest session is now invalid)"
}

cmd_expire() { # internal: sleep until $1, then turn off if that share is still the current one
  local until="$1"
  sleep $(( until - $(date +%s) > 0 ? until - $(date +%s) : 0 ))
  [[ "$(cat "$UNTIL_FILE" 2>/dev/null)" == "$until" ]] && cmd_off
}

cmd_status() {
  echo "tempus sharing"
  if funnel_on; then ok "funnel ON: https://$(ts_name):$FUNNEL_PORT"; else ok "funnel off"; fi
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then ok "guest app running on 127.0.0.1:$GUEST_PORT"; else ok "guest app stopped"; fi
  local until; until="$(cat "$UNTIL_FILE" 2>/dev/null || echo)"
  if [[ -n "$until" && "$until" != 0 ]]; then ok "ends $(date -r "$until" '+%a %H:%M')"; fi
  if funnel_on && ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    printf '  ! funnel is on with no guest app behind it (e.g. after a reboot): run `mise run share:off`\n'
  fi
  echo; tailscale funnel status 2>/dev/null | grep -v '^$' || true
}

case "${1:-}" in
  on) shift; cmd_on "$@" ;;
  off) cmd_off ;;
  status) cmd_status ;;
  expire) cmd_expire "$2" ;;
  *) echo "usage: $0 on [--for 8h|none] [--new-password] | off | status" >&2; exit 2 ;;
esac
