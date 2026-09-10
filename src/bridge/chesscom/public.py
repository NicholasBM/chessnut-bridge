"""chess.com Published-Data API client: the read path, and it needs no auth.

This covers everything the bridge needs to *know* about a game -- which daily
games are in progress, whose turn it is, the current FEN -- leaving the session
cookie needed only for submitting a move. Verified against the real account on
2026-09-07: ``/games`` returned four in-progress 3-day games with usable FENs.

Deliberately built on ``urllib`` rather than httpx or requests. The appliance is
a Pi Zero 2 W with 512 MB of RAM and this module polls one endpoint every couple
of minutes; a dependency would buy nothing and the standard library cannot go out
of date underneath us.

Politeness is not optional here. chess.com asks for a descriptive User-Agent and
serves the pub API from a cache, so every request sends ``If-None-Match`` and a
304 is treated as good news rather than an error -- for a 3-day game the answer
is unchanged the overwhelming majority of the time. A 429 is honoured by reading
``Retry-After``; being rate-limited off the read path would blind the bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

API_ROOT = "https://api.chess.com/pub"

#: chess.com asks integrations to identify themselves. A real contact address
#: belongs here before this is left running unattended for weeks.
USER_AGENT = "chessnut-bridge/0.1 (personal Chessnut-to-chess.com board bridge)"

#: A 3-day game does not need a fast poll, and the read path is a shared public
#: service. Two minutes is already far finer-grained than the game demands.
DEFAULT_POLL_SECONDS = 120.0
_ERROR_BACKOFF_START = 30.0
_ERROR_BACKOFF_CAP = 900.0
_TIMEOUT = 20.0


class ChessComError(RuntimeError):
    """The public API could not be read."""


class RateLimited(ChessComError):
    """Rate-limited. ``retry_after`` is in seconds if the server said."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class DailyGame:
    """One in-progress daily game, as the public API describes it."""

    id: str
    url: str
    fen: str
    turn: str
    my_color: str
    move_by: int | None
    time_control: str
    last_activity: int | None = None

    @property
    def placement(self) -> str:
        """The first FEN field only -- what a sensor board can be compared to."""
        return self.fen.split(" ")[0]

    @property
    def is_my_turn(self) -> bool:
        return self.turn == self.my_color

    @property
    def is_three_day(self) -> bool:
        return self.time_control == "1/259200"


def _game_id(url: str) -> str:
    """Trailing path segment of a game URL, e.g. '.../daily/1022853950'."""
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _colour_played_by(game: dict, username: str) -> str | None:
    """Which colour ``username`` has in this game, by player profile URL.

    Compared on the URL rather than a display name because the final path segment
    is the canonical lower-case username, while display capitalisation varies
    (the account here appears as both "nbaronmorgan" and "Nbaronmorgan").

    Accepts both shapes chess.com uses: the in-progress ``/games`` endpoint gives
    ``"white": "https://api.chess.com/pub/player/name"`` as a bare string, while
    the monthly archives give an object with an ``@id``. Getting this wrong is
    not a cosmetic bug -- ``turn`` alone says nothing about whether it is *our*
    turn, and confusing the two is what makes a bridge submit into a game where
    it is the opponent to move.
    """
    target = username.lower()
    for colour in ("white", "black"):
        player = game.get(colour)
        url = player if isinstance(player, str) else (player or {}).get("@id", "")
        if _game_id(url).lower() == target:
            return colour
    return None


def parse_games(payload: dict, username: str) -> list[DailyGame]:
    """Turn a ``/games`` response into typed games, skipping anything odd.

    Kept separate from the HTTP so it can be tested against captured payloads
    with no network. Games where our colour cannot be determined are dropped
    rather than guessed: the whole point of the sync layer is that we never act
    on a position we are not sure about, and "whose turn is it" is the input that
    decides whether a move gets submitted at all.
    """
    games: list[DailyGame] = []
    for raw in payload.get("games", []):
        url = raw.get("url", "")
        colour = _colour_played_by(raw, username)
        if colour is None or not raw.get("fen") or not raw.get("turn"):
            log.warning("skipping unusable game entry: %s", url or raw)
            continue
        games.append(
            DailyGame(
                id=_game_id(url),
                url=url,
                fen=raw["fen"],
                turn=raw["turn"],
                my_color=colour,
                move_by=raw.get("move_by"),
                time_control=raw.get("time_control", ""),
                last_activity=raw.get("last_activity"),
            )
        )
    return games


