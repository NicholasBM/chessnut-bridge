"""chess.com daily move submission: the write path, and the only irreversible one.

``POST https://www.chess.com/callback/game/{game_id}/submit-move`` with a JSON
body of ``{"lastDate", "plyCount", "move", "squared"}``. This is not a documented
API. It was captured from the browser twice on 2026-09-08 against game
1026053628 and both samples were cross-checked against that game's own PGN, so
every field below is transcribed from observed traffic rather than guessed:

===========  ========  ========  ==================================================
field        sample 1  sample 2  where it comes from
===========  ========  ========  ==================================================
``move``     ``gv``    ``bs``    :func:`bridge.chesscom.tcn.encode_move`
``plyCount`` 2         4         :func:`ply_count_from_fen`, matched 2/2
``lastDate`` …861472   …861517   ``last_activity`` from the public games endpoint
``squared``  1         1         constant; meaning unknown, accepted both times
===========  ========  ========  ==================================================

Note the path is ``/callback/game/{id}/``, **not** ``/callback/daily/game/{id}/``.
The latter exists but carries navigation (``/next``, ``/previous``), both GET.

Because this is undocumented it can change without warning, so the module's job
is to be *loud and specific* when reality stops matching the table above, rather
than to paper over a difference and submit something plausible.

Two things this module deliberately does not do
-----------------------------------------------
**It never retries.** A POST that times out may still have been applied, and
this is the one action in the system that must not happen twice. ``lastDate``
gives real protection -- see :class:`Session` -- but "the server would probably
reject the duplicate" is not a basis for repeating an irreversible write. On any
ambiguous failure the caller re-reads the public API and looks at whether the
move actually landed. That is the only way to tell "did not send" from "sent and
the reply was lost", and it costs one cheap cached GET.

**It never paraphrases an error.** chess.com replies with JSON carrying a
``message``, and that string is surfaced verbatim to the UI. A message we have
never seen before is worth more to whoever is debugging than our guess at which
category it belongs in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import tcn

log = logging.getLogger(__name__)

SUBMIT_URL = "https://www.chess.com/callback/game/{game_id}/submit-move"

#: Sent with every submission. Observed as ``1`` in both captures. Its meaning is
#: unknown -- plausibly "the board was squared up", plausibly unrelated -- so it
#: is reproduced as a constant rather than modelled as a parameter we might set
#: wrongly.
SQUARED = 1

#: A browser-shaped User-Agent, unlike the honest descriptive one the public API
#: gets. The pub API is a documented integration point that asks to be told who
#: is calling; this is an internal browser callback behind Cloudflare, and a
#: bespoke agent string here is a request to be treated as a bot on the one
#: endpoint we cannot afford to have blocked.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_TIMEOUT = 20.0

#: Cookies seen on a real submission. Recorded for diagnostics only: which of
#: these the server actually requires is unverified, so nothing here is enforced.
#: Guessing a required set wrong would mean refusing to send a request that would
#: have worked.
OBSERVED_COOKIES = ("PHPSESSID", "CHESSCOM_REMEMBERME", "ACCESS_TOKEN", "cf_clearance")


class WriteError(RuntimeError):
    """A move could not be submitted. Carries whatever the server said.

    ``message`` is chess.com's own wording when it gave one, so it is safe --
    and preferable -- to show directly in the UI.
    """

    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.message = message

    @property
    def for_display(self) -> str:
        """chess.com's wording if we have it, else ours."""
        return self.message or str(self)


class MoveRejected(WriteError):
    """The server understood the request and declined it.

    Terminal for this move: re-sending the same bytes will fail the same way.
    Something about our view of the game is wrong, so the caller should re-read
    the game rather than try again.
    """


class SessionExpired(WriteError):
    """The session is no longer good. Needs a human to log in again."""


class ChallengePresented(WriteError):
    """Cloudflare served a challenge instead of passing us through.

    Distinguished from an application error by the response being HTML rather
    than JSON. Tested on 2026-09-08 from a UK residential IP with no
    ``cf_clearance`` cookie at all and this did **not** happen -- a plain
    ``urllib`` request reached the application, which returned application JSON.
    So this is a contingency, not the expected path. It is modelled anyway
    because the test was from one IP on one day, and a datacentre IP or a
    reputation change could produce it; if it ever fires, the appliance must say
    so plainly instead of reporting a failed move.
    """


