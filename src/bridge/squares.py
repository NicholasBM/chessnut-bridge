"""Square numbering shared by every module.

One convention, defined once: index ``0`` is a1 and index ``63`` is h8, i.e.
``index = file + 8 * rank``. This matches python-chess and it matches chess.com's
TCN alphabet, which is why it was chosen.

The Chessnut wire format uses a *different* order (h8 first, a1 last). That
translation lives in :mod:`bridge.chessnut.protocol` and nowhere else -- a
mirrored or flipped board is the easiest bug to introduce here and the hardest
to notice, so the conversion has exactly one home.
"""

from __future__ import annotations

FILES = "abcdefgh"
RANKS = "12345678"


class SquareError(ValueError):
    """Raised for a malformed square name or an out-of-range index."""


def square_name(index: int) -> str:
    """0 -> 'a1', 63 -> 'h8'."""
    if not 0 <= index < 64:
        raise SquareError(f"square index out of range: {index}")
    return FILES[index % 8] + RANKS[index // 8]


def square_index(name: str) -> int:
    """'a1' -> 0, 'h8' -> 63."""
    if len(name) != 2 or name[0] not in FILES or name[1] not in RANKS:
        raise SquareError(f"not a square: {name!r}")
    return FILES.index(name[0]) + 8 * RANKS.index(name[1])
