# Dev and Prod Databases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pull prod's data into the dev database, push dev's syncs and filings back without
double-spending, and read prod from dev with no way to write.

**Architecture:** `tempus.remote` runs a short-lived `ssh -L` tunnel to the home base's
Postgres and builds connection strings for its two roles. `tempus.transfer` moves rows
between two open connections with `COPY` (pull: replace dev's copy; push: plan, then merge
whole series into prod), so its tests run between two local databases. `TEMPUS_DB=prod`
makes `get_settings()` point at prod through a process-wide tunnel as the read-only role;
the app turns sync-on-open off and shows a badge, and writing commands refuse.

**Tech Stack:** Python 3.12, uv, typer, rich, psycopg 3 (`Cursor.copy`, `psycopg.sql`),
pydantic-settings, FastAPI, plain ES modules. OpenSSH (system). No new dependencies.

**Spec:** `docs/specs/2026-10-11-dev-prod-db-sync-design.md`. Read it before starting any task.

## Global Constraints

- Settings, verbatim: `TEMPUS_DB` (`dev` | `prod`, default `dev`), `TEMPUS_HOMEBASE_HOST`
  (default `homebase`), `TEMPUS_PROD_DB_PORT` (default `5432`), `TEMPUS_PROD_RO_PASSWORD`,
  `TEMPUS_PROD_PASSWORD`; on the mini, `TEMPUS_DB_RO_PASSWORD`.
- Prod's database is `tempus`; its owner role is `postgres`; the read-only role is `tempus_ro`.
- The tunnel command is `ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:<local>:127.0.0.1:<remote> <host>`.
  SSH is the only transport; nothing new listens on the network.
- Tables copied: `series`, `observations`, `series_concepts`. Never `concepts`, `cards`, `sync_runs`.
- Hypertable reads use `COPY (SELECT …) TO STDOUT`, never `COPY observations TO`.
- Push never deletes a prod series. A pushed series appends a `sync_runs` row with status `pushed`.
- `TEMPUS_DB=prod` never falls back to dev. Writing commands refuse with:
  `TEMPUS_DB=prod is read-only: run this on the home base (ssh homebase), or unset TEMPUS_DB.`
- Pull and push refuse unless `DATABASE_URL`'s host is `localhost`, `127.0.0.1`, `::1` or a socket.
- `mise run test` and `mise run lint` pass after every task. Ruff line length is 100; run
  `uv run ruff format` before lint (the plan's code isn't always formatter-exact).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Each task group is a Linear sub-issue of SPA-46. Work on that sub-issue's Linear branch and
  open one PR per sub-issue (`Refs SPA-46`; the branch name closes the sub-issue).

**Clarifications of the spec** (record them in the spec's "Changes during implementation"
in Task 14):

- Series metadata and observations (the FRED backfill) always move whole. Of the placements,
  only `auto` ones (Jev placements) move in either direction. `manual` pins and `draft`
  placements come from git (`catalog.toml`, `ontology/placements.draft.yaml`) on each side;
  pushing them would ping-pong, since every `ontology load` refreshes a draft's `filed_at`.
- The read-only role is created by a new `tempus db readonly-role` command (testable), which
  `homebase:setup` calls.
- `/api/meta` derives `remote` from `settings.db == "prod"`; no extra settings fields.

## Review Focus

1. **The laptop sleeps while `TEMPUS_DB=prod tempus serve` runs.** The ssh process dies; the
   watchdog restarts it on the same local port, so the app works again after wake without a
   restart. Test: Task 2, `test_watchdog_restarts_a_dead_tunnel`.
2. **`db pull --series` names a series prod doesn't have.** Dev's copy is kept (not deleted
   and left empty), and the CLI reports it as "not on prod". Test: Task 6,
   `test_pull_keeps_dev_series_prod_lacks`.
3. **A wrong or missing prod password.** One line naming the setting to fix, no traceback,
   no tunnel left running. Tests: Task 2 (`test_prod_url_names_the_missing_setting`), Task 7
   (`test_pull_reports_a_refused_connection`).
