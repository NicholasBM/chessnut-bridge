"""Tests for the move submission path.

The centre of gravity here is the two real captures from game 1026053628. They
are the only evidence that any of the request shape is correct, so they are
asserted field by field -- if a refactor changes what goes on the wire, these
fail rather than the appliance quietly submitting something chess.com ignores.

Everything else tests the two properties that matter for an unattended device:
a failure is never mistaken for a success, and credentials never end up in a log
or a traceback.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import urllib.error  # noqa: E402

from bridge.chesscom import write  # noqa: E402

# --- the captured samples -------------------------------------------------
#
# Game 1026053628, 2026-09-08, cross-checked against the game's own PGN
# (1. e4 e5 2. Nf3 Nc6 3. Nc3).

SAMPLE_ONE = {
    "uci": "g1f3",  # Nf3, White's 2nd move
    "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    "last_activity": 1788861472,
    "expected": {"lastDate": 1788861472, "plyCount": 2, "move": "gv", "squared": 1},
}

SAMPLE_TWO = {
    "uci": "b1c3",  # Nc3, White's 3rd move
    "fen": "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
    "last_activity": 1788861517,
    "expected": {"lastDate": 1788861517, "plyCount": 4, "move": "bs", "squared": 1},
}


@pytest.mark.parametrize("sample", [SAMPLE_ONE, SAMPLE_TWO], ids=["Nf3", "Nc3"])
def test_the_payload_matches_the_captured_request_exactly(sample):
    """The whole write path rests on these two observations."""
    built = write.build_payload(
        sample["uci"], sample["fen"], sample["last_activity"]
    )
    assert built == sample["expected"]


@pytest.mark.parametrize("sample", [SAMPLE_ONE, SAMPLE_TWO], ids=["Nf3", "Nc3"])
def test_ply_count_is_derived_not_guessed(sample):
    assert write.ply_count_from_fen(sample["fen"]) == sample["expected"]["plyCount"]


# --- plyCount ------------------------------------------------------------


def test_ply_count_at_the_very_start_of_a_game():
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert write.ply_count_from_fen(fen) == 0


def test_black_to_move_is_an_odd_ply():
    """The half-move offset. Getting this wrong desynchronises every reply."""
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    assert write.ply_count_from_fen(fen) == 1


def test_ply_count_counts_up_through_a_long_game():
    fen = "8/8/8/4k3/8/8/8/4K3 b - - 0 60"
    assert write.ply_count_from_fen(fen) == 119


@pytest.mark.parametrize(
    "fen",
    [
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -",  # truncated
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR x KQkq - 0 1",  # no side
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 zero",  # not a number
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 0",  # 0 is impossible
    ],
)
def test_a_malformed_fen_refuses_rather_than_inventing_a_ply(fen):
    """Better to fail before sending than to submit a confidently wrong number."""
    with pytest.raises(ValueError):
        write.ply_count_from_fen(fen)


def test_ply_derivation_agrees_with_python_chess_across_random_games():
    """The evidence that the arithmetic is right in general, not just on the two
    captured samples.

    ``python-chess`` computes the same quantity in ``Board.ply()``, so it makes a
    free oracle. We do not simply *call* it: that would mean constructing a
    ``Board`` -- which parses and validates a whole position -- on the submission
    path, and raising on positions the server would have accepted. Deriving two
    integers from two FEN fields cannot fail for a reason that matters. But
    agreeing with the oracle everywhere is worth proving.
    """
    import random

    import chess

    random.seed(0)
    checked = 0
    for _ in range(60):
        board = chess.Board()
        for _ in range(random.randint(0, 80)):
            if board.is_game_over():
                break
            board.push(random.choice(list(board.legal_moves)))
            assert write.ply_count_from_fen(board.fen()) == board.ply(), board.fen()
            checked += 1
    assert checked > 2000, "the sweep must actually cover a meaningful sample"


@pytest.mark.parametrize("sample", [SAMPLE_ONE, SAMPLE_TWO], ids=["Nf3", "Nc3"])
def test_the_captured_samples_are_real_positions(sample):
    """Guards the fixtures themselves. A test built on an impossible position
    proves nothing, and these FENs were reconstructed rather than captured."""
    import chess

    board = chess.Board(sample["fen"])
    assert board.is_valid()
    assert chess.Move.from_uci(sample["uci"]) in board.legal_moves


# --- not leaking credentials ---------------------------------------------


def a_session() -> write.Session:
    return write.Session(
        cookies={"PHPSESSID": "sekrit-session", "cf_clearance": "sekrit-clearance"},
        csrf_token="sekrit-csrf",
        play_client="sekrit-client",
    )


def test_a_session_never_renders_its_secrets():
    """This object lands in tracebacks and log lines. The recurring accident in
    this project has been secrets turning up where nobody put them."""
    rendered = f"{a_session()!r} {a_session()!s}"
    for secret in ("sekrit-session", "sekrit-clearance", "sekrit-csrf", "sekrit-client"):
        assert secret not in rendered
    assert "2 redacted" in rendered


def test_a_session_repr_still_says_what_is_populated():
    """Redaction must not make it undebuggable."""
    empty = repr(write.Session())
    assert "csrf_token=unset" in empty
    assert "0 redacted" in empty


def test_submitting_does_not_log_credentials(caplog):
    opener = FakeOpener(body=b"{}")
    with caplog.at_level(logging.DEBUG):
        MoveWriterFor(opener).submit_sync(
            a_session(), "1026053628", "g1f3", SAMPLE_ONE["fen"], 1788861472
        )
    for secret in ("sekrit-session", "sekrit-clearance", "sekrit-csrf"):
        assert secret not in caplog.text
    assert "g1f3" in caplog.text, "the move itself is safe and worth logging"


def test_an_error_does_not_log_credentials(caplog):
    opener = FakeOpener(http_error(403, b'{"message":"nope"}'))
    with caplog.at_level(logging.DEBUG), pytest.raises(write.MoveRejected):
        MoveWriterFor(opener).submit_sync(
            a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
        )
    assert "sekrit-session" not in caplog.text


# --- the request on the wire ---------------------------------------------


class FakeResponse:
    def __init__(self, body: bytes, status: int, content_type: str):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records the request it was handed, then returns a canned reply."""

    def __init__(
        self,
        raising: Exception | None = None,
        body: bytes = b"{}",
        status: int = 200,
        content_type: str = "application/json",
    ):
        self.raising = raising
        self.body = body
        self.status = status
        self.content_type = content_type
        self.requests: list[object] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.raising is not None:
            raise self.raising
        return FakeResponse(self.body, self.status, self.content_type)

    @property
    def sent_json(self) -> dict:
        return json.loads(self.requests[-1].data)


