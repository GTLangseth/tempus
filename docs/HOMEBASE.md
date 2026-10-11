# Home base

The Mac mini is tempus's always-on home: it holds the real database and the API keys, serves
the app to your devices over Tailscale, and is where you start agents that need real data,
even from your phone. Cloud agents (Claude Code routines) still do code-only work without it.

```
 iPhone ─┐                       Mac mini (home base)
         │   tailnet (WireGuard)  ├─ tailscaled (system daemon, up before login)
 MacBook ┼──────────────────────▶ ├─ Remote Login (SSH) ── tmux ── claude
         │  https://<mini>.ts.net ├─ tailscale serve :443 ──▶ tempus serve (127.0.0.1:8765, launchd)
         │                        ├─ Colima ── TimescaleDB (127.0.0.1:5432)
         └─ git ◀── GitHub ──────▶└─ ~/tempus (.env holds FRED + TypeSafe keys)
```

Nothing listens on the network except SSH and Tailscale. The app and the database bind to
localhost; Tailscale publishes the app to your tailnet only, and other devices reach the
database through an SSH tunnel. The one exception is [sharing](#share-with-colleagues), which
is off unless you switch it on.

## Setup (about 20 minutes)

### 0. Bootstrap the Mac mini

1. Sign in to macOS with the user that will own tempus. Turn on **System Settings → General →
   Software Update → automatic updates** as you like, but keep "Install macOS updates" off so
   the box doesn't restart on its own.
2. Install Homebrew (https://brew.sh), then mise, git and the GitHub CLI:
   ```sh
   brew install mise git gh
   echo 'eval "$(mise activate zsh)"' >> ~/.zshrc && exec zsh
   gh auth login                                             # the repo is private
   ```
3. Clone the repo and add your keys. Copy them from the MacBook's `.env`, e.g. with Taildrop
   later, or paste them now:
   ```sh
   gh repo clone GTLangseth/tempus ~/tempus
   cd ~/tempus && cp .env.example .env && open -e .env       # set FRED_API_KEY and TYPESAFE_API_KEY
   ```

### 1. Run the setup task

```sh
cd ~/tempus
mise run homebase:setup                 # add `-- --with-sync` to also sync every 6 hours
```

It's safe to re-run. It asks for your password once, then:

- installs Tailscale, Colima, Docker CLI + compose, tmux and gh. If Tailscale.app is already
  installed, or a Docker engine (e.g. Docker Desktop) is already running, it uses those instead
  of adding a second copy
- stops the Mac from sleeping and restarts it after a power failure (`pmset`)
- turns on Remote Login, or tells you to do it in System Settings if Terminal lacks Full Disk Access
- installs `tailscaled` as a system daemon and joins your tailnet (open the login link it prints)
- starts Colima at login, starts TimescaleDB and runs `tempus db init`. If another Postgres
  already holds port 5432, it stops and asks you to set `TEMPUS_DB_PORT` and `DATABASE_URL` in
  `.env` to a free port (e.g. 5433)
- keeps `tempus serve` running with launchd on 127.0.0.1:8765, logging to `~/Library/Logs/tempus/`
- publishes it to your tailnet with `tailscale serve`
- ends with a list of anything left to do by hand

### 2. Tailscale admin console (https://login.tailscale.com/admin)

- **Machines → the mini → ⋯ → Disable key expiry.** Otherwise it drops off the tailnet after 180 days.
- **DNS:** keep **MagicDNS** on and turn on **HTTPS certificates**. `tailscale serve` needs them.
  If setup flagged this, run `sudo tailscale serve --bg 8765` again afterwards.
- Optional: rename the machine to `homebase`, so it's `ssh homebase` and `https://homebase.<tailnet>.ts.net`.

### 3. Join your other devices

- **iPhone:** install Tailscale from the App Store and sign in with the same account. For
  SSH, use Blink Shell or Termius.
- **MacBook:** install the Tailscale app (Mac App Store or Standalone) and sign in.

### 4. Check it

```sh
mise run homebase:status       # on the mini: every line should be ✓
```

From your phone, open `https://<mini>.<tailnet>.ts.net`. You should see the tempus app.

## Load the explore tree

The 3D tree of ~2,000 FRED series ships with main. After setup:

```sh
cd ~/tempus
uv run tempus catalog import-snapshot data/fred/series-snapshot.json   # metadata for 1,965 series
uv run tempus ontology load                                            # 174 concepts + draft placements
launchctl kickstart -k gui/$(id -u)/com.tempus.serve                   # restart the app
```

Then open `https://<mini>.<tailnet>.ts.net`, press `/` and describe a series. With
`TYPESAFE_API_KEY` set, the answer panel says **jev**. To have Jev re-decide Claude's draft
placements: `uv run tempus ontology file` (slow: ~2,000 series; run it in `tmux`).

## Agents on the go

From the phone (Blink or Termius) or the MacBook:

```sh
ssh <you>@<mini>                        # over Tailscale, from anywhere
tmux new -A -s tempus                   # reattach if it already exists
cd ~/tempus && claude                   # real DB, real keys, real Jev
```

Close the app whenever you like; `tmux` keeps the session running. Reattach later with the
same two commands. Use cloud routines instead for code-only work that doesn't need real data.

## Develop on the mini from Zed

From the MacBook, in the repo:

```sh
mise run homebase:zed                   # opens the mini's checkout in Zed over SSH
```

Zed runs its editor on the MacBook and its language servers, terminal and file access on the
mini, so builds and tests hit the real database. The task finds the checkout at `~/tempus` or
`~/Projects/tempus`, using the `homebase` SSH alias (`~/.ssh/config`). To use a different host
or folder, set `TEMPUS_HOMEBASE_HOST` and `TEMPUS_HOMEBASE_DIR`. This checkout is the one
launchd serves, so keep it on `main` when you're done and restart the app after a pull
(see [Day to day](#day-to-day)).

## Use the mini's database from the MacBook

```sh
ssh -N -L 5433:127.0.0.1:5432 <you>@<mini>      # leave running; use the mini's TEMPUS_DB_PORT on the right
DATABASE_URL=postgres://postgres:password@localhost:5433/tempus uv run tempus series list
```

## Share with colleagues

To show tempus to people who aren't on your tailnet, switch sharing on for a while:

```sh
mise run share:on                       # off again by itself after 8 hours
mise run share:on -- --for 2h           # or 90m, 2d, none
mise run share:status
mise run share:off                      # everyone is logged out at once
```

`share:on` prints the link (`https://<mini>.<tailnet>.ts.net:8443`). The password is
`TEMPUS_SHARE_PASSWORD` in `.env`: the first run generates a random one, and
`-- --new-password` replaces it. Send the link and the password through different channels.

How it's locked down:

- **One port, one app.** Funnel publishes only HTTPS port 8443, to a separate guest copy of
  the app on `127.0.0.1:8766` (`tempus serve --share`). Your tailnet app on 443 → 8765 is
  never published and needs no password. Never run `tailscale funnel reset`: it also wipes
  the tailnet serve.
- **Password page in front of everything.** Without a session cookie, pages redirect to
  `/login` and the API answers 401. The cookie is HttpOnly, Secure and SameSite=Strict and
  lasts 12 hours. Its signing key is new every time the guest app starts, so `share:off`
  (or `share:on` again) logs every guest out. Five wrong passwords from one visitor, or 50
  from everyone, lock logins for 15 minutes. Pages carry `noindex` and can't be framed.
- **Read-only.** Guests can browse and search, but series pages never sync from FRED, and
  `/docs` is off. Search still uses Jev, which uses your TypeSafe key.
- **Off by default.** It turns itself off after the `--for` window: the guest app refuses
  every request after that, and a timer also takes the Funnel down. The guest app's launchd
  job isn't installed in `~/Library/LaunchAgents`, so it never comes back after a reboot. If
  the Funnel config survives a reboot, `share:status` says so; run `share:off`.
- **Only this machine may Funnel.** In the admin console (**Access controls**), grant the
  `funnel` node attribute to the mini alone, instead of the default `autogroup:member`:
  ```json
  "nodeAttrs": [{ "target": ["<you>@<domain>"], "attr": ["funnel"] }]
  ```
  To pin it to the one machine, tag the mini (e.g. `tag:homebase`) and target the tag.

A new Funnel can take a few minutes to become reachable from the internet. Test it from
your phone with Wi-Fi off, not from the mini itself.

## Day to day

| Task | Command (on the mini) |
|---|---|
| Health | `mise run homebase:status` |
| Update to latest main | `git pull && uv sync && uv run tempus db init && launchctl kickstart -k gui/$(id -u)/com.tempus.serve` |
| App logs | `tail -f ~/Library/Logs/tempus/com.tempus.serve.log` |
| Stop / start the app | `launchctl bootout gui/$(id -u)/com.tempus.serve` / re-run `mise run homebase:setup` |
| Sync now | `uv run tempus sync` |
| Share with colleagues | `mise run share:on` / `share:off` / `share:status` |

## Unattended restarts

The launchd agents (app, Colima, scheduled sync) start when your user logs in. After a power
cut the mini restarts by itself, but:

- **FileVault on** (default, more secure): the mini waits at the unlock screen. Tailscale is up
  only once the disk is unlocked, so you need to log in at the machine (or use an Apple
  remote-unlock setup) before anything is reachable.
- **FileVault off + automatic login** (System Settings → Users & Groups): everything comes
  back unattended. The trade-off is that the disk isn't encrypted at rest.

Pick one consciously. For a box that lives at home, either is reasonable.

## Troubleshooting

- `tailscale serve` fails: HTTPS certificates aren't enabled (step 2).
- The app doesn't load over the tailnet but `curl 127.0.0.1:8765` works: run `tailscale serve status`, then `sudo tailscale serve --bg 8765`.
- `docker compose` not found: re-run setup. It registers Homebrew's compose plugin in `~/.docker/config.json`.
- Database down after a reboot: `colima start && mise run db:up` (with Docker Desktop: open it, then `mise run db:up`).