4. **Prod on an older schema** (the mini hasn't pulled main). `SchemaMismatch` says
   "run `tempus db init` on prod". Test: Task 5, `test_schema_mismatch_names_the_side`.
5. **Running push twice, or pull right after push.** The second plan is empty: no ping-pong of
   series or placements between the databases. Test: Task 8,
   `test_push_then_pull_leaves_nothing_to_push`.

---

## Sub-issue 1: tunnel, settings, `db where`, the read-only role (Tasks 1–4)

### Task 1: Settings for dev and prod

**Files:**
- Modify: `src/tempus/config.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_config.py` (create)

**Interfaces:**
- Produces: `Settings.db: Literal["dev", "prod"]`, `Settings.homebase_host: str`,
  `Settings.prod_db_port: int`, `Settings.prod_ro_password: str | None`,
  `Settings.prod_password: str | None`, `Settings.db_ro_password: str | None`.
  `get_settings()` returns `tempus.remote.read_only_settings(settings)` when `db == "prod"`
  (that function arrives in Task 2; until then the branch is unreachable in tests).
  Fixtures `dev_conn` and `prod_conn` (databases `tempus_test_dev`, `tempus_test_prod`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from tempus.config import Settings


def test_dev_is_the_default(monkeypatch):
    monkeypatch.delenv("TEMPUS_DB", raising=False)
    s = Settings(_env_file=None)
    assert s.db == "dev" and s.homebase_host == "homebase" and s.prod_db_port == 5432
    assert s.prod_ro_password is None and s.prod_password is None


def test_prod_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("TEMPUS_DB", "prod")
    monkeypatch.setenv("TEMPUS_HOMEBASE_HOST", "mini")
    monkeypatch.setenv("TEMPUS_PROD_DB_PORT", "5433")
    monkeypatch.setenv("TEMPUS_PROD_RO_PASSWORD", "ro")
    monkeypatch.setenv("TEMPUS_PROD_PASSWORD", "owner")
    s = Settings(_env_file=None)
    assert (s.db, s.homebase_host, s.prod_db_port) == ("prod", "mini", 5433)
    assert (s.prod_ro_password, s.prod_password) == ("ro", "owner")


def test_fields_can_be_set_by_name():
    assert Settings(_env_file=None, db="prod").db == "prod"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'db'`

- [ ] **Step 3: Implement**

Replace `src/tempus/config.py` with:

```python
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    database_url: str = "postgres://postgres:password@localhost:5432/tempus"
    fred_api_key: str | None = None
    typesafe_api_key: str | None = None
    catalog_path: Path = Path("catalog.toml")
    ontology_path: Path = Path("ontology/concepts.yaml")
    drafts_path: Path = Path("ontology/placements.draft.yaml")
    cards_path: Path = Path("cards")
    share_password: str | None = Field(None, validation_alias="TEMPUS_SHARE_PASSWORD")

    # Dev and prod databases: docs/specs/2026-10-11-dev-prod-db-sync-design.md.
    db: Literal["dev", "prod"] = Field("dev", validation_alias="TEMPUS_DB")
    homebase_host: str = Field("homebase", validation_alias="TEMPUS_HOMEBASE_HOST")
    prod_db_port: int = Field(5432, validation_alias="TEMPUS_PROD_DB_PORT")
    prod_ro_password: str | None = Field(None, validation_alias="TEMPUS_PROD_RO_PASSWORD")
    prod_password: str | None = Field(None, validation_alias="TEMPUS_PROD_PASSWORD")
    # On the home base: the password `tempus db readonly-role` gives tempus_ro.
    db_ro_password: str | None = Field(None, validation_alias="TEMPUS_DB_RO_PASSWORD")


def get_settings() -> Settings:
    """Settings from the environment and .env.

    With TEMPUS_DB=prod, `database_url` points at the home base's database through an SSH
    tunnel, as the read-only role (the tunnel opens on first use).
    """
    settings = Settings()
    if settings.db == "prod":
        from tempus.remote import read_only_settings

        return read_only_settings(settings)
    return settings
```

In `tests/conftest.py`, keep tests on dev whatever the shell says, and add the two-database
fixtures. Replace everything after the imports with:

```python
TEST_DB = "tempus_test"


@pytest.fixture(autouse=True)
def no_api_keys(monkeypatch):
    """Tests never call paid or rate-limited APIs, whatever the developer's .env holds.

    Environment variables beat .env in pydantic-settings, so blanking them here wins. A test
    that needs a key passes one to Settings(...) explicitly and fakes the client.
    """
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    monkeypatch.setenv("FRED_API_KEY", "")


@pytest.fixture(autouse=True)
def dev_database(monkeypatch):
    """Tests never open a tunnel to the home base, whatever the developer's shell says."""
    monkeypatch.setenv("TEMPUS_DB", "dev")


def fresh_database(name: str) -> str:
    """Drop and recreate `name` next to the dev database; returns its URL."""
    url = get_settings().database_url
    try:
        admin = psycopg.connect(url, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("database not reachable")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")
    return make_conninfo(url, dbname=name)


def _schema_conn(name: str):
    with db.connect(fresh_database(name)) as c:
        db.init_schema(c)
        yield c


@pytest.fixture
def conn():
    yield from _schema_conn(TEST_DB)


@pytest.fixture
def dev_conn():
    """The dev side of a pull or push (`tempus_test_dev`)."""
    yield from _schema_conn("tempus_test_dev")


@pytest.fixture
def prod_conn():
    """The prod side of a pull or push (`tempus_test_prod`)."""
    yield from _schema_conn("tempus_test_prod")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: all pass (the existing `conn` users are unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/tempus/config.py tests/conftest.py tests/test_config.py
git commit -m "Settings for dev and prod databases (TEMPUS_DB, prod host, port, passwords)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: The SSH tunnel and prod connection strings

**Files:**
- Create: `src/tempus/remote.py`
- Test: `tests/test_remote.py` (create)

**Interfaces:**
- Consumes: `Settings` fields from Task 1.
- Produces:
  - `class RemoteError(RuntimeError)`: one-line message for the CLI.
  - `class Tunnel(host: str, remote_port: int, *, keepalive: bool = False, timeout: float = 10.0, ssh: str = "ssh")`
    with `.local_port: int`, `.command() -> list[str]`, `.start() -> Tunnel`, `.stop() -> None`,
    context manager.
  - `prod_url(settings: Settings, port: int, *, owner: bool = False) -> str`
  - `is_local(url: str) -> bool`
  - `read_only_settings(settings: Settings) -> Settings`
  - Constants `PROD_DB = "tempus"`, `OWNER = "postgres"`, `RO_ROLE = "tempus_ro"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_remote.py
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from tempus import remote
from tempus.config import Settings
from tempus.remote import RemoteError, Tunnel, is_local, prod_url

# Stands in for ssh: listens on the -L local port ("ok"), exits with an ssh-like error
# ("fail"), or never listens ("hang"). The mode comes from FAKE_SSH_MODE.
FAKE_SSH = """\
import os, socket, sys, time
mode = os.environ.get("FAKE_SSH_MODE", "ok")
if mode == "fail":
    print("ssh: Could not resolve hostname homebase: nodename nor servname provided", file=sys.stderr)
    sys.exit(255)
if mode == "hang":
    time.sleep(60)
port = int(sys.argv[sys.argv.index("-L") + 1].split(":")[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen()
while True:
    c, _ = s.accept()
    c.close()
"""


@pytest.fixture
def fake_ssh(tmp_path: Path) -> str:
    path = tmp_path / "ssh"
    path.write_text(f"#!{sys.executable}\n{FAKE_SSH}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_command_forwards_a_local_port_to_the_remote_postgres():
    t = Tunnel("homebase", 5433)
    cmd = t.command()
    assert cmd[0] == "ssh" and cmd[-1] == "homebase" and "-N" in cmd
    assert cmd[cmd.index("-L") + 1] == f"127.0.0.1:{t.local_port}:127.0.0.1:5433"
    assert "ExitOnForwardFailure=yes" in cmd and "BatchMode=yes" in cmd


def test_tunnel_opens_and_closes(fake_ssh):
    with Tunnel("homebase", 5432, ssh=fake_ssh) as t:
        assert remote._port_open(t.local_port)
        proc = t._proc
    assert proc.poll() is not None


def test_tunnel_fails_fast_with_ssh_s_message(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "fail")
    start = time.monotonic()
    with pytest.raises(RemoteError, match="Could not resolve hostname"):
        Tunnel("homebase", 5432, ssh=fake_ssh).start()
    assert time.monotonic() - start < 3


def test_tunnel_times_out_when_nothing_listens(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_MODE", "hang")
    with pytest.raises(RemoteError, match="no tunnel after"):
        Tunnel("homebase", 5432, ssh=fake_ssh, timeout=0.5).start()


def test_watchdog_restarts_a_dead_tunnel(fake_ssh):
    t = Tunnel("homebase", 5432, ssh=fake_ssh, keepalive=True).start()
    try:
        first = t._proc
        first.kill()  # the laptop slept and ssh died
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if t._proc is not first and remote._port_open(t.local_port):
                break
            time.sleep(0.1)
        assert t._proc is not first and remote._port_open(t.local_port)
    finally:
        t.stop()


def test_prod_url_uses_the_tunnel_and_the_right_role():
    s = Settings(_env_file=None, prod_ro_password="ro", prod_password="owner")
    ro = conninfo_to_dict(prod_url(s, 6001))
    assert (ro["host"], ro["port"], ro["dbname"], ro["user"]) == ("127.0.0.1", "6001", "tempus", "tempus_ro")
    assert conninfo_to_dict(prod_url(s, 6001, owner=True))["user"] == "postgres"


def test_prod_url_names_the_missing_setting():
    s = Settings(_env_file=None)
    with pytest.raises(RemoteError, match="TEMPUS_PROD_RO_PASSWORD"):
        prod_url(s, 6001)
    with pytest.raises(RemoteError, match="TEMPUS_PROD_PASSWORD"):
        prod_url(s, 6001, owner=True)


@pytest.mark.parametrize(
    "url, local",
    [
        ("postgres://postgres:password@localhost:5432/tempus", True),
        ("postgres://postgres:password@127.0.0.1/tempus", True),
        ("host=::1 dbname=tempus", True),
        ("dbname=tempus", True),  # Unix socket
        ("postgres://postgres:password@db.example.com/tempus", False),
        ("postgres://postgres:password@grants-mac-mini/tempus", False),
    ],
)
def test_is_local(url, local):
    assert is_local(url) is local


def test_read_only_settings_fails_before_opening_a_tunnel(monkeypatch):
    monkeypatch.setattr(remote, "Tunnel", lambda *a, **k: pytest.fail("opened a tunnel"))
    with pytest.raises(RemoteError, match="TEMPUS_PROD_RO_PASSWORD"):
        remote.read_only_settings(Settings(_env_file=None, db="prod"))


def test_read_only_settings_points_at_the_tunnel(monkeypatch):
    class FakeTunnel:
        local_port = 6123

        def __init__(self, host, port, keepalive):
            assert (host, port, keepalive) == ("homebase", 5432, True)

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(remote, "Tunnel", FakeTunnel)
    monkeypatch.setattr(remote, "_tunnel", None)
    s = remote.read_only_settings(Settings(_env_file=None, db="prod", prod_ro_password="ro"))
    info = conninfo_to_dict(s.database_url)
    assert (info["port"], info["user"]) == ("6123", "tempus_ro") and s.db == "prod"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_remote.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.remote'`

- [ ] **Step 3: Implement `src/tempus/remote.py`**

```python
"""Reach the home base's (prod) database from the MacBook through an SSH tunnel.

Spec: docs/specs/2026-10-11-dev-prod-db-sync-design.md. SSH (the `homebase` alias) is the
only transport: nothing listens on the network on either machine.
"""

import atexit
import socket
import subprocess
import threading
import time

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from tempus.config import Settings

PROD_DB = "tempus"
OWNER = "postgres"
RO_ROLE = "tempus_ro"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class RemoteError(RuntimeError):
    """A problem reaching prod. The message is one line, ready for the CLI."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class Tunnel:
    """`ssh -N -L` from a free local port to Postgres on the home base.

    With `keepalive=True` (long-running processes such as `tempus serve`), a watchdog
    restarts ssh on the same local port when it dies, e.g. after the laptop sleeps, so
    connection strings built from `local_port` stay valid.
    """

    def __init__(
        self,
        host: str,
        remote_port: int,
        *,
        keepalive: bool = False,
        timeout: float = 10.0,
        ssh: str = "ssh",
    ):
        self.host, self.remote_port = host, remote_port
        self.keepalive, self.timeout, self.ssh = keepalive, timeout, ssh
        self.local_port = _free_port()
        self._proc: subprocess.Popen | None = None
        self._stopping = threading.Event()

    def command(self) -> list[str]:
        return [
            self.ssh, "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            self.host,
        ]  # fmt: skip

    def start(self) -> "Tunnel":
        self._spawn()
        if self.keepalive:
            threading.Thread(target=self._watch, daemon=True).start()
        return self

    def _spawn(self) -> None:
        proc = subprocess.Popen(
            self.command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._proc = proc
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                lines = (proc.stderr.read() if proc.stderr else "").strip().splitlines()
                why = lines[-1] if lines else f"ssh exited with {proc.returncode}"
                raise RemoteError(
                    f"can't reach {self.host} over SSH (Tailscale up? Remote Login on?): {why}"
                )
            if _port_open(self.local_port):
                return
            time.sleep(0.05)
        proc.terminate()
        raise RemoteError(f"can't reach {self.host} over SSH: no tunnel after {self.timeout:g}s")

    def _watch(self) -> None:
        delay = 1.0
        while not self._stopping.wait(1.0):
            if self._proc is not None and self._proc.poll() is None:
                delay = 1.0
                continue
            try:
                self._spawn()
                delay = 1.0
            except RemoteError:
                self._stopping.wait(delay)
                delay = min(delay * 2, 30.0)

    def stop(self) -> None:
        self._stopping.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def __enter__(self) -> "Tunnel":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def prod_url(settings: Settings, port: int, *, owner: bool = False) -> str:
    """Connection string for prod through a tunnel on `port`: tempus_ro, or the owner."""
    if owner:
        user, password, setting = OWNER, settings.prod_password, "TEMPUS_PROD_PASSWORD"
    else:
        user, password, setting = RO_ROLE, settings.prod_ro_password, "TEMPUS_PROD_RO_PASSWORD"
    if not password:
        raise RemoteError(f"set {setting} in .env (docs/HOMEBASE.md → Dev and prod databases)")
    return make_conninfo(
        host="127.0.0.1", port=port, dbname=PROD_DB, user=user, password=password
    )


def is_local(url: str) -> bool:
    """Whether a connection string points at this machine (TCP loopback or a socket)."""
    host = conninfo_to_dict(url).get("host") or ""
    return host == "" or host.startswith("/") or host in LOCAL_HOSTS


_tunnel: Tunnel | None = None


def read_only_settings(settings: Settings) -> Settings:
    """`settings` pointed at prod through a process-wide tunnel, as tempus_ro.

    The tunnel opens on first use and closes when the process exits. A missing password
    fails before any ssh starts; nothing ever falls back to the dev database.
    """
    global _tunnel
    prod_url(settings, 0)  # raises RemoteError naming the setting
    if _tunnel is None:
        _tunnel = Tunnel(settings.homebase_host, settings.prod_db_port, keepalive=True).start()
        atexit.register(_tunnel.stop)
    return settings.model_copy(update={"database_url": prod_url(settings, _tunnel.local_port)})
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_remote.py tests/test_config.py -v`
Expected: PASS (the watchdog test takes a few seconds).

- [ ] **Step 5: Commit**

```bash
git add src/tempus/remote.py tests/test_remote.py
git commit -m "remote: an SSH tunnel to the home base's Postgres and prod connection strings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: The read-only role (`tempus db readonly-role`, `homebase:setup`)

**Files:**
- Modify: `src/tempus/db.py`
- Modify: `src/tempus/cli.py` (new command under `# --- db ---`)
- Modify: `scripts/homebase/setup.sh` (after `ok "database ready"`)
- Test: `tests/test_db_roles.py` (create)

**Interfaces:**
- Consumes: `remote.RO_ROLE`.
- Produces: `db.ensure_readonly_role(conn: psycopg.Connection, password: str, role: str = "tempus_ro") -> None`;
  CLI `tempus db readonly-role`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_roles.py
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from tempus import db
from tempus.config import get_settings

ROLE = "tempus_ro_test"


@pytest.fixture
def ro_url(conn):
    db.ensure_readonly_role(conn, "pw1", role=ROLE)
    db.ensure_readonly_role(conn, "pw2", role=ROLE)  # idempotent; the password updates
    yield make_conninfo(get_settings().database_url, dbname="tempus_test", user=ROLE, password="pw2")
    conn.execute(f"DROP OWNED BY {ROLE}")
    conn.execute(f"DROP ROLE {ROLE}")


def test_readonly_role_reads_every_table_including_hypertable_chunks(conn, ro_url):
    conn.execute("INSERT INTO series (source, series_id) VALUES ('fred', 'A')")
    for year in (2001, 2011, 2021):  # three 5-year chunks
        conn.execute(
            "INSERT INTO observations VALUES ('fred', 'A', %s, 1.0)", (datetime(year, 1, 1, tzinfo=UTC),)
        )
    with db.connect(ro_url) as ro:
        assert ro.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 3
        for table in ("series", "series_concepts", "concepts", "cards", "sync_runs"):
            ro.execute(f"SELECT * FROM {table} LIMIT 1")


def test_readonly_role_cannot_write(conn, ro_url):
    with db.connect(ro_url) as ro, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        ro.execute("INSERT INTO series (source, series_id) VALUES ('fred', 'B')")


def test_readonly_role_covers_tables_created_later(conn, ro_url):
    conn.execute("CREATE TABLE later (x int)")
    with db.connect(ro_url) as ro:
        ro.execute("SELECT * FROM later")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db_roles.py -v`
Expected: FAIL with `AttributeError: module 'tempus.db' has no attribute 'ensure_readonly_role'`

- [ ] **Step 3: Implement**

Add to `src/tempus/db.py` (and `from psycopg import sql` to its imports):

```python
def ensure_readonly_role(conn: psycopg.Connection, password: str, role: str = "tempus_ro") -> None:
    """Create or update a login role that can only read this database (idempotent).

    Postgres enforces it: `default_transaction_read_only` plus SELECT-only grants, including
    on tables created later. Grants on a hypertable cover its chunks.
    """
    r = sql.Identifier(role)
    with conn.transaction():
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = sql.SQL("ALTER" if exists else "CREATE")
        conn.execute(
            sql.SQL("{} ROLE {} WITH LOGIN PASSWORD {}").format(verb, r, sql.Literal(password))
        )
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(r))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(conn.info.dbname), r
            )
        )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(r))
        conn.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(r)
        )