def MoveWriterFor(opener: FakeOpener) -> write.MoveWriter:
    return write.MoveWriter(opener=opener)


def http_error(code: int, body: bytes, content_type: str = "application/json"):
    return urllib.error.HTTPError(
        url="https://www.chess.com/callback/game/1/submit-move",
        code=code,
        msg="err",
        hdrs={"Content-Type": content_type},  # type: ignore[arg-type]
        fp=None,
    )


class BodiedHTTPError(urllib.error.HTTPError):
    """An HTTPError whose body can actually be read, as a real one's can."""

    def __init__(self, code: int, body: bytes, content_type: str):
        super().__init__(
            url="https://www.chess.com/callback/game/1/submit-move",
            code=code,
            msg="err",
            hdrs={"Content-Type": content_type},  # type: ignore[arg-type]
            fp=None,
        )
        self._body = body

    def read(self) -> bytes:  # type: ignore[override]
        return self._body


def test_the_request_goes_to_the_game_callback_url():
    """Not /callback/daily/game/... -- that prefix exists but is for navigation."""
    opener = FakeOpener()
    MoveWriterFor(opener).submit_sync(
        a_session(), "1026053628", "g1f3", SAMPLE_ONE["fen"], 1788861472
    )
    request = opener.requests[0]
    assert request.full_url == (
        "https://www.chess.com/callback/game/1026053628/submit-move"
    )
    assert request.method == "POST"


def test_the_body_sent_is_the_captured_body():
    opener = FakeOpener()
    MoveWriterFor(opener).submit_sync(
        a_session(), "1026053628", "g1f3", SAMPLE_ONE["fen"], 1788861472
    )
    assert opener.sent_json == SAMPLE_ONE["expected"]


def test_the_observed_headers_are_sent():
    opener = FakeOpener()
    MoveWriterFor(opener).submit_sync(
        a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
    )
    headers = {k.lower(): v for k, v in opener.requests[0].header_items()}
    assert headers["content-type"] == "application/json"
    assert headers["x-chesscom-csrf-token"] == "sekrit-csrf"
    assert "PHPSESSID=sekrit-session" in headers["cookie"]
    assert headers["user-agent"].startswith("Mozilla/5.0")


def test_optional_headers_are_omitted_rather_than_sent_empty():
    """A blank CSRF header is not the same as no CSRF header, and we do not know
    how the server treats one."""
    opener = FakeOpener()
    session = write.Session(cookies={"PHPSESSID": "x"})
    MoveWriterFor(opener).submit_sync(session, "1", "g1f3", SAMPLE_ONE["fen"], 1)
    header_names = {k.lower() for k, _ in opener.requests[0].header_items()}
    assert "x-chesscom-csrf-token" not in header_names
    assert "x-chesscom-play-client" not in header_names


# --- refusing to send ----------------------------------------------------


def test_an_empty_session_fails_before_touching_the_network():
    opener = FakeOpener()
    with pytest.raises(write.SessionExpired):
        MoveWriterFor(opener).submit_sync(
            write.Session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
        )
    assert opener.requests == [], "must not send a request that cannot succeed"


def test_a_bad_fen_fails_before_touching_the_network():
    opener = FakeOpener()
    with pytest.raises(ValueError):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", "not-a-fen", 1)
    assert opener.requests == []


