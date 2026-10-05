from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tempus.app import create_app
from tempus.config import Settings
from tempus.share import PER_CLIENT_FAILURES, SESSION_SECONDS, WINDOW, ShareGate

PASSWORD = "correct-horse-battery"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def guest(tmp_path: Path):
    (tmp_path / "cards").mkdir()
    return create_app(Settings(cards_path=tmp_path / "cards"), guest=True)


def client(app, clock, until=None):
    # https base URL: the session cookie is Secure, as it is behind Funnel.
    gate = ShareGate(app, PASSWORD, until=until, clock=clock)
    return TestClient(gate, base_url="https://testserver", follow_redirects=False)


def log_in(c, password=PASSWORD, ip="203.0.113.7"):
    return c.post("/login", data={"password": password}, headers={"x-forwarded-for": ip})


def test_everything_is_closed_until_login(guest, clock):
    c = client(guest, clock)
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/api/cards").status_code == 401
    page = c.get("/login")
    assert page.status_code == 200 and b'type="password"' in page.content
    assert page.headers["x-frame-options"] == "DENY"
    assert "noindex" in page.headers["x-robots-tag"]


def test_login_opens_the_app(guest, clock):
    c = client(guest, clock)
    r = log_in(c)
    assert r.status_code == 303 and r.headers["location"] == "/"
    set_cookie = r.headers["set-cookie"]
    for attr in ("HttpOnly", "Secure", "SameSite=Strict", f"Max-Age={SESSION_SECONDS}"):
        assert attr in set_cookie
    assert c.get("/api/cards").status_code == 200
    assert c.get("/logout").status_code == 303
    assert c.get("/api/cards").status_code == 401


def test_wrong_password_and_forged_cookies(guest, clock):
    c = client(guest, clock)
    assert log_in(c, "nope").status_code == 401
    c.cookies.set("tempus_share", f"{int(clock.now) + 999}.deadbeef")
    assert c.get("/api/cards").status_code == 401
    # A cookie signed by another run (or before a restart) doesn't open this one.
    other = client(guest, clock)
    log_in(other)
    c.cookies.set("tempus_share", other.cookies["tempus_share"])
    assert c.get("/api/cards").status_code == 401


def test_sessions_expire(guest, clock):
    c = client(guest, clock)
    log_in(c)
    clock.now += SESSION_SECONDS + 1
    assert c.get("/api/cards").status_code == 401


def test_rate_limit_per_client(guest, clock):
    c = client(guest, clock)
    for _ in range(PER_CLIENT_FAILURES):
        log_in(c, "wrong")
    assert log_in(c).status_code == 429  # even the right password, while locked out
    assert log_in(c, ip="198.51.100.1").status_code == 303  # other visitors unaffected
    clock.now += WINDOW + 1
    assert log_in(c).status_code == 303


def test_sharing_ends_at_until(guest, clock):
    c = client(guest, clock, until=clock.now + 60)
    r = log_in(c)
    assert "Max-Age=60" in r.headers["set-cookie"]
    clock.now += 61
    assert c.get("/login").status_code == 503


def test_guest_app_hides_docs(guest):
    assert TestClient(guest).get("/docs").status_code == 404
    assert TestClient(guest).get("/openapi.json").status_code == 404


def test_short_passwords_are_refused(guest):
    with pytest.raises(ValueError):
        ShareGate(guest, "short")