```

Add to `src/tempus/cli.py` after `db_status` (and `remote` to the `from tempus import …` line):

```python
@db_app.command("readonly-role")
def db_readonly_role():
    """Create or update the read-only tempus_ro role (on the home base; idempotent).

    Its password is TEMPUS_DB_RO_PASSWORD. Copy it to the MacBook's .env as
    TEMPUS_PROD_RO_PASSWORD.
    """
    settings = get_settings()
    if not settings.db_ro_password:
        console.print(
            "[red]Set TEMPUS_DB_RO_PASSWORD in .env first[/red] "
            "([bold]mise run homebase:setup[/bold] generates one)."
        )
        raise typer.Exit(1)
    with _connect() as conn:
        db.ensure_readonly_role(conn, settings.db_ro_password, role=remote.RO_ROLE)
    console.print(f"[green]{remote.RO_ROLE} ready[/green]: read-only, SELECT on every table")
```

In `scripts/homebase/setup.sh`, after the line `ok "database ready"`, add:

```bash
step "Read-only database role for the MacBook (tempus_ro)"
if ! grep -Eq '^TEMPUS_DB_RO_PASSWORD=.+' .env; then
  sed -i '' '/^TEMPUS_DB_RO_PASSWORD=/d' .env
  echo "TEMPUS_DB_RO_PASSWORD=$(openssl rand -hex 16)" >> .env
fi
mise exec -- uv run tempus db readonly-role
todo "On the MacBook, add to .env: TEMPUS_PROD_RO_PASSWORD=$(sed -n 's/^TEMPUS_DB_RO_PASSWORD=//p' .env) and TEMPUS_PROD_DB_PORT=$DB_PORT; for db push also TEMPUS_PROD_PASSWORD (the password in this Mac's DATABASE_URL)"
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_db_roles.py -v && bash -n scripts/homebase/setup.sh`
Expected: PASS, and no syntax errors from bash.

- [ ] **Step 5: Commit**

```bash
git add src/tempus/db.py src/tempus/cli.py scripts/homebase/setup.sh tests/test_db_roles.py
git commit -m "tempus db readonly-role: a SELECT-only tempus_ro role, created by homebase:setup

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: `tempus db where`, and prod-mode connection errors

**Files:**
- Modify: `src/tempus/cli.py` (`_connect`, new `db where`)
- Test: `tests/test_cli_db.py` (create)

**Interfaces:**
- Consumes: `get_settings()` (Task 1), `remote.RemoteError` (Task 2).
- Produces: CLI `tempus db where`; `_connect()` reports `RemoteError` as one line.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_db.py
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from tempus.cli import app
from tempus.config import get_settings

runner = CliRunner()


def test_where_on_dev(conn):
    url = make_conninfo(get_settings().database_url, dbname="tempus_test")
    result = runner.invoke(app, ["db", "where"], env={"DATABASE_URL": url})
    assert result.exit_code == 0, result.output
    assert result.output.startswith("dev") and "postgres@" in result.output
    assert "tempus_test" in result.output and "0 series" in result.output


def test_prod_mode_without_a_password_never_falls_back_to_dev():
    result = runner.invoke(
        app, ["db", "where"], env={"TEMPUS_DB": "prod", "TEMPUS_PROD_RO_PASSWORD": ""}
    )
    assert result.exit_code == 1
    assert "TEMPUS_PROD_RO_PASSWORD" in result.output
    assert "series" not in result.output  # no db where line from the dev database
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_db.py -v`
Expected: FAIL (`No such command 'where'`).

- [ ] **Step 3: Implement**

In `src/tempus/cli.py`, add `from psycopg.conninfo import conninfo_to_dict` to the imports
and replace `_connect`:

```python
def _connect():
    try:
        settings = get_settings()
    except remote.RemoteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    try:
        return db.connect(settings.database_url)
    except Exception as exc:
        console.print(f"[red]Could not connect to the database:[/red] {exc}")
        if settings.db == "prod":
            console.print("Is the home base's database up? Try [bold]mise run homebase:status[/bold] there")
        else:
            console.print("Is it running? Try [bold]mise run db:up[/bold]")
        raise typer.Exit(1) from exc
```

Add after `db_status`:

```python
@db_app.command("where")
def db_where():
    """Which database this shell uses: dev, or prod read-only (TEMPUS_DB=prod)."""
    with _connect() as conn:
        info = db.status(conn)
        user = conn.execute("SELECT current_user AS u").fetchone()["u"]
        dsn = conninfo_to_dict(conn.info.dsn)
    settings = Settings()
    mode = f"prod via {settings.homebase_host} (read-only)" if settings.db == "prod" else "dev"
    console.print(
        f"[bold]{mode}[/bold]  {user}@{dsn.get('host', '')}:{dsn.get('port', '')}/"
        f"{info['database']}  timescaledb {info['timescaledb']}, {info['series']} series"
    )
```

and `Settings` to the config import: `from tempus.config import Settings, get_settings`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli_db.py -v && mise run lint`
Expected: PASS.

- [ ] **Step 5: Commit, push, PR for sub-issue 1**

```bash
git add src/tempus/cli.py tests/test_cli_db.py
git commit -m "tempus db where, and one-line errors when prod can't be reached

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin HEAD
```

Open the PR with the `pr` skill's template (`Refs SPA-46`).

---

## Sub-issue 2: `tempus db pull` (Tasks 5–7)

### Task 5: Transfer basics and the push plan

The push plan comes first because pull's guard is "the push plan isn't empty".

**Files:**
- Create: `src/tempus/transfer.py`
- Test: `tests/test_transfer.py` (create)

**Interfaces:**
- Consumes: `dev_conn` / `prod_conn` fixtures (Task 1); connections from `db.connect`
  (autocommit, `dict_row`).
- Produces:
  - `Key = tuple[str, str]`
  - `class TransferError(RuntimeError)`, `class SchemaMismatch(TransferError)`
  - `shared_columns(dev, prod, table: str) -> list[str]`
  - `copy_rows(src, dst, table: str, cols: list[str], where: sql.Composable = TRUE, params: tuple = (), *, into: str | None = None) -> int`
  - `select_keys(conn, series: list[Key] | None = None, concept: str | None = None) -> list[Key] | None`
  - `@dataclass(frozen=True) PushEntry(key: Key, data: Literal["new", "newer"] | None, placement: bool, observations: int = 0)`
  - `@dataclass PushPlan(entries: list[PushEntry], prod_ahead: list[Key], skipped_placements: list[Key])`, truthy when `entries` is non-empty
  - `plan_push(dev, prod, keys: list[Key] | None = None) -> PushPlan`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_transfer.py
from datetime import UTC, datetime, timedelta

import pytest

from tempus import transfer
from tempus.transfer import SchemaMismatch, copy_rows, plan_push, select_keys, shared_columns

T0 = datetime(2026, 10, 1, tzinfo=UTC)
LATER = T0 + timedelta(days=1)


def add_series(conn, sid, *, upstream=T0, synced=T0, points=3, values_from=0.0):
    """A series with `points` observations, one per 6 years so they span several chunks."""
    conn.execute(
        """
        INSERT INTO series (source, series_id, title, upstream_updated_at, last_synced_at)
        VALUES ('fred', %s, %s, %s, %s)
        ON CONFLICT (source, series_id) DO UPDATE SET
            upstream_updated_at = EXCLUDED.upstream_updated_at,
            last_synced_at = EXCLUDED.last_synced_at
        """,
        (sid, sid, upstream, synced),
    )
    conn.execute("DELETE FROM observations WHERE series_id = %s", (sid,))
    for k in range(points):
        conn.execute(
            "INSERT INTO observations VALUES ('fred', %s, %s, %s)",
            (sid, datetime(2000 + 6 * k, 1, 1, tzinfo=UTC), values_from + k),
        )


def add_concept(conn, cid):
    parent = cid.rsplit(".", 1)[0] if "." in cid else None
    conn.execute(
        "INSERT INTO concepts (id, parent_id, name, description, position)"
        " VALUES (%s, %s, %s, '', 0) ON CONFLICT DO NOTHING",
        (cid, parent, cid),
    )


def place(conn, sid, concept, method="auto", filed=T0):
    conn.execute(
        """
        INSERT INTO series_concepts (source, series_id, concept_id, method, filed_at)
        VALUES ('fred', %s, %s, %s, %s)
        ON CONFLICT (source, series_id) DO UPDATE SET
            concept_id = EXCLUDED.concept_id, method = EXCLUDED.method, filed_at = EXCLUDED.filed_at
        """,
        (sid, concept, method, filed),
    )


def obs(conn, sid):
    rows = conn.execute(
        "SELECT ts, value FROM observations WHERE series_id = %s ORDER BY ts", (sid,)
    )
    return [(r["ts"], r["value"]) for r in rows]


def test_shared_columns_lists_the_common_columns(dev_conn, prod_conn):
    cols = shared_columns(dev_conn, prod_conn, "series")
    assert cols[:2] == ["source", "series_id"] and "last_synced_at" in cols
    prod_conn.execute("ALTER TABLE series ADD COLUMN newer_feature text")
    assert "newer_feature" not in shared_columns(dev_conn, prod_conn, "series")


def test_schema_mismatch_names_the_side(dev_conn, prod_conn):
    prod_conn.execute("ALTER TABLE series DROP COLUMN upstream_updated_at")
    with pytest.raises(SchemaMismatch, match="run `tempus db init` on prod"):
        shared_columns(dev_conn, prod_conn, "series")


def test_copy_rows_reads_every_hypertable_chunk(dev_conn, prod_conn):
    add_series(prod_conn, "A", points=4)
    copy_rows(prod_conn, dev_conn, "series", shared_columns(dev_conn, prod_conn, "series"))
    n = copy_rows(
        prod_conn, dev_conn, "observations", shared_columns(dev_conn, prod_conn, "observations")
    )
    assert n == 4 and obs(dev_conn, "A") == obs(prod_conn, "A")


def test_select_keys(prod_conn):
    for cid in ("labor", "labor.unemployment", "laboratory"):
        add_concept(prod_conn, cid)
    for sid, cid in (("U", "labor.unemployment"), ("L", "labor"), ("X", "laboratory")):
        add_series(prod_conn, sid, points=0)
        place(prod_conn, sid, cid)
    assert select_keys(prod_conn) is None
    assert select_keys(prod_conn, concept="labor") == [("fred", "L"), ("fred", "U")]
    assert select_keys(prod_conn, series=[("fred", "X")], concept="labor.unemployment") == [
        ("fred", "U"),
        ("fred", "X"),
    ]


@pytest.mark.parametrize(
    "prod_state, expected",
    [
        (None, "new"),  # prod has no row
        ({"upstream": None, "synced": None}, "new"),  # snapshot-only on prod
        ({"upstream": T0 - timedelta(days=1), "synced": T0}, "newer"),
        ({"upstream": T0, "synced": T0}, "same"),
        ({"upstream": LATER, "synced": LATER}, "ahead"),
    ],
)
def test_push_plan_for_series_data(dev_conn, prod_conn, prod_state, expected):
    add_series(dev_conn, "A", upstream=T0, synced=T0)
    if prod_state:
        add_series(prod_conn, "A", upstream=prod_state["upstream"], synced=prod_state["synced"])
    plan = plan_push(dev_conn, prod_conn)
    data = {e.key: e.data for e in plan.entries}
    if expected in ("new", "newer"):
        assert data == {("fred", "A"): expected} and plan.entries[0].observations == 3
    elif expected == "same":
        assert not plan and plan.prod_ahead == []
    else:
        assert not plan and plan.prod_ahead == [("fred", "A")]


