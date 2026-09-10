"""Tests for the Chessnut wire format.

The board's scan order is the reverse of ours, so an off-by-one or a flipped
nibble produces a mirrored position that still looks like a legal chess board.
To catch that, the starting position is pinned to a byte string derived by hand
from the vendor spec rather than from our own encoder -- if the encoder and the
literal ever agree by accident, they had to agree twice.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge import squares  # noqa: E402
from bridge.chessnut import protocol  # noqa: E402

# Hand-derived from the vendor spec, scanning h8 -> a8, h7 -> ... -> a1 with the
# low nibble holding the earlier square of each pair.
#   rank 8  r n b k q b n r  ->  8 5 3 2 1 3 5 8  ->  58 23 31 85
#   rank 7  eight black pawns (4)                 ->  44 x4
#   ranks 6-3 empty                               ->  00 x16
#   rank 2  eight white pawns (7)                 ->  77 x4
#   rank 1  R N B K Q B N R  ->  6 a 9 c b 9 a 6  ->  a6 c9 9b 6a
STARTING_BOARD_BYTES = bytes.fromhex(
    "58233185"
    "44444444"
    "00000000000000000000000000000000"
    "77777777"
    "a6c99b6a"
)


# --- square convention ----------------------------------------------------


def test_square_convention_matches_the_tcn_codec():
    """Both modules must agree that a1 is 0, or moves land on mirrored squares."""
    from bridge.chesscom import tcn

    for index in range(64):
        assert squares.square_name(index) == tcn.index_to_square(index)


def test_square_round_trip():
    for index in range(64):
        assert squares.square_index(squares.square_name(index)) == index


@pytest.mark.parametrize("bad", ["a9", "i1", "a", "a11", ""])
def test_rejects_bad_square_names(bad):
    with pytest.raises(squares.SquareError):
        squares.square_index(bad)


# --- position decoding ----------------------------------------------------


def test_starting_position_matches_hand_derived_bytes():
    """Pins the scan order and nibble order against the vendor spec."""
    occupancy = protocol.placement_to_occupancy(protocol.STARTING_PLACEMENT)
    assert protocol.occupancy_to_bytes(occupancy) == STARTING_BOARD_BYTES


def test_hand_derived_bytes_decode_to_the_starting_position():
    occupancy = protocol.read_occupancy(STARTING_BOARD_BYTES)
    assert protocol.occupancy_to_placement(occupancy) == protocol.STARTING_PLACEMENT
    assert len(occupancy) == 32


@pytest.mark.parametrize(
    "square,symbol",
    [
        ("a1", "R"), ("e1", "K"), ("d1", "Q"), ("h1", "R"),
        ("a8", "r"), ("e8", "k"), ("d8", "q"), ("h8", "r"),
        ("a2", "P"), ("h7", "p"),
    ],
)
def test_corner_and_royal_squares_are_not_mirrored(square, symbol):
    """The asymmetric king/queen files are what a flipped board gets wrong."""
    occupancy = protocol.read_occupancy(STARTING_BOARD_BYTES)
    assert occupancy[square] == symbol


def test_empty_board_has_no_occupancy():
    assert protocol.read_occupancy(bytes(32)) == {}
    assert protocol.occupancy_to_placement({}) == "/".join(["8"] * 8)


def test_single_piece_lands_on_the_right_square():
    """One white king on e4 -- the simplest possible orientation check."""
    frame = protocol.build_board_frame("8/8/8/8/4K3/8/8/8")
    assert protocol.parse_board_frame(frame).occupancy == {"e4": "K"}


# --- frame handling -------------------------------------------------------


def test_frame_round_trip_with_tick():
    frame = protocol.build_board_frame(protocol.STARTING_PLACEMENT, tick=0x1234)
    parsed = protocol.parse_board_frame(frame)
    assert len(frame) == protocol.BOARD_FRAME_LENGTH
    assert parsed.placement == protocol.STARTING_PLACEMENT
    assert parsed.tick == 0x1234
    assert parsed.is_starting_position


def test_header_byte_encodes_the_payload_length_not_the_frame_length():
    """The distinction that cost us a run against real hardware.

    0x24 == 36 looked like the total frame length, and the vendor documents a
    "36 byte array", but a real board sends 38 bytes: two header bytes plus a
    36-byte payload.
    """
    assert protocol.BOARD_FRAME_HEADER[1] == protocol.BOARD_PAYLOAD_LENGTH == 36
    assert protocol.BOARD_FRAME_LENGTH == 38


# --- a frame captured off a real Chessnut GO ------------------------------

#: Verbatim from the board over BLE on 2026-09-07 (MTU 185, so nothing was
#: clipped). 374 consecutive frames were byte-identical to this.
REAL_GO_FRAME = bytes.fromhex(
    "0124"
    "2080050044044044000000000005000003a700000300770007000c7706009060"
    "4d000000"
)


def test_a_real_go_frame_is_38_bytes_and_parses():
    assert len(REAL_GO_FRAME) == 38
    parsed = protocol.parse_board_frame(REAL_GO_FRAME)
    # The physical board was set to one of our live daily games. Every square
    # matches that game except a8, where the rook was not sensed -- which is
    # what makes this frame worth pinning: an orientation or nibble-order error
    # could not produce a real game position off by a single piece.
    assert parsed.placement == "3nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R"
    assert parsed.occupancy["g8"] == "k"
    assert parsed.occupancy["a1"] == "R"
    assert "a8" not in parsed.occupancy


def test_the_real_frame_trailer_is_four_bytes():
    """Vendor docs imply two; the GO sends four. Read from the declared length."""
    assert protocol.parse_board_frame(REAL_GO_FRAME).tick == 0x4D


def test_a_shorter_documented_trailer_still_parses():
    """Robustness across models: the length comes from the frame, not a constant."""
    frame = protocol.build_board_frame(
        protocol.STARTING_PLACEMENT, tick=9, trailer_bytes=2
    )
    assert len(frame) == 36
    assert protocol.parse_board_frame(frame).tick == 9


def test_mid_game_placement_round_trips():
    """A real position from one of our live daily games."""
    placement = "r2qk2r/2pb1p2/p1np1npb/4p3/B3P3/2NP1N2/PPP3PP/R1BQK2R"
    frame = protocol.build_board_frame(placement)
    assert protocol.parse_board_frame(frame).placement == placement


def test_truncated_frame_fails_loudly_and_names_the_mtu():
    """A default 23-byte ATT MTU yields 20 bytes; that must never parse."""
    frame = protocol.build_board_frame(protocol.STARTING_PLACEMENT)[:20]
    with pytest.raises(protocol.TruncatedFrameError, match="MTU"):
        protocol.parse_board_frame(frame)


def test_a_frame_one_byte_short_of_its_occupancy_is_truncated():
    """The dangerous case: nearly all the position present, so nearly plausible."""
    frame = protocol.build_board_frame(protocol.STARTING_PLACEMENT)[:33]
    with pytest.raises(protocol.TruncatedFrameError):
        protocol.parse_board_frame(frame)


def test_overlong_frame_is_rejected_without_blaming_the_mtu():
    """A long frame is not a truncated one, and must not be diagnosed as one.

    Reporting "the MTU is too small" for a frame that is too *large* is what sent
    the first real-hardware run chasing the wrong cause.
    """
    frame = protocol.build_board_frame(protocol.STARTING_PLACEMENT) + b"\x00"
    with pytest.raises(protocol.ChessnutProtocolError) as excinfo:
        protocol.parse_board_frame(frame)
    assert not isinstance(excinfo.value, protocol.TruncatedFrameError)
    assert "MTU" not in str(excinfo.value)


def test_a_payload_too_small_for_the_position_is_rejected():
    """A frame may declare its own length, but not one that omits squares."""
    with pytest.raises(protocol.ChessnutProtocolError, match="occupancy"):
        protocol.parse_board_frame(b"\x01\x10" + bytes(16))


def test_wrong_header_is_rejected():
    frame = b"\x23\x01" + bytes(34)
    with pytest.raises(protocol.ChessnutProtocolError, match="header"):
        protocol.parse_board_frame(frame)


def test_unknown_piece_code_is_rejected():
    """Codes 13-15 are undefined; guessing would invent a piece."""
    board = bytearray(STARTING_BOARD_BYTES)
    board[10] = 0x0D
    with pytest.raises(protocol.ChessnutProtocolError, match="unknown piece code"):
        protocol.read_occupancy(bytes(board))


def test_short_position_data_is_rejected():
    with pytest.raises(protocol.ChessnutProtocolError, match="32 bytes"):
        protocol.read_occupancy(bytes(31))


@pytest.mark.parametrize(
    "bad",
    [
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP",          # 7 ranks
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNRR",  # 9 files
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBN",    # 7 files
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBXKBNR",   # bad symbol
    ],
)
def test_malformed_placement_is_rejected(bad):
    with pytest.raises(protocol.ChessnutProtocolError):
        protocol.placement_to_occupancy(bad)


def test_placement_accepts_a_full_fen_and_ignores_the_extra_fields():
    fen = "r2qk2r/2pb1p2/p1np1npb/4p3/B3P3/2NP1N2/PPP3PP/R1BQK2R w KQkq - 1 11"
    occupancy = protocol.placement_to_occupancy(fen)
    assert protocol.occupancy_to_placement(occupancy) == fen.split(" ")[0]


# --- LEDs -----------------------------------------------------------------


def test_led_command_length_and_prefix():
    command = protocol.led_command(["e4"])
    assert len(command) == 10
    assert command[:2] == protocol.LED_COMMAND_PREFIX


@pytest.mark.parametrize(
    "square,offset,bit",
    [("a8", 0, 7), ("h8", 0, 0), ("a1", 7, 7), ("h1", 7, 0), ("e4", 4, 3)],
)
def test_led_bit_layout(square, offset, bit):
    """Byte 0 is rank 8, bit 0 is file h -- the vendor's "8 to 1, h to a"."""
    payload = protocol.led_command([square])[2:]
    assert payload[offset] == 1 << bit
    assert sum(payload) == 1 << bit  # nothing else lit


