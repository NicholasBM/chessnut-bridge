"""Tests for the BLE transport, using a fake client so no board is needed.

What is worth testing without hardware is the lifecycle, not the radio: that
reporting is actually enabled (nothing arrives if it is not), that a truncated
frame is counted and never delivered as a position, that writes while
disconnected fail quietly instead of raising, and that the run loop survives
errors it has never seen before. Those are the behaviours that decide whether an
unattended appliance recovers on its own or needs someone to SSH in.
"""

import asyncio
import dataclasses
import logging
import sys
import time
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bleak.exc import BleakError  # noqa: E402

from bridge.chessnut import ble, protocol  # noqa: E402


#: Captured before the autouse fixture below replaces the module attribute, so the
#: recovery path's own tests can reach the real implementation.
hang_up_stale_link = ble.hang_up_stale_link


@pytest.fixture(autouse=True)
def no_real_bluetoothctl(monkeypatch):
    """Never shell out during a test, on any platform.

    The stale-link recovery path runs whenever a scan comes back empty, which many
    tests here arrange deliberately. On a Linux dev machine that would talk to the
    real BlueZ and could hang up somebody's actual board.
    """

    async def refuse(*args):
        raise AssertionError(f"a test ran bluetoothctl {' '.join(args)}")

    monkeypatch.setattr(ble, "_bluetoothctl", refuse)
    monkeypatch.setattr(ble, "hang_up_stale_link", _nothing_stale)


async def _nothing_stale() -> str | None:
    return None


class FakeClient:
    """The slice of BleakClient that ChessnutBoard actually touches."""

    def __init__(self, address: str, mtu: int = 517, fail_on_connect: bool = False):
        self.address = address
        self._mtu = mtu
        self.writes: list[tuple[str, bytes]] = []
        self.subscriptions: dict[str, object] = {}
        self.fail_on_connect = fail_on_connect
        self.exited = False

    # A property, not an attribute, so subclasses can model backends that warn
    # or raise instead of answering -- which is what BlueZ and WinRT do.
    @property
    def mtu_size(self) -> int:
        return self._mtu

    async def __aenter__(self):
        if self.fail_on_connect:
            raise BleakError("device not reachable")
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def start_notify(self, char, callback):
        self.subscriptions[str(char).lower()] = callback

    async def write_gatt_char(self, char, data):
        self.writes.append((str(char).lower(), bytes(data)))

    # test helpers
    async def push_position(self, frame: bytes):
        await self.subscriptions[protocol.FEN_NOTIFY_UUID](None, bytearray(frame))

    async def push_misc(self, data: bytes):
        await self.subscriptions[protocol.COMMAND_NOTIFY_UUID](None, bytearray(data))

    @property
    def written_payloads(self) -> list[bytes]:
        return [payload for _char, payload in self.writes]