def test_push_plan_ignores_series_dev_never_synced(dev_conn, prod_conn):
    add_series(dev_conn, "A", upstream=None, synced=None, points=0)
    assert not plan_push(dev_conn, prod_conn)


def test_push_plan_falls_back_to_sync_times_without_revision_times(dev_conn, prod_conn):
    add_series(dev_conn, "A", upstream=None, synced=LATER)
    add_series(prod_conn, "A", upstream=None, synced=T0)
    assert [e.data for e in plan_push(dev_conn, prod_conn).entries] == ["newer"]


@pytest.mark.parametrize(
    "prod_placement, pushed",
    [
        (None, True),
        (("draft", T0), True),  # a Jev filing beats a draft
        (("auto", T0 - timedelta(days=1)), True),  # a newer filing wins
        (("auto", LATER), False),  # prod filed it more recently
        (("manual", T0 - timedelta(days=1)), False),  # never over a pin
    ],
)
def test_push_plan_for_placements(dev_conn, prod_conn, prod_placement, pushed):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
        add_series(conn, "A", upstream=None, synced=None, points=0)  # same metadata both sides
    place(dev_conn, "A", "labor", "auto", T0)
    if prod_placement:
        place(prod_conn, "A", "labor", *prod_placement)
    entries = plan_push(dev_conn, prod_conn).entries
    assert [(e.key, e.data, e.placement) for e in entries] == (
        [(("fred", "A"), None, True)] if pushed else []
    )


def test_push_plan_skips_placements_into_concepts_prod_lacks(dev_conn, prod_conn):
    add_concept(dev_conn, "labor.new_branch_concept")
    for conn in (dev_conn, prod_conn):
        add_series(conn, "A", upstream=None, synced=None, points=0)
    place(dev_conn, "A", "labor.new_branch_concept")
    plan = plan_push(dev_conn, prod_conn)
    assert not plan and plan.skipped_placements == [("fred", "A")]


def test_push_plan_never_pushes_drafts_or_pins(dev_conn, prod_conn):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
        add_series(conn, "A", upstream=None, synced=None, points=0)
        add_series(conn, "B", upstream=None, synced=None, points=0)
    place(dev_conn, "A", "labor", "draft", LATER)
    place(dev_conn, "B", "labor", "manual", LATER)
    assert not plan_push(dev_conn, prod_conn)


def test_push_plan_narrowed_to_keys(dev_conn, prod_conn):
    add_series(dev_conn, "A")
    add_series(dev_conn, "B")
    plan = plan_push(dev_conn, prod_conn, [("fred", "B")])
    assert [e.key for e in plan.entries] == [("fred", "B")]


def test_transfer_has_no_side_effects_when_planning(dev_conn, prod_conn):
    add_series(dev_conn, "A")
    plan_push(dev_conn, prod_conn)
    assert prod_conn.execute("SELECT count(*) AS n FROM series").fetchone()["n"] == 0
    assert transfer.TransferError  # exported for the CLI
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tempus.transfer'`

- [ ] **Step 3: Implement `src/tempus/transfer.py`**

```python
"""Copy data between the dev database and prod (the home base): `tempus db pull` / `push`.

Spec: docs/specs/2026-10-11-dev-prod-db-sync-design.md. Every function takes two open
connections, so tests run them between two local databases and the CLI supplies the tunnel.
Only `auto` placements (Jev filings) move: pins and drafts come from git on each side.
When SPA-16 adds `jev_decisions`, it joins pull and push as an insert-only table.
"""

from dataclasses import dataclass, field
from typing import Literal

import psycopg
from psycopg import sql

Key = tuple[str, str]  # (source, series_id)
RANK = {"draft": 0, "auto": 1, "manual": 2}
TRUE = sql.SQL("TRUE")

# Columns each table must have on both sides for a copy to mean anything.
REQUIRED = {
    "series": ("source", "series_id", "upstream_updated_at", "last_synced_at"),
    "observations": ("source", "series_id", "ts", "value"),
    "series_concepts": ("source", "series_id", "concept_id", "method", "filed_at"),
}


class TransferError(RuntimeError):
    """A pull or push that can't go ahead. The message is one line, ready for the CLI."""


class SchemaMismatch(TransferError):
    pass


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position
        """,
        (table,),
    )
    return [r["column_name"] for r in rows]


def shared_columns(dev: psycopg.Connection, prod: psycopg.Connection, table: str) -> list[str]:
    """`table`'s columns present on both sides, in dev's order."""
    dev_cols, prod_cols = _columns(dev, table), _columns(prod, table)
    for side, cols in (("dev", dev_cols), ("prod", prod_cols)):
        missing = [c for c in REQUIRED[table] if c not in cols]
        if missing:
            raise SchemaMismatch(
                f"{table} on {side} lacks {', '.join(missing)}: run `tempus db init` on {side}"
            )
    return [c for c in dev_cols if c in prod_cols]


def _where(keys: list[Key] | None) -> tuple[sql.Composable, tuple]:
    if keys is None:
        return TRUE, ()
    return sql.SQL("(source || ':' || series_id) = ANY(%s)"), ([f"{s}:{i}" for s, i in keys],)


def copy_rows(
    src: psycopg.Connection,
    dst: psycopg.Connection,
    table: str,
    cols: list[str],
    where: sql.Composable = TRUE,
    params: tuple = (),
    *,
    into: str | None = None,
) -> int:
    """Stream `table`'s rows matching `where` from src into dst (or its table `into`).

    The SELECT form matters: plain `COPY observations TO` on a hypertable returns no rows.
    Binary when both servers share a major version, else text.
    """
    same_major = src.info.server_version // 10000 == dst.info.server_version // 10000
    fmt = sql.SQL("BINARY" if same_major else "TEXT")
    col_list = sql.SQL(", ").join(map(sql.Identifier, cols))
    out = sql.SQL("COPY (SELECT {c} FROM {t} WHERE {w}) TO STDOUT (FORMAT {f})").format(
        c=col_list, t=sql.Identifier(table), w=where, f=fmt
    )
    inn = sql.SQL("COPY {t} ({c}) FROM STDIN (FORMAT {f})").format(
        t=sql.Identifier(into or table), c=col_list, f=fmt
    )
    with src.cursor() as sc, dst.cursor() as dc:
        with sc.copy(out, params) as reader, dc.copy(inn) as writer:
            for block in reader:
                writer.write(block)
        return dc.rowcount


def select_keys(
    conn: psycopg.Connection, series: list[Key] | None = None, concept: str | None = None
) -> list[Key] | None:
    """The series a pull or push covers: `series` plus everything placed at or under
    `concept` (by `conn`'s placements). None means all."""
    if series is None and concept is None:
        return None
    keys = set(series or [])
    if concept:
        rows = conn.execute(
            "SELECT source, series_id FROM series_concepts"
            " WHERE concept_id = %(c)s OR concept_id LIKE %(under)s",
            {"c": concept, "under": concept + ".%"},
        )
        keys |= {(r["source"], r["series_id"]) for r in rows}
    return sorted(keys)


@dataclass(frozen=True)
class PushEntry:
    key: Key
    data: Literal["new", "newer"] | None  # None: only the placement changes
    placement: bool
    observations: int = 0


@dataclass
class PushPlan:
    entries: list[PushEntry] = field(default_factory=list)
    prod_ahead: list[Key] = field(default_factory=list)  # prod has a later revision: pull
    skipped_placements: list[Key] = field(default_factory=list)  # concept not in prod's tree

    def __bool__(self) -> bool:
        return bool(self.entries)


def _series_state(conn: psycopg.Connection, keys: list[Key] | None, *, synced_only: bool):
    w, p = _where(keys)
    q = sql.SQL(
        "SELECT source, series_id, upstream_updated_at, last_synced_at FROM series WHERE {w}"
    ).format(w=w)
    if synced_only:
        q += sql.SQL(" AND last_synced_at IS NOT NULL")
    return {(r["source"], r["series_id"]): r for r in conn.execute(q, p)}


def _placements(conn: psycopg.Connection, keys: list[Key] | None, methods: tuple[str, ...]):
    w, p = _where(keys)
    q = sql.SQL(
        "SELECT source, series_id, concept_id, method, filed_at FROM series_concepts"
        " WHERE method = ANY(%s) AND {w}"
    ).format(w=w)
    return {(r["source"], r["series_id"]): r for r in conn.execute(q, (list(methods), *p))}


def _observation_counts(conn: psycopg.Connection, keys: list[Key]) -> dict[Key, int]:
    if not keys:
        return {}
    w, p = _where(keys)
    q = sql.SQL(
        "SELECT source, series_id, count(*) AS n FROM observations WHERE {w} GROUP BY 1, 2"
    ).format(w=w)
    return {(r["source"], r["series_id"]): r["n"] for r in conn.execute(q, p)}


def _data_reason(dev_row: dict, prod_row: dict | None) -> str:
    """new | newer (push) or same | ahead (don't). The provider's revision time decides;
    sources that don't report one fall back to when each side synced."""
    if prod_row is None or prod_row["last_synced_at"] is None:
        return "new"
    d, p = dev_row["upstream_updated_at"], prod_row["upstream_updated_at"]
    if d is None or p is None:
        d, p = dev_row["last_synced_at"], prod_row["last_synced_at"]
    if d > p:
        return "newer"
    return "same" if d == p else "ahead"


def _wins(mine: dict, theirs: dict | None) -> bool:
    """manual > auto > draft; between equal methods, the later filing."""
    if theirs is None:
        return True
    if RANK[mine["method"]] != RANK[theirs["method"]]:
        return RANK[mine["method"]] > RANK[theirs["method"]]
    return mine["filed_at"] > theirs["filed_at"]


def plan_push(
    dev: psycopg.Connection, prod: psycopg.Connection, keys: list[Key] | None = None
) -> PushPlan:
    """What `tempus db push` would send: whole series dev synced more recently, and Jev
    filings that outrank prod's placements. Reads only."""
    plan = PushPlan()
    dev_series = _series_state(dev, keys, synced_only=True)
    dev_placed = _placements(dev, keys, ("auto",))
    wanted = sorted(set(dev_series) | set(dev_placed))
    prod_series = _series_state(prod, wanted, synced_only=False)

    data: dict[Key, str] = {}
    for key, row in dev_series.items():
        reason = _data_reason(row, prod_series.get(key))
        if reason in ("new", "newer"):
            data[key] = reason
        elif reason == "ahead":
            plan.prod_ahead.append(key)

    prod_placed = _placements(prod, sorted(dev_placed), tuple(RANK))
    prod_concepts = {r["id"] for r in prod.execute("SELECT id FROM concepts")}
    placed: set[Key] = set()
    for key, row in dev_placed.items():
        if key not in prod_series and key not in data:
            continue  # the series isn't on prod and isn't being pushed
        if not _wins(row, prod_placed.get(key)):
            continue
        if row["concept_id"] is not None and row["concept_id"] not in prod_concepts:
            plan.skipped_placements.append(key)
            continue
        placed.add(key)

    counts = _observation_counts(dev, sorted(data))
    for key in sorted(set(data) | placed):
        plan.entries.append(PushEntry(key, data.get(key), key in placed, counts.get(key, 0)))
    plan.prod_ahead.sort()
    plan.skipped_placements.sort()
    return plan
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py -v && mise run lint`
Expected: PASS. If `Cursor.copy` rejects `params`, inline the key list with
`sql.Literal([...])` inside `_where` instead and keep the tests unchanged. If `dc.rowcount`
is `-1` after `COPY FROM`, return `SELECT count(*)` over the same `where` on `src` instead.