# --- classifying the reply -----------------------------------------------


def test_an_application_error_surfaces_chesscoms_own_words():
    """The real 403 seen while testing Cloudflare. Paraphrasing this would lose
    the one piece of information that explains what happened."""
    body = b'{"message":"Oops! This game is already over."}'
    opener = FakeOpener(BodiedHTTPError(403, body, "application/json"))
    with pytest.raises(write.MoveRejected) as caught:
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)
    assert caught.value.message == "Oops! This game is already over."
    assert caught.value.for_display == "Oops! This game is already over."
    assert caught.value.status == 403


def test_an_error_without_a_message_still_displays_something():
    opener = FakeOpener(BodiedHTTPError(400, b"", "application/json"))
    with pytest.raises(write.MoveRejected) as caught:
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)
    assert "400" in caught.value.for_display


def test_a_401_is_a_session_problem_not_a_move_problem():
    """These route to different places in the UI: one asks for a login, the
    other says the move was refused."""
    opener = FakeOpener(BodiedHTTPError(401, b'{"message":"Unauthorized"}', "application/json"))
    with pytest.raises(write.SessionExpired):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_a_server_error_is_ambiguous_not_a_rejection():
    """A 5xx may have applied the move. Calling it a rejection would invite a
    retry that plays twice."""
    opener = FakeOpener(BodiedHTTPError(503, b"", "application/json"))
    with pytest.raises(write.WriteUnavailable):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_an_html_error_page_is_interception_not_rejection():
    """Sends the reader after a network problem instead of a chess problem."""
    page = b"<!DOCTYPE html><html><body>Attention Required</body></html>"
    opener = FakeOpener(BodiedHTTPError(403, page, "text/html; charset=UTF-8"))
    with pytest.raises(write.ChallengePresented):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_an_html_body_on_a_200_is_still_interception():
    """A challenge can arrive with a success status. Reporting the move as sent
    would be the worst possible outcome."""
    opener = FakeOpener(body=b"<html>just a moment</html>", content_type="text/html")
    with pytest.raises(write.ChallengePresented):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_html_is_detected_even_when_the_content_type_lies():
    opener = FakeOpener(body=b"  <!doctype html><html></html>", content_type="application/json")
    with pytest.raises(write.ChallengePresented):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_a_network_failure_is_reported_as_ambiguous():
    """The case that must never be retried automatically: the move may already
    have been applied and only a re-read of the game can say."""
    opener = FakeOpener(urllib.error.URLError("connection reset"))
    with pytest.raises(write.WriteUnavailable):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_a_timeout_is_reported_as_ambiguous():
    opener = FakeOpener(TimeoutError("timed out"))
    with pytest.raises(write.WriteUnavailable):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)


def test_every_failure_is_a_write_error():
    """So a caller can catch one type and never crash the sync loop, which is
    what keeps an unattended appliance running."""
    for exc_type in (
        write.MoveRejected,
        write.SessionExpired,
        write.ChallengePresented,
        write.WriteUnavailable,
    ):
        assert issubclass(exc_type, write.WriteError)


# --- success -------------------------------------------------------------


def test_a_success_returns_the_decoded_reply():
    opener = FakeOpener(body=b'{"ok":true}')
    result = MoveWriterFor(opener).submit_sync(
        a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
    )
    assert result == {"ok": True}


def test_an_empty_success_body_is_not_an_error():
    """The reply shape was never captured, so requiring a field would be an
    invented contract."""
    opener = FakeOpener(body=b"")
    assert MoveWriterFor(opener).submit_sync(
        a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
    ) == {}


def test_a_non_json_success_body_is_not_an_error():
    opener = FakeOpener(body=b"OK")
    assert MoveWriterFor(opener).submit_sync(
        a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
    ) == {}


def test_one_call_sends_exactly_one_request():
    """No retry anywhere in this class. Duplicate submission is the one failure
    mode with no recovery."""
    opener = FakeOpener()
    MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)
    assert len(opener.requests) == 1


def test_a_failed_call_sends_exactly_one_request():
    opener = FakeOpener(urllib.error.URLError("down"))
    with pytest.raises(write.WriteUnavailable):
        MoveWriterFor(opener).submit_sync(a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1)
    assert len(opener.requests) == 1


@pytest.mark.asyncio
async def test_the_async_wrapper_works():
    opener = FakeOpener(body=b'{"ok":true}')
    result = await MoveWriterFor(opener).submit(
        a_session(), "1", "g1f3", SAMPLE_ONE["fen"], 1
    )
    assert result == {"ok": True}


# --- diagnostics ---------------------------------------------------------


def test_missing_observed_cookies_are_reportable():
    """For the UI to explain a half-captured session without enforcing a set we
    have not actually verified is required."""
    session = write.Session(cookies={"PHPSESSID": "x"})
    assert "cf_clearance" in session.missing_observed_cookies
    assert "PHPSESSID" not in session.missing_observed_cookies
