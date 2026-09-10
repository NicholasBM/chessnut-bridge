"""Tests for the reconciliation state machine.

The property that matters most is negative: no input that is not exactly one
legal move may ever produce a submittable result. Several tests below assert
``not is_submittable`` on deliberately awkward boards -- a knocked-over piece, a
piece placed on the wrong square, a pawn left sitting on the eighth rank -- since
those are the inputs that would otherwise put a wrong move into a real game.
"""

import sys
from pathlib import Path

import chess
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chessnut import protocol  # noqa: E402
from bridge.sync.reconcile import (  # noqa: E402
    Reconciliation,
    SyncState,
    occupancy_from_fen,
    reconcile,
)

START = chess.STARTING_FEN


def board_sees(fen: str) -> dict[str, str]:
    """Occupancy as the Chessnut driver would report it for a position.

    Deliberately routed through the real protocol encoder and decoder rather
    than built directly, so these tests also cover the driver -> reconciler
    handoff and would catch a mismatch in square naming between the two.
    """
    frame = protocol.build_board_frame(fen.split(" ")[0])
    return protocol.parse_board_frame(frame).occupancy


# --- the agreeing case ----------------------------------------------------


def test_untouched_starting_board_is_in_sync():
    result = reconcile(START, board_sees(START))
    assert result.state is SyncState.IN_SYNC
    assert result.move is None
    assert not result.is_submittable


def test_driver_and_reconciler_agree_on_occupancy():
    """Guards against a square-naming drift between the two modules."""
    assert board_sees(START) == occupancy_from_fen(START)


# --- a move being made ----------------------------------------------------


def test_completed_move_is_ready_to_submit():
    board = chess.Board(START)
    board.push_uci("e2e4")
    result = reconcile(START, board_sees(board.fen()))
    assert result.state is SyncState.MOVE_READY
    assert result.move == "e2e4"
    assert result.is_submittable
    assert result.squares_to_highlight == ("e2", "e4")


def test_lifted_piece_is_in_progress_not_a_mismatch():
    """A hand hovering mid-move must not look like an error."""
    observed = board_sees(START)
    del observed["e2"]
    result = reconcile(START, observed)
    assert result.state is SyncState.MOVE_IN_PROGRESS
    assert result.lifted == ("e2",)
    assert not result.is_submittable


def test_capture_in_progress_with_two_pieces_lifted():
    """Lifting the captured piece before the capturer is normal handling."""
    fen = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    observed = board_sees(fen)
    del observed["d5"]  # captured pawn removed first
    del observed["e4"]  # then the capturer picked up
    result = reconcile(fen, observed)
    assert result.state is SyncState.MOVE_IN_PROGRESS
    assert result.lifted == ("d5", "e4")
    assert not result.is_submittable


def test_completed_capture_is_ready():
    fen = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    board = chess.Board(fen)
    board.push_uci("e4d5")
    result = reconcile(fen, board_sees(board.fen()))
    assert result.state is SyncState.MOVE_READY
    assert result.move == "e4d5"


# --- the moves that break naive square diffing ----------------------------


def test_castling_moves_two_pieces_and_is_still_one_move():
    fen = "rnbqk2r/pppp1ppp/5n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 6 5"
    board = chess.Board(fen)
    board.push_uci("e1g1")
    result = reconcile(fen, board_sees(board.fen()))
    assert result.state is SyncState.MOVE_READY
    assert result.move == "e1g1"
    assert result.is_submittable


def test_en_passant_removes_a_pawn_the_capturer_never_touched():
    fen = "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 4"
    board = chess.Board(fen)
    board.push_uci("e5f6")
    observed = board_sees(board.fen())
    assert "f5" not in observed  # the captured pawn is gone from an untouched square
    result = reconcile(fen, observed)
    assert result.state is SyncState.MOVE_READY
    assert result.move == "e5f6"


def test_promotion_uses_the_piece_actually_placed():
    fen = "8/4P3/8/8/8/8/8/K6k w - - 0 1"
    for promotion in ("q", "r", "b", "n"):
        board = chess.Board(fen)
        board.push_uci(f"e7e8{promotion}")
        result = reconcile(fen, board_sees(board.fen()))
        assert result.state is SyncState.MOVE_READY, promotion
        assert result.move == f"e7e8{promotion}", promotion


def test_pawn_left_on_the_last_rank_is_not_submittable():
    """No legal move leaves a pawn on the eighth rank; refuse to assume a queen."""
    observed = occupancy_from_fen("4P3/8/8/8/8/8/8/K6k")
    result = reconcile("8/4P3/8/8/8/8/8/K6k w - - 0 1", observed)
    assert result.state is SyncState.MISMATCH
    assert not result.is_submittable


# --- the normal state of a 3-day game -------------------------------------


def test_board_not_yet_caught_up_with_the_opponent():
    """Walking up to the board hours after the opponent moved."""
    previous = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    board = chess.Board(previous)
    board.push_uci("e7e5")
    current = board.fen()

    result = reconcile(current, board_sees(previous), previous_fen=previous)
    assert result.state is SyncState.OPPONENT_MOVE_PENDING
    assert result.move == "e7e5"
    assert result.squares_to_highlight == ("e7", "e5")
    assert not result.is_submittable


