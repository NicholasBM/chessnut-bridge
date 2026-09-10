"""BLE transport for the Chessnut board: connect, stay connected, hand up frames.

Everything format-related lives in :mod:`bridge.chessnut.protocol`; this module
only moves bytes and owns the connection lifecycle. The split is deliberate --
the protocol module stays importable and testable with no BLE stack at all, and
this module's reconnect logic is testable with a fake client, so the only thing
that genuinely needs hardware is confirming real frames arrive intact.

Design notes worth knowing before changing anything here:

* **One owner of the board at a time.** BLE is a single-connection medium. While
  this process holds the Chessnut, the phone app and any browser extension
  cannot, and vice versa.
* **A dropped connection is normal, not exceptional.** The board sleeps, goes out
  of range, and gets powered off. The loop treats disconnection as the expected
  case and reconnects with backoff forever rather than raising.
* **Device addresses are not portable.** macOS reports a CoreBluetooth UUID and
  Linux reports a MAC address, so a remembered address from this laptop is
  meaningless on the Pi. Reconnect always falls back to a scan by name.
* **A connected board is invisible to a scan**, because a connected peripheral
  stops advertising, and bleak refuses to connect to anything it has not just
  seen advertise. So a link BlueZ still holds after this process died is a
  deadlock the scan alone can never break -- see :func:`hang_up_stale_link`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sys
import time
import warnings
from dataclasses import dataclass, replace
from enum import Enum
from typing import Awaitable, Callable, Iterable, Protocol

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import protocol

log = logging.getLogger(__name__)

#: The minimum ATT MTU that carries a whole board frame: 38 bytes of frame plus
#: 3 bytes of ATT header. The BLE default is 23, which truncates every frame to
#: 20 bytes. The vendor's advice is to raise the MTU to 500 after connecting.
#: macOS negotiated 185 unasked, so this is really a Linux/BlueZ concern.
MIN_USABLE_MTU = protocol.BOARD_FRAME_LENGTH + 3

#: bleak's BlueZ backend does not read the negotiated MTU unless the private
#: ``_acquire_mtu()`` is called. Asked anyway, it returns this hardcoded BLE
#: default and emits a warning saying so.
_BLUEZ_UNKNOWN_MTU_WARNING = "default MTU"

_BACKOFF_START = 1.0
_BACKOFF_CAP = 30.0

#: Seconds to wait after hanging up a stale link before scanning again. BlueZ
#: acknowledges the disconnect well before the peripheral resumes advertising.
#:
#: This was 2.0, which was too eager: on 2026-09-10 a board reconnected that soon
#: after being force-hung-up came back accepting commands -- it answered battery
#: requests -- but never streamed another position, and only a power cycle of the
#: board itself cleared it. Giving the firmware time to finish tearing down its own
#: side is cheap; the alternative is a state no software here can get out of.
_RESCAN_AFTER_HANGUP = 8.0

#: How long a connected board may say nothing before the link is treated as dead.
#: A working board sends about ten frames a second, so this is two orders of
#: magnitude beyond normal -- generous enough that a slow start is never mistaken
#: for a fault. See :meth:`ChessnutBoard._wait_while_connected`.
SILENCE_TIMEOUT = 30.0

#: Long enough for a busy BlueZ, short enough not to stall the reconnect loop.
_BLUETOOTHCTL_TIMEOUT = 10.0

#: ``Device 00:1B:10:51:30:83 Chessnut GO``, as ``bluetoothctl devices`` prints
#: it. Searched rather than matched because the real output wraps some lines in
#: ANSI colour escapes.
_DEVICE_LINE = re.compile(r"Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) +(.*?)\s*$", re.I)


class BoardState(Enum):
    DISCONNECTED = "disconnected"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    CONNECTED = "connected"


@dataclass(frozen=True)
class BoardStatus:
    """Everything the web UI needs to describe the board, with no BLE types."""

    state: BoardState = BoardState.DISCONNECTED
    device_name: str | None = None
    address: str | None = None
    mtu: int | None = None
    connected_since: float | None = None
    last_frame_at: float | None = None
    frames: int = 0
    #: Frames rejected as truncated. Non-zero means the MTU is too small and no
    #: position from this session can be trusted.
    truncated_frames: int = 0
    #: Frames rejected for any other reason -- a header or length we do not
    #: understand. Counted separately from truncation because the two have
    #: different causes and different fixes, and because a board whose every
    #: frame is rejected must be visible in the UI either way. That is not
    #: hypothetical: the first run against a real Chessnut GO rejected 374
    #: consecutive frames, and conflating the two counters made it look like an
    #: MTU problem when the frames were simply longer than expected.
    rejected_frames: int = 0
    battery: protocol.Battery | None = None
    last_error: str | None = None

    @property
    def is_connected(self) -> bool:
        return self.state is BoardState.CONNECTED

    @property
    def mtu_is_sufficient(self) -> bool | None:
        """None when unknown -- not every backend reports the MTU."""
        return None if self.mtu is None else self.mtu >= MIN_USABLE_MTU

    @property
    def is_healthy(self) -> bool:
        """Connected, receiving frames, and none of them rejected."""
        return (
            self.is_connected
            and self.frames > 0
            and self.truncated_frames == 0
            and self.rejected_frames == 0
        )


class _ClientLike(Protocol):
    """The slice of BleakClient this module uses, so tests can substitute it."""

    address: str

    @property
    def mtu_size(self) -> int: ...
    async def __aenter__(self) -> "_ClientLike": ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def start_notify(self, char: str, callback: object) -> None: ...
    async def write_gatt_char(self, char: str, data: bytes) -> None: ...


def read_mtu(client: object) -> int | None:
    """The negotiated ATT MTU, or None when the backend will not tell us.

    Worth the ceremony because the naive read is not merely unhelpful, it is
    actively misleading. bleak on BlueZ returns a hardcoded 23 -- the BLE
    default, and below :data:`MIN_USABLE_MTU` -- whenever it has not read the
    real value, which it only does if the private ``_acquire_mtu()`` was called.
    Against a real Chessnut GO on a Pi Zero 2 W that printed
    ``MTU=23 (TOO SMALL)`` 52 times while 576 of 576 frames decoded with none
    truncated. A 38-byte frame cannot cross a genuine 23-byte MTU, so the number
    was never the negotiated one.

    A device that really negotiated 23 has its value set and warns about
    nothing, so catching the warning tells "too small" and "unknown" apart
    exactly, rather than guessing from the number. ``_acquire_mtu()`` would
    yield the true figure, but it is private and it claims the notify handle
    over a file descriptor, which is a real risk to the notifications we depend
    on -- a diagnostic nicety is not worth destabilising the read path for.

    The count of truncated frames remains the signal that actually matters: it
    observes the outcome instead of predicting it from a number the backend may
    not have.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            mtu = getattr(client, "mtu_size", None)
        except Exception as exc:  # noqa: BLE001 -- backends raise when not connected
            log.debug("could not read MTU: %r", exc)
            return None
    if any(_BLUEZ_UNKNOWN_MTU_WARNING in str(w.message) for w in caught):
        return None
    return mtu


