"""The process: read the card, serve the page, run the bridge.

Everything here is wiring, but three of the choices are load-bearing on an
appliance with no screen and no keyboard.

**The page outlives the bridge.** The web server and the bridge loop run as
independent tasks, and the server is the one that decides when the process ends.
If the BLE loop or the poll loop dies, the page keeps being served and reports
what it can see -- a board that is not connected, a read that happened longer ago
than it should have. A process that exited on the way down would take with it the
only means anyone has of finding out why.

**A misconfigured appliance still binds the port.** When the boot partition has no
usable settings we serve the list of problems on the same address, rather than
refusing to start. Systemd would restart-loop a unit that exits, and from the
owner's side a restart loop and a wiring fault look identical: nothing answers.

**One secret, two uses, kept apart on disk.** The device key encrypts the stored
chess.com session and, through a domain-separated derivation, signs the web
cookie. It lives in ``/etc`` while the data it protects lives in ``/var/lib``,
which is the arrangement :mod:`bridge.state.secrets` asks for: the likely accident
is copying the data file, and a copy is useless without a file nobody thought to
take. A consequence worth knowing: replace the key and every browser has to sign
in again, which is the correct outcome rather than a bug.

Port 80 is the default because the whole point is that ``http://<address>/``
works with nothing appended. That needs ``CAP_NET_BIND_SERVICE`` from the systemd
unit, not root -- and if it is missing this exits loudly rather than quietly
moving to another port, because an appliance listening somewhere the owner will
not look is worse than one that is plainly broken.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

import uvicorn

from .chesscom import public
from .service import BridgeService
from .state.secrets import (
    CredentialStore,
    DeviceKey,
    SecretsUnavailable,
    SessionStore,
)
from .state.store import StateStore
from .web.app import create_app, create_misconfigured_app
from .web.config import Config, load_config

log = logging.getLogger("bridge")

#: Data the appliance rewrites as it runs. Survives reboots; expendable.
DEFAULT_STATE_DIR = Path("/var/lib/chessnut-bridge")

#: The device key, deliberately not in the state directory. See the module note.
DEFAULT_KEY_PATH = Path("/etc/chessnut-bridge/session.key")

STATE_DIR_ENV_VAR = "CHESSNUT_BRIDGE_STATE_DIR"
KEY_PATH_ENV_VAR = "CHESSNUT_BRIDGE_KEY"


def _paths() -> tuple[Path, Path]:
    """The state directory and key file, environment overrides included.

    The overrides exist so this can be run from a checkout, on a laptop, without
    write access to ``/var/lib`` -- which is how it gets exercised at all before
    there is an image to flash.
    """
    state_dir = Path(os.environ.get(STATE_DIR_ENV_VAR) or DEFAULT_STATE_DIR)
    key_path = Path(os.environ.get(KEY_PATH_ENV_VAR) or DEFAULT_KEY_PATH)
    return state_dir, key_path


def build(config: Config) -> tuple[object, BridgeService | None]:
    """Turn settings into an app and, if it can run, a service to go with it.

    Returns ``(app, None)`` when the appliance cannot run safely. Every reason for
    that lands in ``config.problems`` first, so the page explains itself rather
    than the journal being the only record.
    """
    if not config.is_usable:
        return create_misconfigured_app(config), None

    state_dir, key_path = _paths()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        key_material = DeviceKey(key_path).key()
    except (OSError, SecretsUnavailable) as exc:
        # No key means no encrypted session store and no signed cookie, so there
        # is no safe reduced mode to fall back to. Reported as a problem because
        # the fix is a filesystem permission on the appliance, and the owner
        # cannot read a log.
        log.error("cannot prepare local storage: %s", exc)
        return (
            create_misconfigured_app(
                replace(
                    config,
                    problems=config.problems
                    + (f"cannot write to {state_dir} or read {key_path}: {exc}",),
                )
            ),
            None,
        )

    assert config.chesscom_username  # is_usable guarantees it
    service = BridgeService(
        username=config.chesscom_username,
        session_store=SessionStore(state_dir / "session.enc", DeviceKey(key_path)),
        # Its own file, encrypted with the same device key: the owner can delete
        # this one alone to stop the appliance signing itself in, without losing
        # the session it already has.
        credential_store=CredentialStore(
            state_dir / "credentials.enc", DeviceKey(key_path)
        ),
        state_store=StateStore(state_dir / "state.json"),
        poll_interval=config.poll_seconds or public.DEFAULT_POLL_SECONDS,
    )
    app = create_app(service, config, signing_key=key_material)
    return app, service


async def serve(app: object, service: BridgeService | None, port: int) -> int:
    """Run the server, and the bridge alongside it if there is one."""
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="0.0.0.0",  # noqa: S104 -- an appliance the owner reaches by IP
            port=port,
            log_config=None,
            # The Zero 2 W has four slow cores and one reader. Access logs on a
            # once-a-second event stream would be the noisiest thing in the
            # journal and tell nobody anything.
            access_log=False,
        )
    )

    bridge: asyncio.Task | None = None
    if service is not None:
        bridge = asyncio.create_task(_run_bridge(service), name="bridge")

    # uvicorn installs its own SIGINT/SIGTERM handlers and sets should_exit, so
    # the shutdown path is: signal -> serve() returns -> we stop the bridge.
    try:
        await server.serve()
    finally:
        if service is not None:
            service.stop()
        if bridge is not None:
            await _stop_bridge(bridge)
    return 0


#: Long enough for BlueZ to carry a disconnect to the board, short enough that
#: systemd's default 90s stop timeout is never the thing that ends us.
SHUTDOWN_GRACE_SECONDS = 5.0


async def _stop_bridge(bridge: asyncio.Task) -> None:
    """Let the bridge finish its own teardown, and only then insist.

    ``service.stop()`` sets an event; the loop acts on it the next time it is
    scheduled. Cancelling in the same breath -- which this did until 2026-09-09 --
    never yields to the loop, so the graceful path did not run even once. The cost
    was a real deadlock: bleak's disconnect was awaited inside an already-cancelled
    task and never reached BlueZ, which kept the LE link open after the process
    died. A connected board cannot advertise, so the next run could not find it.
    Every restart wedged the board until someone intervened.

    The timeout is the point: waiting forever would hand a hung BLE stack the power
    to block shutdown, so a teardown that overruns still gets cancelled.
    """
    done, _ = await asyncio.wait({bridge}, timeout=SHUTDOWN_GRACE_SECONDS)
    if not done:
        log.warning(
            "bridge did not stop within %.0fs; cancelling it. The board may be "
            "left connected at the BlueZ level, which the next run will hang up.",
            SHUTDOWN_GRACE_SECONDS,
        )
        bridge.cancel()
    await asyncio.gather(bridge, return_exceptions=True)


async def _run_bridge(service: BridgeService) -> None:
    """The bridge loop, whose failure must not take the page down with it."""
    try:
        await service.run()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 -- deliberately the end of the line
        # Logged with a traceback and then left alone. Not restarted here: a loop
        # that failed once at startup will fail again immediately, and a restart
        # loop inside the process would bury the traceback that explains it. What
        # the owner sees on the page is a board that never connects and a read
        # that keeps getting older, which is the honest description.
        log.exception("the bridge loop stopped; the page is still being served")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chessnut to Chess.com bridge.")
    parser.add_argument(
        "--config",
        type=Path,
        help="configuration file to read instead of searching the boot partition",
    )
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument(
        "--verbose", action="store_true", help="log at DEBUG instead of INFO"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        # No timestamp: the journal adds one, and this only ever runs under
        # systemd or in a terminal that has its own.
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config((args.config,) if args.config else None)
    port = args.port or config.web_port
    app, service = build(config)
    if service is None:
        log.error("serving the problem page on port %s and nothing else", port)
    else:
        log.info("serving on port %s as %s", port, config.chesscom_username)

    try:
        return asyncio.run(serve(app, service, port))
    except PermissionError:
        # Overwhelmingly port 80 without CAP_NET_BIND_SERVICE.
        log.error(
            "not allowed to listen on port %s. Either give the unit "
            "AmbientCapabilities=CAP_NET_BIND_SERVICE or set web_port= in the "
            "configuration file",
            port,
        )
        return 1
    except OSError as exc:
        log.error("cannot listen on port %s: %s", port, exc)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
