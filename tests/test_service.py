"""Tests for the coordinator, concentrated on the write policy it owns.

The service delegates almost everything, so testing it means testing the one
decision nobody below it can make: what to do when a submission does not
obviously succeed. The cases that matter are the ambiguous ones, because those
are where an appliance either plays a move twice or silently stops playing at
all.

Everything here uses fakes: no BLE, no network. The engine, the board transport
and the writer all have their own tests.
"""

import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge import service as service_module  # noqa: E402
from bridge.chesscom import login as chesscom_login  # noqa: E402
from bridge.chesscom import public, write  # noqa: E402
from bridge.chessnut import ble, protocol  # noqa: E402
from bridge.service import BridgeService, WriteState  # noqa: E402
from bridge.state.secrets import (  # noqa: E402
    InMemoryCredentialStore,
    InMemorySessionStore,
    StoredSession,
)
from bridge.sync.engine import SyncEngine  # noqa: E402

USERNAME = "nbaronmorgan"

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def a_game(
    game_id: str = "1026053628",
    fen: str = START_FEN,
    turn: str = "white",
    last_activity: int | None = 1788861472,
) -> public.DailyGame:
    return public.DailyGame(
        id=game_id,
        url=f"https://www.chess.com/game/daily/{game_id}",
        fen=fen,
        turn=turn,
        my_color="white",
        move_by=None,
        time_control="1/259200",
        last_activity=last_activity,
    )


def a_session(username: str = USERNAME) -> StoredSession:
    return StoredSession(
        session=write.Session(cookies={"PHPSESSID": "x"}, csrf_token="c"),
        username=username,
        stored_at=1788861472.0,
    )


class FakeWriter:
    """Records submissions and raises whatever it was told to."""

    def __init__(self, raising: Exception | None = None):
        self.raising = raising
        self.calls: list[tuple] = []

    async def submit(self, session, game_id, uci, fen, last_activity):
        self.calls.append((game_id, uci, fen, last_activity))
        if self.raising is not None:
            raise self.raising
        return {}


class FakeBoard:
    """Stands in for ChessnutBoard: the service only wires callbacks and reads
    status, so nothing here needs a radio."""

    def __init__(self):
        self.status = ble.BoardStatus()
        self._on_position = None
        self._on_status = None
        self._on_battery = None
        self.leds: list[tuple[str, ...]] = []
        self.reconnects = 0
        self.stopped = False

    async def run(self):
        await asyncio.Event().wait()

    def stop(self):
        self.stopped = True

    def request_reconnect(self):
        self.reconnects += 1

    async def set_leds(self, squares):
        self.leds.append(tuple(squares))
        return True


class FakePublicClient:
    def __init__(self, games=None):
        self._games = games if games is not None else []
        self.last_games = list(self._games)
        self.fetches = 0

    async def fetch_games(self):
        self.fetches += 1
        return list(self._games)

    def set_games(self, games):
        self._games = list(games)
        self.last_games = list(games)


def make_service(
    writer=None, games=None, session=None, board=None, board_connected=True
) -> BridgeService:
    store = InMemorySessionStore(session)
    svc = BridgeService(
        username=USERNAME,
        board=board or FakeBoard(),
        public_client=FakePublicClient(games if games is not None else [a_game()]),
        writer=writer or FakeWriter(),
        session_store=store,
        engine=SyncEngine(settle_seconds=0),
    )
    if board_connected:
        # The engine will not reconcile a board it does not believe is there, so
        # most tests need the connected level set as the transport would.
        svc._board_was_connected = True
        svc.engine.set_board_connected(True)
    return svc


# --- reading status -------------------------------------------------------


def test_status_is_readable_before_anything_has_happened():
    """A view must be able to render at boot, before board or network."""
    status = make_service().status
    assert status.write.state is WriteState.IDLE
    assert status.logged_in_as is None
    assert not status.session_stored
    assert status.games_read_at is None


def test_status_reports_who_is_logged_in():
    status = make_service(session=a_session()).status
    assert status.logged_in_as == USERNAME
    assert status.session_stored


def test_an_unreadable_session_store_does_not_break_the_view():
    """A view that 500s because a file is corrupt is worse than one that says
    'logged out'."""

    class Exploding:
        def load(self):
            raise OSError("disk gone")

    svc = make_service()
    svc.session_store = Exploding()
    assert svc.status.logged_in_as is None


