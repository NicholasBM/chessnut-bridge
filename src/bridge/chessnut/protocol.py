"""Chessnut BLE wire protocol: pure byte handling, no BLE and no chess rules.

Source of truth is the vendor's own documentation at
github.com/chessnutech/Chessnut_eBoards, which explicitly covers the Go as well
as the Air, Air+ and Pro. Cross-checked against staubsauger/ChessnutPy, a
working Python client for the Air; where the two disagree the vendor wins, and
both disagreements are called out in comments below.

Deliberately dependency-free and I/O-free so the whole format can be tested with
synthetic packets, without a board and without a BLE stack.

One thing this module does *not* do is produce a FEN. A sensor board reports
which squares are occupied and by what; it cannot know whose turn it is, what
castling rights remain, whether en passant is available, or the move counters.
Calling the result a FEN invites code that trusts fields the hardware never
measured, so what comes back here is a ``placement`` -- the first field of a FEN
and nothing more. Reconstructing the rest is the sync layer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..squares import square_index, square_name

# --- BLE addressing -------------------------------------------------------

# The vendor documents the advertised name as "Chessnut (*)" covering Air, Air+,
# Go and Pro. Note ChessnutPy filters on the literal names "Chessnut Air" and
# "Smart Chess", neither of which would match a Go -- hence the prefix match.
DEVICE_NAME_PREFIX = "Chessnut"

FEN_SERVICE_UUID = "1b7e8261-2877-41c3-b46e-cf057c562023"
FEN_NOTIFY_UUID = "1b7e8262-2877-41c3-b46e-cf057c562023"

COMMAND_SERVICE_UUID = "1b7e8271-2877-41c3-b46e-cf057c562023"
COMMAND_WRITE_UUID = "1b7e8272-2877-41c3-b46e-cf057c562023"
COMMAND_NOTIFY_UUID = "1b7e8273-2877-41c3-b46e-cf057c562023"

# Enables real-time position reporting. Nothing arrives on the FEN
# characteristic until this is written, even with notifications subscribed.
CMD_ENABLE_REPORTING = b"\x21\x01\x00"
CMD_GET_BATTERY = b"\x29\x01\x00"

# --- board frames ---------------------------------------------------------

# A real Chessnut GO sends 38-byte frames, not the 36 the vendor documents
# (observed 2026-09-07, 374 consecutive frames, all 38 bytes). The second header
# byte is 0x24 == 36, which turns out to be the length of the *payload* after the
# two header bytes -- 32 occupancy bytes plus a 4-byte trailer -- not the length
# of the whole frame. The vendor's "36 byte array" appears to describe the
# payload, or an older model with a 2-byte trailer.
#
# So frames are validated against their own declared length rather than a
# constant. That is what makes this robust across models: ChessnutPy gets away
# with the same variation only because it slices the trailer open-ended
# (``data[34:]``) and never checks the length at all.
BOARD_FRAME_START = 0x01
BOARD_PAYLOAD_LENGTH = 0x24  # 36 == 32 occupancy bytes + a 4-byte trailer
BOARD_FRAME_HEADER = bytes((BOARD_FRAME_START, BOARD_PAYLOAD_LENGTH))
#: Total length of a frame from a Chessnut GO: two header bytes plus the payload.
BOARD_FRAME_LENGTH = 2 + BOARD_PAYLOAD_LENGTH  # 38
_BOARD_BYTES = 32
_OCCUPANCY_START = 2
_OCCUPANCY_END = _OCCUPANCY_START + _BOARD_BYTES  # 34

# Index is the 4-bit code on the wire. Lower-case is black, upper-case white.
PIECES = ("", "q", "k", "b", "p", "n", "R", "P", "r", "B", "N", "Q", "K")
_PIECE_CODES = {symbol: code for code, symbol in enumerate(PIECES) if symbol}

STARTING_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"


class ChessnutProtocolError(ValueError):
    """Raised when bytes from the board cannot be trusted."""


class TruncatedFrameError(ChessnutProtocolError):
    """A board frame arrived *shorter* than the length it declares.

    Almost always an unnegotiated ATT MTU rather than a corrupt board: the
    default MTU of 23 leaves 20 bytes of notification payload, so the frame is
    silently cut short. The vendor's remedy is to raise the MTU to 500 after
    connecting. This is a hard failure on purpose -- a half-read frame parses
    into a plausible-looking but wrong position, which is exactly the input that
    would make us submit a wrong move.

    Raised only for short frames. A frame that is *longer* than expected is a
    different fault with a different cause, so it raises the plain protocol error
    instead -- blaming the MTU for a long frame sends whoever is debugging it in
    precisely the wrong direction, which is what happened here on the first run
    against real hardware.
    """


def _scan_to_square_index(scan: int) -> int:
    """Wire scan position -> our square index.

    The board reports h8, g8, ... a8, h7, ... a1, which is the exact reverse of
    a1..h8, so the mapping is a subtraction. Verified against the vendor's
    JavaScript sample and independently against ChessnutPy's ``63 - i``.
    """
    return 63 - scan


def read_occupancy(board: bytes) -> dict[str, str]:
    """Decode the 32 position bytes into ``{'e1': 'K', ...}``.

    Empty squares are omitted rather than mapped to ``None``, so ``len()`` is
    the piece count and ``in`` means occupied.
    """
    if len(board) != _BOARD_BYTES:
        raise ChessnutProtocolError(
            f"position data must be {_BOARD_BYTES} bytes, got {len(board)}"
        )

    occupancy: dict[str, str] = {}
    for scan in range(64):
        byte = board[scan // 2]
        # Low nibble is the earlier square of the pair, high nibble the later.
        code = byte & 0x0F if scan % 2 == 0 else byte >> 4
        if code >= len(PIECES):
            raise ChessnutProtocolError(
                f"unknown piece code {code} at scan position {scan}"
            )
        if code:
            occupancy[square_name(_scan_to_square_index(scan))] = PIECES[code]
    return occupancy


def occupancy_to_bytes(occupancy: Mapping[str, str]) -> bytes:
    """Inverse of :func:`read_occupancy`, for tests and LED/diff work."""
    codes = [0] * 64
    for name, symbol in occupancy.items():
        if symbol not in _PIECE_CODES:
            raise ChessnutProtocolError(f"not a piece symbol: {symbol!r}")
        codes[63 - square_index(name)] = _PIECE_CODES[symbol]

    return bytes(
        codes[scan] | (codes[scan + 1] << 4) for scan in range(0, 64, 2)
    )


def occupancy_to_placement(occupancy: Mapping[str, str]) -> str:
    """Render occupancy as a FEN placement field (rank 8 first, file a first)."""
    ranks = []
    for rank in "87654321":
        row = ""
        empty = 0
        for file in "abcdefgh":
            symbol = occupancy.get(file + rank)
            if symbol is None:
                empty += 1
                continue
            if empty:
                row += str(empty)
                empty = 0
            row += symbol
        if empty:
            row += str(empty)
        ranks.append(row)
    return "/".join(ranks)


def placement_to_occupancy(placement: str) -> dict[str, str]:
    """Parse a FEN placement field. Accepts a full FEN and ignores the rest."""
    placement = placement.split(" ")[0]
    rows = placement.split("/")
    if len(rows) != 8:
        raise ChessnutProtocolError(f"placement needs 8 ranks, got {len(rows)}")

    occupancy: dict[str, str] = {}
    for row, rank in zip(rows, "87654321"):
        file = 0
        for char in row:
            if char.isdigit():
                file += int(char)
            elif char in _PIECE_CODES:
                if file > 7:
                    raise ChessnutProtocolError(f"rank {rank} overflows: {row!r}")
                occupancy["abcdefgh"[file] + rank] = char
                file += 1
            else:
                raise ChessnutProtocolError(f"bad placement character {char!r}")
        if file != 8:
            raise ChessnutProtocolError(f"rank {rank} has {file} files: {row!r}")
    return occupancy


@dataclass(frozen=True)
class BoardFrame:
    """One decoded position notification."""

    occupancy: dict[str, str]
    placement: str
    #: Board-side counter from byte 34 onwards, undocumented by the vendor.
    #: 4 bytes on a real Chessnut GO. ChessnutPy reads it as a little-endian
    #: timestamp and uses it only to order changes. Exposed for diagnostics; do
    #: not depend on its meaning.
    tick: int

    @property
    def is_starting_position(self) -> bool:
        return self.placement == STARTING_PLACEMENT


def parse_board_frame(frame: bytes) -> BoardFrame:
    """Decode a position notification (38 bytes from a Chessnut GO).

    Length is checked against the frame's own declared payload length rather than
    a hard-coded constant, so a model with a differently sized trailer still
    parses. What is *not* negotiable is the 32 occupancy bytes: anything that
    cannot supply all of them is rejected rather than partially decoded.
    """
    if len(frame) < 2:
        raise TruncatedFrameError(f"frame too short to have a header: {len(frame)} bytes")
    if frame[0] != BOARD_FRAME_START:
        raise ChessnutProtocolError(
            f"bad frame header {frame[:2].hex()}, expected "
            f"{BOARD_FRAME_START:#04x} then a payload length"
        )

    declared = 2 + frame[1]
    if frame[1] < _BOARD_BYTES:
        raise ChessnutProtocolError(
            f"frame declares a {frame[1]}-byte payload, too small to hold the "
            f"{_BOARD_BYTES} occupancy bytes"
        )
    if len(frame) < declared:
        raise TruncatedFrameError(
            f"frame declares {declared} bytes but only {len(frame)} arrived"
            " -- if this is 20 bytes it is the default 23-byte ATT MTU cutting"
            " the frame; negotiate a larger MTU before trusting positions"
        )
    if len(frame) > declared:
        # Not a truncation, so deliberately not a TruncatedFrameError: nothing
        # here points at the MTU. Most likely two notifications coalesced, and
        # silently ignoring the tail would drop a position update.
        raise ChessnutProtocolError(
            f"frame declares {declared} bytes but {len(frame)} arrived; "
            f"trailing bytes {frame[declared:].hex()} are unaccounted for"
        )

    occupancy = read_occupancy(frame[_OCCUPANCY_START:_OCCUPANCY_END])
    return BoardFrame(
        occupancy=occupancy,
        placement=occupancy_to_placement(occupancy),
        tick=int.from_bytes(frame[_OCCUPANCY_END:declared], "little"),
    )


def build_board_frame(
    placement: str, tick: int = 0, trailer_bytes: int = BOARD_PAYLOAD_LENGTH - _BOARD_BYTES
) -> bytes:
    """Construct a frame the board would have sent. Test helper.

    ``trailer_bytes`` defaults to the 4 a real Chessnut GO sends; it is a
    parameter so tests can build the shorter trailer the vendor documents.
    """
    payload = occupancy_to_bytes(placement_to_occupancy(placement)) + tick.to_bytes(
        trailer_bytes, "little"
    )
    return bytes((BOARD_FRAME_START, len(payload))) + payload


# --- LEDs -----------------------------------------------------------------

LED_COMMAND_PREFIX = b"\x0a\x08"
_LED_BYTES = 8


def led_command(squares: Iterable[str]) -> bytes:
    """Build the LED command lighting exactly ``squares`` and nothing else.

    Layout: byte 0 is rank 8 through byte 7 is rank 1; within a byte bit 0 is
    file h through bit 7 is file a. The vendor phrases this as "index from 8 to
    1, h to a", which reads LSB-first, and ChessnutPy's table (``a`` -> 0x80,
    ``h`` -> 0x01) agrees.

    The vendor calls this a "34-byte command" but then shows ten bytes, and ten
    is what ChessnutPy sends successfully; the byte count in the doc looks like
    a copy-paste of the frame length.
    """
    payload = bytearray(_LED_BYTES)
    for name in squares:
        index = square_index(name)
        payload[7 - index // 8] |= 1 << (7 - index % 8)
    return LED_COMMAND_PREFIX + bytes(payload)


def led_command_squares(command: bytes) -> set[str]:
    """Inverse of :func:`led_command`, so the bit layout can be round-tripped."""
    if len(command) != len(LED_COMMAND_PREFIX) + _LED_BYTES:
        raise ChessnutProtocolError(f"LED command must be 10 bytes, got {len(command)}")
    if command[:2] != LED_COMMAND_PREFIX:
        raise ChessnutProtocolError(f"bad LED prefix {command[:2].hex()}")

    lit = set()
    for offset, byte in enumerate(command[2:]):
        for bit in range(8):
            if byte & (1 << bit):
                lit.add(square_name((7 - offset) * 8 + (7 - bit)))
    return lit


# --- battery --------------------------------------------------------------

BATTERY_RESPONSE_PREFIX = b"\x2a\x02"


@dataclass(frozen=True)
class Battery:
    percent: int
    charging: bool


def parse_battery(data: bytes) -> Battery:
    """Decode a battery notification.

    The vendor and ChessnutPy disagree here. The vendor packs the charging flag
    into bit 7 of byte 2 with the percentage in bits 0-6; ChessnutPy reads
    charging from byte 3 (the vendor's "reserved") and clamps byte 2 to 100 --
    which would turn a charging 80% (0x80 | 80 == 208) into a flat 100.

    Rather than bet on one firmware, treat either signal as charging and always
    mask off bit 7 before reading the percentage. Both readings then give the
    right number, and the only cost is that a stray byte 3 could report charging
    when it is not -- a cosmetic error in a status line, not a wrong move.
    """
    if len(data) < 3 or data[:2] != BATTERY_RESPONSE_PREFIX:
        raise ChessnutProtocolError(f"not a battery response: {data.hex()}")

    raw = data[2]
    charging = bool(raw & 0x80) or (len(data) > 3 and data[3] == 1)
    return Battery(percent=min(raw & 0x7F, 100), charging=charging)