class ScriptedBoard(ble.ChessnutBoard):
    """Runs one connection, performs a scripted action, then stops."""

    def __init__(self, *args, script=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._script = script
        self.connections = 0

    async def _wait_while_connected(self) -> None:
        self.connections += 1
        if self._script is not None:
            await self._script(self)
        self.stop()


def make_board(client: FakeClient | None = None, **kwargs) -> ScriptedBoard:
    client = client or FakeClient("FAKE-ADDRESS")
    board = ScriptedBoard(
        address="FAKE-ADDRESS",
        client_factory=lambda _addr, _cb: client,
        **kwargs,
    )
    board.client = client  # type: ignore[attr-defined]
    return board


# --- connection setup -----------------------------------------------------


@pytest.mark.asyncio
async def test_connect_enables_reporting_and_subscribes():
    """Without the enable command the board reports nothing at all."""
    client = FakeClient("FAKE-ADDRESS")
    board = make_board(client)
    await board.run()

    assert protocol.FEN_NOTIFY_UUID in client.subscriptions
    assert protocol.COMMAND_NOTIFY_UUID in client.subscriptions
    assert protocol.CMD_ENABLE_REPORTING in client.written_payloads
    assert client.exited


@pytest.mark.asyncio
async def test_connect_requests_battery():
    client = FakeClient("FAKE-ADDRESS")
    board = make_board(client)
    await board.run()
    assert protocol.CMD_GET_BATTERY in client.written_payloads


@pytest.mark.asyncio
async def test_status_reports_a_sufficient_mtu():
    board = make_board(FakeClient("FAKE-ADDRESS", mtu=517))
    seen: list[ble.BoardStatus] = []
    board._on_status = seen.append
    await board.run()

    connected = [s for s in seen if s.state is ble.BoardState.CONNECTED]
    assert connected and connected[0].mtu == 517
    assert connected[0].mtu_is_sufficient is True


@pytest.mark.asyncio
async def test_status_flags_the_default_mtu_as_insufficient():
    """23 is the BLE default and truncates every frame; it must be visible."""
    board = make_board(FakeClient("FAKE-ADDRESS", mtu=23))
    seen: list[ble.BoardStatus] = []
    board._on_status = seen.append
    await board.run()

    connected = [s for s in seen if s.state is ble.BoardState.CONNECTED]
    assert connected[0].mtu_is_sufficient is False
    assert ble.MIN_USABLE_MTU == 41  # 38-byte frame + 3-byte ATT header


class BlueZLikeClient(FakeClient):
    """A client that reports the MTU the way bleak's BlueZ backend does.

    That backend returns a hardcoded 23 *and warns* whenever it has not actually
    read the negotiated value. Reproduced faithfully here because the real thing
    cost a debugging session: on a Pi Zero 2 W it printed "MTU=23 (TOO SMALL)"
    while all 576 frames decoded and none were truncated.
    """

    @property
    def mtu_size(self) -> int:
        warnings.warn(
            "Using default MTU value. Call _acquire_mtu() or set _mtu_size "
            "first to avoid this warning."
        )
        return 23


class MtulessClient(FakeClient):
    """A backend that raises rather than answering. WinRT has done this."""

    @property
    def mtu_size(self) -> int:
        raise BleakError("Not connected")


def test_an_unreported_mtu_reads_as_unknown_not_as_too_small():
    """The distinction the whole helper exists for.

    A backend that has not read the MTU must not be mistaken for a link that
    negotiated the 23-byte minimum, because the remedies are opposite: one needs
    nothing done, the other needs the connection abandoned as useless.
    """
    assert ble.read_mtu(BlueZLikeClient("FAKE-ADDRESS")) is None


def test_a_genuinely_tiny_mtu_is_still_reported():
    """The warning, not the number, is what marks a value as unknown.

    A device that really negotiated 23 sets the value and warns about nothing,
    so this must keep failing loudly. Otherwise the fix for the false alarm
    would have silenced the true one too.
    """
    assert ble.read_mtu(FakeClient("FAKE-ADDRESS", mtu=23)) == 23


def test_a_usable_mtu_survives_the_helper():
    assert ble.read_mtu(FakeClient("FAKE-ADDRESS", mtu=517)) == 517


def test_a_backend_that_raises_is_unknown_rather_than_fatal():
    assert ble.read_mtu(MtulessClient("FAKE-ADDRESS")) is None


def test_reading_the_mtu_does_not_leak_a_warning_to_the_caller():
    """Otherwise `-W error` runs, or a UI that surfaces warnings, sees noise
    about a condition we have already decided is unremarkable."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ble.read_mtu(BlueZLikeClient("FAKE-ADDRESS"))
    assert caught == []


@pytest.mark.asyncio
async def test_connecting_over_bluez_leaves_the_mtu_unknown_not_insufficient():
    """End to end: the status the UI reads must say "unknown", so that
    `mtu_is_sufficient` stays None and nothing renders a false failure."""
    board = make_board(BlueZLikeClient("FAKE-ADDRESS"))
    seen: list[ble.BoardStatus] = []
    board._on_status = seen.append
    await board.run()

    connected = [s for s in seen if s.state is ble.BoardState.CONNECTED]
    assert connected and connected[0].mtu is None
    assert connected[0].mtu_is_sufficient is None


@pytest.mark.asyncio
async def test_an_unknown_mtu_is_not_logged_as_an_error(caplog):
    """It is the ordinary case on Linux. Logging it at ERROR trains the reader
    to ignore the log, which is worse than saying nothing."""
    board = make_board(BlueZLikeClient("FAKE-ADDRESS"))
    with caplog.at_level("ERROR", logger="bridge.chessnut.ble"):
        await board.run()
    assert "MTU" not in caplog.text


@pytest.mark.asyncio
async def test_frames_still_decode_when_the_mtu_is_unknown():
    """The observed Pi behaviour, pinned: unknown MTU, frames arriving whole.

    This is the combination that the old code called a failure, so it is the
    one worth having a regression test for.
    """
    client = BlueZLikeClient("FAKE-ADDRESS")

    async def script(board):
        await client.push_position(
            protocol.build_board_frame(protocol.STARTING_PLACEMENT)
        )
        assert board.status.is_healthy, "an unknown MTU must not mean unhealthy"

    board = make_board(client, script=script)
    await board.run()

    assert board.status.frames == 1
    assert board.status.truncated_frames == 0
    assert board.status.mtu is None


# --- receiving positions --------------------------------------------------


@pytest.mark.asyncio
async def test_position_notification_is_decoded_and_delivered():
    positions = []
    client = FakeClient("FAKE-ADDRESS")

    async def script(_board):
        await client.push_position(
            protocol.build_board_frame(protocol.STARTING_PLACEMENT, tick=7)
        )

    board = make_board(client, on_position=positions.append, script=script)
    await board.run()

    assert len(positions) == 1
    assert positions[0].is_starting_position
    assert positions[0].tick == 7
    assert board.status.frames == 1
    assert board.status.truncated_frames == 0


@pytest.mark.asyncio
async def test_truncated_frame_is_counted_and_never_delivered():
    """The whole point: a half-frame must not reach the sync layer."""
    positions = []
    client = FakeClient("FAKE-ADDRESS", mtu=23)

    async def script(_board):
        full = protocol.build_board_frame(protocol.STARTING_PLACEMENT)
        await client.push_position(full[:20])  # what a 23-byte MTU delivers

    board = make_board(client, on_position=positions.append, script=script)
    await board.run()

    assert positions == []
    assert board.status.truncated_frames == 1
    assert board.status.frames == 0
    assert "MTU" in (board.status.last_error or "")
    assert not board.status.is_healthy


@pytest.mark.asyncio
async def test_garbage_frame_does_not_kill_the_notification_handler():
    positions = []
    client = FakeClient("FAKE-ADDRESS")

    async def script(_board):
        await client.push_position(b"\x99\x99" + bytes(34))  # bad header
        await client.push_position(
            protocol.build_board_frame(protocol.STARTING_PLACEMENT)
        )

    board = make_board(client, on_position=positions.append, script=script)
    await board.run()

    assert len(positions) == 1  # the good frame still arrived
    assert board.status.frames == 1
    assert board.status.rejected_frames == 1


@pytest.mark.asyncio
async def test_undecodable_frames_are_counted_even_with_a_healthy_mtu():
    """The real failure mode from the first hardware run.

    Every frame was rejected while the MTU was 185 -- fine. Unless that shows up
    as a count of its own, the UI would report a connected board, no positions,
    and nothing at all to explain the gap.
    """
    positions = []
    client = FakeClient("FAKE-ADDRESS", mtu=185)

    async def script(board):
        good = protocol.build_board_frame(protocol.STARTING_PLACEMENT)
        for _ in range(3):
            await client.push_position(good + b"\xff\xff")  # longer than declared
        # Checked here rather than after the run: the MTU is cleared on
        # disconnect, and the point is that it was ample while frames failed.
        assert board.status.mtu_is_sufficient is True
        assert not board.status.is_healthy

    board = make_board(client, on_position=positions.append, script=script)
    await board.run()

    assert positions == []
    assert board.status.rejected_frames == 3
    assert board.status.truncated_frames == 0, "not a truncation; must not say so"
    assert "MTU" not in (board.status.last_error or "")


@pytest.mark.asyncio
async def test_a_real_go_frame_is_accepted_by_the_transport():
    """Bytes captured verbatim off a Chessnut GO must survive the whole path."""
    positions = []
    client = FakeClient("FAKE-ADDRESS", mtu=185)
    real_frame = bytes.fromhex(
        "0124"
        "2080050044044044000000000005000003a700000300770007000c7706009060"
        "4d000000"
    )

    async def script(_board):
        await client.push_position(real_frame)

    board = make_board(client, on_position=positions.append, script=script)
    await board.run()

    assert len(positions) == 1
    assert positions[0].placement == "3nr1k1/ppp2ppp/8/5n2/4NP1b/2PP3b/PP1K3P/R1B4R"
    assert board.status.frames == 1
    assert board.status.rejected_frames == 0


@pytest.mark.asyncio
async def test_healthy_only_when_frames_arrive_intact():
    client = FakeClient("FAKE-ADDRESS")

    async def script(board):
        await client.push_position(
            protocol.build_board_frame(protocol.STARTING_PLACEMENT)
        )
        assert board.status.is_healthy

    board = make_board(client, script=script)
    await board.run()
    assert not board.status.is_healthy  # disconnected at the end


@pytest.mark.asyncio
async def test_sync_callbacks_are_supported_as_well_as_async():
    positions = []
    client = FakeClient("FAKE-ADDRESS")

    async def script(_board):
        await client.push_position(
            protocol.build_board_frame(protocol.STARTING_PLACEMENT)
        )

    def sync_callback(frame):
        positions.append(frame)

    board = make_board(client, on_position=sync_callback, script=script)
    await board.run()
    assert len(positions) == 1


# --- battery --------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_notification_updates_status():
    batteries = []
    client = FakeClient("FAKE-ADDRESS")

    async def script(_board):
        await client.push_misc(b"\x2a\x02\xcb\x00")  # 75%, charging

    board = make_board(client, on_battery=batteries.append, script=script)
    await board.run()

    assert batteries == [protocol.Battery(75, True)]
    assert board.status.battery == protocol.Battery(75, True)


@pytest.mark.asyncio
async def test_heartbeat_is_ignored_without_error():
    client = FakeClient("FAKE-ADDRESS")

    async def script(_board):
        await client.push_misc(b"\x23\x01\x00")

    board = make_board(client, script=script)
    await board.run()
    assert board.status.battery is None
    assert board.status.last_error is None


# --- commands -------------------------------------------------------------


@pytest.mark.asyncio
async def test_led_command_is_written_while_connected():
    client = FakeClient("FAKE-ADDRESS")

    async def script(board):
        assert await board.set_leds(["e2", "e4"]) is True

    board = make_board(client, script=script)
    await board.run()
    assert protocol.led_command(["e2", "e4"]) in client.written_payloads


@pytest.mark.asyncio
async def test_writes_while_disconnected_fail_quietly():
    """Callers should not have to guard every write with a try/except."""
    board = make_board()
    assert await board.set_leds(["e4"]) is False
    assert await board.request_battery() is False
    assert await board.clear_leds() is False


@pytest.mark.asyncio
async def test_write_failure_is_reported_not_raised():
    client = FakeClient("FAKE-ADDRESS")

    async def failing_write(_char, _data):
        raise BleakError("link lost")

    async def script(board):
        client.write_gatt_char = failing_write
        assert await board.set_leds(["e4"]) is False

    board = make_board(client, script=script)
    await board.run()


# --- recovery -------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnects_after_a_failed_connection(monkeypatch):
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    attempts = []

    def factory(address, _on_disconnect):
        attempts.append(address)
        return FakeClient(address, fail_on_connect=len(attempts) == 1)

    board = ScriptedBoard(address="FAKE-ADDRESS", client_factory=factory)
    await asyncio.wait_for(board.run(), timeout=5)

    assert len(attempts) == 2  # failed, then succeeded
    assert board.connections == 1
    assert board.status.state is ble.BoardState.DISCONNECTED


@pytest.mark.asyncio
async def test_an_unexpected_error_does_not_end_the_run_loop(monkeypatch):
    """A board that misbehaves must not require a restart of the service."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    attempts = []

    def factory(address, _on_disconnect):
        attempts.append(address)
        if len(attempts) == 1:
            raise RuntimeError("something nobody predicted")
        return FakeClient(address)

    board = ScriptedBoard(address="FAKE-ADDRESS", client_factory=factory)
    await asyncio.wait_for(board.run(), timeout=5)

    assert len(attempts) == 2
    assert board.connections == 1


@pytest.mark.asyncio
async def test_scans_when_no_address_is_known():
    found = []

    async def finder(_timeout):
        found.append(True)
        return ("DISCOVERED-ADDRESS", "Chessnut Go")

    client = FakeClient("DISCOVERED-ADDRESS")
    board = ScriptedBoard(client_factory=lambda _a, _cb: client, finder=finder)
    await board.run()

    assert found == [True]
    assert board.status.address == "DISCOVERED-ADDRESS"
    assert board.status.device_name == "Chessnut Go"


@pytest.mark.asyncio
async def test_failed_scan_is_retried_not_fatal(monkeypatch):
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    calls = []

    async def finder(_timeout):
        calls.append(True)
        return None if len(calls) == 1 else ("ADDR", "Chessnut Go")

    board = ScriptedBoard(
        client_factory=lambda a, _cb: FakeClient(a), finder=finder
    )
    await asyncio.wait_for(board.run(), timeout=5)
    assert len(calls) == 2


# --- stale links ----------------------------------------------------------
#
# Observed live 2026-09-09: the board's light was solid, hcitool showed an open LE
# link with the Pi as central, and this process could not touch it. A connected
# peripheral stops advertising and bleak will not connect to anything it has not
# just seen advertise, so the scan could never succeed. Only BlueZ could break it.


@pytest.mark.asyncio
async def test_an_empty_scan_hangs_up_a_stale_link_and_scans_again(monkeypatch):
    """The deadlock: the board is invisible precisely because it is connected."""
    monkeypatch.setattr(ble, "_RESCAN_AFTER_HANGUP", 0)
    scans = []

    async def finder(_timeout):
        scans.append(True)
        return None if len(scans) == 1 else ("ADDR", "Chessnut GO")

    async def hang_up():
        return "00:1B:10:51:30:83"

    board = ScriptedBoard(
        client_factory=lambda a, _cb: FakeClient(a), finder=finder, hang_up=hang_up
    )
    await asyncio.wait_for(board.run(), timeout=5)

    assert len(scans) == 2, "the point of hanging up is to scan again"
    assert board.connections == 1
    assert board.status.address == "ADDR"


@pytest.mark.asyncio
async def test_nothing_stale_leaves_the_scan_failure_standing(monkeypatch):
    """With no orphan to blame, an empty scan is just an absent board -- and must
    not be turned into a second scan on every attempt."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    scans = []

    async def finder(_timeout):
        scans.append(True)
        return None if len(scans) == 1 else ("ADDR", "Chessnut GO")

    board = ScriptedBoard(
        client_factory=lambda a, _cb: FakeClient(a),
        finder=finder,
        hang_up=_nothing_stale,
    )
    await asyncio.wait_for(board.run(), timeout=5)

    assert len(scans) == 2, "one scan per attempt, not two"


@pytest.mark.asyncio
async def test_a_known_address_never_hangs_up_anything():
    """The connection it would hang up could be our own: this path runs only
    after a scan has failed, which means we hold no client."""
    hung_up = []

    async def hang_up():
        hung_up.append(True)
        return None

    board = make_board(hang_up=hang_up)
    await board.run()

    assert hung_up == []


@pytest.mark.asyncio
async def test_a_hang_up_that_fails_is_not_fatal(monkeypatch):
    """Recovery is best-effort; failing at it must leave the caller where it was."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    scans = []

    async def finder(_timeout):
        scans.append(True)
        return None if len(scans) == 1 else ("ADDR", "Chessnut GO")

    async def hang_up():
        raise OSError("no bluetoothctl here")

    board = ScriptedBoard(
        client_factory=lambda a, _cb: FakeClient(a), finder=finder, hang_up=hang_up
    )
    await asyncio.wait_for(board.run(), timeout=5)

    assert board.connections == 1


@pytest.mark.asyncio
async def test_hang_up_disconnects_the_board_bluez_still_holds(monkeypatch):
    monkeypatch.setattr(ble.sys, "platform", "linux")
    calls = []

    async def fake_bluetoothctl(*args):
        calls.append(args)
        if args[0] == "devices":
            # Real output, ANSI escapes and unrelated devices included.
            return (
                "Device D0:38:33:33:29:2F Govee_H6076_292F\n"
                "\x1b[1;30mDevice 00:1B:10:51:30:83 Chessnut GO\x1b[0m\n"
            )
        return ""

    monkeypatch.setattr(ble, "_bluetoothctl", fake_bluetoothctl)

    assert await hang_up_stale_link() == "00:1B:10:51:30:83"
    assert ("disconnect", "00:1B:10:51:30:83") in calls


@pytest.mark.asyncio
async def test_hang_up_leaves_other_peoples_devices_alone(monkeypatch):
    """A doorbell and a pair of headphones are not ours to disconnect."""
    monkeypatch.setattr(ble.sys, "platform", "linux")
    calls = []

    async def fake_bluetoothctl(*args):
        calls.append(args)
        return "Device D0:38:33:33:29:2F Govee_H6076_292F\n" if args[0] == "devices" else ""

    monkeypatch.setattr(ble, "_bluetoothctl", fake_bluetoothctl)

    assert await hang_up_stale_link() is None
    assert not any(args[0] == "disconnect" for args in calls)


@pytest.mark.asyncio
async def test_hang_up_does_nothing_off_linux(monkeypatch):
    """Only BlueZ behaves this way, and macOS is the development machine."""
    monkeypatch.setattr(ble.sys, "platform", "darwin")
    assert await hang_up_stale_link() is None


@pytest.mark.asyncio
async def test_stop_ends_the_loop():
    board = ScriptedBoard(
        address="FAKE-ADDRESS", client_factory=lambda a, _cb: FakeClient(a)
    )
    board.stop()
    await asyncio.wait_for(board.run(), timeout=5)
    assert board.connections == 0


@pytest.mark.asyncio
async def test_board_powered_off_then_on_reconnects(monkeypatch):
    """The brief's case: the board drops the link, then comes back.

    Driven through bleak's disconnected_callback, which is the only signal a
    dropped link produces -- ``async with client`` does not raise on its own, so
    without this wiring the loop would sit connected to a dead board forever.
    """
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    clients: list[FakeClient] = []
    disconnectors: list[object] = []

    def factory(address, on_disconnect):
        clients.append(FakeClient(address))
        disconnectors.append(on_disconnect)
        return clients[-1]

    class Board(ble.ChessnutBoard):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.connections = 0

        async def _wait_while_connected(self):
            self.connections += 1
            if self.connections == 1:
                # Board switched off: bleak notifies us out of band.
                disconnectors[-1](clients[-1])
                await super()._wait_while_connected()
            else:
                self.stop()

    board = Board(address="FAKE-ADDRESS", client_factory=factory)
    await asyncio.wait_for(board.run(), timeout=5)

    assert board.connections == 2, "should have reconnected after the drop"
    assert len(clients) == 2
    # Reporting must be re-enabled on the new connection, not just the first.
    assert protocol.CMD_ENABLE_REPORTING in clients[1].written_payloads


@pytest.mark.asyncio
async def test_a_dead_connections_callback_cannot_kill_a_live_one(monkeypatch):
    """The flapping seen live on 2026-09-10.

    bleak fires a disconnect callback during teardown. If the callback reads
    ``self._disconnected`` when it runs rather than when it was made, the next
    connection has already replaced that event -- so the dead connection's callback
    tears down the live one, whose own callback then does the same to its
    replacement. On the appliance this became a 2-4 second connect/disconnect loop
    that reported battery on every cycle and never lived long enough to send a
    single position, so the board's LEDs stayed frozen on the last command.
    """
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.001)
    clients: list[FakeClient] = []
    disconnectors: list[object] = []

    def factory(address, on_disconnect):
        clients.append(FakeClient(address))
        disconnectors.append(on_disconnect)
        return clients[-1]

    class Board(ble.ChessnutBoard):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.connections = 0

        async def _wait_while_connected(self):
            self.connections += 1
            if self.connections == 1:
                disconnectors[-1](clients[-1])  # a real drop
                await super()._wait_while_connected()
                return
            # Now connection 2 is live. The *first* connection's callback fires
            # late, as bleak's does during teardown. It must not end this one.
            disconnectors[0](clients[0])
            await asyncio.sleep(0)  # let call_soon_threadsafe deliver it
            assert not self._disconnected.is_set(), (
                "a dead connection's callback ended the live connection"
            )
            self.stop()

    board = Board(address="FAKE-ADDRESS", client_factory=factory)
    await asyncio.wait_for(board.run(), timeout=5)

    assert board.connections == 2, "the second connection should have survived"