# --- submitting -----------------------------------------------------------


async def settle_board(svc: BridgeService, occupancy: dict[str, str]) -> None:
    """Push frames through until the position settles, as the transport would.

    Two frames, not one: the settler treats the first sight of a position as a
    new candidate and only emits it on a later frame that still agrees. A real
    board sends about ten frames a second, so any position a player actually
    left standing is seen many times.
    """
    frame = protocol.BoardFrame(
        occupancy=occupancy, placement=_placement_of(occupancy), tick=1
    )
    await svc._on_position(frame)
    await svc._on_position(frame)


def occupancy_for(fen: str) -> dict[str, str]:
    import chess

    board = chess.Board(fen)
    return {
        chess.square_name(sq): piece.symbol()
        for sq, piece in board.piece_map().items()
    }


def _placement_of(occupancy: dict[str, str]) -> str:
    """The FEN placement field for an occupancy map, as the parser would give."""
    import chess

    board = chess.Board(None)
    for square, symbol in occupancy.items():
        board.set_piece_at(chess.parse_square(square), chess.Piece.from_symbol(symbol))
    return board.board_fen()


@pytest.mark.asyncio
async def test_a_move_is_submitted_with_the_captured_field_shapes():
    writer = FakeWriter()
    svc = make_service(writer=writer, session=a_session())
    svc._observe([a_game()])
    svc.select_game("1026053628")

    await settle_board(svc, occupancy_for(AFTER_E4_FEN))

    assert writer.calls, "a legal, unambiguous move should have been submitted"
    game_id, uci, fen, last_activity = writer.calls[0]
    assert game_id == "1026053628"
    assert uci == "e2e4"
    assert last_activity == 1788861472, "must be the polled last_activity verbatim"
    assert svc.status.write.state is WriteState.ACCEPTED


@pytest.mark.asyncio
async def test_nothing_is_submitted_without_a_session():
    writer = FakeWriter()
    svc = make_service(writer=writer)  # no session
    svc._observe([a_game()])
    svc.select_game("1026053628")

    await settle_board(svc, occupancy_for(AFTER_E4_FEN))

    assert writer.calls == [], "must not send a request that cannot succeed"
    assert svc.status.write.state is WriteState.NEEDS_LOGIN
    assert svc.status.write.needs_attention


@pytest.mark.asyncio
async def test_nothing_is_submitted_without_a_last_activity_timestamp():
    """Sending a guessed lastDate would throw away the only guard that makes the
    write path safe."""
    writer = FakeWriter()
    svc = make_service(
        writer=writer, games=[a_game(last_activity=None)], session=a_session()
    )
    svc._observe([a_game(last_activity=None)])
    svc.select_game("1026053628")

    await settle_board(svc, occupancy_for(AFTER_E4_FEN))

    assert writer.calls == []
    assert svc.status.write.state is WriteState.UNVERIFIED


@pytest.mark.asyncio
async def test_a_move_is_submitted_only_once():
    """The engine dedupes, and the service must not defeat it by resubmitting."""
    writer = FakeWriter()
    svc = make_service(writer=writer, session=a_session())
    svc._observe([a_game()])
    svc.select_game("1026053628")

    occupancy = occupancy_for(AFTER_E4_FEN)
    await settle_board(svc, occupancy)
    await settle_board(svc, occupancy)
    await settle_board(svc, occupancy)

    assert len(writer.calls) == 1


# --- classifying failures -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc, expected",
    [
        (write.SessionExpired("gone"), WriteState.NEEDS_LOGIN),
        (write.ChallengePresented("challenge"), WriteState.BLOCKED),
        (write.MoveRejected("no", message="Oops! This game is already over."),
         WriteState.REJECTED),
        (write.WriteUnavailable("timeout"), WriteState.UNVERIFIED),
    ],
)
async def test_each_failure_reaches_its_own_state(exc, expected):
    """These route to different places in the UI, so conflating them would send
    the reader after the wrong problem."""
    svc = make_service(writer=FakeWriter(raising=exc), session=a_session())
    svc._observe([a_game()])
    svc.select_game("1026053628")

    await settle_board(svc, occupancy_for(AFTER_E4_FEN))

    assert svc.status.write.state is expected
    assert svc.status.write.needs_attention


