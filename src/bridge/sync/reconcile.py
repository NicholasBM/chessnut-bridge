"""Deciding whether the physical board and the online game agree.

This is where the project's central safety rule lives: never submit a move
because the board *probably* means it. A sensor board reports occupancy only, so
the question "what move did the player make?" has to be answered by asking which
*legal* move from the authoritative online position would produce exactly the
occupancy we can see. If the answer is not exactly one move, nothing is
submitted and the caller is handed a state to display instead.

The online game is always the authority. The board is an input device.

Legality comes from python-chess rather than hand-rolled square diffing, because
castling moves two pieces, en passant removes a piece from a square the capturer
never touches, and promotion changes a piece's identity. Enumerating legal moves
and comparing the resulting occupancy gets all three right for free; diffing
squares gets all three wrong in ways that look plausible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

import chess


# How many pieces may be off the board at once before we stop calling it a move
# in progress. Every normal case needs at most two: a capture lifts the captured
# piece and the capturer, castling lifts the king and the rook. Three allows for
# an accidental nudge. Beyond that the board is being rearranged, not played, and
# reporting "move in progress" would be misleading in the UI. Both readings are
# equally non-submittable, so this threshold affects wording, never safety.
_MAX_LIFTED_MID_MOVE = 3


class SyncState(Enum):
    """What the board is telling us, relative to the online game."""

    #: Board matches the online position. Nothing to do.
    IN_SYNC = "in_sync"

    #: The board still shows the position from before the opponent's last move.
    #: For 3-day games this is the *normal* state on walking up to the board:
    #: the opponent moved hours ago and the pieces have not been touched since.
    #: The player has to replay the opponent's move physically; ``move`` says
    #: which one, so the UI and the LEDs can show it.
    OPPONENT_MOVE_PENDING = "opponent_move_pending"

    #: Pieces have been lifted but not yet put down. Transient and expected --
    #: a hand is mid-move. Not an error and not actionable.
    MOVE_IN_PROGRESS = "move_in_progress"

    #: Exactly one legal move explains the board. ``move`` holds it in UCI.
    #: This is the only state from which a move may be submitted.
    MOVE_READY = "move_ready"

    #: More than one legal move would produce this occupancy. Should be
    #: unreachable with full piece identity, but if it ever happens we refuse to
    #: guess rather than pick the first candidate.
    AMBIGUOUS = "ambiguous"

    #: The board cannot be explained by any legal move. Needs a human.
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class Reconciliation:
    """The result of comparing the board against the online game."""

    state: SyncState
    #: UCI move. Set for MOVE_READY (the player's move to submit) and for
    #: OPPONENT_MOVE_PENDING (the move to replay on the board). None otherwise.
    move: str | None = None
    #: All legal moves matching the observed occupancy, when AMBIGUOUS.
    candidates: tuple[str, ...] = ()
    #: Squares the online game expects to be occupied but the board reports empty.
    lifted: tuple[str, ...] = ()
    #: Squares the board reports occupied that the online game expects empty.
    added: tuple[str, ...] = ()
    #: Squares occupied in both but by a different piece.
    changed: tuple[str, ...] = ()

    @property
    def is_submittable(self) -> bool:
        """Only ever true in one state. Callers should gate submission on this."""
        return self.state is SyncState.MOVE_READY and self.move is not None

    @property
    def squares_to_highlight(self) -> tuple[str, ...]:
        """Squares worth lighting up, for the LED and UI layers."""
        if self.move:
            return (self.move[0:2], self.move[2:4])
        return tuple(sorted(set(self.lifted) | set(self.added) | set(self.changed)))


def occupancy_from_board(board: chess.Board) -> dict[str, str]:
    """Render a python-chess board as ``{'e1': 'K', ...}``, matching the driver."""
    return {
        chess.square_name(square): piece.symbol()
        for square, piece in board.piece_map().items()
    }


def occupancy_from_fen(fen: str) -> dict[str, str]:
    """Occupancy of a FEN, accepting a bare placement field too."""
    return occupancy_from_board(chess.Board(_normalise_fen(fen)))


def _normalise_fen(fen: str) -> str:
    """Allow a bare placement field by supplying neutral defaults.

    Only used for positions we compare occupancy against, never for the position
    we generate legal moves from -- side to move and castling rights matter
    there, and inventing them would invent moves.
    """
    return fen if " " in fen.strip() else f"{fen.strip()} w - - 0 1"


def _diff(
    expected: Mapping[str, str], observed: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    lifted = tuple(sorted(sq for sq in expected if sq not in observed))
    added = tuple(sorted(sq for sq in observed if sq not in expected))
    changed = tuple(
        sorted(sq for sq, piece in observed.items() if expected.get(sq, piece) != piece)
    )
    return lifted, added, changed


def _move_between(previous: chess.Board, current_occupancy: Mapping[str, str]) -> str | None:
    """Which legal move from ``previous`` produces ``current_occupancy``?"""
    matches = [
        move.uci()
        for move in previous.legal_moves
        if _occupancy_after(previous, move) == current_occupancy
    ]
    return matches[0] if len(matches) == 1 else None


def _occupancy_after(board: chess.Board, move: chess.Move) -> dict[str, str]:
    board.push(move)
    try:
        return occupancy_from_board(board)
    finally:
        board.pop()


def reconcile(
    fen: str,
    observed: Mapping[str, str],
    previous_fen: str | None = None,
) -> Reconciliation:
    """Compare the board against the authoritative online position.

    ``fen`` must be a full FEN from the online game -- side to move and castling
    rights are what make the legal-move set correct. ``observed`` is occupancy
    from the board driver. ``previous_fen`` is the position from the poll before
    this one, which is what lets us recognise a board that simply has not caught
    up with the opponent's move yet.
    """
    board = chess.Board(fen)
    expected = occupancy_from_board(board)
    observed = dict(observed)
    lifted, added, changed = _diff(expected, observed)

    if observed == expected:
        return Reconciliation(SyncState.IN_SYNC)

    # Has the board simply not been updated with the opponent's move? Checked
    # before legal moves because it is both the commonest case in a 3-day game
    # and the one with a clear instruction attached.
    if previous_fen is not None:
        previous = chess.Board(_normalise_fen(previous_fen))
        if occupancy_from_board(previous) == observed:
            return Reconciliation(
                state=SyncState.OPPONENT_MOVE_PENDING,
                move=_move_between(previous, expected),
                lifted=lifted,
                added=added,
                changed=changed,
            )

    matches = [
        move.uci()
        for move in board.legal_moves
        if _occupancy_after(board, move) == observed
    ]
    if len(matches) == 1:
        return Reconciliation(state=SyncState.MOVE_READY, move=matches[0])
    if len(matches) > 1:
        return Reconciliation(
            state=SyncState.AMBIGUOUS,
            candidates=tuple(sorted(matches)),
            lifted=lifted,
            added=added,
            changed=changed,
        )

    # Nothing added or altered, only pieces missing: a hand is mid-move.
    if not added and not changed and 0 < len(lifted) <= _MAX_LIFTED_MID_MOVE:
        return Reconciliation(
            state=SyncState.MOVE_IN_PROGRESS, lifted=lifted
        )

    return Reconciliation(
        state=SyncState.MISMATCH, lifted=lifted, added=added, changed=changed
    )