# --- the silence watchdog -------------------------------------------------
#
# A board can hold the link open and send nothing, which every other signal here
# reads as healthy: bleak fires no callback, the client stays entered, battery
# requests are even answered. Seen live on 2026-09-10 after BlueZ force-hung-up
# the board and the loop reconnected two seconds later. Nothing but a human
# noticing got it out of that state, and the protocol has no "send me the
# position" command to ask with -- so the only cure is to rebuild the link.


def _connected_board(**kwargs) -> ble.ChessnutBoard:
    """A board in the state ``_wait_while_connected`` is called in."""
    board = ble.ChessnutBoard(
        address="FAKE-ADDRESS",
        client_factory=lambda a, _cb: FakeClient(a),
        **kwargs,
    )
    board._status = dataclasses.replace(
        board._status, state=ble.BoardState.CONNECTED, connected_since=time.time()
    )
    board._disconnected = asyncio.Event()
    return board


@pytest.mark.asyncio
async def test_silence_is_measured_from_the_last_frame_not_the_connection():
    """Otherwise a board streaming happily for an hour would be torn down at 30s."""
    board = _connected_board()
    board._status = dataclasses.replace(
        board._status, connected_since=time.time() - 600, last_frame_at=time.time()
    )
    assert board._silent_for() < 1.0