@pytest.mark.asyncio
async def test_a_rejection_surfaces_chesscoms_own_words():
    exc = write.MoveRejected("no", message="Oops! This game is already over.")
    svc = make_service(writer=FakeWriter(raising=exc), session=a_session())
    svc._observe([a_game()])
    svc.select_game("1026053628")

    await settle_board(svc, occupancy_for(AFTER_E4_FEN))

    assert svc.status.write.detail == "Oops! This game is already over."


@pytest.mark.asyncio
async def test_a_failed_write_does_not_kill_the_service():
    """An unattended appliance must survive every write failure."""
    svc = make_service(
        writer=FakeWriter(raising=write.WriteUnavailable("down")), session=a_session()
    )
    svc._observe([a_game()])
    svc.select_game("1026053628")
    await settle_board(svc, occupancy_for(AFTER_E4_FEN))
    # Still usable afterwards.
    assert svc.status.write.state is WriteState.UNVERIFIED
    assert svc.status.snapshot is not None


# --- resolving an ambiguous write ----------------------------------------
#
# The heart of this module. After a timeout we do not know whether the move was
# applied, and the only honest way to find out is to look.


def stalled_service(writer=None) -> BridgeService:
    svc = make_service(
        writer=writer or FakeWriter(raising=write.WriteUnavailable("timeout")),
        session=a_session(),
    )
    svc._write = service_module.WriteStatus(
        state=WriteState.UNVERIFIED,
        game_id="1026053628",
        move="e2e4",
        fen=START_FEN,
        at=1.0,
    )
    return svc


def test_an_unchanged_game_proves_the_move_never_landed():
    """Position untouched and still our turn: the move demonstrably never
    arrived, so resending cannot be a duplicate."""
    svc = stalled_service()
    svc._resolve_unverified([a_game(fen=START_FEN, turn="white")])

    assert svc.status.write.state is WriteState.NOT_SENT
    assert svc.status.write.is_retryable


def test_a_changed_position_proves_the_move_did_land():
    """The reply was lost, not the move. Resending here would play twice."""
    svc = stalled_service()
    svc._resolve_unverified([a_game(fen=AFTER_E4_FEN, turn="black")])

    assert svc.status.write.state is WriteState.ACCEPTED
    assert not svc.status.write.is_retryable


def test_the_opponents_turn_means_the_move_landed():
    """Even with a FEN we do not recognise, it is not our move any more, so
    something of ours got through."""
    svc = stalled_service()
    svc._resolve_unverified([a_game(fen=START_FEN, turn="black")])
    assert svc.status.write.state is WriteState.ACCEPTED


def test_a_vanished_game_is_not_retried():
    """Finished or aborted. There is nothing left to send into."""
    svc = stalled_service()
    svc._resolve_unverified([])

    assert svc.status.write.state is WriteState.REJECTED
    assert not svc.status.write.is_retryable


def test_resolution_only_applies_to_an_unverified_write():
    svc = make_service()
    svc._write = service_module.WriteStatus(state=WriteState.ACCEPTED, move="e2e4")
    svc._resolve_unverified([a_game(fen=START_FEN)])
    assert svc.status.write.state is WriteState.ACCEPTED


# --- retrying -------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_a_proven_unsent_move_may_be_retried():
    """The single irreversible action a view can trigger, so the guard is real
    rather than a comment."""
    svc = stalled_service()  # UNVERIFIED, not NOT_SENT
    with pytest.raises(RuntimeError, match="must not be resent"):
        await svc.retry_write()


@pytest.mark.asyncio
async def test_an_accepted_write_may_not_be_retried():
    svc = make_service(session=a_session())
    svc._write = service_module.WriteStatus(
        state=WriteState.ACCEPTED, game_id="1", move="e2e4", fen=START_FEN
    )
    with pytest.raises(RuntimeError):
        await svc.retry_write()


@pytest.mark.asyncio
async def test_a_proven_unsent_move_can_be_resent():
    writer = FakeWriter()
    svc = make_service(writer=writer, session=a_session())
    svc._observe([a_game()])
    svc._write = service_module.WriteStatus(
        state=WriteState.NOT_SENT, game_id="1026053628", move="e2e4", fen=START_FEN
    )

    await svc.retry_write()

    assert writer.calls == [("1026053628", "e2e4", START_FEN, 1788861472)]
    assert svc.status.write.state is WriteState.ACCEPTED