class PublicClient:
    """Reads the public API, remembering ETags so repeat polls stay cheap."""

    def __init__(self, username: str, opener: Callable[..., object] | None = None):
        self.username = username
        self._etags: dict[str, str] = {}
        self._opener = opener or urllib.request.urlopen
        self._last_games: list[DailyGame] | None = None

    def _get(self, path: str) -> dict | None:
        """GET a JSON path. Returns None when the server says 304 Not Modified."""
        url = f"{API_ROOT}{path}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        etag = self._etags.get(path)
        if etag:
            request.add_header("If-None-Match", etag)

        try:
            with self._opener(request, timeout=_TIMEOUT) as response:  # type: ignore[operator]
                if response.status == 304:
                    return None
                body = response.read()
                new_etag = response.headers.get("ETag")
                if new_etag:
                    self._etags[path] = new_etag
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                # urllib raises on 304 unless a cache handler is installed, so
                # this is the branch that actually runs in production.
                return None
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                raise RateLimited(
                    f"rate-limited by chess.com on {path}",
                    float(retry_after) if retry_after else None,
                ) from exc
            raise ChessComError(f"HTTP {exc.code} from {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChessComError(f"cannot reach {url}: {exc}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ChessComError(f"malformed JSON from {url}: {exc}") from exc

    def fetch_games_sync(self) -> list[DailyGame] | None:
        """In-progress daily games, or None if unchanged since the last poll."""
        payload = self._get(f"/player/{self.username}/games")
        if payload is None:
            return None
        self._last_games = parse_games(payload, self.username)
        return list(self._last_games)

    @property
    def last_games(self) -> list[DailyGame] | None:
        """The most recent games we successfully read, or None if we never have.

        A level to sit beside the edge that ``fetch_games_sync`` returns. Without
        it, "unchanged" (a 304) is indistinguishable from "unknown" to anyone who
        was not listening at the moment the data last changed -- which is the same
        trap that made a BLE reconnect silently stale. Callers that need to
        re-derive state after an interruption read this; callers that only want to
        react to changes keep using the return value.
        """
        return None if self._last_games is None else list(self._last_games)

    async def fetch_games(self) -> list[DailyGame] | None:
        """Async wrapper: urllib blocks, so it runs off the event loop."""
        return await asyncio.to_thread(self.fetch_games_sync)


async def watch_games(
    client: PublicClient,
    on_games: Callable[[list[DailyGame]], None | Awaitable[None]],
    interval: float = DEFAULT_POLL_SECONDS,
    stop: asyncio.Event | None = None,
) -> None:
    """Poll for game state until ``stop`` is set. Never raises.

    Mirrors the BLE transport's stance: an unreachable server is the normal
    condition of a device on domestic WiFi, not an exception, so failures back
    off and retry rather than ending the loop. The callback is only invoked when
    the data actually changed -- a 304 means the caller has nothing to do.
    """
    stop = stop or asyncio.Event()
    backoff = _ERROR_BACKOFF_START

    while not stop.is_set():
        delay = interval
        try:
            games = await client.fetch_games()
        except RateLimited as exc:
            delay = exc.retry_after or backoff
            backoff = min(backoff * 2, _ERROR_BACKOFF_CAP)
            log.warning("rate-limited; next poll in %.0fs", delay)
        except ChessComError as exc:
            delay = backoff
            backoff = min(backoff * 2, _ERROR_BACKOFF_CAP)
            log.warning("read path unavailable (%s); retrying in %.0fs", exc, delay)
        else:
            backoff = _ERROR_BACKOFF_START
            if games is not None:
                result = on_games(games)
                if asyncio.iscoroutine(result):
                    await result

        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
