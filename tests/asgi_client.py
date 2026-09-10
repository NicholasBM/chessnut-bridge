"""A minimal ASGI client, so the web tests need no HTTP dependency.

Starlette's own ``TestClient`` requires ``httpx``, which this project has
deliberately not installed: the appliance is a 512 MB Pi Zero 2 W and the read and
write paths both use ``urllib`` precisely to avoid carrying a client library. A
test-only dependency would still end up in the image unless the build learned to
split them, so a forty-line driver is the cheaper answer.

It calls the app the way a server does -- through the real ASGI interface, so
middleware, routing and response construction are all exercised. Redirects are
*not* followed and cookies are *not* stored automatically, which suits these tests:
every one is about whether a cookie was required, issued or refused, and making
that explicit stops a test passing because the client papered over it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from urllib.parse import urlencode


@dataclass
class Reply:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        wanted = name.lower().encode()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value.decode()
        return None

    @property
    def location(self) -> str | None:
        return self.header("location")

    @property
    def cookies(self) -> dict[str, str]:
        """Cookie name to value, from every Set-Cookie on the reply."""
        jar: dict[str, str] = {}
        for key, value in self.headers:
            if key.lower() == b"set-cookie":
                parsed = SimpleCookie()
                parsed.load(value.decode())
                for name, morsel in parsed.items():
                    jar[name] = morsel.value
        return jar


async def request(
    app,
    path: str,
    *,
    method: str = "GET",
    form: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    stream_chunks: int | None = None,
) -> Reply:
    """One request/response cycle against an ASGI app.

    ``stream_chunks`` is for endless responses. A browser holds an event stream
    open indefinitely, so the handler is right to loop forever and something has to
    play the part of the reader closing the tab. Pass the number of body chunks to
    collect and the client hangs up once they have arrived; leave it ``None`` and
    the hangup is reported as soon as the request body has been read, which is what
    an ordinary handler should see.
    """
    body = urlencode(form or {}).encode() if form is not None else b""
    headers: list[tuple[bytes, bytes]] = []
    if form is not None:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
        headers.append((b"content-length", str(len(body)).encode()))
    if cookies:
        jar = "; ".join(f"{name}={value}" for name, value in cookies.items())
        headers.append((b"cookie", jar.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("192.0.2.10", 54321),
        "server": ("192.0.2.2", 80),
    }

    sent = [{"type": "http.request", "body": body, "more_body": False}]
    hung_up = asyncio.Event()
    if stream_chunks is None:
        hung_up.set()

    async def receive():
        if sent:
            return sent.pop(0)
        # A real server would block here until the client went away. Waiting on
        # the event is how that is spelled: it is already set unless the caller
        # asked for some chunks first, so a handler polling for a disconnect never
        # hangs the suite.
        await hung_up.wait()
        return {"type": "http.disconnect"}

    status = 500
    out_headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []

    async def send(message):
        nonlocal status, out_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            chunks.append(chunk)
            if stream_chunks is not None and len([c for c in chunks if c]) >= stream_chunks:
                hung_up.set()

    await app(scope, receive, send)
    return Reply(status=status, headers=out_headers, body=b"".join(chunks))


def get(app, path: str, **kwargs) -> Reply:
    """Blocking convenience for tests that are not otherwise async."""
    return asyncio.run(request(app, path, **kwargs))
