"""Tests for the public-API read path, against captured payload shapes.

The behaviour worth defending here is ``is_my_turn``. The API's ``turn`` field
says which colour moves next and nothing about which colour we are, so trusting
it alone means happily concluding it is our move in a game we are not to move in.
That is a wrong-move bug rather than a display bug, so it is pinned from both
sides -- a game where we are White and one where we are Black, both with
``turn: white``.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom import public  # noqa: E402

USERNAME = "nbaronmorgan"

#: Trimmed from a real response on 2026-09-07. The player fields are bare URL
#: strings, which is what this endpoint actually returns.
PAYLOAD = {
    "games": [
        {
            "url": "https://www.chess.com/game/daily/1022853950",
            "move_by": 1788997970,
            "time_control": "1/259200",
            "last_activity": 1788708312,
            "rated": True,
            "turn": "white",
            "fen": "r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R w - - 3 22",
            "time_class": "daily",
            "rules": "chess",
            "white": "https://api.chess.com/pub/player/heathbm2",
            "black": "https://api.chess.com/pub/player/nbaronmorgan",
        },
        {
            "url": "https://www.chess.com/game/daily/1023281994",
            "move_by": 1788967511,
            "time_control": "1/259200",
            "turn": "white",
            "fen": "r3k1nr/1p3ppp/n1pp2q1/8/Q1P1Pp2/3P1N1b/PP4PP/RN2R1K1 w kq - 2 14",
            "time_class": "daily",
            "rules": "chess",
            "white": "https://api.chess.com/pub/player/nbaronmorgan",
            "black": "https://api.chess.com/pub/player/someone",
        },
    ]
}


def parse():
    return public.parse_games(PAYLOAD, USERNAME)


# --- whose turn is it -----------------------------------------------------


def test_turn_alone_does_not_mean_our_turn():
    """Both games say turn=white; only one of them is ours to move."""
    black_game, white_game = parse()

    assert black_game.turn == white_game.turn == "white"
    assert black_game.my_color == "black"
    assert black_game.is_my_turn is False
    assert white_game.my_color == "white"
    assert white_game.is_my_turn is True


def test_colour_is_matched_case_insensitively():
    """chess.com renders this account as both 'nbaronmorgan' and 'Nbaronmorgan'."""
    games = public.parse_games(PAYLOAD, "Nbaronmorgan")
    assert [g.my_color for g in games] == ["black", "white"]


def test_a_game_we_are_not_in_is_dropped_not_guessed():
    """Rather than default to a colour and risk acting on it."""
    assert public.parse_games(PAYLOAD, "someone-else") == []


def test_player_objects_are_accepted_as_well_as_urls():
    """The monthly archives use objects; the same parser should cope."""
    payload = {
        "games": [
            dict(
                PAYLOAD["games"][0],
                white={"@id": "https://api.chess.com/pub/player/heathbm2"},
                black={"@id": f"https://api.chess.com/pub/player/{USERNAME}"},
            )
        ]
    }
    assert public.parse_games(payload, USERNAME)[0].my_color == "black"


# --- field handling -------------------------------------------------------


def test_game_id_and_placement_are_derived():
    game = parse()[0]
    assert game.id == "1022853950"
    assert game.placement == "r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R"
    assert " " not in game.placement  # placement only, never a whole FEN
    assert game.is_three_day


def test_incomplete_games_are_skipped():
    payload = {
        "games": [
            {"url": "x/1", "white": f"p/{USERNAME}", "black": "p/o", "turn": "white"},
            {"url": "x/2", "white": f"p/{USERNAME}", "black": "p/o", "fen": "8/8"},
        ]
    }
    assert public.parse_games(payload, USERNAME) == []


def test_missing_optional_fields_are_tolerated():
    raw = dict(PAYLOAD["games"][1])
    del raw["move_by"]
    game = public.parse_games({"games": [raw]}, USERNAME)[0]
    assert game.move_by is None
    assert game.last_activity is None


def test_empty_response_is_not_an_error():
    assert public.parse_games({"games": []}, USERNAME) == []
    assert public.parse_games({}, USERNAME) == []


# --- HTTP behaviour -------------------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_client(responses):
    """A client whose opener replays ``responses`` and records the requests.

    Once the script runs out it keeps serving the last entry, so a polling test
    cannot fail merely because the loop ticked more often than expected -- which
    is a property of the scheduler, not of the code under test.
    """
    requests = []
    queue = list(responses)

    def opener(request, timeout=None):
        requests.append(request)
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, Exception):
            raise result
        return result

    client = public.PublicClient(USERNAME, opener=opener)
    return client, requests


def test_a_descriptive_user_agent_is_sent():
    """chess.com asks integrations to identify themselves."""
    body = json.dumps(PAYLOAD).encode()
    client, requests = make_client([FakeResponse(body)])
    client.fetch_games_sync()
    assert "chessnut-bridge" in requests[0].get_header("User-agent")


def test_etag_is_remembered_and_replayed():
    body = json.dumps(PAYLOAD).encode()
    client, requests = make_client(
        [
            FakeResponse(body, headers={"ETag": 'W/"abc"'}),
            FakeResponse(status=304),
        ]
    )
    assert client.fetch_games_sync() is not None
    assert requests[0].get_header("If-none-match") is None

    assert client.fetch_games_sync() is None, "304 means unchanged, not an error"
    assert requests[1].get_header("If-none-match") == 'W/"abc"'


def test_a_304_raised_as_an_httperror_is_still_not_modified():
    """urllib raises on 304 without a cache handler, which is the real path."""
    import urllib.error

    error = urllib.error.HTTPError("u", 304, "Not Modified", {}, None)
    client, _ = make_client([error])
    assert client.fetch_games_sync() is None


def test_rate_limiting_surfaces_retry_after():
    import urllib.error

    error = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "42"}, None)
    client, _ = make_client([error])
    with pytest.raises(public.RateLimited) as excinfo:
        client.fetch_games_sync()
    assert excinfo.value.retry_after == 42.0


def test_server_errors_become_chesscom_errors():
    import urllib.error

    error = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
    client, _ = make_client([error])
    with pytest.raises(public.ChessComError):
        client.fetch_games_sync()


def test_unreachable_host_becomes_a_chesscom_error():
    import urllib.error

    client, _ = make_client([urllib.error.URLError("no route to host")])
    with pytest.raises(public.ChessComError, match="cannot reach"):
        client.fetch_games_sync()


def test_malformed_json_is_rejected_loudly():
    client, _ = make_client([FakeResponse(b"<html>nope</html>")])
    with pytest.raises(public.ChessComError, match="malformed"):
        client.fetch_games_sync()


# --- the polling loop -----------------------------------------------------


@pytest.mark.asyncio
async def test_watch_calls_back_only_when_data_changed():
    import asyncio

    body = json.dumps(PAYLOAD).encode()
    client, _ = make_client(
        [FakeResponse(body, headers={"ETag": 'W/"a"'}), FakeResponse(status=304)]
    )
    stop = asyncio.Event()
    seen = []

    async def on_games(games):
        seen.append(games)
        if len(seen) == 1:
            # Let the second (304) poll happen, then finish.
            asyncio.get_running_loop().call_later(0.05, stop.set)

    await asyncio.wait_for(
        public.watch_games(client, on_games, interval=0.01, stop=stop), timeout=5
    )
    assert len(seen) == 1, "a 304 must not re-notify the caller"


@pytest.mark.asyncio
async def test_watch_survives_an_unreachable_server(monkeypatch):
    """Domestic WiFi drops; the poll loop must not be what ends the service."""
    import asyncio
    import urllib.error

    monkeypatch.setattr(public, "_ERROR_BACKOFF_START", 0.01)
    body = json.dumps(PAYLOAD).encode()
    client, _ = make_client(
        [urllib.error.URLError("down"), FakeResponse(body, headers={"ETag": 'W/"a"'})]
    )
    stop = asyncio.Event()

    async def on_games(games):
        stop.set()

    await asyncio.wait_for(
        public.watch_games(client, on_games, interval=0.01, stop=stop), timeout=5
    )
    assert stop.is_set(), "should have recovered and delivered the second poll"


# --- a level beside the edge ----------------------------------------------


def test_the_last_games_are_retained_across_a_304():
    """So "unchanged" is never mistaken for "unknown" after an interruption.

    The same trap as the BLE reconnect: a consumer that needs to re-derive state
    cannot do so from an edge it was not present for.
    """
    body = json.dumps(PAYLOAD).encode()
    client, _ = make_client(
        [FakeResponse(body, headers={"ETag": 'W/"a"'}), FakeResponse(status=304)]
    )
    assert client.last_games is None, "nothing read yet is not the same as empty"

    assert client.fetch_games_sync() is not None
    assert client.fetch_games_sync() is None, "304 is still an edge of 'no change'"
    assert [g.id for g in client.last_games] == ["1022853950", "1023281994"]


def test_the_retained_level_cannot_be_mutated_by_a_caller():
    body = json.dumps(PAYLOAD).encode()
    client, _ = make_client([FakeResponse(body)])
    client.fetch_games_sync().clear()
    assert len(client.last_games) == 2