- [ ] **Step 5: Commit**

```bash
git add src/tempus/transfer.py tests/test_transfer.py
git commit -m "transfer: shared columns, COPY between databases, and the push plan

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 6: `transfer.pull`

**Files:**
- Modify: `src/tempus/transfer.py`
- Test: `tests/test_transfer.py` (append)

**Interfaces:**
- Consumes: everything from Task 5.
- Produces:
  - `class UnpushedWork(TransferError)` with `.plan: PushPlan`
  - `@dataclass(frozen=True) PullResult(rows: dict[str, int], skipped_placements: int, missing: list[Key])`
  - `pull(dev, prod, keys: list[Key] | None = None, *, metadata_only: bool = False, discard_local: bool = False, prepare: Callable[[psycopg.Connection], None] = _nothing) -> PullResult`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_transfer.py`)

```python
from tempus.transfer import UnpushedWork, pull


def series_ids(conn):
    return [r["series_id"] for r in conn.execute("SELECT series_id FROM series ORDER BY 1")]


def test_full_pull_replaces_dev(dev_conn, prod_conn):
    add_series(dev_conn, "OLD", upstream=None, synced=None)  # snapshot-only: nothing to push
    add_series(prod_conn, "A", points=4)
    add_series(prod_conn, "B", points=2)
    result = pull(dev_conn, prod_conn)
    assert series_ids(dev_conn) == ["A", "B"]
    assert obs(dev_conn, "A") == obs(prod_conn, "A")
    assert result.rows == {"series": 2, "observations": 6, "series_concepts": 0}


def test_pull_refuses_while_dev_holds_unpushed_syncs(dev_conn, prod_conn):
    add_series(prod_conn, "A", upstream=T0)
    add_series(dev_conn, "A", upstream=LATER, values_from=10)  # a backfill not pushed yet
    with pytest.raises(UnpushedWork, match="push them first") as exc:
        pull(dev_conn, prod_conn)
    assert [e.key for e in exc.value.plan.entries] == [("fred", "A")]
    assert obs(dev_conn, "A")[0][1] == 10  # dev untouched
    pull(dev_conn, prod_conn, discard_local=True)
    assert obs(dev_conn, "A")[0][1] == 0


def test_narrowed_pull_leaves_other_dev_series_alone(dev_conn, prod_conn):
    add_series(dev_conn, "KEEP", upstream=None, synced=None)
    add_series(prod_conn, "A")
    add_series(prod_conn, "B")
    pull(dev_conn, prod_conn, [("fred", "A")])
    assert series_ids(dev_conn) == ["A", "KEEP"]


def test_pull_keeps_dev_series_prod_lacks(dev_conn, prod_conn):
    add_series(dev_conn, "DEVONLY", upstream=None, synced=None, points=2)
    add_series(prod_conn, "A")
    result = pull(dev_conn, prod_conn, [("fred", "A"), ("fred", "DEVONLY")])
    assert result.missing == [("fred", "DEVONLY")]
    assert len(obs(dev_conn, "DEVONLY")) == 2


def test_metadata_only_pull_copies_no_observations(dev_conn, prod_conn):
    add_series(prod_conn, "A")
    result = pull(dev_conn, prod_conn, metadata_only=True)
    assert series_ids(dev_conn) == ["A"] and obs(dev_conn, "A") == []
    assert "observations" not in result.rows


def test_pull_merges_prod_filings_under_the_branch_tree(dev_conn, prod_conn):
    for cid in ("labor", "gone"):
        add_concept(prod_conn, cid)
    for sid, cid in (("A", "labor"), ("B", "gone"), ("C", "labor")):
        add_series(prod_conn, sid, points=0)
        place(prod_conn, sid, cid, "auto")

    def prepare(conn):  # stands in for `ontology load`: this branch's tree and a pin
        add_concept(conn, "labor")
        add_concept(conn, "pinned")
        place(conn, "C", "pinned", "manual")

    result = pull(dev_conn, prod_conn, prepare=prepare)
    rows = dev_conn.execute(
        "SELECT series_id, concept_id, method FROM series_concepts ORDER BY 1"
    ).fetchall()
    assert [(r["series_id"], r["concept_id"], r["method"]) for r in rows] == [
        ("A", "labor", "auto"),
        ("C", "pinned", "manual"),  # a pin from git beats prod's filing
    ]
    assert result.skipped_placements == 1  # B's concept isn't in this branch's tree


def test_prepare_runs_after_series_arrive(dev_conn, prod_conn):
    add_series(prod_conn, "A")
    seen = []
    pull(dev_conn, prod_conn, prepare=lambda c: seen.append(series_ids(c)))
    assert seen == [["A"]]


def test_failed_pull_leaves_dev_untouched(dev_conn, prod_conn):
    add_series(dev_conn, "OLD", upstream=None, synced=None)
    add_series(prod_conn, "A")

    def boom(conn):
        raise RuntimeError("ontology failed to load")

    with pytest.raises(RuntimeError):
        pull(dev_conn, prod_conn, prepare=boom)
    assert series_ids(dev_conn) == ["OLD"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py -v -k "pull or prepare"`
Expected: FAIL with `ImportError: cannot import name 'UnpushedWork'`

- [ ] **Step 3: Implement** (append to `src/tempus/transfer.py`; add `from collections.abc import Callable`)

```python
class UnpushedWork(TransferError):
    def __init__(self, plan: PushPlan):
        self.plan = plan
        super().__init__(
            f"dev holds {len(plan.entries)} series or filings prod doesn't: "
            "push them first (tempus db push --yes), or pull with --discard-local"
        )


@dataclass(frozen=True)
class PullResult:
    rows: dict[str, int]
    skipped_placements: int  # prod filings into concepts this branch's tree lacks
    missing: list[Key]  # asked for, but not on prod: dev's copy kept


def _nothing(conn: psycopg.Connection) -> None:
    pass


def _set_clause(cols: list[str]) -> sql.Composable:
    return sql.SQL(", ").join(
        sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
        for c in cols
        if c not in ("source", "series_id")
    )


def pull(
    dev: psycopg.Connection,
    prod: psycopg.Connection,
    keys: list[Key] | None = None,
    *,
    metadata_only: bool = False,
    discard_local: bool = False,
    prepare: Callable[[psycopg.Connection], None] = _nothing,
) -> PullResult:
    """Replace dev's copy of the selected series with prod's, in one dev transaction.

    `prepare(dev)` rebuilds the git-derived tables (concepts, pins, drafts, cards) once the
    series are in and before prod's filings merge in, so filings land only under concepts
    this branch's tree has.
    """
    missing: list[Key] = []
    if keys is not None:
        on_prod = set(_series_state(prod, keys, synced_only=False))
        missing = [k for k in keys if k not in on_prod]
        keys = [k for k in keys if k in on_prod]
    if not discard_local:
        plan = plan_push(dev, prod, keys)
        if plan:
            raise UnpushedWork(plan)
    series_cols = shared_columns(dev, prod, "series")
    obs_cols = shared_columns(dev, prod, "observations")
    placement_cols = shared_columns(dev, prod, "series_concepts")
    w, p = _where(keys)
    rows: dict[str, int] = {}
    with dev.transaction():
        if keys is None:
            dev.execute("TRUNCATE observations, series_concepts, series")
        else:
            dev.execute(sql.SQL("DELETE FROM series WHERE {w}").format(w=w), p)  # cascades
        rows["series"] = copy_rows(prod, dev, "series", series_cols, w, p)
        if not metadata_only:
            rows["observations"] = copy_rows(prod, dev, "observations", obs_cols, w, p)
        prepare(dev)
        dev.execute(
            "CREATE TEMP TABLE _pull_placements (LIKE series_concepts INCLUDING DEFAULTS)"
            " ON COMMIT DROP"
        )
        auto = sql.SQL("method = 'auto' AND {w}").format(w=w)
        copy_rows(prod, dev, "series_concepts", placement_cols, auto, p, into="_pull_placements")
        cols = sql.SQL(", ").join(map(sql.Identifier, placement_cols))
        rows["series_concepts"] = dev.execute(
            sql.SQL(
                """
                INSERT INTO series_concepts ({cols}) SELECT {cols} FROM _pull_placements t
                WHERE (t.concept_id IS NULL
                       OR EXISTS (SELECT 1 FROM concepts c WHERE c.id = t.concept_id))
                  AND EXISTS (SELECT 1 FROM series s
                              WHERE s.source = t.source AND s.series_id = t.series_id)
                ON CONFLICT (source, series_id) DO UPDATE SET {sets}
                WHERE series_concepts.method <> 'manual'
                """
            ).format(cols=cols, sets=_set_clause(placement_cols))
        ).rowcount
        skipped = dev.execute(
            """
            SELECT count(*) AS n FROM _pull_placements t
            WHERE t.concept_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM concepts c WHERE c.id = t.concept_id)
            """
        ).fetchone()["n"]
    return PullResult(rows, skipped, missing)
```

Note `rows["series_concepts"]` counts inserted or updated placements; the `ON CONFLICT … WHERE`
skip of a pinned series doesn't count.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py -v && mise run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tempus/transfer.py tests/test_transfer.py
git commit -m "transfer.pull: replace dev's copy with prod's, guarded against unpushed work

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 7: `tempus db pull` and the writer guard

The writer guard lands here because `pull` is the first command that must refuse in prod
mode; Task 10 applies it to the existing commands.

**Files:**
- Modify: `src/tempus/cli.py`
- Test: `tests/test_cli_db.py` (append)

**Interfaces:**
- Consumes: `transfer.pull`, `transfer.select_keys`, `transfer.UnpushedWork`, `transfer.PushPlan`,
  `remote.Tunnel`, `remote.prod_url`, `remote.is_local`, `_load_ontology`, `_cards`.
- Produces:
  - `READ_ONLY: str` (the Global Constraints message)
  - `writes(fn)`: decorator refusing when `Settings().db == "prod"`
  - `_parse_keys(value: str | None) -> list[transfer.Key] | None`
  - `_prod(owner: bool)`: context manager yielding `(settings, dev, prod)`
  - `_print_plan(plan: transfer.PushPlan) -> None`
  - CLI `tempus db pull [--series a:b,c:d] [--concept id] [--metadata-only] [--discard-local]`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_db.py`)

```python
from contextlib import contextmanager
from pathlib import Path

import pytest

from tempus import cli
from tempus.config import Settings

from .test_transfer import LATER, add_series

ROOT = Path(__file__).parents[1]


@pytest.fixture
def linked(monkeypatch, dev_conn, prod_conn):
    """`_prod` yielding the two test databases instead of opening a tunnel."""
    monkeypatch.chdir(ROOT)  # the real ontology, catalog and cards

    @contextmanager
    def fake(owner):
        yield Settings(), dev_conn, prod_conn

    monkeypatch.setattr(cli, "_prod", fake)
    return dev_conn, prod_conn