@pytest.mark.asyncio
async def test_silence_is_measured_from_the_connection_before_any_frame():
    """The case that actually happened: not one frame ever arrived, so there is no
    last frame to measure from and the connection itself is the start of the wait."""
    board = _connected_board()
    board._status = dataclasses.replace(
        board._status, connected_since=time.time() - 5, last_frame_at=None
    )
    assert 4.0 < board._silent_for() < 7.0


@pytest.mark.asyncio
async def test_a_connected_board_that_sends_nothing_has_its_link_rebuilt(caplog):
    board = _connected_board(silence_timeout=0.05)
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(board._wait_while_connected(), timeout=2)
    assert "no positions" in caplog.text
    assert not board._disconnected.is_set(), "nothing dropped the link; we gave up on it"


@pytest.mark.asyncio
async def test_a_board_that_keeps_streaming_is_left_alone():
    """The watchdog must not interrupt a working connection, which is the state the
    appliance spends nearly all of its life in."""
    board = _connected_board(silence_timeout=0.15)

    async def keep_streaming():
        for _ in range(6):
            await asyncio.sleep(0.05)
            board._status = dataclasses.replace(
                board._status, last_frame_at=time.time()
            )
        board._disconnected.set()  # only this ends it

    streaming = asyncio.create_task(keep_streaming())
    await asyncio.wait_for(board._wait_while_connected(), timeout=2)
    await streaming
    assert board._disconnected.is_set(), "the watchdog fired on a streaming board"