def test_replaying_the_opponents_move_returns_to_sync():
    previous = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    board = chess.Board(previous)
    board.push_uci("e7e5")
    current = board.fen()

    result = reconcile(current, board_sees(current), previous_fen=previous)
    assert result.state is SyncState.IN_SYNC


def test_stale_board_without_previous_fen_is_a_mismatch_not_a_guess():
    """Absent history we cannot explain a stale board, so we must not try."""
    previous = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
    board = chess.Board(previous)
    board.push_uci("e7e5")
    result = reconcile(board.fen(), board_sees(previous))
    assert result.state is SyncState.MISMATCH
    assert not result.is_submittable


def test_opponent_move_pending_takes_priority_over_a_coincidental_legal_move():
    """The stale-board reading is checked first and must win."""
    previous = "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1"
    board = chess.Board(previous)
    board.push_uci("g8f6")
    result = reconcile(board.fen(), board_sees(previous), previous_fen=previous)
    assert result.state is SyncState.OPPONENT_MOVE_PENDING


# --- things that must never be submitted ----------------------------------


def test_piece_on_a_square_no_legal_move_reaches():
    """A rook teleported across the board: explainable by nothing."""
    observed = board_sees(START)
    del observed["a1"]
    observed["a5"] = "R"
    result = reconcile(START, observed)
    assert result.state is SyncState.MISMATCH
    assert result.lifted == ("a1",)
    assert result.added == ("a5",)
    assert not result.is_submittable


def test_knocked_over_piece_replaced_by_the_wrong_one():
    """Same square occupied, different piece -- reported as changed, not a move."""
    observed = board_sees(START)
    observed["e2"] = "N"
    result = reconcile(START, observed)
    assert result.state is SyncState.MISMATCH
    assert result.changed == ("e2",)
    assert not result.is_submittable


def test_an_extra_piece_appearing_is_a_mismatch():
    observed = board_sees(START)
    observed["e4"] = "Q"
    result = reconcile(START, observed)
    assert result.state is SyncState.MISMATCH
    assert result.added == ("e4",)
    assert not result.is_submittable


def test_opponents_piece_moved_on_our_turn_is_not_our_move():
    """It is white to move; a black piece moving is not a legal white move."""
    board = chess.Board(START)
    observed = board_sees(START)
    del observed["e7"]
    observed["e5"] = "p"
    result = reconcile(START, observed)
    assert result.state is SyncState.MISMATCH
    assert not result.is_submittable
    assert board.turn is chess.WHITE


def test_empty_board_is_a_mismatch_not_a_move_in_progress():
    """Only removals, but 32 of them: the board is being rearranged, not played."""
    result = reconcile(START, {})
    assert result.state is SyncState.MISMATCH
    assert len(result.lifted) == 32
    assert not result.is_submittable


def test_lifting_too_many_pieces_stops_being_a_move_in_progress():
    observed = board_sees(START)
    for square in ("e2", "d2", "c2"):
        del observed[square]
    assert reconcile(START, observed).state is SyncState.MOVE_IN_PROGRESS

    del observed["b2"]
    assert reconcile(START, observed).state is SyncState.MISMATCH


def test_side_to_move_changes_which_moves_are_legal():
    """The same occupancy must not be submittable for the wrong side."""
    white_to_move = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    black_to_move = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
    board = chess.Board(white_to_move)
    board.push_uci("e2e4")
    observed = board_sees(board.fen())

    assert reconcile(white_to_move, observed).state is SyncState.MOVE_READY
    assert reconcile(black_to_move, observed).state is SyncState.MISMATCH


def test_only_move_ready_is_ever_submittable():
    """Belt and braces: enumerate the states and check the gate."""
    for state in SyncState:
        result = Reconciliation(state=state, move="e2e4")
        assert result.is_submittable == (state is SyncState.MOVE_READY)


# --- a real position from one of our live games ----------------------------


def test_live_daily_game_position():
    """Game 1023312426 as of 2026-09-07, black to move."""
    fen = "rn2k1nr/1b1p2pp/2pb4/1p2pP2/1R6/B1NBPN2/P1P2PPP/5RK1 b kq - 0 14"
    assert reconcile(fen, board_sees(fen)).state is SyncState.IN_SYNC

    board = chess.Board(fen)
    legal = next(iter(board.legal_moves))
    board.push(legal)
    result = reconcile(fen, board_sees(board.fen()))
    assert result.state is SyncState.MOVE_READY
    assert result.move == legal.uci()


@pytest.mark.parametrize(
    "fen",
    [
        "r2qk2r/2pb1p2/p1np1npb/4p3/B3P3/2NP1N2/PPP3PP/R1BQK2R w KQkq - 1 11",
        "rn2k1nr/1b1p2pp/2pb4/1p2pP2/1R6/B1NBPN2/P1P2PPP/5RK1 b kq - 0 14",
        "r3k1nr/1p1b1ppp/n1pp2q1/8/Q1P1Pp2/3P1N2/PP4PP/RN2R1K1 b kq - 1 13",
        "r2nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R w - - 3 22",
    ],
)
def test_every_legal_move_in_our_live_games_round_trips(fen):
    """For four real positions, every legal move must be uniquely recognised."""
    board = chess.Board(fen)
    for move in board.legal_moves:
        board.push(move)
        observed = board_sees(board.fen())
        board.pop()
        result = reconcile(fen, observed)
        assert result.state is SyncState.MOVE_READY, (fen, move.uci(), result.state)
        assert result.move == move.uci()