PositionCallback = Callable[[protocol.BoardFrame], None | Awaitable[None]]
BatteryCallback = Callable[[protocol.Battery], None | Awaitable[None]]
StatusCallback = Callable[[BoardStatus], None | Awaitable[None]]


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        await result


def advertises_a_board(device: object, advertisement: object) -> bool:
    """Does this advertisement look like a Chessnut board?

    Checks the advertisement's ``local_name`` as well as ``device.name``, because
    on CoreBluetooth ``device.name`` is frequently None on the first sighting
    while the name is present in the advertisement payload. Matching only on
    ``device.name`` finds the board intermittently or not at all -- observed
    against a real Chessnut GO, which a full scan saw but this filter missed.

    Matching is case-insensitive on the documented ``Chessnut (*)`` prefix. The
    real board advertises ``Chessnut GO`` -- capital O -- so an exact-case check
    against a list of expected names would silently fail to find it.
    """
    for name in (getattr(device, "name", None), getattr(advertisement, "local_name", None)):
        if name and name.lower().startswith(protocol.DEVICE_NAME_PREFIX.lower()):
            return True
    return False


async def find_board(timeout: float = 10.0) -> tuple[str, str] | None:
    """Scan for a Chessnut board. Returns ``(address, name)`` or None."""
    found: dict[str, str] = {}

    def filter_and_remember(device: object, advertisement: object) -> bool:
        if not advertises_a_board(device, advertisement):
            return False
        found["name"] = (
            getattr(device, "name", None)
            or getattr(advertisement, "local_name", None)
            or ""
        )
        return True

    device = await BleakScanner.find_device_by_filter(
        filter_and_remember, timeout=timeout
    )
    if device is None:
        return None
    return device.address, found.get("name") or device.name or ""


