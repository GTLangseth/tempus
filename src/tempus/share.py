"""The password gate in front of a shared (Tailscale Funnel) tempus.

`tempus serve --share` wraps the app in `ShareGate`: every request needs a session cookie,
which `/login` hands out for the right password. The cookie is signed with a key made fresh
at each start, so stopping sharing logs everyone out. Failed logins are rate-limited per
client and in total. Runbook: docs/HOMEBASE.md → Share with colleagues.
"""

import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import parse_qs

COOKIE = "tempus_share"
FONTS = (
    "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@700"
    "&family=Inter:wght@400;500&family=JetBrains+Mono:wght@500&display=swap"
)
SESSION_SECONDS = 12 * 3600
MIN_PASSWORD = 12
WINDOW = 15 * 60  # seconds over which failed logins are counted
PER_CLIENT_FAILURES = 5
TOTAL_FAILURES = 50
MAX_FORM_BYTES = 4096

SECURITY_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-robots-tag", b"noindex, nofollow"),
]


class ShareGate:
    """ASGI middleware: a login page in front of `app`, open only until `until` (epoch s)."""

    def __init__(
        self,
        app,
        password: str,
        until: float | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"the share password needs at least {MIN_PASSWORD} characters")
        self.app = app
        self.password = password.encode()
        self.until = until
        self.clock = clock
        self.key = secrets.token_bytes(32)
        self.failures: dict[str, deque[float]] = {}
        self.all_failures: deque[float] = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_secured(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + SECURITY_HEADERS
            await send(message)

        now = self.clock()
        path, method = scope["path"], scope["method"]
        if self.until is not None and now >= self.until:
            return await respond(send_secured, 503, "text/plain", b"Sharing has ended.")
        if path == "/login" and method == "POST":
            return await self.login(scope, receive, send_secured, now)
        if path == "/login":
            return await respond(send_secured, 200, "text/html", login_page())
        if path == "/logout":
            return await redirect(send_secured, "/login", clear_cookie())
        if self.valid(cookie(scope), now):
            return await self.app(scope, receive, send_secured)
        if path.startswith("/api/"):
            body = json.dumps({"detail": "log in first"}).encode()
            return await respond(send_secured, 401, "application/json", body)
        return await redirect(send_secured, "/login")

    async def login(self, scope, receive, send, now: float):
        client = client_id(scope)
        if self.locked_out(client, now):
            return await respond(send, 429, "text/html", login_page("Too many tries. Wait a bit."))
        form = parse_qs((await read_body(receive)).decode(errors="replace"))
        given = form.get("password", [""])[0].encode()
        if not hmac.compare_digest(given, self.password):
            self.failures.setdefault(client, deque()).append(now)
            self.all_failures.append(now)
            return await respond(send, 401, "text/html", login_page("That's not it."))
        self.failures.pop(client, None)
        expires = now + SESSION_SECONDS
        if self.until is not None:
            expires = min(expires, self.until)
        value = f"{int(expires)}.{self.sign(int(expires))}"
        max_age = int(expires - now)
        attrs = f"Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Strict"
        return await redirect(send, "/", f"{COOKIE}={value}; {attrs}")

    def locked_out(self, client: str, now: float) -> bool:
        for log in (self.failures.get(client, deque()), self.all_failures):
            while log and log[0] <= now - WINDOW:
                log.popleft()
        return (
            len(self.failures.get(client, ())) >= PER_CLIENT_FAILURES
            or len(self.all_failures) >= TOTAL_FAILURES
        )

    def sign(self, expires: int) -> str:
        return hmac.new(self.key, str(expires).encode(), hashlib.sha256).hexdigest()

    def valid(self, value: str | None, now: float) -> bool:
        if not value or "." not in value:
            return False
        expires, sig = value.split(".", 1)
        if not expires.isdigit() or int(expires) <= now:
            return False
        return hmac.compare_digest(sig, self.sign(int(expires)))


def client_id(scope) -> str:
    """Funnel proxies from localhost and passes the visitor's address in X-Forwarded-For.

    The last entry is the one the proxy added; earlier ones are whatever the client sent.
    """
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            return value.decode(errors="replace").split(",")[-1].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def cookie(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            for part in value.decode(errors="replace").split(";"):
                key, _, val = part.strip().partition("=")
                if key == COOKIE:
                    return val
    return None


def clear_cookie() -> str:
    return f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Strict"


async def read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if len(body) > MAX_FORM_BYTES:
            return b""
        if not message.get("more_body"):
            return body


async def respond(send, status: int, content_type: str, body: bytes):
    headers = [
        (b"content-type", f"{content_type}; charset=utf-8".encode()),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def redirect(send, location: str, set_cookie: str | None = None):
    headers = [(b"location", location.encode()), (b"cache-control", b"no-store")]
    if set_cookie:
        headers.append((b"set-cookie", set_cookie.encode()))
    await send({"type": "http.response.start", "status": 303, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


def login_page(error: str = "") -> bytes:
    # Neon Notebook (docs/DESIGN.md), self-contained: nothing behind the gate is loaded here.
    note = f'<p class="err" role="alert">{error}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>tempus</title>
<link href="{FONTS}" rel="stylesheet">
<style>
  :root {{ --canvas:#f2efe6; --paper:#fbf9f3; --grid:#e2ddcf; --hairline:#d4cfbf;
    --ink:#1c1b19; --pencil:#6b675e; --primary:#ff1f8f; --violet:#8b3dff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; padding:16px;
    color:var(--ink); font:400 16px/1.55 Inter, system-ui, sans-serif;
    background-color:var(--canvas);
    background-image:linear-gradient(var(--grid) 1px, transparent 1px),
      linear-gradient(90deg, var(--grid) 1px, transparent 1px);
    background-size:24px 24px; }}
  form {{ width:min(360px, 100%); background:var(--paper); border:1px solid var(--ink);
    border-radius:8px; box-shadow:3px 3px 0 var(--ink); padding:24px; display:grid; gap:14px; }}
  h1 {{ margin:0; font:700 32px/1 'Space Grotesk', sans-serif; letter-spacing:-0.03em; }}
  h1 span {{ color:var(--primary); }}
  label {{ font:500 11px/1 'JetBrains Mono', monospace; letter-spacing:.06em;
    text-transform:uppercase; color:var(--pencil); }}
  input {{ font:inherit; padding:10px 12px; border:1px solid var(--hairline); border-radius:4px;
    background:#fff; color:var(--ink); }}
  input:focus {{ outline:2px solid var(--violet); outline-offset:1px; border-color:var(--ink); }}
  button {{ font:500 14px/1 Inter, sans-serif; background:var(--ink); color:var(--paper);
    border:1px solid var(--ink); border-radius:4px; padding:11px 16px; cursor:pointer; }}
  button:hover {{ background:var(--violet); border-color:var(--violet); }}
  .err {{ margin:0; color:#c4005f; font-size:14px; }}
</style></head>
<body><form method="post" action="/login">
  <h1>tempus<span>.</span></h1>
  <label for="pw">Password</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
  {note}
  <button type="submit">Open</button>
</form></body></html>""".encode()
