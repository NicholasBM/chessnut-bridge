#!/usr/bin/env python3
"""Diagnostics for the Chessnut board over BLE. Runs on macOS or the Pi.

    python scripts/board_doctor.py scan     # list every BLE device nearby
    python scripts/board_doctor.py session  # connect once and hold it open
    python scripts/board_doctor.py watch    # connect, stream, exit with a verdict

Prefer ``session`` when a human is at the board. The Go only advertises for a
minute or two after being physically woken and sleeps again once we disconnect,
so every dropped connection costs someone walking over and tapping it -- and BLE
allows one owner at a time, so two diagnostic runs cannot overlap.

``scan`` exists because the first unknown is what the Go actually advertises. It
prints everything it can see rather than only name matches, so a board naming
itself something unexpected still shows up.

``watch`` is the real test: it reports the negotiated MTU, streams the position
as an ASCII board, and counts truncated frames. A non-zero truncated count is the
single most important output here -- it means the MTU is too small and no
position can be trusted.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bleak import BleakScanner  # noqa: E402

from bridge.chesscom.public import DailyGame, PublicClient, watch_games  # noqa: E402
from bridge.chessnut import protocol  # noqa: E402
from bridge.chessnut.ble import (  # noqa: E402
    MIN_USABLE_MTU,
    BoardStatus,
    ChessnutBoard,
)
from bridge.state.store import StateStore  # noqa: E402
from bridge.sync.engine import SyncEngine  # noqa: E402


def render(occupancy: dict[str, str]) -> str:
    """An ASCII board, rank 8 at the top, so orientation errors are obvious."""
    lines = []
    for rank in "87654321":
        cells = (occupancy.get(file + rank, ".") for file in "abcdefgh")
        lines.append(f"{rank}  " + " ".join(cells))
    return "\n".join(lines) + "\n   " + " ".join("abcdefgh")


async def cmd_scan(args: argparse.Namespace) -> int:
    print(f"scanning {args.timeout:g}s for BLE devices...\n")
    devices = await BleakScanner.discover(timeout=args.timeout, return_adv=True)

    if not devices:
        print("nothing found. on macOS, check Bluetooth permission for your")
        print("terminal in System Settings > Privacy & Security > Bluetooth.")
        return 1

    matches = 0
    for address, (device, adv) in sorted(
        devices.items(), key=lambda kv: -(kv[1][1].rssi or -999)
    ):
        name = device.name or adv.local_name or "(unnamed)"
        is_board = name.startswith(protocol.DEVICE_NAME_PREFIX)
        matches += is_board
        marker = " <-- Chessnut" if is_board else ""
        print(f"  {adv.rssi:>4} dBm  {name:<32} {address}{marker}")

    print(f"\n{len(devices)} device(s), {matches} matching "
          f"{protocol.DEVICE_NAME_PREFIX!r}")
    if not matches:
        print("\nno Chessnut found. turn the board on and make sure it is not")
        print("already connected to a phone or a browser -- BLE allows one")
        print("owner at a time.")
        return 1
    return 0


async def cmd_session(args: argparse.Namespace) -> int:
    """Hold one connection open and do every check inside it.

    Exists because of a hardware constraint that shapes the whole appliance: the
    board only advertises for a minute or two after being physically woken, and
    once we disconnect it goes back to sleep. So every disconnect costs a human
    walking over and tapping it. Short-lived diagnostic runs are the wrong shape
    -- connect once, verify everything, stay connected.

    Runs until interrupted. Commands can be fed to the live connection through a
    file (--commands), so the board can be driven without ever disconnecting:

        echo 'leds b1 c3' > /tmp/board_cmd
        echo 'clear'      > /tmp/board_cmd
        echo 'battery'    > /tmp/board_cmd
        echo 'pin 1022853950' > /tmp/board_cmd   # choose a game; survives reboots
        echo 'pin'            > /tmp/board_cmd   # unpin
        echo 'games'          > /tmp/board_cmd   # list games and the pinned one

    On connecting it requests the battery and, unless --no-leds, lights **b1**
    alone and leaves it lit. b1 is chosen deliberately: it is asymmetric under
    every plausible mapping error, so one observation identifies which of them
    (if any) applies. Corner squares are useless for this -- a1 and h8 are 180
    degree rotations of each other, so a fully mirrored layout produces exactly
    the output a correct one does.
    """
    started = time.time()
    last_placement: str | None = None
    frames_seen = 0

    #: What the physically-lit square tells us. The keys are what the user sees.
    LED_VERDICTS = {
        "b1": "correct -- LED mapping matches our square convention",
        "g1": "FILES MIRRORED (a<->h): LED byte bit order is reversed",
        "b8": "RANKS FLIPPED (1<->8): LED byte order is reversed",
        "g8": "ROTATED 180: both bit order and byte order are reversed",
    }

    engine = SyncEngine(store=StateStore(args.state))
    reported_flicker: tuple[str, ...] = ()
    last_reason: str | None = None

    async def on_position(frame: protocol.BoardFrame) -> None:
        """Feed the engine and report what it derived. No logic of its own.

        Everything that used to live here -- debouncing, picking a game,
        reconciling -- is now the engine's, so the script cannot drift from the
        service. This is a view over the engine, nothing more.
        """
        nonlocal last_placement, frames_seen, reported_flicker, last_reason
        frames_seen += 1

        before = engine.snapshot.evaluated_at
        snapshot = engine.on_frame(frame.occupancy)

        # Flicker is announced as soon as it appears, because a square that will
        # not hold still stops the board ever settling -- the bridge then fails
        # safe but also fails to do anything, and the human needs to know why.
        flickering = engine.settle_status.flickering
        if flickering != reported_flicker:
            reported_flicker = flickering
            if flickering:
                # Ordered by what actually turned out to be the cause the first
                # time this fired, which was not a broken board: magnets left on
                # the same table biased the squares' power-on baseline.
                print(f"\n!!! UNRELIABLE SQUARE(S): {', '.join(flickering)} "
                      f"-- changing with nobody touching the board. check for "
                      f"magnets near the board, then power-cycle to re-baseline; "
                      f"failing that reseat the piece, then suspect a weak piece "
                      f"magnet, then the sensor", flush=True)

        if snapshot.evaluated_at == before:
            if frame.placement != last_placement:
                last_placement = frame.placement
                print(f"  ...board moving ({frame.placement})", flush=True)
            return

        last_placement = frame.placement
        note = "  [starting position]" if frame.is_starting_position else ""
        print(f"\n=== SETTLED at {time.time() - started:6.1f}s  tick={frame.tick}{note}",
              flush=True)
        print(render(frame.occupancy), flush=True)
        print(f"placement: {frame.placement}", flush=True)
        await report(snapshot)

    async def report(snapshot) -> None:
        """Print what the engine derived, and drive the LEDs from it."""
        nonlocal last_reason
        if snapshot.game is None:
            if snapshot.reason != last_reason:
                last_reason = snapshot.reason
                print(f"  {snapshot.reason}", flush=True)
                for choice in engine.choices():
                    hint = "  <-- matches the board" if choice.matches_board else ""
                    turn = "YOUR MOVE" if choice.game.is_my_turn else "their move"
                    print(f"    pin {choice.game.id}  {turn}{hint}", flush=True)
            return

        last_reason = None
        game, result = snapshot.game, snapshot.reconciliation
        print(f"game {game.id} ({game.url})", flush=True)
        print(f"  you are {game.my_color}, turn={game.turn}, "
              f"your move={game.is_my_turn}", flush=True)
        if snapshot.awaiting_setup:
            print(f"  SETUP NEEDED: {snapshot.reason}", flush=True)
        print(f"  STATE: {result.state.value}   submittable={result.is_submittable}",
              flush=True)
        if result.move:
            print(f"  move: {result.move}", flush=True)
        if result.lifted or result.added or result.changed:
            print(f"  lifted={result.lifted} added={result.added} "
                  f"changed={result.changed}", flush=True)

        # Nothing is sent anywhere yet -- there is no write path. Taking the
        # submission here proves the gate works and that it fires exactly once.
        submission = engine.take_submission()
        if submission is not None:
            print(f"  >>> WOULD SUBMIT {submission.move} to game "
                  f"{submission.game_id} (no write path yet)", flush=True)

        squares = snapshot.setup_guidance or result.squares_to_highlight
        if squares:
            await board.set_leds(squares)

    async def on_battery(battery: protocol.Battery) -> None:
        state = "charging" if battery.charging else "on battery"
        print(f"battery: {battery.percent}% ({state})", flush=True)

    last_status_line: str | None = None
    was_connected = False

    async def on_status(status: BoardStatus) -> None:
        # A reconnect must re-establish the position from scratch. The settler
        # emits edges, so carrying a pre-disconnect position across the gap makes
        # an unchanged board indistinguishable from no board at all -- observed
        # live: after an automatic reconnect nothing was reported, because the
        # position had not changed since before the drop. This is the same hazard
        # as a Pi reboot, where the appliance must not assume anything it knew.
        nonlocal was_connected
        if status.is_connected != was_connected:
            was_connected = status.is_connected
            engine.set_board_connected(status.is_connected)
            if status.is_connected:
                print("(reconnected -- re-reading the board from scratch)",
                      flush=True)

        # Deduplicated on purpose. Status is republished on every frame, and a
        # board reporting a few times a second produced hundreds of identical
        # lines that buried the events worth reading.
        nonlocal last_status_line
        line = f"[{status.state.value}]"
        if status.device_name:
            line += f" {status.device_name}"
        if status.mtu is not None:
            line += f" MTU={status.mtu}"
        if status.rejected_frames or status.truncated_frames:
            line += (
                f" rejected={status.rejected_frames}"
                f" truncated={status.truncated_frames}"
            )
        if status.last_error:
            line += f" err={status.last_error}"
        if line == last_status_line:
            return
        last_status_line = line
        print(line, flush=True)

    board = ChessnutBoard(
        on_position=on_position,
        on_battery=on_battery,
        on_status=on_status,
        scan_timeout=args.timeout,
    )
    task = asyncio.create_task(board.run())

    async def await_connection() -> bool:
        while not board.status.is_connected:
            if task.done():
                return False
            await asyncio.sleep(0.5)
        return True

    async def led_check() -> None:
        """Light one asymmetric square and leave it lit -- no timing to misread."""
        if not await await_connection():
            return
        await asyncio.sleep(2)
        if not await board.set_leds(["b1"]):
            print(">>> LED write failed", flush=True)
            return
        print("\n>>> LED CHECK: b1 is now lit, and stays lit.", flush=True)
        print(">>> Which square is physically lit? Answer decides the mapping:",
              flush=True)
        for square, verdict in LED_VERDICTS.items():
            print(f">>>   {square} -> {verdict}", flush=True)
        print(">>> (b1 = second file from the left, on the bottom rank as White "
              "sits)\n", flush=True)

    async def command_loop() -> None:
        """Drive the live board from a file, so no check costs a reconnection."""
        path = Path(args.commands)
        if not await await_connection():
            return
        while True:
            await asyncio.sleep(0.3)
            try:
                if not path.exists():
                    continue
                text = path.read_text().strip()
                path.write_text("")
            except OSError:
                continue
            for line in filter(None, (ln.strip() for ln in text.splitlines())):
                verb, *rest = line.split()
                print(f"\n>>> command: {line}", flush=True)
                if verb == "leds":
                    ok = await board.set_leds(rest)
                    print(f">>> lit {rest}: {'ok' if ok else 'FAILED'}", flush=True)
                elif verb == "clear":
                    print(f">>> cleared: {await board.clear_leds()}", flush=True)
                elif verb == "pin":
                    await report(engine.select_game(rest[0] if rest else None))
                elif verb == "games":
                    await report(engine.snapshot)
                elif verb == "battery":
                    await board.request_battery()
                elif verb == "quit":
                    board.stop()
                    return
                else:
                    print(f">>> unknown command {verb!r}", flush=True)

    async def poll_games() -> None:
        """Keep the live game list fresh so reconciliation has a real FEN."""
        if not args.username:
            return

        async def remember(fetched: list[DailyGame]) -> None:
            mine = [g.id for g in fetched if g.is_my_turn]
            print(f"chess.com: {len(fetched)} game(s); your move in "
                  f"{mine or 'none'}", flush=True)
            await report(engine.observe_games(fetched))

        await watch_games(
            PublicClient(args.username), remember, interval=args.poll_seconds
        )

    helpers = [
        asyncio.create_task(command_loop()),
        asyncio.create_task(poll_games()),
    ]
    if not args.no_leds:
        helpers.append(asyncio.create_task(led_check()))
    print(f"holding the connection open; commands via {args.commands}; "
          "ctrl-c to release the board.\n", flush=True)
    try:
        await task
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        for helper in helpers:
            helper.cancel()
        board.stop()
        task.cancel()

    print(f"\n--- summary ---\nframes: {frames_seen}  "
          f"rejected: {board.status.rejected_frames}  "
          f"truncated: {board.status.truncated_frames}", flush=True)
    return 0 if frames_seen and not board.status.rejected_frames else 1


async def cmd_watch(args: argparse.Namespace) -> int:
    started = time.time()
    last_placement: str | None = None

    async def on_position(frame: protocol.BoardFrame) -> None:
        nonlocal last_placement
        if frame.placement == last_placement:
            return
        last_placement = frame.placement
        note = "  [starting position]" if frame.is_starting_position else ""
        print(f"\n--- {time.time() - started:6.1f}s  tick={frame.tick}{note}")
        print(render(frame.occupancy))
        print(f"placement: {frame.placement}")

    async def on_battery(battery: protocol.Battery) -> None:
        state = "charging" if battery.charging else "on battery"
        print(f"battery: {battery.percent}% ({state})")

    last_status_line: str | None = None

    async def on_status(status: BoardStatus) -> None:
        # Status fires on every frame, so an undeduplicated print emits ~10
        # identical lines a second and buries the board diagrams this command
        # exists to show. A first run printed the connection banner 576 times.
        # Deduplicating by text still shows every real transition, because a
        # reconnect or a rejected frame changes the line.
        nonlocal last_status_line
        line = f"[{status.state.value}]"
        if status.device_name:
            line += f" {status.device_name}"
        if status.is_connected and status.mtu is None:
            line += " MTU=unreported"
        elif status.mtu is not None:
            verdict = "ok" if status.mtu >= MIN_USABLE_MTU else "TOO SMALL"
            line += f" MTU={status.mtu} ({verdict}, need >={MIN_USABLE_MTU})"
        if status.truncated_frames:
            line += f" TRUNCATED={status.truncated_frames}"
        if status.rejected_frames:
            line += f" REJECTED={status.rejected_frames}"
        if status.last_error:
            line += f" err={status.last_error}"
        if line == last_status_line:
            return
        last_status_line = line
        print(line)

    board = ChessnutBoard(
        on_position=on_position,
        on_battery=on_battery,
        on_status=on_status,
        scan_timeout=args.timeout,
    )

    limit = f" for {args.seconds:g}s" if args.seconds else ""
    print(f"connecting{limit}; move a piece to see updates. ctrl-c to stop.\n")
    task = asyncio.create_task(board.run())
    try:
        if args.seconds:
            await asyncio.wait_for(asyncio.shield(task), timeout=args.seconds)
        else:
            await task
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        board.stop()
        task.cancel()

    status = board.status
    print("\n--- summary ---")
    print(f"frames received : {status.frames}")
    print(f"frames truncated: {status.truncated_frames}")
    print(f"frames rejected : {status.rejected_frames}")
    # "unreported" rather than a number, because bleak on BlueZ hands back the
    # 23-byte BLE default when it simply has not read the MTU. Printing that as
    # if it were measured invites exactly the wrong conclusion while frames are
    # arriving whole.
    print(f"MTU             : {status.mtu if status.mtu is not None else 'unreported'}")

    # Truncation and rejection are reported separately and worded differently.
    # An earlier version blamed the MTU for every unparseable frame, which sent
    # a real debugging session after the wrong cause: the board's frames were
    # longer than expected, not cut short, and the MTU was never the problem.
    if status.truncated_frames:
        print("\nFAIL: frames arrived shorter than they declare, so positions")
        print("cannot be trusted. the ATT MTU is the usual cause -- this is the")
        got = status.mtu if status.mtu is not None else "unreported"
        print(f"BlueZ/Linux failure mode; need >={MIN_USABLE_MTU}, got {got}.")
        return 1
    if status.rejected_frames:
        print("\nFAIL: frames arrived but could not be decoded, and the MTU is")
        print("not the reason. re-run with -v to log the raw bytes; this board")
        print("may speak a variant the parser does not handle yet.")
        print(f"last error: {status.last_error}")
        return 1
    if status.frames:
        print("\nPASS: frames decoded.")
        return 0
    print("\nno frames received.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="list nearby BLE devices")
    scan.add_argument("--timeout", type=float, default=8.0)
    scan.set_defaults(func=cmd_scan)

    session = sub.add_parser(
        "session", help="hold one connection open and run every check inside it"
    )
    session.add_argument("--timeout", type=float, default=20.0)
    session.add_argument(
        "--commands",
        default="/tmp/board_cmd",
        help=("file polled for commands: leds <squares> | clear | battery | "
              "pin [game_id] | games | quit"),
    )
    session.add_argument("--no-leds", action="store_true")
    session.add_argument(
        "--username",
        default="nbaronmorgan",
        help="chess.com account to reconcile against ('' to disable)",
    )
    session.add_argument("--poll-seconds", type=float, default=120.0)
    session.add_argument(
        "--state",
        default=str(Path.home() / ".chessnut-bridge" / "state.json"),
        help="where the pinned game is remembered across restarts",
    )
    session.set_defaults(func=cmd_session)

    watch = sub.add_parser("watch", help="connect and stream positions")
    watch.add_argument("--timeout", type=float, default=15.0)
    watch.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="stop after this long and print a summary (0 = run until ctrl-c)",
    )
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