@pytest.mark.asyncio
async def test_stop_ends_the_wait_without_waiting_out_the_silence():
    """Shutdown must not be delayed by however much of the 30s is left."""
    board = _connected_board()  # the shipped timeout
    waiting = asyncio.create_task(board._wait_while_connected())
    await asyncio.sleep(0)
    board.stop()
    await asyncio.wait_for(waiting, timeout=1)


def test_the_shipped_silence_timeout_is_many_frames_long():
    """A board streams about ten frames a second, so 30s is hundreds of missed
    frames -- long enough that a momentary stall cannot trip it."""
    assert ble.SILENCE_TIMEOUT >= 15.0


def test_the_rescan_delay_leaves_the_board_time_to_advertise_again():
    """2.0s was too aggressive. On 2026-09-10 the loop hung up a stale link and
    reconnected two seconds later, and the board -- which had just been dropped
    without warning by BlueZ -- held the link open and never sent a position again
    until it was power-cycled."""
    assert ble._RESCAN_AFTER_HANGUP >= 5.0


@pytest.mark.asyncio
async def test_stop_during_backoff_is_immediate(monkeypatch):
    """A 30s backoff must not delay shutdown by 30s."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 30.0)

    def factory(address, _on_disconnect):
        return FakeClient(address, fail_on_connect=True)

    board = ble.ChessnutBoard(address="FAKE-ADDRESS", client_factory=factory)
    runner = asyncio.create_task(board.run())
    await asyncio.sleep(0.05)  # let it fail once and enter the backoff
    board.stop()
    await asyncio.wait_for(runner, timeout=1)  # would time out if it slept 30s


# --- reconnecting on request ----------------------------------------------
#
# The web UI's one button that touches the radio. Its whole value is in the
# cases where the loop is *already* waiting for something -- a connected board,
# or a long backoff -- so every test here checks that the wait was cut short
# rather than merely that a flag was set.


def _rediscovering_factory(clients, disconnectors):
    def factory(address, on_disconnect):
        clients.append(FakeClient(address))
        disconnectors.append(on_disconnect)
        return clients[-1]

    return factory


async def _finds_the_board(_timeout):
    """Every test here needs a finder: a requested reconnect clears the cached
    address on purpose, so the following attempt scans rather than reusing it."""
    return "RESCANNED-ADDRESS", "Chessnut GO"


@pytest.mark.asyncio
async def test_a_requested_reconnect_rebuilds_a_live_connection():
    """The case the button exists for: the link looks up but is not working."""
    clients: list[FakeClient] = []
    disconnectors: list[object] = []

    class Board(ble.ChessnutBoard):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.connections = 0

        async def _wait_while_connected(self):
            self.connections += 1
            if self.connections == 1:
                self.request_reconnect()
                await super()._wait_while_connected()
            else:
                self.stop()

    board = Board(
        address="FAKE-ADDRESS",
        client_factory=_rediscovering_factory(clients, disconnectors),
        finder=_finds_the_board,
    )
    await asyncio.wait_for(board.run(), timeout=5)

    assert board.connections == 2, "the connected wait should have been cut short"
    # Reporting has to be re-enabled on the rebuilt link, not just the first one.
    assert protocol.CMD_ENABLE_REPORTING in clients[1].written_payloads


@pytest.mark.asyncio
async def test_a_requested_reconnect_rescans_rather_than_trusting_the_address():
    """A board that came back at a different address -- or a macOS UUID that went
    stale -- is exactly when someone presses the button."""
    clients: list[FakeClient] = []
    disconnectors: list[object] = []
    scans = 0

    async def finder(_timeout):
        nonlocal scans
        scans += 1
        return "RESCANNED-ADDRESS", "Chessnut GO"

    class Board(ble.ChessnutBoard):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.connections = 0

        async def _wait_while_connected(self):
            self.connections += 1
            if self.connections == 1:
                self.request_reconnect()
                await super()._wait_while_connected()
            else:
                self.stop()

    board = Board(
        address="FAKE-ADDRESS",
        client_factory=_rediscovering_factory(clients, disconnectors),
        finder=finder,
    )
    await asyncio.wait_for(board.run(), timeout=5)

    assert scans == 1, "should have scanned rather than reusing the cached address"
    assert clients[1].address == "RESCANNED-ADDRESS"


@pytest.mark.asyncio
async def test_a_requested_reconnect_does_not_wait_out_the_backoff(monkeypatch):
    """Pressing the button is new information: whatever was broken may have just
    been fixed, so making the player wait 30 seconds would be perverse."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 30.0)
    attempts = 0

    def factory(address, _on_disconnect):
        nonlocal attempts
        attempts += 1
        return FakeClient(address, fail_on_connect=True)

    board = ble.ChessnutBoard(
        address="FAKE-ADDRESS", client_factory=factory, finder=_finds_the_board
    )
    runner = asyncio.create_task(board.run())
    for _ in range(100):  # let the first attempt fail and enter the backoff
        if attempts >= 1:
            break
        await asyncio.sleep(0.01)

    board.request_reconnect()
    for _ in range(100):
        if attempts >= 2:
            break
        await asyncio.sleep(0.01)

    board.stop()
    await asyncio.wait_for(runner, timeout=1)
    assert attempts >= 2, "the 30s backoff should have been abandoned"


