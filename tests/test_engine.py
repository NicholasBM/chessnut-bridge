"""Tests for the level-driven engine, centred on the bug that motivated it.

The headline test is ``test_a_disconnect_anywhere_does_not_change_the_outcome``:
it inserts a BLE drop at *every* point in a frame sequence and asserts the final
reconciliation is identical each time. That is one property covering every
placement of the interruption, rather than one test per scenario -- and it is the
shape of bug that shipped, so a scenario list would probably have missed it
again.
"""

import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom.public import DailyGame  # noqa: E402
from bridge.sync.engine import Discontinuity, SyncEngine  # noqa: E402
from bridge.sync.reconcile import SyncState, occupancy_from_fen  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


#: A real position from game 1022853950, black to move after 1... is white's.
FEN = "r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R w - - 3 22"
OTHER_FEN = "r3k1nr/1p3ppp/n1pp2q1/8/Q1P1Pp2/3P1N1b/PP4PP/RN2R1K1 w kq - 2 14"


def game(game_id: str = "1022853950", fen: str = FEN, colour: str = "white") -> DailyGame:
    return DailyGame(
        id=game_id,
        url=f"https://www.chess.com/game/daily/{game_id}",
        fen=fen,
        turn="white",
        my_color=colour,
        move_by=None,
        time_control="1/259200",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine(clock: FakeClock) -> SyncEngine:
    return SyncEngine(clock=clock, settle_seconds=2.0)


def hold(engine: SyncEngine, clock: FakeClock, occupancy, seconds: float = 3.0):
    """Present one position long enough for the settle gate to accept it."""
    engine.on_frame(occupancy)
    clock.advance(seconds)
    return engine.on_frame(occupancy)


def after_move(fen: str, uci: str) -> dict[str, str]:
    board = chess.Board(fen)
    board.push_uci(uci)
    return occupancy_from_fen(board.board_fen())


# --- the bug ---------------------------------------------------------------


def test_a_reconnect_re_derives_even_though_nothing_changed(engine, clock):
    """The exact live failure: reconnect, unchanged board, previously silent."""
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.reconciliation.state is SyncState.IN_SYNC

    engine.set_board_connected(False)
    engine.set_board_connected(True)
    assert engine.snapshot.reconciliation is None, "must not trust a stale board"
    assert engine.snapshot.reason == "waiting for the board to settle"

    # The board is showing exactly what it showed before the drop.
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.reconciliation.state is SyncState.IN_SYNC, (
        "an unchanged board after a reconnect must still produce a reconciliation"
    )


@pytest.mark.parametrize("insert_at", range(6))
def test_a_disconnect_anywhere_does_not_change_the_outcome(insert_at, clock):
    """Inserting a BLE drop at any point must not alter the final state.

    Written as an invariant rather than a scenario because the failure mode is
    "the interruption happened somewhere we did not think to test".
    """
    start = occupancy_from_fen(FEN)
    lifted = {sq: p for sq, p in start.items() if sq != "e4"}
    done = after_move(FEN, "e4f6")
    script = [start, start, lifted, lifted, done, done]

    def run(with_drop_at: int | None):
        engine = SyncEngine(clock=FakeClock(), settle_seconds=2.0)
        engine.observe_games([game()])
        engine.select_game("1022853950")
        engine.set_board_connected(True)
        for index, occupancy in enumerate(script):
            if index == with_drop_at:
                engine.set_board_connected(False)
                engine.set_board_connected(True)
            engine.on_frame(occupancy)
            engine.clock.advance(3.0)
        # Whatever happened, the board is now steady on the final position.
        for _ in range(3):
            engine.on_frame(done)
            engine.clock.advance(3.0)
        return engine.snapshot

    baseline = run(None)
    interrupted = run(insert_at)

    assert baseline.reconciliation == interrupted.reconciliation
    assert interrupted.reconciliation.state is SyncState.MOVE_READY
    assert interrupted.reconciliation.move == "e4f6"


def test_a_connect_without_a_matching_disconnect_still_discards(clock):
    """The case the connect-side reset actually defends, found by mutation.

    Resetting on disconnect makes resetting on connect look redundant -- but only
    while both edges are observed. A transport that re-establishes its own link,
    or one whose disconnect notification is dropped, reports a connect with no
    preceding disconnect. Trust has to be re-earned per connection, not per
    observed gap.
    """
    engine = SyncEngine(clock=clock, settle_seconds=2.0)
    engine.observe_games([game()])
    engine.select_game("1022853950")
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.board_known, "a position was settled"

    engine.set_board_connected(True)  # no disconnect ever seen
    assert not engine.snapshot.board_known
    assert engine.snapshot.reason == "waiting for the board to settle"


def test_losing_the_board_discards_the_position_rather_than_remembering_it(
    engine, clock
):
    """A remembered position is indistinguishable from a current one."""
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.board_known

    engine.set_board_connected(False)
    assert not engine.snapshot.board_known
    assert engine.snapshot.reconciliation is None
    assert engine.snapshot.reason == "board not connected"
    assert engine.take_submission() is None


def test_every_discontinuity_re_evaluates(engine, clock):
    """Each one must move the timestamp, or it is not really being handled."""
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))

    for discontinuity in (
        Discontinuity.NETWORK_RETURNED,
        Discontinuity.REAUTHENTICATED,
    ):
        clock.advance(60)
        before = engine.snapshot.evaluated_at
        engine.note(discontinuity)
        assert engine.snapshot.evaluated_at > before
        assert engine.snapshot.last_discontinuity is discontinuity


