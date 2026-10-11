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
    print("ssh: Could not resolve hostname homebase: nodename nor servname", file=sys.stderr)
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
    assert (ro["host"], ro["port"], ro["dbname"], ro["user"]) == (
        "127.0.0.1",
        "6001",
        "tempus",
        "tempus_ro",
    )
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