def test_led_command_round_trips_every_square():
    for index in range(64):
        name = squares.square_name(index)
        assert protocol.led_command_squares(protocol.led_command([name])) == {name}


def test_led_command_with_no_squares_clears_the_board():
    assert protocol.led_command([]) == protocol.LED_COMMAND_PREFIX + bytes(8)


def test_led_command_lights_a_move_pair():
    lit = protocol.led_command_squares(protocol.led_command(["e2", "e4"]))
    assert lit == {"e2", "e4"}


def test_led_all_squares_sets_every_bit():
    everything = [squares.square_name(i) for i in range(64)]
    assert protocol.led_command(everything)[2:] == b"\xff" * 8


# --- battery --------------------------------------------------------------

def test_battery_vendor_encoding():
    """Vendor: bit 7 of byte 2 is charging, bits 0-6 the percentage."""
    assert protocol.parse_battery(b"\x2a\x02\x4b\x00") == protocol.Battery(75, False)
    assert protocol.parse_battery(b"\x2a\x02\xcb\x00") == protocol.Battery(75, True)


def test_battery_chessnutpy_encoding():
    """ChessnutPy reads charging from byte 3; we accept that too."""
    assert protocol.parse_battery(b"\x2a\x02\x4b\x01") == protocol.Battery(75, True)


def test_battery_percentage_is_clamped():
    assert protocol.parse_battery(b"\x2a\x02\x7f\x00").percent == 100


def test_battery_rejects_other_notifications():
    with pytest.raises(protocol.ChessnutProtocolError):
        protocol.parse_battery(b"\x23\x01\x00")  # heartbeat


# --- BLE addressing -------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Chessnut Go", "Chessnut Air", "Chessnut Air+", "Chessnut Pro"]
)
def test_device_name_prefix_matches_every_documented_board(name):
    assert name.startswith(protocol.DEVICE_NAME_PREFIX)


def test_uuids_are_lowercase_for_bleak_comparison():
    """bleak normalises to lowercase; mixed case has bitten other clients."""
    for uuid in (
        protocol.FEN_SERVICE_UUID,
        protocol.FEN_NOTIFY_UUID,
        protocol.COMMAND_SERVICE_UUID,
        protocol.COMMAND_WRITE_UUID,
        protocol.COMMAND_NOTIFY_UUID,
    ):
        assert uuid == uuid.lower()
        assert len(uuid) == 36