def test_startup_is_itself_a_discontinuity(engine):
    """So cold start and reconnect cannot drift apart into two code paths."""
    assert engine.snapshot.last_discontinuity is Discontinuity.STARTUP
    assert engine.snapshot.reconciliation is None
    assert not engine.snapshot.board_connected


def test_staleness_is_measurable(engine, clock):
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    clock.advance(600)
    assert engine.seconds_since_evaluated() == pytest.approx(600, abs=1)


# --- suppression happens once, at the output ------------------------------


def test_a_move_is_handed_out_exactly_once(engine, clock):
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, after_move(FEN, "e4f6"))

    submission = engine.take_submission()
    assert submission is not None
    assert submission.move == "e4f6"
    assert submission.game_id == "1022853950"
    assert engine.take_submission() is None, "must not offer the same move twice"


def test_re_evaluating_does_not_re_offer_a_taken_move(engine, clock):
    """Frequent re-derivation is the whole design; it must stay safe."""
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, after_move(FEN, "e4f6"))
    assert engine.take_submission() is not None

    for discontinuity in Discontinuity:
        engine.note(discontinuity)
        assert engine.take_submission() is None


def test_a_reconnect_does_not_resubmit_a_move_already_sent(engine, clock):
    """The dangerous version of the original bug, in the other direction."""
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    moved = after_move(FEN, "e4f6")
    hold(engine, clock, moved)
    assert engine.take_submission() is not None

    engine.set_board_connected(False)
    engine.set_board_connected(True)
    hold(engine, clock, moved)
    assert engine.snapshot.is_submittable, "still a legitimate reading of the board"
    assert engine.take_submission() is None, "but it has already been sent"


def test_the_same_move_in_a_later_position_is_not_swallowed(engine, clock):
    """Suppression is keyed on the position too, so repetition still works."""
    first = game(fen="8/8/8/8/8/8/8/R3K2k w - - 0 1")
    engine.observe_games([first])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, after_move(first.fen, "a1b1"))
    assert engine.take_submission().move == "a1b1"

    # Later, the rook is back on a1 with the same move available again.
    engine.observe_games([game(fen="8/8/8/8/8/7k/8/R3K3 w - - 4 3")])
    hold(engine, clock, after_move("8/8/8/8/8/7k/8/R3K3 w - - 4 3", "a1b1"))
    assert engine.take_submission().move == "a1b1", "a genuine repetition"


