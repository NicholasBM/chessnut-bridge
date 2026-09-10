"""Tests for the settle gate, driven by faults a real board actually produced.

Two hazards are pinned here, both of which end in submitting a move the player
never made:

1. A flickering sensor. Square e8 on the real board reported occupied and empty
   on alternating frames with nobody touching it.
2. A piece set down briefly on an intermediate square on its way somewhere else.
   That transient is a *complete, legal, uniquely identified* move, so
   reconciliation is right to call it MOVE_READY -- which is exactly why
   reconciliation must not be handed unsettled positions.

The second needs no faulty hardware at all, which is what makes the settle gate a
correctness requirement rather than noise reduction.
"""

import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chessnut import protocol  # noqa: E402
from bridge.sync.reconcile import SyncState, reconcile  # noqa: E402
from bridge.sync.settle import PositionSettler, SettleStatus  # noqa: E402


class FakeClock:
    """A clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settler(clock: FakeClock) -> PositionSettler:
    return PositionSettler(settle_seconds=2.0, clock=clock)


def occupancy(placement: str) -> dict[str, str]:
    return protocol.placement_to_occupancy(placement)


START = occupancy(protocol.STARTING_PLACEMENT)


# --- the basic gate -------------------------------------------------------


def test_a_position_is_not_emitted_until_it_has_been_held(settler, clock):
    assert settler.update(START) is None, "first sighting is never settled"
    clock.advance(1.0)
    assert settler.update(START) is None, "not held long enough yet"
    clock.advance(1.5)
    assert settler.update(START) == START


def settle(settler, clock, occ, seconds: float = 3.0):
    """Hold ``occ`` long enough to settle, returning what was emitted.

    Two calls are needed by design: the first sighting starts the timer, and only
    a later frame confirms the board is still showing the same thing.
    """
    settler.update(occ)
    clock.advance(seconds)
    return settler.update(occ)


def test_a_settled_position_is_emitted_once_not_repeatedly(settler, clock):
    assert settle(settler, clock, START) is not None
    for _ in range(5):
        assert settler.update(START) is None, "callers want an edge, not a level"


def test_the_settle_timer_restarts_on_every_change(settler, clock):
    """A hand hovering over the board must not accumulate credit."""
    moved = dict(START)
    del moved["e2"]

    for _ in range(10):
        settler.update(START)
        clock.advance(1.9)
        settler.update(moved)
        clock.advance(1.9)
    assert settler.last_settled is None


# --- the real e8 flicker --------------------------------------------------


def test_the_observed_e8_flicker_never_settles(settler, clock):
    """Replays the fault seen on real hardware: e8 alternating every frame.

    The board reports several times a second, so the flicker is fed at that rate
    against a 2s settle window.
    """
    present = occupancy("r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R")
    missing = occupancy("r2n2k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R")

    emitted = []
    for i in range(60):
        result = settler.update(present if i % 2 == 0 else missing)
        if result is not None:
            emitted.append(result)
        clock.advance(0.25)

    assert emitted == [], "a flickering square must never produce a position"


def test_a_flickering_square_is_reported_as_suspect_hardware(settler, clock):
    """Filtering the noise silently would have hidden a real board fault."""
    present = occupancy("r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R")
    missing = occupancy("r2n2k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R")

    for i in range(20):
        settler.update(present if i % 2 == 0 else missing)
        clock.advance(0.25)

    status = settler.status
    assert status.flickering == ("e8",)
    assert status.has_unreliable_squares
    assert status.changes_while_unsettled > 3
    assert not status.settled


def test_a_steady_board_reports_no_flicker(settler, clock):
    settle(settler, clock, START)
    assert settler.status.flickering == ()
    assert settler.status.settled
    assert not settler.status.has_unreliable_squares


def test_the_flicker_tally_does_not_outlive_the_episode(settler, clock):
    """One bad episode must not mark a square suspect for the whole session."""
    lifted = dict(START)
    del lifted["e2"]
    for _ in range(8):
        settler.update(START)
        clock.advance(0.2)
        settler.update(lifted)
        clock.advance(0.2)
    assert settler.status.flickering

    clock.advance(3)
    assert settler.update(lifted) is not None
    assert settler.status.flickering == ()


# --- the hazard that needs no faulty hardware -----------------------------


def test_a_piece_resting_en_route_would_otherwise_be_submitted(clock):
    """Rd8-d5 with the rook set down briefly on d7 on the way.

    The transient is a real, legal, uniquely-identified move, so reconciliation
    correctly calls it MOVE_READY. Without the settle gate the bridge would
    submit Rd8d7 while the player's hand was still moving.
    """
    fen = "3r2k1/ppp2ppp/8/8/8/8/PPP2PPP/3R2K1 b - - 0 1"
    board = chess.Board(fen)

    resting = chess.Board(fen)
    resting.push_uci("d8d7")
    intended = chess.Board(fen)
    intended.push_uci("d8d5")

    en_route = occupancy(resting.board_fen())
    final = occupancy(intended.board_fen())

    # Reconciliation on raw frames would submit the wrong move.
    unguarded = reconcile(fen, en_route)
    assert unguarded.state is SyncState.MOVE_READY
    assert unguarded.move == "d8d7"
    assert unguarded.is_submittable, "this is the wrong move, and it looks perfect"

    # With the gate, the resting position never reaches the reconciler.
    settler = PositionSettler(settle_seconds=2.0, clock=clock)
    assert settler.update(occupancy(board.board_fen())) is None
    clock.advance(3)
    settler.update(occupancy(board.board_fen()))

    assert settler.update(en_route) is None
    clock.advance(0.6)  # hand keeps moving
    assert settler.update(en_route) is None

    assert settler.update(final) is None
    clock.advance(3)
    settled = settler.update(final)

    assert settled is not None
    result = reconcile(fen, settled)
    assert result.move == "d8d5", "only the move actually completed"
    assert result.is_submittable


def test_a_slow_deliberate_move_still_settles(clock):
    """The gate must not block a player who simply moves slowly."""
    settler = PositionSettler(settle_seconds=2.0, clock=clock)
    lifted = dict(START)
    del lifted["e2"]
    moved = dict(lifted)
    moved["e4"] = "P"

    clock.advance(3)
    settler.update(START)
    settler.update(lifted)
    clock.advance(10)  # a long think, piece in hand
    assert settler.update(lifted) is not None, "a held lift is a real state"

    settler.update(moved)
    clock.advance(3)
    assert settler.update(moved) == moved


# --- lifecycle ------------------------------------------------------------


def test_reset_forgets_everything(settler, clock):
    settle(settler, clock, START)
    assert settler.last_settled is not None

    settler.reset()
    assert settler.last_settled is None
    assert settler.status.flickering == ()
    assert settler.update(START) is None, "must re-earn trust after a reconnect"


def test_status_is_reportable_before_any_frame(settler):
    assert settler.status == SettleStatus(
        settled=False, flickering=(), changes_while_unsettled=0, pending_for=0.0
    )


def test_the_emitted_occupancy_is_a_copy(settler, clock):
    """Callers must not be able to mutate the settler's idea of the board."""
    settled = settle(settler, clock, START)
    settled["e2"] = "Q"
    assert settler.last_settled["e2"] == "P"
