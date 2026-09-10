"""Tests for the TCN codec.

The vectors here come from two independent sources: a live capture of our own
account making a move, and a published worked example. Getting this wrong means
sending a legal-looking but incorrect move into a real rated game, so the
round-trip property is tested exhaustively over every square pair.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom import tcn  # noqa: E402


def test_alphabet_prefix_is_unique():
    """The 64 square characters must be unambiguous."""
    squares = tcn.ALPHABET[:64]
    assert len(set(squares)) == 64


def test_captured_move_from_live_session():
    """Our own capture, 2026-09-07: {"gameSeekId": 1523433852, "move": "mC"}."""
    assert tcn.decode("mC") == ["e2e4"]
    assert tcn.encode_move("e2e4") == "mC"


@pytest.mark.parametrize(
    "square,index",
    [("a1", 0), ("e2", 12), ("e4", 28), ("h1", 7), ("a8", 56), ("h8", 63)],
)
def test_square_index_mapping(square, index):
    assert tcn.square_to_index(square) == index
    assert tcn.index_to_square(index) == square


def test_published_worked_example():
    """Twelve moves including a promotion, from a public TCN decoder's docs."""
    encoded = "fnCunm6cdculgpBDowDnmnl{"
    expected = [
        "f1f2", "e4e3", "f2e2", "c8c1", "d1c1", "e3d2",
        "g1h2", "d4f4", "g2g3", "f4f2", "e2f2", "d2c1q",
    ]
    assert tcn.decode(encoded) == expected


def test_worked_example_round_trips():
    encoded = "fnCunm6cdculgpBDowDnmnl{"
    assert tcn.encode(tcn.decode(encoded)) == encoded


def test_round_trip_every_plain_move():
    """Every ordered pair of distinct squares must survive encode -> decode."""
    for src in range(64):
        for dst in range(64):
            if src == dst:
                continue
            uci = tcn.index_to_square(src) + tcn.index_to_square(dst)
            assert tcn.decode(tcn.encode_move(uci)) == [uci], uci


@pytest.mark.parametrize("piece", list(tcn.PROMOTION_PIECES))
def test_round_trip_white_promotions(piece):
    """White pawn on rank 7 promoting straight, and by capture either way."""
    for from_file, to_file in [("d", "c"), ("d", "d"), ("d", "e")]:
        uci = f"{from_file}7{to_file}8{piece}"
        assert tcn.decode(tcn.encode_move(uci)) == [uci], uci


@pytest.mark.parametrize("piece", list(tcn.PROMOTION_PIECES))
def test_round_trip_black_promotions(piece):
    """Black pawn on rank 2 promoting: encoded with a backwards rank step."""
    for from_file, to_file in [("d", "c"), ("d", "d"), ("d", "e")]:
        uci = f"{from_file}2{to_file}1{piece}"
        assert tcn.decode(tcn.encode_move(uci)) == [uci], uci


def test_castling_is_a_plain_king_move():
    """chess.com encodes castling as the king's two-square move, not O-O."""
    assert tcn.encode_move("e1g1") == tcn.ALPHABET[4] + tcn.ALPHABET[6]
    assert tcn.decode(tcn.encode_move("e8c8")) == ["e8c8"]


def test_rejects_odd_length():
    with pytest.raises(tcn.TcnError):
        tcn.decode("mCk")


def test_rejects_unknown_character():
    with pytest.raises(tcn.TcnError):
        tcn.decode("m%")


def test_rejects_bad_promotion_piece():
    with pytest.raises(tcn.TcnError):
        tcn.encode_move("d7d8p")


def test_rejects_crazyhouse_drops():
    """Indices past the last king promotion are drops; we must not guess."""
    drop = tcn.ALPHABET[0] + tcn.ALPHABET[79]
    with pytest.raises(tcn.TcnError, match="drop"):
        tcn.decode(drop)


def test_alphabet_tail_is_ambiguous_as_documented():
    """'+' appears twice, which is why we refuse to interpret the drop range."""
    assert tcn.ALPHABET.count("+") == 2
    assert len(set(tcn.ALPHABET)) == len(tcn.ALPHABET) - 1
    # The ambiguity sits above the range we ever encode or decode.
    assert tcn.ALPHABET.index("+") > tcn._MAX_INDEX
