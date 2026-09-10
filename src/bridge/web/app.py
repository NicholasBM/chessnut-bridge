"""The remote configuration page: sign in, pick a game, watch it work.

Server-rendered on purpose. The appliance is a Pi Zero 2 W with 512 MB of RAM and
the page is read a few times a week, so a JavaScript build step would add a
toolchain, a deploy artefact and a class of failure ("the bundle is stale") in
exchange for nothing a plain form cannot do. The only script on the page is a
dozen lines that swap one element when the server pushes an update.

Three things here are less obvious than they look:

**Authentication is default-deny by path.** A middleware guards everything except
a short allowlist, rather than each handler checking for itself. Adding a route
and forgetting the check is the classic way this goes wrong, and this way the
mistake fails closed.

**A misconfigured appliance serves one page and nothing else.** If the boot
partition has no ``web_password``, every path renders the list of problems with a
503. Serving a working-but-open UI would be worse than serving nothing, because
this page can submit moves in real games.

**The update stream reads a level on a timer instead of subscribing to changes.**
``BoardStatus`` is republished on every BLE frame -- about ten a second -- so a
subscription would push ten events a second at an idle browser. That is already
recorded as a hazard from ``board_doctor watch``, which printed its banner 576
times in a minute. Polling a level cannot miss a transition, because the level is
the truth rather than a notification about it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from ..chesscom import login as chesscom_login_flow
from ..service import BridgeService
from . import auth
from .config import Config
from .view import render_view

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

#: Paths reachable without a cookie. Everything else is denied by default.
PUBLIC_PATHS = frozenset({"/login", "/healthz"})

#: How often the update stream re-reads the level. One second is far slower than
#: the board's frame rate and far faster than a person notices.
STREAM_INTERVAL_SECONDS = 1.0

#: Send something at least this often even when nothing changed, so an idle
#: connection is not closed by a browser or proxy that thinks it has died.
STREAM_HEARTBEAT_SECONDS = 20.0


async def _form(request: Request) -> dict[str, str]:
    """Read an ``application/x-www-form-urlencoded`` body.

    Not ``request.form()``: Starlette asserts ``python-multipart`` is installed
    before it will parse *any* form, including a urlencoded one that needs none of
    it. Every form on this appliance is a handful of short text fields, so a
    ``parse_qsl`` over the body does the whole job -- and the read and write paths
    already stay on ``urllib`` deliberately, to keep the image small enough for a
    512 MB Zero 2 W.

    Last value wins on a repeated field, matching what a browser and Starlette
    would both hand a handler that asks for a single value.
    """
    body = await request.body()
    return dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))


class RequireLogin:
    """Default-deny ASGI middleware. A new route is protected by omission."""

    def __init__(self, app: ASGIApp, signer: auth.TokenSigner) -> None:
        self.app = app
        self.signer = signer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if self.signer.verify(request.cookies.get(auth.COOKIE_NAME)):
            await self.app(scope, receive, send)
            return

        if request.method == "GET":
            response: Response = RedirectResponse("/login", status_code=303)
        else:
            # Not a redirect: a browser would follow it and the owner would see
            # the login page with no sign that their action was dropped.
            response = PlainTextResponse("Not signed in.", status_code=403)
        await response(scope, receive, send)


def _set_cookie(response: Response, signer: auth.TokenSigner) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        signer.issue(),
        max_age=int(signer.lifetime_seconds),
        httponly=True,
        # Lax is what stops another site POSTing to the appliance on the owner's
        # behalf, which is why there is no separate CSRF token: the cookie simply
        # is not sent on a cross-site form submission.
        samesite="lax",
        # Deliberately not Secure: the appliance is plain HTTP on a home LAN, and
        # a Secure cookie would never be sent at all.
        secure=False,
        path="/",
    )


def create_misconfigured_app(config: Config) -> Starlette:
    """An appliance that cannot run safely, explaining itself on every path.

    503 rather than 200 so anything watching can tell this is not the real UI,
    and the same body on every path so a bookmark straight to ``/games`` gets the
    explanation too.
    """
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    async def problems(request: Request) -> Response:
        log.error("refusing to serve the UI: %s", "; ".join(config.problems))
        return templates.TemplateResponse(
            request, "problems.html", {"config": config}, status_code=503
        )

    return Starlette(routes=[Route("/{path:path}", problems, methods=["GET", "POST"])])


def create_app(
    service: BridgeService,
    config: Config,
    *,
    signing_key: bytes,
    clock: Callable[[], float] = time.time,
    stream_interval: float = STREAM_INTERVAL_SECONDS,
    heartbeat_seconds: float = STREAM_HEARTBEAT_SECONDS,
) -> Starlette:
    """The real UI. Assumes ``config.is_usable``; call the other one otherwise."""
    if not config.is_usable:
        return create_misconfigured_app(config)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    signer = auth.TokenSigner(
        secret=auth.derive_signing_key(signing_key), clock=clock
    )
    guard = auth.LoginGuard(password=config.web_password, clock=clock)

    def view():
        return render_view(service.status, clock())

    # --- pages ------------------------------------------------------------

    async def index(request: Request) -> Response:
        return templates.TemplateResponse(
            request, "status.html", {"view": view(), "config": config}
        )

    async def login_form(request: Request) -> Response:
        if signer.verify(request.cookies.get(auth.COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"error": ""})

    async def login(request: Request) -> Response:
        form = await _form(request)
        attempt = form.get("password") or ""
        waiting = guard.seconds_to_wait()
        if waiting > 0:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": f"Too many attempts. Try again in {int(waiting) + 1} seconds."},
                status_code=429,
            )
        if not guard.check(attempt):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "That is not the password from the SD card."},
                status_code=401,
            )
        response = RedirectResponse("/", status_code=303)
        _set_cookie(response, signer)
        return response

    async def logout(request: Request) -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    # --- actions ----------------------------------------------------------
    #
    # All POST, all redirecting back to the page. A GET that changed state would
    # be re-run by every browser prefetch and every reload -- and one of these
    # actions submits a move that cannot be taken back.

    async def select_game(request: Request) -> Response:
        form = await _form(request)
        game_id = (form.get("game_id") or "").strip() or None
        service.select_game(game_id)
        return RedirectResponse("/", status_code=303)

    async def reconnect_board(request: Request) -> Response:
        service.reconnect_board()
        return RedirectResponse("/", status_code=303)

    async def retry_write(request: Request) -> Response:
        try:
            await service.retry_write()
        except RuntimeError as exc:
            # The service refuses to resend anything not proven unsent. Reaching
            # here means the state changed between rendering the button and
            # pressing it, which is exactly when a resend would be a duplicate.
            log.warning("refused to resend a move: %s", exc)
            return PlainTextResponse(str(exc), status_code=409)
        return RedirectResponse("/", status_code=303)

    async def dismiss_write(request: Request) -> Response:
        service.clear_write_alert()
        return RedirectResponse("/", status_code=303)

    async def chesscom_logout(request: Request) -> Response:
        service.log_out()
        return RedirectResponse("/", status_code=303)

    # --- signing in to chess.com ------------------------------------------
    #
    # Behind the same middleware as everything else: this form takes the owner's
    # real chess.com password, so it is protected by omission from PUBLIC_PATHS
    # rather than by a check written here that a later edit could drop.

    async def chesscom_login_form(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "chesscom_login.html",
            {
                "username": config.chesscom_username or "",
                "error": "",
                "hint": "",
            },
        )

    async def chesscom_login(request: Request) -> Response:
        form = await _form(request)
        username = (form.get("username") or config.chesscom_username or "").strip()
        password = form.get("password") or ""
        remember = form.get("remember") == "on"

        def again(error: str, hint: str = "", status: int = 400) -> Response:
            # The password is never echoed back into the form. Retyping it is a
            # small cost next to a page that holds it in its HTML.
            return templates.TemplateResponse(
                request,
                "chesscom_login.html",
                {"username": username, "error": error, "hint": hint},
                status_code=status,
            )

        if not username or not password:
            return again("Both a Chess.com username and password are needed.")

        try:
            await service.sign_in(username, password, remember=remember)
        except chesscom_login_flow.VerificationRequired as exc:
            # Called out separately because it is the likeliest way this fails on
            # a real account, and it is not the owner mistyping anything.
            return again(
                exc.for_display,
                "Two-factor authentication cannot be completed by the appliance.",
                status=409,
            )
        except chesscom_login_flow.LoginError as exc:
            log.warning("chess.com sign-in failed: %s", exc)
            return again(exc.for_display, status=502 if exc.retryable else 401)
        except Exception:  # noqa: BLE001 -- a form must not 500 with a traceback
            log.exception("unexpected failure signing in to chess.com")
            return again(
                "Something went wrong signing in. The journal has the details.",
                status=500,
            )
        return RedirectResponse("/", status_code=303)

    async def chesscom_forget(request: Request) -> Response:
        service.forget_credentials()
        return RedirectResponse("/", status_code=303)

    async def chesscom_retry_login(request: Request) -> Response:
        await service.sign_in_again()
        return RedirectResponse("/", status_code=303)

    # --- the update stream ------------------------------------------------

    async def events(request: Request) -> Response:
        async def stream():
            last_payload: str | None = None
            last_sent = 0.0
            while True:
                if await request.is_disconnected():
                    return
                payload = templates.get_template("_status.html").render(
                    view=view(), config=config
                )
                now = clock()
                if payload != last_payload:
                    yield _sse("status", payload)
                    last_payload = payload
                    last_sent = now
                elif now - last_sent >= heartbeat_seconds:
                    yield ": still here\n\n"
                    last_sent = now
                await asyncio.sleep(stream_interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    async def healthz(request: Request) -> Response:
        """Unauthenticated liveness, carrying nothing about the account.

        Deliberately says only whether the process is up, because it is the one
        path anything on the network can reach.
        """
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/", index),
            Route("/login", login_form),
            Route("/login", login, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/select-game", select_game, methods=["POST"]),
            Route("/reconnect-board", reconnect_board, methods=["POST"]),
            Route("/retry-write", retry_write, methods=["POST"]),
            Route("/dismiss-write", dismiss_write, methods=["POST"]),
            Route("/chesscom/login", chesscom_login_form),
            Route("/chesscom/login", chesscom_login, methods=["POST"]),
            Route("/chesscom/logout", chesscom_logout, methods=["POST"]),
            Route("/chesscom/forget", chesscom_forget, methods=["POST"]),
            Route("/chesscom/retry-login", chesscom_retry_login, methods=["POST"]),
            Route("/events", events),
            Route("/healthz", healthz),
        ],
        middleware=[Middleware(RequireLogin, signer=signer)],
    )


def _sse(event: str, payload: str) -> str:
    """One server-sent event. Every line of the body needs its own ``data:``
    prefix, and a body that skipped that would silently truncate at the first
    newline -- which HTML is full of."""
    lines = "".join(f"data: {line}\n" for line in payload.splitlines())
    return f"event: {event}\n{lines}\n"