class WriteUnavailable(WriteError):
    """The request did not complete: network, timeout, or a 5xx.

    The ambiguous case. Whether the move was applied is genuinely unknown, so
    the caller must re-read the game rather than assume either way.
    """


def ply_count_from_fen(fen: str) -> int:
    """The ``plyCount`` the server expects, derived from the game's own FEN.

    Zero-based count of moves already played: ``(fullmove - 1) * 2``, plus one if
    black is to move. Matched the captured value in both samples -- ply 2 for
    White's second move and ply 4 for White's third.

    Derived rather than tracked, because a counter of our own would be one more
    piece of state to get out of step with the server across a reboot. The FEN we
    already poll carries the answer.
    """
    parts = fen.split()
    if len(parts) < 6:
        raise ValueError(f"FEN is missing the fields plyCount needs: {fen!r}")
    turn, fullmove_text = parts[1], parts[5]
    if turn not in ("w", "b"):
        raise ValueError(f"FEN has no legible side to move: {fen!r}")
    try:
        fullmove = int(fullmove_text)
    except ValueError as exc:
        raise ValueError(f"FEN has a non-numeric move number: {fen!r}") from exc
    if fullmove < 1:
        raise ValueError(f"FEN move number must be 1 or more: {fen!r}")
    return (fullmove - 1) * 2 + (0 if turn == "w" else 1)


@dataclass(frozen=True)
class Session:
    """The credentials a submission needs, and nothing else.

    ``lastDate`` is not stored here but is worth explaining next to it: it is a
    server-side optimistic-concurrency guard. The value is the game's
    ``last_activity`` as the public API reports it, so sending a stale one means
    our picture of the game is out of date -- exactly the situation in which we
    must not move. It converts "we might submit into a position that has already
    changed" from a race we would have to win into an error the server raises for
    us. Its behaviour on mismatch is **not yet verified**; see
    :meth:`MoveWriter.submit`.

    ``__repr__`` is overridden because these values are live credentials and this
    object will end up in tracebacks and log lines. Earlier in this project the
    single biggest recurring accident was secrets appearing where nobody meant to
    put them, so the type refuses to render them at all.
    """

    cookies: Mapping[str, str] = field(default_factory=dict)
    csrf_token: str | None = None
    #: Observed as a 7-character per-session token plus fixed fields. Both values
    #: seen were accepted, and whether the header is required at all is unknown,
    #: so it is optional and passed through when present.
    play_client: str | None = None

    def __repr__(self) -> str:
        return (
            f"Session(cookies=[{len(self.cookies)} redacted], "
            f"csrf_token={'set' if self.csrf_token else 'unset'}, "
            f"play_client={'set' if self.play_client else 'unset'})"
        )

    __str__ = __repr__

    @property
    def is_populated(self) -> bool:
        """Whether there is anything worth trying. Not a validity check.

        Only the server can say whether a session is still good; this just
        avoids a pointless request when the store is plainly empty.
        """
        return bool(self.cookies)

    @property
    def missing_observed_cookies(self) -> tuple[str, ...]:
        """Observed-but-absent cookie names, for diagnostics only."""
        return tuple(name for name in OBSERVED_COOKIES if name not in self.cookies)

    def headers(self) -> dict[str, str]:
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.chess.com",
            "Referer": "https://www.chess.com/",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        if self.csrf_token:
            headers["x-chesscom-csrf-token"] = self.csrf_token
        if self.play_client:
            headers["x-chesscom-play-client"] = self.play_client
        return headers


def build_payload(uci: str, fen: str, last_activity: int) -> dict[str, object]:
    """The request body, from data the bridge already holds.

    Separate from the HTTP so it can be asserted against the two captured
    samples byte for byte, which is the only evidence that any of this is right.
    """
    return {
        "lastDate": last_activity,
        "plyCount": ply_count_from_fen(fen),
        "move": tcn.encode_move(uci),
        "squared": SQUARED,
    }