@pytest.mark.asyncio
async def test_a_retry_uses_a_freshly_polled_timestamp():
    """The guard has to be current, or the resend is checked against a stale
    view of the game."""
    writer = FakeWriter()
    svc = make_service(writer=writer, session=a_session())
    svc._observe([a_game(last_activity=1788861472)])
    svc._write = service_module.WriteStatus(
        state=WriteState.NOT_SENT, game_id="1026053628", move="e2e4", fen=START_FEN
    )
    # A later poll moved the game on; the resend must use the newer value.
    svc._observe([a_game(last_activity=1788999999)])

    await svc.retry_write()
    assert writer.calls[0][3] == 1788999999


# --- selecting and authenticating ----------------------------------------


def test_selecting_a_game_clears_a_stale_write_alert():
    """The failed write belonged to a game the player is no longer pointing at."""
    svc = stalled_service()
    svc.select_game("999")
    assert svc.status.write.state is WriteState.IDLE


def test_logging_in_stores_the_session_and_marks_a_discontinuity():
    svc = make_service()
    svc.log_in(a_session())

    assert svc.status.logged_in_as == USERNAME
    from bridge.sync.engine import Discontinuity

    assert svc.status.snapshot.last_discontinuity is Discontinuity.REAUTHENTICATED


def test_logging_in_clears_a_previous_needs_login_alert():
    svc = make_service()
    svc._write = service_module.WriteStatus(state=WriteState.NEEDS_LOGIN)
    svc.log_in(a_session())
    assert svc.status.write.state is WriteState.IDLE


def test_logging_out_forgets_the_session():
    svc = make_service(session=a_session())
    svc.log_out()
    assert svc.status.logged_in_as is None
    assert svc.status.write.state is WriteState.NEEDS_LOGIN


def test_reconnecting_the_board_asks_the_transport_not_the_radio():
    board = FakeBoard()
    svc = make_service(board=board)
    svc.reconnect_board()
    assert board.reconnects == 1


def test_a_write_alert_can_be_acknowledged_without_resending():
    svc = stalled_service()
    svc.clear_write_alert()
    assert svc.status.write.state is WriteState.IDLE
    assert not svc.status.write.needs_attention


# --- board edges ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reconnect_tells_the_engine_to_re_derive():
    """Carrying a position across a gap makes an unchanged board look like no
    board at all -- observed live, and the same hazard as a reboot."""
    board = FakeBoard()
    svc = make_service(board=board, board_connected=False)

    await svc._on_board_status(replace(board.status, state=ble.BoardState.CONNECTED))
    assert svc.status.snapshot.board_connected

    await svc._on_board_status(replace(board.status, state=ble.BoardState.DISCONNECTED))
    assert not svc.status.snapshot.board_connected


@pytest.mark.asyncio
async def test_repeated_identical_board_status_is_not_a_discontinuity():
    """Status republishes on every frame; treating each as an edge would reset
    the engine ten times a second."""
    board = FakeBoard()
    svc = make_service(board=board, board_connected=False)
    connected = replace(board.status, state=ble.BoardState.CONNECTED)

    await svc._on_board_status(connected)
    first = svc.status.snapshot.last_discontinuity
    await svc._on_board_status(connected)
    await svc._on_board_status(connected)

    assert svc.status.snapshot.last_discontinuity is first


@pytest.mark.asyncio
async def test_led_failures_are_not_fatal():
    """LEDs are cosmetic; a board that refuses them must not stop a move."""

    class BadLeds(FakeBoard):
        async def set_leds(self, squares):
            raise OSError("no leds")

    svc = make_service(board=BadLeds(), session=a_session())
    svc._observe([a_game()])
    svc.select_game("1026053628")
    await settle_board(svc, occupancy_for(START_FEN))  # a mismatch lights squares
    assert svc.status.snapshot is not None