def test_pull_cli(linked):
    dev, prod = linked
    add_series(prod, "A")
    result = runner.invoke(app, ["db", "pull"])
    assert result.exit_code == 0, result.output
    assert "Pulled 1 series, 3 observations" in result.output
    assert dev.execute("SELECT count(*) AS n FROM concepts").fetchone()["n"] > 100  # prepare ran


def test_pull_cli_shows_unpushed_work(linked):
    dev, prod = linked
    add_series(prod, "A")
    add_series(dev, "A", upstream=LATER)
    result = runner.invoke(app, ["db", "pull"])
    assert result.exit_code == 1
    assert "fred:A" in result.output and "--discard-local" in result.output


def test_pull_refuses_in_prod_mode():
    result = runner.invoke(app, ["db", "pull"], env={"TEMPUS_DB": "prod"})
    assert result.exit_code == 1 and "read-only" in result.output


def test_pull_refuses_when_dev_is_not_local():
    url = "postgres://postgres:password@db.example.com/tempus"
    result = runner.invoke(app, ["db", "pull"], env={"DATABASE_URL": url})
    assert result.exit_code == 1 and "isn't local" in result.output


def test_pull_names_a_missing_password():
    result = runner.invoke(app, ["db", "pull"], env={"TEMPUS_PROD_RO_PASSWORD": ""})
    assert result.exit_code == 1 and "TEMPUS_PROD_RO_PASSWORD" in result.output


def test_pull_reports_a_refused_connection(monkeypatch):
    class DeadTunnel:  # a tunnel whose far end refuses: port 1 is never open
        local_port = 1

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(cli.remote, "Tunnel", DeadTunnel)
    result = runner.invoke(app, ["db", "pull"], env={"TEMPUS_PROD_RO_PASSWORD": "x"})
    assert result.exit_code == 1
    assert "Can't connect to prod" in result.output and "Traceback" not in result.output


@pytest.mark.parametrize(
    "text, keys",
    [(None, None), ("fred:UNRATE", [("fred", "UNRATE")]), ("fred:A, fred:B", [("fred", "A"), ("fred", "B")])],
)
def test_parse_keys(text, keys):
    assert cli._parse_keys(text) == keys


def test_parse_keys_needs_a_source():
    import typer

    with pytest.raises(typer.BadParameter, match="source:series_id"):
        cli._parse_keys("UNRATE")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_db.py -v`
Expected: FAIL (`No such command 'pull'`).

- [ ] **Step 3: Implement** (in `src/tempus/cli.py`)

Add to the imports:

```python
import functools
from contextlib import contextmanager

import psycopg

from tempus import catalog, db, ontology, remote, transfer
```

Add after `_parse_interval`:

```python
READ_ONLY = (
    "TEMPUS_DB=prod is read-only: run this on the home base (ssh homebase), or unset TEMPUS_DB."
)


