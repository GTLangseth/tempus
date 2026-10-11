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
    return make_conninfo(host="127.0.0.1", port=port, dbname=PROD_DB, user=user, password=password)


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