class FakeClock:
    """A clock the test moves by hand, so a linger costs no wall time."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _following_service(board: FakeBoard, clock=None) -> BridgeService:
    svc = make_service(board=board, session=a_session())
    if clock is not None:
        svc.clock = clock
    svc._observe([a_game()])
    svc.select_game("1026053628")
    return svc


@pytest.mark.asyncio
async def test_nothing_to_show_clears_the_leds():
    """Observed live 2026-09-09: the two squares of a submitted move stayed lit
    until the board slept, because an empty highlight set skipped the write. The
    LED command has no "leave as you were", so empty is an instruction."""
    board = FakeBoard()
    clock = FakeClock()
    svc = await _following_service(board, clock=clock)
    occupancy = occupancy_for(START_FEN)

    await settle_board(svc, {k: v for k, v in occupancy.items() if k != "d2"})
    assert board.leds[-1] == ("d2",)

    await settle_board(svc, occupancy)
    clock.advance(service_module.LED_LINGER_SECONDS + 0.1)
    await settle_board(svc, occupancy)
    assert board.leds[-1] == ()


@pytest.mark.asyncio
async def test_the_squares_of_your_own_move_linger_before_going_dark():
    """The highlight is the only confirmation the board itself gives, and it goes
    empty the instant the move is accepted -- which is the moment it is worth
    seeing. So the clear waits; the light never does."""
    board = FakeBoard()
    clock = FakeClock()
    svc = await _following_service(board, clock=clock)
    occupancy = occupancy_for(START_FEN)

    await settle_board(svc, {k: v for k, v in occupancy.items() if k != "d2"})
    assert board.leds[-1] == ("d2",)

    await settle_board(svc, occupancy)
    assert board.leds[-1] == ("d2",), "cleared immediately; nothing to see"

    clock.advance(service_module.LED_LINGER_SECONDS - 0.1)
    await settle_board(svc, occupancy)
    assert board.leds[-1] == ("d2",), "cleared early"

    clock.advance(0.2)
    await settle_board(svc, occupancy)
    assert board.leds[-1] == ()


@pytest.mark.asyncio
async def test_something_new_to_show_outranks_a_pending_clear():
    """Deferring a clear is safe. Deferring a *light* would not be: if the board
    needs something shown while the linger is running, it must be shown now."""
    board = FakeBoard()
    clock = FakeClock()
    svc = await _following_service(board, clock=clock)
    occupancy = occupancy_for(START_FEN)
    lifted = {k: v for k, v in occupancy.items() if k != "d2"}

    await settle_board(svc, lifted)
    await settle_board(svc, occupancy)  # linger starts

    clock.advance(1.0)
    await settle_board(svc, {k: v for k, v in occupancy.items() if k != "e2"})
    assert board.leds[-1] == ("e2",)

    # And the cancelled deadline does not fire later on its own.
    clock.advance(service_module.LED_LINGER_SECONDS + 1.0)
    await settle_board(svc, {k: v for k, v in occupancy.items() if k != "e2"})
    assert board.leds[-1] == ("e2",)


@pytest.mark.asyncio
async def test_a_reconnect_during_a_linger_does_not_leave_a_stale_deadline():
    """The board came back dark, so there is nothing left to erase -- and a
    deadline held over from before the gap would suppress the next clear."""
    board = FakeBoard()
    clock = FakeClock()
    svc = await _following_service(board, clock=clock)
    occupancy = occupancy_for(START_FEN)

    await settle_board(svc, {k: v for k, v in occupancy.items() if k != "d2"})
    await settle_board(svc, occupancy)  # linger starts
    await svc._on_board_status(replace(board.status, state=ble.BoardState.DISCONNECTED))
    await svc._on_board_status(replace(board.status, state=ble.BoardState.CONNECTED))

    await settle_board(svc, occupancy)
    assert svc._clear_leds_at is None
    assert board.leds[-1] == ()


@pytest.mark.asyncio
async def test_leds_are_written_only_when_they_change():
    """A board sends about ten frames a second and shares its radio with WiFi;
    one write per frame to say the same thing is waste, not caution."""
    board = FakeBoard()
    svc = await _following_service(board)
    lifted = {k: v for k, v in occupancy_for(START_FEN).items() if k != "d2"}

    await settle_board(svc, lifted)
    settled = len(board.leds)
    await settle_board(svc, lifted)
    await settle_board(svc, lifted)

    assert len(board.leds) == settled


@pytest.mark.asyncio
async def test_a_board_that_slept_is_lit_again_on_reconnect():
    """It came back dark whatever we last sent it, so the cache must not
    suppress the rewrite."""
    board = FakeBoard()
    svc = await _following_service(board)
    lifted = {k: v for k, v in occupancy_for(START_FEN).items() if k != "d2"}
    await settle_board(svc, lifted)

    await svc._on_board_status(replace(board.status, state=ble.BoardState.DISCONNECTED))
    await svc._on_board_status(replace(board.status, state=ble.BoardState.CONNECTED))
    await settle_board(svc, lifted)

    assert board.leds[-1] == ("d2",)


@pytest.mark.asyncio
async def test_a_dropped_led_write_is_tried_again():
    """set_leds returns False rather than raising when the board is away.
    Recording that as lit would leave the board dark until the set changed."""

    class DroppingBoard(FakeBoard):
        async def set_leds(self, squares):
            await super().set_leds(squares)
            return False

    board = DroppingBoard()
    svc = await _following_service(board)
    lifted = {k: v for k, v in occupancy_for(START_FEN).items() if k != "d2"}

    await settle_board(svc, lifted)
    await settle_board(svc, lifted)

    assert board.leds.count(("d2",)) > 1


# --- the poll loop --------------------------------------------------------


@pytest.mark.asyncio
async def test_polling_records_when_it_last_succeeded():
    """Silence is the symptom a stalled poll shows, so a view needs a clock."""
    svc = make_service()
    svc.poll_interval = 3600
    task = asyncio.create_task(svc._poll_loop())
    await asyncio.sleep(0)
    for _ in range(50):
        if svc.status.games_read_at is not None:
            break
        await asyncio.sleep(0.01)
    svc.stop()
    task.cancel()
    assert svc.status.games_read_at is not None
    assert svc.status.read_error is None


@pytest.mark.asyncio
async def test_a_read_failure_is_surfaced_not_raised():
    class Failing:
        last_games = []

        async def fetch_games(self):
            raise public.ChessComError("cannot reach api.chess.com")

    svc = make_service()
    svc.public_client = Failing()
    svc.poll_interval = 3600
    task = asyncio.create_task(svc._poll_loop())
    for _ in range(50):
        if svc.status.read_error:
            break
        await asyncio.sleep(0.01)
    svc.stop()
    task.cancel()
    assert svc.status.read_error is not None
    assert not task.done() or task.cancelled(), "the loop must not die"


@pytest.mark.asyncio
async def test_a_poll_can_be_requested_out_of_band():
    """After a write, waiting out a two-minute interval with a stale position
    would be pointless."""
    svc = make_service()
    svc.poll_interval = 3600
    task = asyncio.create_task(svc._poll_loop())
    for _ in range(50):
        if svc.public_client.fetches >= 1:
            break
        await asyncio.sleep(0.01)
    first = svc.public_client.fetches

    svc.request_poll()
    for _ in range(50):
        if svc.public_client.fetches > first:
            break
        await asyncio.sleep(0.01)

    svc.stop()
    task.cancel()
    assert svc.public_client.fetches > first


@pytest.mark.asyncio
async def test_stopping_ends_the_poll_loop():
    svc = make_service()
    svc.poll_interval = 3600
    task = asyncio.create_task(svc._poll_loop())
    await asyncio.sleep(0.02)
    svc.stop()
    await asyncio.wait_for(task, timeout=2)
    assert task.done()


# --- signing in with a stored password -------------------------------------
#
# This is the policy that makes the appliance unattended, and it has exactly one
# dangerous edge: re-sending a move after signing in again. That is safe *only*
# because a 401 proves the move was never applied, so the tests below pin both
# the recovery and the boundaries of it -- no re-login on an ambiguous failure,
# no second re-login, and no retry loop against a password that cannot work.


class FakeLogin:
    """Stands in for ChessComLogin: records attempts, returns or raises."""

    def __init__(self, session=None, raising=None):
        self.session = session or write.Session(cookies={"PHPSESSID": "fresh"})
        self.raising = raising
        self.attempts: list = []

    async def log_in(self, credentials):
        self.attempts.append(credentials)
        if self.raising is not None:
            raise self.raising
        return self.session


def with_login(svc, fake):
    """Point the service at one login client and hand it back for inspection."""
    svc.login_factory = lambda: fake
    return fake


def make_service_with_password(
    writer=None, games=None, session=None, credentials=None, login_client=None
):
    svc = make_service(writer=writer, games=games, session=session)
    svc.credential_store = InMemoryCredentialStore(
        credentials if credentials is not None else some_credentials()
    )
    with_login(svc, login_client or FakeLogin())
    return svc


def some_credentials():
    return chesscom_login.Credentials(username=USERNAME, password="sekrit-password")


@pytest.mark.asyncio
async def test_signing_in_stores_the_session_and_the_password():
    svc = make_service()
    fake = with_login(svc, FakeLogin())
    await svc.sign_in(USERNAME, "sekrit-password")

    assert svc.status.logged_in_as == USERNAME
    assert svc.status.credentials_stored
    assert fake.attempts[0].password == "sekrit-password"


@pytest.mark.asyncio
async def test_declining_to_remember_gets_a_session_and_stores_no_password():
    svc = make_service()
    with_login(svc, FakeLogin())
    await svc.sign_in(USERNAME, "sekrit-password", remember=False)

    assert svc.status.logged_in_as == USERNAME
    assert not svc.status.credentials_stored


@pytest.mark.asyncio
async def test_a_refused_password_is_not_stored():
    """Keeping a password chess.com has just rejected would have the appliance
    retrying it on a timer, which is how an account gets locked."""
    svc = make_service()
    with_login(svc, FakeLogin(raising=chesscom_login.BadCredentials("nope")))

    with pytest.raises(chesscom_login.BadCredentials):
        await svc.sign_in(USERNAME, "wrong-password")
    assert not svc.status.credentials_stored
    assert svc.status.logged_in_as is None


@pytest.mark.asyncio
async def test_a_move_with_no_session_signs_in_first_then_sends():
    """The ordinary state after a session lapses: the password is the only reason
    the move goes out at all."""
    writer = FakeWriter()
    svc = make_service_with_password(writer=writer, session=None)
    svc._observe([a_game()])

    status = await svc._submit("1026053628", "e2e4", START_FEN)

    assert status.state is WriteState.ACCEPTED
    assert writer.calls  # the move really was sent
    assert svc.status.logged_in_as == USERNAME


@pytest.mark.asyncio
async def test_an_expired_session_is_replaced_and_the_move_resent_once():
    """A 401 is proof the move was not applied, so this resend cannot duplicate
    it -- the one place the no-retry rule is deliberately relaxed."""

    class ExpiresOnceWriter:
        def __init__(self):
            self.calls = []

        async def submit(self, session, game_id, uci, fen, last_activity):
            self.calls.append(session.cookies.get("PHPSESSID"))
            if len(self.calls) == 1:
                raise write.SessionExpired("HTTP 401", status=401)
            return {}

    writer = ExpiresOnceWriter()
    svc = make_service_with_password(writer=writer, session=a_session())
    svc._observe([a_game()])

    status = await svc._submit("1026053628", "e2e4", START_FEN)

    assert status.state is WriteState.ACCEPTED
    assert writer.calls == ["x", "fresh"]  # resent with the new session


@pytest.mark.asyncio
async def test_a_second_expiry_is_not_met_with_a_second_sign_in():
    """Something a third login will not fix. It becomes an ordinary
    NEEDS_LOGIN for a person rather than a loop."""
    writer = FakeWriter(raising=write.SessionExpired("HTTP 401", status=401))
    fake = FakeLogin()
    svc = make_service_with_password(
        writer=writer, session=a_session(), login_client=fake
    )
    svc._observe([a_game()])

    status = await svc._submit("1026053628", "e2e4", START_FEN)

    assert status.state is WriteState.NEEDS_LOGIN
    assert len(fake.attempts) == 1
    assert len(writer.calls) == 2


@pytest.mark.asyncio
async def test_an_ambiguous_failure_does_not_trigger_a_sign_in_or_a_resend():
    """The protection that matters. A timeout may already have applied the move,
    so nothing here may resend it -- signing in must not become a back door to
    the retry the write path refuses."""
    writer = FakeWriter(raising=write.WriteUnavailable("timed out"))
    fake = FakeLogin()
    svc = make_service_with_password(
        writer=writer, session=a_session(), login_client=fake
    )
    svc._observe([a_game()])

    status = await svc._submit("1026053628", "e2e4", START_FEN)

    assert status.state is WriteState.UNVERIFIED
    assert fake.attempts == []
    assert len(writer.calls) == 1


@pytest.mark.asyncio
async def test_a_rejected_move_does_not_trigger_a_sign_in():
    writer = FakeWriter(raising=write.MoveRejected("illegal move"))
    fake = FakeLogin()
    svc = make_service_with_password(
        writer=writer, session=a_session(), login_client=fake
    )
    svc._observe([a_game()])

    status = await svc._submit("1026053628", "e2e4", START_FEN)

    assert status.state is WriteState.REJECTED
    assert fake.attempts == []


@pytest.mark.asyncio
async def test_a_password_that_cannot_work_is_forgotten_rather_than_retried():
    """2FA and a wrong password will never succeed unattended. Forgetting stops
    an endless retry and makes the page ask for something that can work."""
    svc = make_service_with_password(
        session=None,
        login_client=FakeLogin(
            raising=chesscom_login.VerificationRequired("needs two-factor")
        ),
    )
    assert not await svc._sign_in_from_store()

    assert not svc.status.credentials_stored
    assert "two-factor" in svc.status.login_error


@pytest.mark.asyncio
async def test_a_temporary_failure_keeps_the_password_for_another_go():
    """A network blip must not cost the owner their stored password."""
    svc = make_service_with_password(
        session=None, login_client=FakeLogin(raising=chesscom_login.LoginUnavailable("down"))
    )
    assert not await svc._sign_in_from_store()

    assert svc.status.credentials_stored
    assert svc.status.login_error


@pytest.mark.asyncio
async def test_repeated_failures_respect_the_retry_floor():
    """Without this the appliance POSTs a password at chess.com every time a move
    is ready, which is what gets a device treated as an attacker."""
    fake = FakeLogin(raising=chesscom_login.LoginUnavailable("down"))
    svc = make_service_with_password(session=None, login_client=fake)

    assert not await svc._sign_in_from_store()
    assert not await svc._sign_in_from_store()  # immediately again

    assert len(fake.attempts) == 1


@pytest.mark.asyncio
async def test_a_person_pressing_the_button_ignores_the_retry_floor():
    fake = FakeLogin(raising=chesscom_login.LoginUnavailable("down"))
    svc = make_service_with_password(session=None, login_client=fake)

    assert not await svc._sign_in_from_store()
    assert not await svc.sign_in_again()  # deliberate, so it goes now

    assert len(fake.attempts) == 2


@pytest.mark.asyncio
async def test_no_stored_password_means_no_attempt_and_a_clear_state():
    fake = FakeLogin()
    svc = make_service(session=None)
    svc.credential_store = InMemoryCredentialStore()
    with_login(svc, fake)
    svc._observe([a_game()])

    status = await svc._submit("1026053628", "e2e4", START_FEN)

    assert status.state is WriteState.NEEDS_LOGIN
    assert fake.attempts == []


@pytest.mark.asyncio
async def test_an_unexpected_failure_signing_in_does_not_escape():
    """The recovery path runs from the write path, which must not die of it."""
    svc = make_service_with_password(
        session=None, login_client=FakeLogin(raising=RuntimeError("boom"))
    )
    assert not await svc._sign_in_from_store()
    assert "boom" in svc.status.login_error


@pytest.mark.asyncio
async def test_signing_out_also_forgets_the_password():
    """Otherwise the appliance signs straight back in on the next move, and a
    sign-out button that does not sign you out is worse than none."""
    svc = make_service_with_password(session=a_session())
    svc.log_out()

    assert svc.status.logged_in_as is None
    assert not svc.status.credentials_stored
    assert svc.status.write.state is WriteState.NEEDS_LOGIN


@pytest.mark.asyncio
async def test_forgetting_the_password_keeps_the_session():
    """"Stop signing me in automatically" without "stop working now"."""
    svc = make_service_with_password(session=a_session())
    svc.forget_credentials()

    assert svc.status.logged_in_as == USERNAME
    assert not svc.status.credentials_stored


@pytest.mark.asyncio
async def test_the_password_never_reaches_the_logs(caplog):
    svc = make_service_with_password(session=None)
    with caplog.at_level(logging.DEBUG):
        await svc.sign_in(USERNAME, "sekrit-password")
        await svc._sign_in_from_store(force=True)
    assert "sekrit-password" not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_sign_in_is_reported_in_chesscoms_own_words():
    svc = make_service_with_password(
        session=None,
        login_client=FakeLogin(
            raising=chesscom_login.BadCredentials("chess.com did not accept that")
        ),
    )
    await svc._sign_in_from_store()
    assert "did not accept" in svc.status.login_error