# --- choosing which game ---------------------------------------------------


def test_nothing_is_selected_automatically(engine, clock):
    """Even with a single game that plainly matches the board.

    Detection can only identify the game already set up, and picking the wrong
    game means submitting a legal move into a game the player is not playing --
    which looks entirely normal and so would not be caught by anything else.
    """
    engine.observe_games([game()])
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))

    assert engine.snapshot.game is None
    assert engine.snapshot.reason == "no game selected -- pick one"
    assert engine.take_submission() is None


def test_the_picker_matching_hint_never_selects(engine, clock):
    engine.observe_games([game("1111", OTHER_FEN), game("2222", FEN)])
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))

    hints = {choice.game.id: choice.matches_board for choice in engine.choices()}
    assert hints == {"1111": False, "2222": True}
    assert engine.snapshot.game is None, "a hint is not a selection"


def test_the_picker_puts_the_urgent_games_first(engine):
    engine.observe_games(
        [
            game("later", colour="white"),
            game("mine-soon", colour="white"),
            game("theirs", colour="black"),
        ]
    )
    # move_by is not settable through the helper, so rebuild with deadlines.
    from dataclasses import replace as _replace

    games = [
        _replace(game("later", colour="white"), move_by=9999),
        _replace(game("mine-soon", colour="white"), move_by=100),
        _replace(game("theirs", colour="black"), move_by=1),
    ]
    engine.observe_games(games)
    assert [c.game.id for c in engine.choices()] == ["mine-soon", "later", "theirs"]


def test_a_pinned_game_not_yet_in_the_list_is_explained(engine, clock):
    """The pin holds even before the first successful poll returns it."""
    engine.select_game("4242")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.game is None
    assert engine.snapshot.selected_game_id == "4242"
    assert "waiting for game 4242" in engine.snapshot.reason


# --- pinning until the game is done ---------------------------------------


def test_the_pinned_game_survives_a_restart(clock, tmp_path):
    """A power cut must not silently un-pick the player's game."""
    from bridge.state.store import StateStore

    path = tmp_path / "state.json"
    first = SyncEngine(clock=clock, settle_seconds=2.0, store=StateStore(path))
    first.observe_games([game()])
    first.select_game("1022853950")

    restarted = SyncEngine(clock=clock, settle_seconds=2.0, store=StateStore(path))
    assert restarted.snapshot.selected_game_id == "1022853950"
    restarted.observe_games([game()])
    restarted.set_board_connected(True)
    hold(restarted, clock, occupancy_from_fen(FEN))
    assert restarted.snapshot.reconciliation.state is SyncState.IN_SYNC


def test_a_finished_game_unpins_and_does_not_choose_another(engine, clock):
    engine.observe_games([game("1022853950"), game("9999", OTHER_FEN)])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))

    for _ in range(2):
        engine.observe_games([game("9999", OTHER_FEN)])

    assert engine.snapshot.last_discontinuity is Discontinuity.GAME_FINISHED
    assert engine.snapshot.selected_game_id is None
    assert engine.snapshot.game is None, "must not silently advance to the next game"
    assert engine.snapshot.reason == "no game selected -- pick one"


def test_one_poll_missing_the_game_does_not_unpin_it(engine, clock):
    """A single poll can omit a game for reasons other than it being over.

    ``parse_games`` drops entries missing required fields, so one bad payload
    would otherwise unpin a live game -- and the player would have no reason to
    suspect they were no longer connected to it.
    """
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.observe_games([])
    assert engine.snapshot.selected_game_id == "1022853950"

    engine.observe_games([game()])  # it comes back
    engine.observe_games([])
    assert engine.snapshot.selected_game_id == "1022853950", "counter must reset"