def _looks_like_html(body: bytes, content_type: str) -> bool:
    """Is this a challenge or error page rather than an API reply?

    Checked on the body as well as the header because a challenge does not
    reliably announce itself, and mistaking one for a move failure would send
    someone looking for a chess problem instead of a network one.
    """
    if "text/html" in content_type.lower():
        return True
    return body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))


class MoveWriter:
    """Submits moves. One attempt per call, no retries, no hidden state."""

    def __init__(self, opener: Callable[..., object] | None = None):
        self._opener = opener or urllib.request.urlopen

    def submit_sync(
        self,
        session: Session,
        game_id: str,
        uci: str,
        fen: str,
        last_activity: int,
    ) -> dict[str, object]:
        """Submit one move. Returns the decoded reply; raises on any failure.

        The reply body's shape was never captured -- only requests were -- so
        nothing is read out of it. It is returned for logging and returned
        empty rather than treated as an error when a success carries no body,
        because inventing a required response field is exactly the kind of
        assumption that breaks silently later.
        """
        if not session.is_populated:
            raise SessionExpired("no chess.com session stored; log in first")

        payload = build_payload(uci, fen, last_activity)
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            SUBMIT_URL.format(game_id=game_id),
            data=body,
            headers=session.headers(),
            method="POST",
        )

        # The payload is safe to log in full: a move, a ply number and a
        # timestamp. The headers are not, and are never logged.
        log.info("submitting %s to game %s as %s", uci, game_id, payload["move"])

        try:
            with self._opener(request, timeout=_TIMEOUT) as response:  # type: ignore[operator]
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            raise self._classify(exc.code, raw, content_type) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Deliberately not retried, and deliberately its own type: the move
            # may or may not have been applied and only a re-read can say.
            raise WriteUnavailable(
                f"submission to game {game_id} did not complete: {exc}"
            ) from exc

        if _looks_like_html(raw, content_type):
            raise ChallengePresented(
                f"chess.com returned an HTML page rather than JSON (HTTP {status}); "
                "the request was probably intercepted before reaching the site"
            )

        decoded = self._decode(raw)
        log.info("move accepted by game %s (HTTP %d)", game_id, status)
        return decoded

    def _decode(self, raw: bytes) -> dict[str, object]:
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("submission reply was not JSON (%d bytes)", len(raw))
            return {}
        return parsed if isinstance(parsed, dict) else {"response": parsed}

    def _classify(self, status: int, raw: bytes, content_type: str) -> WriteError:
        """Turn an HTTP error into the narrowest type the evidence supports.

        Conservative on purpose. Only two mappings are grounded in observation:
        an HTML body means interception, and 401 means the session is gone.
        Everything else becomes :class:`MoveRejected` carrying chess.com's own
        message, because the alternative is inventing a taxonomy for replies we
        have never seen. In particular **the response to a stale ``lastDate`` has
        not been captured yet** -- when it is, it belongs here as its own type,
        since it is recoverable by re-reading whereas a genuine rejection is not.
        """
        if _looks_like_html(raw, content_type):
            return ChallengePresented(
                f"chess.com returned an HTML page rather than JSON (HTTP {status})",
                status=status,
            )

        message = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                value = parsed.get("message")
                message = value if isinstance(value, str) else None
        except json.JSONDecodeError:
            pass

        detail = f"HTTP {status} submitting move"
        if status in (401, 419):
            return SessionExpired(f"{detail}; the session is no longer valid",
                                  status=status, message=message)
        if status >= 500:
            return WriteUnavailable(f"{detail}; chess.com is having trouble",
                                    status=status, message=message)
        return MoveRejected(detail, status=status, message=message)

    async def submit(
        self,
        session: Session,
        game_id: str,
        uci: str,
        fen: str,
        last_activity: int,
    ) -> dict[str, object]:
        """Async wrapper: urllib blocks, so it runs off the event loop."""
        return await asyncio.to_thread(
            self.submit_sync, session, game_id, uci, fen, last_activity
        )