@pytest.mark.asyncio
async def test_a_requested_reconnect_resets_the_backoff(monkeypatch):
    """Not just this wait: the next failure must start from one second again,
    or the button would leave the loop still capped at 30s."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 0.01)
    monkeypatch.setattr(ble, "_BACKOFF_CAP", 0.08)
    waits: list[float | None] = []
    real_wait = asyncio.wait

    async def recording_wait(*args, **kwargs):
        if "timeout" in kwargs:
            waits.append(kwargs["timeout"])
        return await real_wait(*args, **kwargs)

    monkeypatch.setattr(asyncio, "wait", recording_wait)

    def factory(address, _on_disconnect):
        return FakeClient(address, fail_on_connect=True)

    board = ble.ChessnutBoard(
        address="FAKE-ADDRESS", client_factory=factory, finder=_finds_the_board
    )
    runner = asyncio.create_task(board.run())
    for _ in range(200):  # let the backoff grow past its starting value
        if len(waits) >= 3:
            break
        await asyncio.sleep(0.01)
    grown = len(waits)
    assert waits[-1] > ble._BACKOFF_START, "backoff should have doubled by now"

    board.request_reconnect()
    for _ in range(200):
        if len(waits) > grown:
            break
        await asyncio.sleep(0.01)

    board.stop()
    await asyncio.wait_for(runner, timeout=1)
    assert waits[grown] == ble._BACKOFF_START


@pytest.mark.asyncio
async def test_a_reconnect_requested_while_disconnected_is_not_an_error():
    """The UI cannot know what the loop is doing when the button is pressed, and
    the disconnected case is the likeliest one."""
    board = ble.ChessnutBoard(
        address="FAKE-ADDRESS",
        client_factory=lambda a, _cb: FakeClient(a, fail_on_connect=True),
        finder=_finds_the_board,
    )
    board.request_reconnect()  # before run() has ever been called
    board.stop()
    await asyncio.wait_for(board.run(), timeout=5)


@pytest.mark.asyncio
async def test_a_requested_reconnect_is_consumed_rather_than_latched(monkeypatch):
    """A request left set would make the loop reconnect forever with no wait --
    a busy loop on a board that is simply switched off."""
    monkeypatch.setattr(ble, "_BACKOFF_START", 30.0)
    attempts = 0

    def factory(address, _on_disconnect):
        nonlocal attempts
        attempts += 1
        return FakeClient(address, fail_on_connect=True)

    board = ble.ChessnutBoard(
        address="FAKE-ADDRESS", client_factory=factory, finder=_finds_the_board
    )
    runner = asyncio.create_task(board.run())
    for _ in range(100):
        if attempts >= 1:
            break
        await asyncio.sleep(0.01)

    board.request_reconnect()
    for _ in range(100):
        if attempts >= 2:
            break
        await asyncio.sleep(0.01)

    await asyncio.sleep(0.1)  # if it were latched, attempts would keep climbing
    settled = attempts
    await asyncio.sleep(0.1)

    board.stop()
    await asyncio.wait_for(runner, timeout=1)
    assert attempts == settled == 2, "one request should buy one extra attempt"


# --- device name matching -------------------------------------------------


class FakeDevice:
    def __init__(self, name=None):
        self.name = name
        self.address = "ADDR"


class FakeAdvertisement:
    def __init__(self, local_name=None):
        self.local_name = local_name
        self.rssi = -70


@pytest.mark.parametrize(
    "name",
    [
        "Chessnut GO",  # the real board, observed 2026-09-07 -- capital O
        "Chessnut Go",
        "Chessnut Air",
        "Chessnut Air+",
        "Chessnut Pro",
        "chessnut go",
    ],
)
def test_matches_real_and_documented_board_names(name):
    assert ble.advertises_a_board(FakeDevice(name), FakeAdvertisement())


def test_matches_when_only_the_advertisement_carries_the_name():
    """CoreBluetooth often reports device.name as None on first sighting.

    This is not hypothetical: it is why the first connection attempt against a
    real Chessnut GO failed while a full scan could see the board.
    """
    assert ble.advertises_a_board(
        FakeDevice(None), FakeAdvertisement("Chessnut GO")
    )


@pytest.mark.parametrize("name", ["Govee_H6076", "ACTON III [LE]", "", None])
def test_ignores_other_devices(name):
    assert not ble.advertises_a_board(FakeDevice(name), FakeAdvertisement(name))


def test_min_usable_mtu_accounts_for_the_att_header():
    """38-byte frame plus the 3-byte ATT notification header."""
    assert ble.MIN_USABLE_MTU == protocol.BOARD_FRAME_LENGTH + 3