def test_unpinning_is_persisted_too(clock, tmp_path):
    from bridge.state.store import StateStore

    path = tmp_path / "state.json"
    engine = SyncEngine(clock=clock, settle_seconds=2.0, store=StateStore(path))
    engine.select_game("1022853950")
    engine.select_game(None)
    assert SyncEngine(
        clock=clock, settle_seconds=2.0, store=StateStore(path)
    ).snapshot.selected_game_id is None


# --- setting the board up for the pinned game -----------------------------


def test_pinning_a_game_the_board_does_not_show_asks_for_setup(engine, clock):
    """The switching case: pin game B while the board still shows game A."""
    engine.observe_games([game("A", FEN), game("B", OTHER_FEN)])
    engine.select_game("B")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))

    snapshot = engine.snapshot
    assert snapshot.awaiting_setup
    assert snapshot.reason == "set up the position for game B on the board"
    assert snapshot.setup_guidance, "the LEDs need squares to light"
    assert engine.take_submission() is None


def test_setup_completes_when_the_position_is_built(engine, clock):
    engine.observe_games([game("A", FEN), game("B", OTHER_FEN)])
    engine.select_game("B")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.awaiting_setup

    hold(engine, clock, occupancy_from_fen(OTHER_FEN))
    assert not engine.snapshot.awaiting_setup
    assert engine.snapshot.reconciliation.state is SyncState.IN_SYNC
    assert engine.snapshot.setup_guidance == ()


def test_a_mismatch_after_play_started_is_not_called_setup(engine, clock):
    """Knocking the pieces over mid-game must not read as 'set up your game'."""
    engine.observe_games([game()])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    assert engine.snapshot.reconciliation.state is SyncState.IN_SYNC

    hold(engine, clock, occupancy_from_fen("4k3/8/8/8/8/8/8/4K3"))
    assert engine.snapshot.reconciliation.state is SyncState.MISMATCH
    assert not engine.snapshot.awaiting_setup


def test_setup_is_required_again_after_switching_games(engine, clock):
    """The latch must be per-selection, not per-session."""
    engine.observe_games([game("A", FEN), game("B", OTHER_FEN)])
    engine.select_game("A")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(FEN))
    assert not engine.snapshot.awaiting_setup

    engine.select_game("B")
    hold(engine, clock, occupancy_from_fen(FEN), seconds=0.1)
    engine.on_frame(occupancy_from_fen(FEN))
    clock.advance(3)
    engine.on_frame(occupancy_from_fen(FEN))
    assert engine.snapshot.awaiting_setup


# --- the opponent's move ---------------------------------------------------


def test_a_board_that_has_not_caught_up_is_recognised(engine, clock):
    """The normal state of a 3-day game, and it needs the retained previous FEN."""
    before = game(fen="r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R w - - 3 22")
    engine.observe_games([before])
    engine.select_game("1022853950")
    engine.set_board_connected(True)
    hold(engine, clock, occupancy_from_fen(before.fen))

    # The opponent moves; we learn about it from the poll, board untouched.
    board = chess.Board(before.fen)
    board.push_uci("e4f6")
    engine.observe_games([game(fen=board.fen())])
    hold(engine, clock, occupancy_from_fen(before.fen))

    assert engine.snapshot.reconciliation.state is SyncState.OPPONENT_MOVE_PENDING
    assert engine.snapshot.reconciliation.move == "e4f6", "the move to replay"
    assert engine.take_submission() is None


# --- flicker is a live level ----------------------------------------------


def test_flicker_is_read_live_so_it_cannot_go_stale(engine, clock):
    """Kept off the Snapshot deliberately: a stored status would lag reality."""
    present = occupancy_from_fen(FEN)
    missing = {sq: p for sq, p in present.items() if sq != "e8"}
    engine.set_board_connected(True)
    for index in range(20):
        engine.on_frame(present if index % 2 == 0 else missing)
        clock.advance(0.25)

    assert engine.settle_status.flickering == ("e8",)
    assert engine.snapshot.reconciliation is None