async def _bluetoothctl(*args: str) -> str:
    """Run ``bluetoothctl`` and return its stdout. Empty on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        # No bluetoothctl on this machine, or no permission to exec it.
        log.debug("could not run bluetoothctl %s: %s", " ".join(args), exc)
        return ""
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_BLUETOOTHCTL_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.debug("bluetoothctl %s timed out", " ".join(args))
        proc.kill()
        return ""
    return stdout.decode("utf-8", errors="replace")


async def hang_up_stale_link(
    prefix: str = protocol.DEVICE_NAME_PREFIX,
) -> str | None:
    """Drop a board link BlueZ still holds but no process owns.

    Returns the address hung up, or None if there was nothing to hang up.

    This exists because of a deadlock seen live on 2026-09-09. The board's light
    was solid, ``hcitool con`` showed an open LE link with the Pi as central, and
    this process could not touch it: a connected peripheral stops advertising, and
    bleak will not connect to a device it has not just seen advertise -- not even
    by address, which fails with ``BleakDeviceNotFoundError``. So the scan could
    never succeed, and the reconnect loop backed off forever with the board sitting
    right there. Only BlueZ could break it, and only by being asked to hang up.

    The orphan came from this process being killed mid-connection, which a fixed
    shutdown path now avoids -- but a crash, an OOM kill or a power cut cannot run
    a shutdown path at all, so recovering without a human is the point.

    Called only when a scan has just failed *and* we hold no client, so any
    connected board is by definition not ours.

    Best-effort throughout: this is a recovery path, and every failure in it leaves
    the caller exactly where it already was.
    """
    if sys.platform != "linux":
        # Only BlueZ behaves this way; macOS is the development machine.
        return None
    for line in (await _bluetoothctl("devices", "Connected")).splitlines():
        match = _DEVICE_LINE.search(line)
        if match and match.group(2).lower().startswith(prefix.lower()):
            address = match.group(1)
            await _bluetoothctl("disconnect", address)
            return address
    return None


class ChessnutBoard:
    """Maintains a connection to the board and publishes decoded frames."""

    def __init__(
        self,
        on_position: PositionCallback | None = None,
        on_battery: BatteryCallback | None = None,
        on_status: StatusCallback | None = None,
        address: str | None = None,
        scan_timeout: float = 10.0,
        client_factory: Callable[[str, Callable[[object], None]], _ClientLike]
        | None = None,
        finder: Callable[[float], Awaitable[tuple[str, str] | None]] | None = None,
        hang_up: Callable[[], Awaitable[str | None]] | None = None,
        silence_timeout: float = SILENCE_TIMEOUT,
    ) -> None:
        self._on_position = on_position
        self._on_battery = on_battery
        self._on_status = on_status
        self._scan_timeout = scan_timeout
        self._client_factory = client_factory or (
            lambda addr, on_disconnect: BleakClient(
                addr, disconnected_callback=on_disconnect
            )
        )
        self._finder = finder or find_board
        self._hang_up = hang_up or hang_up_stale_link
        self._silence_timeout = silence_timeout

        self._status = BoardStatus(address=address)
        self._client: _ClientLike | None = None
        #: Set by stop(). Checked rather than assigned by run(), so stopping
        #: before starting is honoured instead of being overwritten.
        self._stop = asyncio.Event()
        #: Set by :meth:`request_reconnect`. Distinct from ``_disconnected``,
        #: which says the link went away on its own.
        self._reconnect_now = asyncio.Event()
        #: Replaced per connection; set from bleak's disconnected callback.
        self._disconnected = asyncio.Event()

    # --- observable state -------------------------------------------------

    @property
    def status(self) -> BoardStatus:
        return self._status

    async def _set_status(self, **changes: object) -> None:
        self._status = replace(self._status, **changes)  # type: ignore[arg-type]
        if self._on_status is not None:
            await _maybe_await(self._on_status(self._status))

    # --- notification handling --------------------------------------------

    async def _handle_position(self, _char: object, data: bytearray) -> None:
        try:
            frame = protocol.parse_board_frame(bytes(data))
        except protocol.TruncatedFrameError as exc:
            # Counted rather than raised: an exception here is swallowed by the
            # BLE stack, and a truncated frame is a configuration fault we want
            # surfaced in the UI, not a crash buried in a log.
            await self._set_status(
                truncated_frames=self._status.truncated_frames + 1,
                last_error=str(exc),
            )
            log.error("truncated board frame (%d bytes): %s", len(data), exc)
            return
        except protocol.ChessnutProtocolError as exc:
            # The raw hex goes in the log on purpose: when a board turns out to
            # speak a dialect we did not expect, the bytes are the only thing
            # that identifies it, and re-capturing them means getting the board
            # back into the same state.
            await self._set_status(
                rejected_frames=self._status.rejected_frames + 1,
                last_error=str(exc),
            )
            log.error("unparseable board frame %s: %s", bytes(data).hex(), exc)
            return

        await self._set_status(
            frames=self._status.frames + 1, last_frame_at=time.time()
        )
        if self._on_position is not None:
            await _maybe_await(self._on_position(frame))

    async def _handle_misc(self, _char: object, data: bytearray) -> None:
        raw = bytes(data)
        if raw.startswith(protocol.BATTERY_RESPONSE_PREFIX):
            battery = protocol.parse_battery(raw)
            await self._set_status(battery=battery)
            if self._on_battery is not None:
                await _maybe_await(self._on_battery(battery))
        else:
            # Heartbeats and on-the-board-storage chatter live here too. Logged
            # at debug so an unknown response is discoverable without noise.
            log.debug("misc notification: %s", raw.hex())

    # --- commands ---------------------------------------------------------

    async def set_leds(self, squares: Iterable[str]) -> bool:
        """Light exactly ``squares``. False if the board is not connected."""
        return await self._write(protocol.led_command(squares))

    async def clear_leds(self) -> bool:
        return await self.set_leds(())

    async def request_battery(self) -> bool:
        """Ask for a battery level; the answer arrives via ``on_battery``."""
        return await self._write(protocol.CMD_GET_BATTERY)

    async def _write(self, payload: bytes) -> bool:
        if self._client is None or not self._status.is_connected:
            log.debug("dropping write while disconnected: %s", payload.hex())
            return False
        try:
            await self._client.write_gatt_char(protocol.COMMAND_WRITE_UUID, payload)
            return True
        except BleakError as exc:
            # Losing the link mid-write is expected; the run loop reconnects.
            log.warning("write failed: %s", exc)
            return False

    # --- lifecycle --------------------------------------------------------

    async def _connect_once(self) -> None:
        address = self._status.address
        name = self._status.device_name

        if address is None:
            await self._set_status(state=BoardState.SCANNING)
            found = await self._finder(self._scan_timeout)
            if found is None:
                # A scan cannot see a board that is already connected, so before
                # calling this a failure, check whether the thing hiding it is a
                # link left over from a previous run of this process.
                stale = await self._hang_up()
                if stale is not None:
                    log.warning(
                        "hung up a stale link to %s that no process owned; "
                        "the board could not advertise while it was open",
                        stale,
                    )
                    await asyncio.sleep(_RESCAN_AFTER_HANGUP)
                    found = await self._finder(self._scan_timeout)
            if found is None:
                raise BleakError(
                    f"no device advertising a name starting "
                    f"{protocol.DEVICE_NAME_PREFIX!r} found in "
                    f"{self._scan_timeout:g}s"
                )
            address, name = found

        await self._set_status(
            state=BoardState.CONNECTING, address=address, device_name=name
        )

        self._disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()

        # Bound here, not read from self inside the callback. bleak fires a
        # disconnect callback during teardown, by which time the next connection
        # may already have replaced self._disconnected -- so reading it late let a
        # dead connection's callback kill a live one. That is self-perpetuating:
        # every replacement is killed by its predecessor. Observed live 2026-09-10
        # as a 2-4 second connect/disconnect loop in which the board reported its
        # battery on every cycle and never survived long enough to send a
        # position, leaving the LEDs frozen on the last command.
        disconnected = self._disconnected

        def on_disconnect(_client: object) -> None:
            # Called from the BLE stack's thread on some backends, so hop back
            # onto our loop rather than touching the Event directly.
            log.info("board disconnected")
            loop.call_soon_threadsafe(disconnected.set)

        client = self._client_factory(address, on_disconnect)
        async with client:
            self._client = client
            mtu = read_mtu(client)
            await self._set_status(
                state=BoardState.CONNECTED,
                mtu=mtu,
                connected_since=time.time(),
                last_error=None,
            )
            if mtu is None:
                # Normal on BlueZ; see read_mtu. Not a warning, because there is
                # nothing to act on and truncated_frames answers the question.
                log.info(
                    "connected to %s (ATT MTU not reported by this backend; "
                    "truncated frames are the signal to watch)",
                    name or address,
                )
            elif mtu < MIN_USABLE_MTU:
                # Not fatal here: the frame parser rejects short frames anyway,
                # and this way the UI can explain *why* nothing is arriving.
                log.error(
                    "ATT MTU %d is below the %d needed for a %d-byte frame; "
                    "positions will be rejected as truncated",
                    mtu,
                    MIN_USABLE_MTU,
                    protocol.BOARD_FRAME_LENGTH,
                )
            else:
                log.info("connected to %s (MTU %s)", name or address, mtu)

            await client.start_notify(protocol.FEN_NOTIFY_UUID, self._handle_position)
            await client.start_notify(protocol.COMMAND_NOTIFY_UUID, self._handle_misc)
            # Nothing is reported until this is written, even with the
            # subscription in place.
            await client.write_gatt_char(
                protocol.COMMAND_WRITE_UUID, protocol.CMD_ENABLE_REPORTING
            )
            await self.request_battery()

            await self._wait_while_connected()

    def _silent_for(self) -> float:
        """Seconds since the last frame, or since connecting if none ever came."""
        since = self._status.last_frame_at or self._status.connected_since
        return 0.0 if since is None else max(time.time() - since, 0.0)

    async def _wait_while_connected(self) -> None:
        """Block until the board drops the link, goes silent, or :meth:`stop`.

        Event-driven rather than polled: a 0.5s poll would keep the Zero 2 W's
        CPU awake for the 99.99% of a correspondence game where nothing happens,
        and would delay shutdown by up to half a second for no benefit. The one
        timeout is the silence watchdog.

        A connected board streams about ten frames a second, so silence means the
        link is up but useless. Returning ends the connection and the run loop
        rebuilds it -- the same thing the page's Reconnect button does. Seen live
        2026-09-10: after BlueZ force-hung-up the board and this reconnected two
        seconds later, the board answered a battery request on every attempt and
        never sent a single position. Without this, the appliance sat in that state
        indefinitely and only a human noticing got it fixed.
        """
        waiters = [
            asyncio.create_task(self._stop.wait()),
            asyncio.create_task(self._disconnected.wait()),
        ]
        try:
            while True:
                budget = self._silence_timeout - self._silent_for()
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=max(budget, 0.05),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if done:
                    return
                if self._silent_for() >= self._silence_timeout:
                    log.warning(
                        "connected to the board but no positions for %.0fs; "
                        "rebuilding the link",
                        self._silent_for(),
                    )
                    return
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def run(self) -> None:
        """Connect and keep reconnecting until :meth:`stop`. Does not raise."""
        backoff = _BACKOFF_START

        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = _BACKOFF_START
            except asyncio.CancelledError:
                raise
            except (BleakError, OSError) as exc:
                await self._set_status(
                    state=BoardState.DISCONNECTED, mtu=None, last_error=str(exc)
                )
                log.warning("board unavailable (%s); retrying in %.0fs", exc, backoff)
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                await self._set_status(
                    state=BoardState.DISCONNECTED, mtu=None, last_error=repr(exc)
                )
                log.exception("unexpected board error; retrying in %.0fs", backoff)
            else:
                await self._set_status(state=BoardState.DISCONNECTED, mtu=None)
            finally:
                self._client = None

            if self._stop.is_set():
                break

            # A request may already be pending -- made while we were connected --
            # in which case there is nothing to wait for.
            if not self._reconnect_now.is_set():
                # Interruptible sleep, so stop() or a reconnect request during a
                # 30s backoff is immediate.
                waiters = [
                    asyncio.create_task(self._stop.wait()),
                    asyncio.create_task(self._reconnect_now.wait()),
                ]
                try:
                    await asyncio.wait(
                        waiters, timeout=backoff, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for waiter in waiters:
                        waiter.cancel()
                if self._stop.is_set():
                    break
                if not self._reconnect_now.is_set():
                    # An ordinary retry. Once backoff has reached the cap, forget
                    # the address so a board that changed identity -- or a macOS
                    # UUID that went stale across a reboot -- is rediscovered by
                    # scanning rather than retried forever.
                    if backoff >= _BACKOFF_CAP:
                        await self._set_status(address=None)
                    backoff = min(backoff * 2, _BACKOFF_CAP)
                    continue

            # An explicit request, consumed in exactly this one place: it ends any
            # wait, starts from a clean backoff, and rescans rather than trusting
            # the cached address. Clearing it anywhere else as well would spend
            # one press on two connection attempts.
            self._reconnect_now.clear()
            self._disconnected.clear()
            backoff = _BACKOFF_START
            await self._set_status(address=None, last_error=None)

    def request_reconnect(self) -> None:
        """Drop the link and rebuild it now. The web UI's reconnect button.

        Implemented by ending the connected wait rather than closing the client
        here, so teardown stays on the run loop's single path instead of two
        callers racing to close the same connection.

        Clears the backoff, because someone pressing a button is new information:
        whatever made the last attempt fail may have just been fixed -- the board
        woken, the radio unblocked -- and making them wait out a 30-second
        backoff would be perverse. Also forgets the cached address, so a board
        that changed identity is rediscovered by scanning rather than retried
        forever at an address that no longer answers.

        Safe to call when already disconnected: the run loop is then either
        mid-scan or mid-backoff, and this cuts the wait short.
        """
        log.info("reconnect requested")
        self._reconnect_now.set()
        self._disconnected.set()

    def stop(self) -> None:
        """Ask the run loop to finish. Safe to call before :meth:`run`."""
        self._stop.set()
