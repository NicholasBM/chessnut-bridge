"""chess.com TCN move encoding.

chess.com's web client does not send SAN or UCI. Each move is two characters
from a fixed alphabet: the first is the origin square, the second is the
destination square -- except that a destination index past the 64 squares
encodes a promotion instead.

Square index is ``file + 8 * rank``, so index 0 is a1 and index 63 is h8.

Verified against a live capture (2026-09-07): a daily challenge opening move
was sent as ``{"gameSeekId": ..., "move": "mC"}``, and 'm' -> 12 -> e2,
'C' -> 28 -> e4, i.e. e2e4.
"""

from __future__ import annotations

# Indices 0-63 are squares. 64-78 are promotions (three files x five pieces).
# 79 and up are crazyhouse-style drops, which we neither need nor support --
# note the alphabet contains '+' twice, so those tail indices are ambiguous by
# design and must not be round-tripped.
ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "!?{~}(^)[_]@#$,./&-*++="
)

# Order matters: (dst - 64) // 3 indexes into this. 'k' is only reachable in
# variants such as Giveaway, but it costs nothing to encode correctly.
PROMOTION_PIECES = "qnrbk"

# Highest index we are willing to emit or interpret: the last king promotion.
_MAX_INDEX = 64 + len(PROMOTION_PIECES) * 3 - 1  # 78

_FILES = "abcdefgh"


class TcnError(ValueError):
    """Raised when a TCN string or UCI move cannot be represented."""


def square_to_index(square: str) -> int:
    """'e2' -> 12."""
    if len(square) != 2 or square[0] not in _FILES or square[1] not in "12345678":
        raise TcnError(f"not a square: {square!r}")
    return _FILES.index(square[0]) + 8 * (int(square[1]) - 1)


def index_to_square(index: int) -> str:
    """12 -> 'e2'."""
    if not 0 <= index < 64:
        raise TcnError(f"square index out of range: {index}")
    return _FILES[index % 8] + str(index // 8 + 1)


def decode(tcn: str) -> list[str]:
    """Decode a TCN string into a list of UCI moves.

    Promotions come back with the piece suffix, e.g. ``d2c1q``.
    """
    if len(tcn) % 2:
        raise TcnError(f"TCN length must be even, got {len(tcn)}")

    moves: list[str] = []
    for i in range(0, len(tcn), 2):
        try:
            src = ALPHABET.index(tcn[i])
            dst = ALPHABET.index(tcn[i + 1])
        except ValueError as exc:
            raise TcnError(f"character not in TCN alphabet: {tcn[i:i + 2]!r}") from exc

        promotion = ""
        if dst > _MAX_INDEX:
            raise TcnError(
                f"destination index {dst} is a drop, not a move: {tcn[i:i + 2]!r}"
            )
        if dst > 63:
            promotion = PROMOTION_PIECES[(dst - 64) // 3]
            # A pawn on rank 2 promotes backwards (it is black); otherwise forwards.
            rank_step = -8 if src < 16 else 8
            file_step = ((dst - 64) % 3) - 1
            dst = src + rank_step + file_step

        moves.append(index_to_square(src) + index_to_square(dst) + promotion)

    return moves


def encode_move(uci: str) -> str:
    """Encode a single UCI move as two TCN characters.

    ``e2e4`` -> ``mC``.  ``d2c1q`` -> the promotion form.
    """
    if len(uci) not in (4, 5):
        raise TcnError(f"not a UCI move: {uci!r}")

    src = square_to_index(uci[0:2])
    dst = square_to_index(uci[2:4])

    if len(uci) == 5:
        piece = uci[4].lower()
        if piece not in PROMOTION_PIECES:
            raise TcnError(f"cannot promote to {uci[4]!r}")
        rank_step = -8 if src < 16 else 8
        file_step = dst - src - rank_step
        if file_step not in (-1, 0, 1):
            raise TcnError(f"promotion is not a single-file move: {uci!r}")
        dst = 64 + PROMOTION_PIECES.index(piece) * 3 + (file_step + 1)

    return ALPHABET[src] + ALPHABET[dst]


def encode(ucis: list[str]) -> str:
    """Encode a list of UCI moves into one TCN string."""
    return "".join(encode_move(m) for m in ucis)