def writes(fn):
    """Mark a command that writes: it refuses in prod mode, before any tunnel opens."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if Settings().db == "prod":
            console.print(f"[red]{READ_ONLY}[/red]")
            raise typer.Exit(1)
        return fn(*args, **kwargs)

    return wrapper


def _parse_keys(value: str | None) -> list[transfer.Key] | None:
    if value is None:
        return None
    keys = []
    for part in value.split(","):
        source, sep, series_id = part.strip().partition(":")
        if not sep or not source or not series_id:
            raise typer.BadParameter(f"'{part.strip()}': use source:series_id, e.g. fred:UNRATE")
        keys.append((source, series_id))
    return keys


@contextmanager
def _prod(owner: bool):
    """(settings, dev, prod) for pull and push: dev must be local; prod through a tunnel,
    as tempus_ro or (owner=True) as the owner."""
    settings = Settings()
    if not remote.is_local(settings.database_url):
        console.print(
            "[red]DATABASE_URL isn't local:[/red] pull and push only write to a dev database "
            "on this machine."
        )
        raise typer.Exit(1)
    setting = "TEMPUS_PROD_PASSWORD" if owner else "TEMPUS_PROD_RO_PASSWORD"
    try:
        remote.prod_url(settings, 0, owner=owner)  # a missing password fails before ssh
        with (
            remote.Tunnel(settings.homebase_host, settings.prod_db_port) as tunnel,
            db.connect(settings.database_url) as dev,
        ):
            try:
                prod = db.connect(remote.prod_url(settings, tunnel.local_port, owner=owner))
            except psycopg.OperationalError as exc:
                why = str(exc).strip().splitlines()[-1]
                console.print(
                    f"[red]Can't connect to prod:[/red] {why}\nCheck {setting} and "
                    f"TEMPUS_PROD_DB_PORT ({settings.prod_db_port}) in .env."
                )
                raise typer.Exit(1) from exc
            with prod:
                yield settings, dev, prod
    except (remote.RemoteError, transfer.TransferError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _key(key: transfer.Key) -> str:
    return f"{key[0]}:{key[1]}"


def _print_plan(plan: transfer.PushPlan) -> None:
    if plan.entries:
        table = Table("series", "data", "observations", "placement")
        for e in plan.entries:
            table.add_row(
                _key(e.key), e.data or "—", str(e.observations) if e.data else "—",
                "Jev placement" if e.placement else "—",
            )  # fmt: skip
        console.print(table)
    if plan.prod_ahead:
        console.print(
            f"[yellow]Prod is ahead for {len(plan.prod_ahead)} series (pull them):[/yellow] "
            + ", ".join(map(_key, plan.prod_ahead))
        )
    if plan.skipped_placements:
        console.print(
            "[yellow]Jev placements skipped, their concepts aren't on prod yet:[/yellow] "
            + ", ".join(map(_key, plan.skipped_placements))
        )
```

Add after `db_where`:

```python
@db_app.command("pull")
@writes
def db_pull(
    series: str | None = typer.Option(None, help="Only these, e.g. fred:UNRATE,fred:CPIAUCSL"),
    concept: str | None = typer.Option(None, help="Only series placed at or under this concept"),
    metadata_only: bool = typer.Option(False, "--metadata-only", help="No observations"),
    discard_local: bool = typer.Option(
        False, "--discard-local", help="Overwrite dev's unpushed syncs and filings"
    ),
):
    """Copy prod's series, observations and Jev filings into the dev database."""
    from tempus.cards.repository import sync_index

    started = time.monotonic()
    with _prod(owner=False) as (settings, dev, prod):
        keys = transfer.select_keys(prod, _parse_keys(series), concept)

        def prepare(conn):
            _load_ontology(conn, settings, quiet=True)
            sync_index(conn, _cards())

        try:
            result = transfer.pull(
                dev, prod, keys,
                metadata_only=metadata_only, discard_local=discard_local, prepare=prepare,
            )  # fmt: skip
        except transfer.UnpushedWork as exc:
            _print_plan(exc.plan)
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    rows = ", ".join(f"{n} {table.replace('series_concepts', 'Jev placements')}" for table, n in result.rows.items())
    console.print(f"[green]Pulled {rows}[/green] in {time.monotonic() - started:.1f}s")
    if result.missing:
        console.print(
            "[yellow]Not on prod (dev's copy kept):[/yellow] " + ", ".join(map(_key, result.missing))
        )
    if result.skipped_placements:
        console.print(
            f"[yellow]{result.skipped_placements} Jev placements skipped:[/yellow] "
            "their concepts aren't in this branch's ontology"
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli_db.py -v && mise run test && mise run lint`
Expected: PASS. If ruff reformats the `# fmt: skip` lines away, keep its formatting.

- [ ] **Step 5: Commit, push, PR for sub-issue 2**

```bash
git add src/tempus/cli.py tests/test_cli_db.py
git commit -m "tempus db pull: prod's series, observations and filings into dev over the tunnel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin HEAD
```

---

## Sub-issue 3: `tempus db push` (Tasks 8–9)

### Task 8: `transfer.apply_push`

**Files:**
- Modify: `src/tempus/transfer.py`
- Test: `tests/test_transfer.py` (append)

**Interfaces:**
- Consumes: `PushPlan`, `PushEntry`, `copy_rows`, `shared_columns`, `_where`,
  `_observation_counts`, `_set_clause` (Tasks 5–6).
- Produces: `@dataclass(frozen=True) PushResult(series: int, observations: int, placements: int)`;
  `apply_push(dev, prod, plan: PushPlan) -> PushResult`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_transfer.py`)

```python
from tempus.transfer import apply_push


def test_push_replaces_prod_s_copy_of_a_newer_series(dev_conn, prod_conn):
    add_series(prod_conn, "A", upstream=T0, points=5)  # stale, with points since revised away
    add_series(dev_conn, "A", upstream=LATER, points=3, values_from=10)
    result = apply_push(dev_conn, prod_conn, plan_push(dev_conn, prod_conn))
    assert (result.series, result.observations) == (1, 3)
    assert obs(prod_conn, "A") == obs(dev_conn, "A")
    row = prod_conn.execute("SELECT upstream_updated_at FROM series WHERE series_id = 'A'").fetchone()
    assert row["upstream_updated_at"] == LATER
    run = prod_conn.execute("SELECT status, rows_upserted, finished_at FROM sync_runs").fetchone()
    assert run["status"] == "pushed" and run["rows_upserted"] == 3 and run["finished_at"]


def test_push_adds_new_series_and_never_deletes_others(dev_conn, prod_conn):
    add_series(prod_conn, "PRODONLY")
    add_series(dev_conn, "NEW")
    apply_push(dev_conn, prod_conn, plan_push(dev_conn, prod_conn))
    assert series_ids(prod_conn) == ["NEW", "PRODONLY"] and len(obs(prod_conn, "PRODONLY")) == 3


def test_push_ships_a_jev_filing_over_a_draft(dev_conn, prod_conn):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
        add_concept(conn, "prices")
        add_series(conn, "A", upstream=None, synced=None, points=0)
    place(prod_conn, "A", "prices", "draft")
    place(dev_conn, "A", "labor", "auto", LATER)
    result = apply_push(dev_conn, prod_conn, plan_push(dev_conn, prod_conn))
    assert (result.series, result.placements) == (0, 1)
    row = prod_conn.execute("SELECT concept_id, method FROM series_concepts").fetchone()
    assert (row["concept_id"], row["method"]) == ("labor", "auto")
    assert prod_conn.execute("SELECT count(*) AS n FROM sync_runs").fetchone()["n"] == 0


def test_push_then_pull_leaves_nothing_to_push(dev_conn, prod_conn):
    for conn in (dev_conn, prod_conn):
        add_concept(conn, "labor")
    add_series(dev_conn, "A", upstream=LATER)
    place(dev_conn, "A", "labor", "auto", LATER)
    apply_push(dev_conn, prod_conn, plan_push(dev_conn, prod_conn))
    assert not plan_push(dev_conn, prod_conn)  # pushing twice sends nothing
    pull(dev_conn, prod_conn, prepare=lambda c: add_concept(c, "labor"))
    assert not plan_push(dev_conn, prod_conn)  # and a pull doesn't create work


def test_failed_push_rolls_back(dev_conn, prod_conn):
    add_series(dev_conn, "A", upstream=LATER)
    add_series(prod_conn, "A", upstream=T0, values_from=5)
    plan = plan_push(dev_conn, prod_conn)
    prod_conn.execute("ALTER TABLE sync_runs ADD CONSTRAINT no_push CHECK (status <> 'pushed')")
    with pytest.raises(Exception):
        apply_push(dev_conn, prod_conn, plan)
    assert obs(prod_conn, "A")[0][1] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_transfer.py -v -k push`
Expected: FAIL with `ImportError: cannot import name 'apply_push'`

- [ ] **Step 3: Implement** (append to `src/tempus/transfer.py`)

```python
@dataclass(frozen=True)
class PushResult:
    series: int
    observations: int
    placements: int


def apply_push(dev: psycopg.Connection, prod: psycopg.Connection, plan: PushPlan) -> PushResult:
    """Apply `plan` to prod in one transaction. Each series in it is replaced whole (its row
    and all its observations); nothing else in prod is deleted. Pushed series log a
    `sync_runs` row with status `pushed`."""
    data_keys = [e.key for e in plan.entries if e.data]
    placement_keys = [e.key for e in plan.entries if e.placement]
    series_cols = shared_columns(dev, prod, "series")
    obs_cols = shared_columns(dev, prod, "observations")
    placement_cols = shared_columns(dev, prod, "series_concepts")
    observations = 0
    with prod.transaction():
        w, p = _where([e.key for e in plan.entries])
        # A scheduled sync on the mini touching these series waits for this transaction.
        prod.execute(sql.SQL("SELECT 1 FROM series WHERE {w} FOR UPDATE").format(w=w), p)
        if data_keys:
            w, p = _where(data_keys)
            cols = sql.SQL(", ").join(map(sql.Identifier, series_cols))
            prod.execute(
                "CREATE TEMP TABLE _push_series (LIKE series INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            copy_rows(dev, prod, "series", series_cols, w, p, into="_push_series")
            prod.execute(
                sql.SQL(
                    "INSERT INTO series ({cols}) SELECT {cols} FROM _push_series"
                    " ON CONFLICT (source, series_id) DO UPDATE SET {sets}"
                ).format(cols=cols, sets=_set_clause(series_cols))
            )
            prod.execute(sql.SQL("DELETE FROM observations WHERE {w}").format(w=w), p)
            observations = copy_rows(dev, prod, "observations", obs_cols, w, p)
            counts = _observation_counts(prod, data_keys)
            with prod.cursor() as cur:
                cur.executemany(
                    "INSERT INTO sync_runs (source, series_id, finished_at, status, rows_upserted)"
                    " VALUES (%s, %s, now(), 'pushed', %s)",
                    [(s, i, counts.get((s, i), 0)) for s, i in data_keys],
                )
        if placement_keys:
            w, p = _where(placement_keys)
            cols = sql.SQL(", ").join(map(sql.Identifier, placement_cols))
            prod.execute(
                "CREATE TEMP TABLE _push_placements"
                " (LIKE series_concepts INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            copy_rows(dev, prod, "series_concepts", placement_cols, w, p, into="_push_placements")
            prod.execute(
                sql.SQL(
                    "INSERT INTO series_concepts ({cols}) SELECT {cols} FROM _push_placements"
                    " ON CONFLICT (source, series_id) DO UPDATE SET {sets}"
                ).format(cols=cols, sets=_set_clause(placement_cols))
            )
    return PushResult(len(data_keys), observations, len(placement_keys))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_transfer.py -v && mise run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tempus/transfer.py tests/test_transfer.py
git commit -m "transfer.apply_push: merge whole series and Jev filings into prod, never deleting

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 9: `tempus db push`

**Files:**
- Modify: `src/tempus/cli.py`
- Test: `tests/test_cli_db.py` (append)

**Interfaces:**
- Consumes: `transfer.plan_push`, `transfer.apply_push`, `_prod`, `_print_plan`, `_parse_keys`,
  `writes` (Task 7).
- Produces: CLI `tempus db push [--series …] [--concept …] [--yes]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_db.py`)

```python
def test_push_is_a_dry_run_by_default(linked):
    dev, prod = linked
    add_series(dev, "A")
    result = runner.invoke(app, ["db", "push"])
    assert result.exit_code == 0, result.output
    assert "fred:A" in result.output and "Dry run" in result.output
    assert prod.execute("SELECT count(*) AS n FROM series").fetchone()["n"] == 0


def test_push_yes_applies_the_plan(linked):
    dev, prod = linked
    add_series(dev, "A")
    result = runner.invoke(app, ["db", "push", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Pushed 1 series (3 observations), 0 Jev placements" in result.output
    assert prod.execute("SELECT count(*) AS n FROM observations").fetchone()["n"] == 3


def test_push_with_nothing_to_send(linked):
    result = runner.invoke(app, ["db", "push", "--yes"])
    assert result.exit_code == 0 and "Nothing to push" in result.output


def test_push_refuses_in_prod_mode():
    result = runner.invoke(app, ["db", "push", "--yes"], env={"TEMPUS_DB": "prod"})
    assert result.exit_code == 1 and "read-only" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_db.py -v -k push`
Expected: FAIL (`No such command 'push'`).

- [ ] **Step 3: Implement** (after `db_pull` in `src/tempus/cli.py`)

```python
@db_app.command("push")
@writes
def db_push(
    series: str | None = typer.Option(None, help="Only these, e.g. fred:UNRATE,fred:CPIAUCSL"),
    concept: str | None = typer.Option(None, help="Only series placed at or under this concept"),
    yes: bool = typer.Option(False, "--yes", help="Apply the plan (default: only show it)"),
):
    """Send series dev synced more recently, and Jev filings, to prod. Never deletes."""
    with _prod(owner=True) as (_settings, dev, prod):
        plan = transfer.plan_push(dev, prod, transfer.select_keys(dev, _parse_keys(series), concept))
        _print_plan(plan)
        if not plan:
            console.print("Nothing to push.")
            return
        if not yes:
            console.print("Dry run: nothing written. Run [bold]tempus db push --yes[/bold] to push.")
            return
        result = transfer.apply_push(dev, prod, plan)
    console.print(
        f"[green]Pushed {result.series} series ({result.observations} observations), "
        f"{result.placements} Jev placements[/green]"
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_cli_db.py -v && mise run test && mise run lint`
Expected: PASS.

- [ ] **Step 5: Commit, push, PR for sub-issue 3**

```bash
git add src/tempus/cli.py tests/test_cli_db.py
git commit -m "tempus db push: dry run by default, --yes merges into prod

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin HEAD
```

---

## Sub-issue 4: read-only prod mode (Tasks 10–12)

### Task 10: Writing commands refuse in prod mode; a backstop for the rest

**Files:**
- Modify: `src/tempus/cli.py` (decorate writers; add `main`)
- Modify: `pyproject.toml` (`[project.scripts]`)
- Test: `tests/test_cli_db.py` (append)

**Interfaces:**
- Consumes: `writes`, `READ_ONLY` (Task 7), `remote.RemoteError`.
- Produces: `main() -> None`, the `tempus` entry point.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli_db.py`)

```python
WRITERS = [
    ["db", "init"],
    ["db", "readonly-role"],
    ["catalog", "add", "fred", "UNRATE"],
    ["catalog", "remove", "fred", "UNRATE"],
    ["catalog", "import-snapshot", "data/fred/series-snapshot.json"],
    ["sync"],
    ["series", "delete", "UNRATE"],
    ["ontology", "load"],
    ["ontology", "file"],
    ["card", "load"],
]


@pytest.mark.parametrize("argv", WRITERS, ids=" ".join)
def test_writers_refuse_in_prod_mode_before_any_tunnel(argv, monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(cli.remote, "Tunnel", lambda *a, **k: pytest.fail("opened a tunnel"))
    result = runner.invoke(app, argv, env={"TEMPUS_DB": "prod", "TEMPUS_PROD_RO_PASSWORD": "x"})
    assert result.exit_code == 1, result.output
    assert "read-only" in result.output


def test_main_turns_a_read_only_error_into_one_line(monkeypatch, capsys):
    def boom():
        raise psycopg.errors.ReadOnlySqlTransaction("cannot execute INSERT in a read-only transaction")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1 and "TEMPUS_DB=prod is read-only" in capsys.readouterr().out


def test_main_turns_a_remote_error_into_one_line(monkeypatch, capsys):
    def boom():
        raise cli.remote.RemoteError("can't reach homebase over SSH")

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1 and "can't reach homebase" in capsys.readouterr().out
```

Add `import psycopg` to the top of `tests/test_cli_db.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_db.py -v -k "writers or main"`
Expected: FAIL (writers other than pull/push exit 0 or try to connect; `cli.main` doesn't exist).

- [ ] **Step 3: Implement**

In `src/tempus/cli.py`, add `@writes` directly under the `@….command(…)` decorator of each of:
`db_init`, `db_readonly_role`, `catalog_add`, `catalog_import_snapshot`, `catalog_remove`,
`sync`, `series_delete`, `ontology_load`, `ontology_file`, `card_load`. For example:

```python
@db_app.command("init")
@writes
def db_init():
```

Add at the end of `src/tempus/cli.py`:

```python
def main() -> None:
    """The `tempus` entry point: `app`, plus one-line messages for prod-mode errors any
    command can hit (prod unreachable; a write that slipped past `writes`)."""
    try:
        app()
    except remote.RemoteError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    except psycopg.errors.ReadOnlySqlTransaction as exc:
        console.print(f"[red]{READ_ONLY}[/red]")
        raise SystemExit(1) from exc
```

In `pyproject.toml`:

```toml
[project.scripts]
tempus = "tempus.cli:main"
```

Then run `uv sync` so the `tempus` script points at `main`.

- [ ] **Step 4: Run the tests**

Run: `uv sync && uv run pytest tests/test_cli_db.py -v && mise run test && mise run lint`
Expected: PASS. `uv run tempus --help` still lists every command.

- [ ] **Step 5: Commit**

```bash
git add src/tempus/cli.py pyproject.toml uv.lock tests/test_cli_db.py
git commit -m "Writing commands refuse under TEMPUS_DB=prod; tempus.cli:main catches the rest

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 11: The app's `read_only` flag and `/api/meta`

**Files:**
- Modify: `src/tempus/app.py`
- Test: `tests/test_app.py` (append)

**Interfaces:**
- Consumes: `Settings.db` (Task 1).
- Produces: `create_app(settings=None, sync_source=None, guest=False, read_only=False)`;
  `GET /api/meta -> {"read_only": bool, "remote": bool}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_app.py`)

```python
from tempus.config import Settings as _Settings


class _CountingSource:
    def __init__(self):
        self.calls = 0

    def __call__(self, name):
        self.calls += 1
        return None


def _unsynced(conn):
    conn.execute("INSERT INTO series (source, series_id, title) VALUES ('fred', 'DGS10', 'Ten')")


def test_meta_on_dev(settings):
    assert TestClient(create_app(settings)).get("/api/meta").json() == {
        "read_only": False,
        "remote": False,
    }


def test_meta_when_reading_prod(settings):
    remote_settings = settings.model_copy(update={"db": "prod"})
    assert TestClient(create_app(remote_settings)).get("/api/meta").json() == {
        "read_only": True,
        "remote": True,
    }


def test_meta_for_the_guest_app(settings):
    meta = TestClient(create_app(settings, guest=True)).get("/api/meta").json()
    assert meta == {"read_only": True, "remote": False}


def test_read_only_turns_sync_on_open_off(conn, settings):
    _unsynced(conn)
    source = _CountingSource()
    client = TestClient(create_app(settings, sync_source=source, read_only=True))
    page = client.get("/api/series/fred/DGS10").json()
    assert source.calls == 0 and page["sync"] is None
    assert page["hint"] == "tempus catalog add fred DGS10 --sync  # on the home base"


def test_writable_app_still_syncs_on_open(conn, settings):
    _unsynced(conn)
    source = _CountingSource()
    TestClient(create_app(settings, sync_source=source)).get("/api/series/fred/DGS10")
    assert source.calls == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -v -k "meta or read_only or still_syncs"`
Expected: FAIL (`/api/meta` returns 404 from the static mount; `read_only` is an unexpected keyword).

- [ ] **Step 3: Implement** (in `src/tempus/app.py`)

Replace the start of `create_app`:

```python
def create_app(
    settings: Settings | None = None,
    sync_source: SourceFactory | None = None,
    guest: bool = False,
    read_only: bool = False,
) -> FastAPI:
    """`read_only=True` never writes: series pages don't sync on open. Reading prod through
    the tunnel (TEMPUS_DB=prod) is always read-only. `guest=True` is the shared (Funnel)
    app: read-only, and no API docs."""
    settings = settings or get_settings()
    is_remote = settings.db == "prod"
    read_only = guest or read_only or is_remote
    sync_source = None if read_only else sync_source or default_sync_source(settings)
```

Add a route before `@app.get("/api/cards")`:

```python
    @app.get("/api/meta")
    def meta() -> dict:
        return {"read_only": read_only, "remote": is_remote}
```

In `series_route`, after the 404 check:

```python
        if read_only and page["hint"]:
            page["hint"] = f"{page['hint']}  # on the home base"
        return page
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_app.py tests/test_share.py tests/test_explore.py -v && mise run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tempus/app.py tests/test_app.py
git commit -m "App: a read_only flag (sync-on-open off) and /api/meta

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 12: The "prod · read-only" badge

**Files:**
- Modify: `src/tempus/web/app.js`
- Modify: `src/tempus/web/style.css`

**Interfaces:**
- Consumes: `GET /api/meta` (Task 11).

- [ ] **Step 1: Add the badge**

In `src/tempus/web/app.js`, after `await document.fonts.ready;`:

```js
// Reading prod through the tunnel (TEMPUS_DB=prod): say so on every page.
const meta = await fetch("/api/meta").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
if (meta.remote) {
  const badge = document.createElement("span");
  badge.className = "chip remote-badge";
  badge.textContent = "prod · read-only";
  badge.title = "The home base's database, through an SSH tunnel. Nothing here can change it.";
  document.querySelector("header .brand").after(badge);
}
```

In `src/tempus/web/style.css`, after the `.chip .dot` rule:

```css
/* TEMPUS_DB=prod: the header says which database you're looking at. */
.remote-badge { border-color: var(--ink); color: var(--ink); }
```

- [ ] **Step 2: Check it in the browser**

Run, in two terminals:

```bash
uv run tempus serve --port 8799
TEMPUS_DB=prod uv run tempus serve --port 8798   # needs TEMPUS_PROD_RO_PASSWORD; see Task 14
```

Open both (use the `run` skill to screenshot). Expected: 8799 shows no badge; 8798 shows a
"PROD · READ-ONLY" pill next to the brand, and an unsynced series page's command ends in
`# on the home base`. If prod isn't set up yet, check the badge by temporarily serving with
`Settings(db="prod")` through `create_app` in a Python shell, and leave the real check to
Task 14.

- [ ] **Step 3: Run the checks**

Run: `mise run test && mise run lint && mise run design:lint`
Expected: PASS.

- [ ] **Step 4: Commit, push, PR for sub-issue 4**

```bash
git add src/tempus/web/app.js src/tempus/web/style.css
git commit -m "A 'prod · read-only' badge when the app reads prod through the tunnel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin HEAD
```

---

## Sub-issue 5: docs and the end-to-end check (Tasks 13–14)

### Task 13: Docs

**Files:**
- Modify: `docs/HOMEBASE.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/STACK.md`,
  `.env.example`, `docs/specs/2026-10-11-dev-prod-db-sync-design.md`

- [ ] **Step 1: `.env.example`** — append:

```sh
# Dev and prod databases (docs/HOMEBASE.md → Dev and prod databases). On the MacBook:
# TEMPUS_DB=dev                    # prod: read the home base's database, read-only
# TEMPUS_HOMEBASE_HOST=homebase    # SSH host or ~/.ssh/config alias
# TEMPUS_PROD_DB_PORT=5432         # the home base's TEMPUS_DB_PORT
# TEMPUS_PROD_RO_PASSWORD=         # = the home base's TEMPUS_DB_RO_PASSWORD (pull, TEMPUS_DB=prod)
# TEMPUS_PROD_PASSWORD=            # the home base's database password (push only)
# On the home base (generated by mise run homebase:setup):
# TEMPUS_DB_RO_PASSWORD=
```

- [ ] **Step 2: `docs/HOMEBASE.md`** — replace the section "Use the mini's database from the
  MacBook" with:

````markdown
## Dev and prod databases

The MacBook's database is **dev**: change it freely. The mini's is **prod**. Three commands
connect them over SSH (a tunnel through the `homebase` alias opens and closes for you; no
port opens on either machine):

```sh
uv run tempus db pull                     # prod's series, observations and Jev filings → dev
uv run tempus db push                     # what dev synced or filed that prod lacks (dry run)
uv run tempus db push --yes               # …and send it; never deletes anything on prod
TEMPUS_DB=prod uv run tempus serve        # your code over prod's data, read-only
uv run tempus db where                    # which one am I on?
```

`pull` and `push` take `--series fred:UNRATE,…` or `--concept labor`; `pull` also takes
`--metadata-only`. `pull` refuses while dev holds syncs or filings prod doesn't have: push
them, or pull with `--discard-local`. Backfill in dev, then push, and prod never fetches
the same data twice. `sync` after a `pull` downloads only what changed since prod's copy.

**Once, on the mini:** `mise run homebase:setup` creates the read-only `tempus_ro` role and
prints the line to copy. **On the MacBook**, add to `.env`: `TEMPUS_PROD_RO_PASSWORD` (from
that line), `TEMPUS_PROD_DB_PORT` (the mini's `TEMPUS_DB_PORT`), and for `push`,
`TEMPUS_PROD_PASSWORD` (the password in the mini's `DATABASE_URL`). Keep them with your
other secrets (`secrets push|pull`), never in git.

With `TEMPUS_DB=prod`, the header shows **prod · read-only**, series pages don't sync on
open, and commands that write refuse. Postgres enforces it too: `tempus_ro` can only read.
Cards come from your local `cards/`; the concept tree comes from prod, so an unshipped
`ontology/concepts.yaml` edit won't show in this mode.
````

- [ ] **Step 3: `README.md`** — in the CLI section, add these lines to the command list
  (match its existing format):

```text
tempus db where                   # dev, or prod read-only (TEMPUS_DB=prod)
tempus db pull [--series …] [--concept …] [--metadata-only] [--discard-local]
tempus db push [--series …] [--concept …] [--yes]
tempus db readonly-role           # on the home base: the tempus_ro role
```

- [ ] **Step 4: `docs/ARCHITECTURE.md`**
  - Components table, after "Home base", add:
    `| Dev and prod databases | ✅ built | src/tempus/remote.py, src/tempus/transfer.py | `tempus db pull/push/where` and `TEMPUS_DB=prod`: an SSH tunnel to the home base's Postgres; pull replaces dev's copy, push merges whole series and Jev filings into prod (never deleting), read-only mode serves local code over prod as tempus_ro |`
  - Data flow: under the diagram add the line
    `dev DB ◀── tempus db pull ── ssh -L (homebase) ── prod DB;  dev DB ── tempus db push --yes ──▶ prod DB`
  - Decisions: add a dated entry:
    `- **2026-10-11 · Dev and prod databases over SSH (SPA-46).** Copy rows with COPY through a short-lived ssh -L tunnel, merge by whole series on the provider's revision time, and read prod as a SELECT-only role, instead of extract files (a format to own; deferred until cloud sessions need prod data) or replication (fragile with hypertables, overkill for one user). Only Jev filings move between databases; pins and drafts come from git on each side. Spec: docs/specs/2026-10-11-dev-prod-db-sync-design.md.`

- [ ] **Step 5: `docs/STACK.md`** — Open decisions table, add a row (match its columns):
  `| Prod data in cloud sessions | Deferred | Snapshot + Jev replay fixtures; no prod access | One-way once files exist | A versioned extract format (Parquet or similar) written by tempus | When a cloud task is blocked on real observations |`

- [ ] **Step 6: The spec** — set `- **Status:** Implemented YYYY-MM-DD (SPA-46)` with the
  merge date and add before "## One-way doors":

```markdown
## Changes during implementation

- Only `auto` placements (Jev filings) move in either direction; pins and drafts come from
  git on each side (pushing drafts would ping-pong: every `ontology load` refreshes their
  `filed_at`).
- `tempus db readonly-role` creates the role; `homebase:setup` calls it.
- `/api/meta` returns `remote` from `TEMPUS_DB=prod` rather than a `database` name.
- `pull --series` keeps dev's copy of a series prod doesn't have and reports it.
```

- [ ] **Step 7: Check and commit**

Run: `mise run lint && mise run design:lint`

```bash
git add .env.example docs README.md
git commit -m "Docs: dev and prod databases (HOMEBASE, README, ARCHITECTURE, STACK, spec)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 14: End-to-end against the real mini

Manual; needs the mini on main with sub-issues 1–4 merged.

- [ ] **Step 1: On the mini:** `ssh homebase`, `cd ~/Projects/tempus && git pull && mise run homebase:setup`.
  Expected: a "Read-only database role" step ending in `tempus_ro ready`, and a TODO line with
  `TEMPUS_PROD_RO_PASSWORD=…` and `TEMPUS_PROD_DB_PORT=…`.
- [ ] **Step 2: On the MacBook:** add those lines and `TEMPUS_PROD_PASSWORD` to `.env`.
  Run `uv run tempus db where` → `dev …`; `TEMPUS_DB=prod uv run tempus db where` →
  `prod via homebase (read-only)  tempus_ro@127.0.0.1:…`.
- [ ] **Step 3:** `TEMPUS_DB=prod uv run tempus sync` → the read-only message, exit 1.
- [ ] **Step 4:** `uv run tempus db pull --metadata-only`, then `uv run tempus db pull`.
  Expected: counts match `ssh homebase 'cd ~/Projects/tempus && zsh -lic "uv run tempus db status"'`.
- [ ] **Step 5:** pick a series prod hasn't synced (`uv run tempus series list` shows none for
  it): `uv run tempus catalog add fred <ID> --sync`, then `uv run tempus db push` (plan lists
  it as `new`), then `uv run tempus db push --yes`. On the mini, `uv run tempus runs` shows a
  `pushed` row; `uv run tempus db push` on the MacBook now says "Nothing to push."
- [ ] **Step 6:** `TEMPUS_DB=prod uv run tempus serve`; open it; the header shows the badge;
  the pushed series' page renders from prod. Close the laptop lid for a minute, reopen,
  reload: the page still loads (watchdog).
- [ ] **Step 7:** Commit the spec status if Task 13 didn't, push, open the sub-issue 5 PR,
  and move SPA-46 to Done when it merges.
