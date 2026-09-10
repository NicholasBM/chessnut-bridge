"""Tests for the process itself.

Wiring is usually not worth testing, but three properties of this wiring are, and
all three are invisible from inside any single module:

* **A browser stays signed in across a restart**, because the cookie key is
  derived from the device key on disk rather than generated per process.
* **A restart does not silently un-protect anything**: a fault in local storage
  produces the problem page, not a working UI and not a crash.
* **The page outlives the bridge loop.** On a machine with no screen the page is
  the only diagnostic, so a loop that dies must not take it down.
"""

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from asgi_client import request  # noqa: E402

from bridge import main  # noqa: E402
from bridge.state.store import StateStore  # noqa: E402
from bridge.web import auth  # noqa: E402
from bridge.web.config import Config  # noqa: E402

PASSWORD = "a-long-enough-password"


@pytest.fixture(autouse=True)
def brisk_shutdown(monkeypatch):
    """Don't spend the real shutdown grace in tests that are not about it.

    Tests that drive ``main()`` end up with a real board whose run loop is inside a
    10s BLE scan, and ``stop()`` cannot interrupt a scan already in flight. That is
    acceptable on the appliance -- systemd allows 90s, and there is no connection to
    tear down mid-scan anyway -- but it added 5s to each of three tests that only
    care about argument handling.

    The shipped default is asserted separately, so shortening it here cannot hide a
    value too small to carry a disconnect.
    """
    monkeypatch.setattr(main, "SHUTDOWN_GRACE_SECONDS", 0.2)


#: Read before the fixture above can replace it.
SHIPPED_SHUTDOWN_GRACE = main.SHUTDOWN_GRACE_SECONDS


def test_the_shipped_shutdown_grace_leaves_room_for_a_disconnect():
    """Guards the constant the fixture above replaces."""
    assert SHIPPED_SHUTDOWN_GRACE >= 2.0


@pytest.fixture
def on_disk(tmp_path, monkeypatch):
    """Point the state directory and key file somewhere writable."""
    monkeypatch.setenv(main.STATE_DIR_ENV_VAR, str(tmp_path / "state"))
    monkeypatch.setenv(main.KEY_PATH_ENV_VAR, str(tmp_path / "keys" / "session.key"))
    return tmp_path


def a_config(**overrides) -> Config:
    values = {
        "chesscom_username": "nbaronmorgan",
        "web_password": PASSWORD,
        "source": Path("/boot/firmware/chessnut-bridge.conf"),
    }
    values.update(overrides)
    return Config(**values)


# --- what gets built ------------------------------------------------------


def test_a_usable_config_yields_a_service_wired_to_disk(on_disk):
    app, service = main.build(a_config(poll_seconds=42.0))
    assert app is not None
    assert service is not None
    assert service.username == "nbaronmorgan"
    assert service.poll_interval == 42.0
    assert isinstance(service.state_store, StateStore)


def test_the_key_lives_outside_the_state_directory(on_disk):
    """The point of the split: the file people copy is the useless one."""
    main.build(a_config())
    state_dir = Path(str(on_disk / "state"))
    key = Path(str(on_disk / "keys" / "session.key"))
    assert key.is_file()
    assert key.parent != state_dir
    assert not list(state_dir.glob("*.key"))


def test_an_unusable_config_yields_no_service_and_explains_itself(on_disk):
    app, service = main.build(Config(problems=("web_password is not set",)))
    assert service is None
    reply = asyncio.run(request(app, "/"))
    assert reply.status == 503
    assert "web_password" in reply.text


def test_an_unusable_config_touches_no_local_storage(on_disk):
    """Nothing should be created on the way to refusing."""
    main.build(Config(problems=("no configuration file found",)))
    assert not (on_disk / "keys" / "session.key").exists()


def test_unwritable_storage_becomes_a_problem_page_not_a_crash(tmp_path, monkeypatch):
    """A permission fault on the appliance has to be *readable* by someone with no
    shell, so it joins the list on the page rather than only the journal."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("this is a file, so mkdir underneath it fails")
    monkeypatch.setenv(main.STATE_DIR_ENV_VAR, str(blocked / "state"))
    monkeypatch.setenv(main.KEY_PATH_ENV_VAR, str(blocked / "session.key"))

    app, service = main.build(a_config())
    assert service is None
    reply = asyncio.run(request(app, "/"))
    assert reply.status == 503
    assert "state" in reply.text


# --- staying signed in ----------------------------------------------------


def test_a_cookie_survives_a_restart(on_disk):
    """Otherwise every power cut costs the owner a trip to find the password on
    the SD card, and the whole appliance is meant to be untouched for months."""
    first, _ = main.build(a_config())
    reply = asyncio.run(
        request(first, "/login", method="POST", form={"password": PASSWORD})
    )
    token = reply.cookies[auth.COOKIE_NAME]

    second, _ = main.build(a_config())  # a new process, same disk
    follow = asyncio.run(request(second, "/", cookies={auth.COOKIE_NAME: token}))
    assert follow.status == 200


def test_a_cookie_does_not_survive_replacing_the_key(on_disk, monkeypatch):
    """Replacing the key discards the stored chess.com session too, so treating
    old cookies as valid would leave a browser signed in to an appliance that has
    forgotten everything else."""
    first, _ = main.build(a_config())
    reply = asyncio.run(
        request(first, "/login", method="POST", form={"password": PASSWORD})
    )
    token = reply.cookies[auth.COOKIE_NAME]

    monkeypatch.setenv(main.KEY_PATH_ENV_VAR, str(on_disk / "keys" / "other.key"))
    second, _ = main.build(a_config())
    follow = asyncio.run(request(second, "/", cookies={auth.COOKIE_NAME: token}))
    assert follow.status == 303


# --- the page outlives the bridge ----------------------------------------


class FailingService:
    def __init__(self, error=None):
        self.error = error or RuntimeError("bluetooth is not available")
        self.stopped = False

    async def run(self):
        raise self.error

    def stop(self):
        self.stopped = True


class EndlessService(FailingService):
    """Ignores stop(), so it can only be ended by cancellation."""

    async def run(self):
        await asyncio.Event().wait()


class TidyService(FailingService):
    """Stops when asked, as the real service does, and has teardown to do.

    ``hung_up`` stands for the BLE disconnect: work on the way out that only
    happens if the graceful path is actually allowed to run.
    """

    def __init__(self):
        super().__init__()
        self._done = asyncio.Event()
        self.cancelled = False
        self.hung_up = False

    async def run(self):
        try:
            await self._done.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.hung_up = True

    def stop(self):
        self.stopped = True
        self._done.set()


def test_a_bridge_loop_that_dies_is_logged_and_not_reraised(caplog):
    with caplog.at_level(logging.ERROR):
        asyncio.run(main._run_bridge(FailingService()))
    assert "bluetooth is not available" in caplog.text
    assert "still being served" in caplog.text


def test_a_cancelled_bridge_loop_stays_cancelled():
    """Shutdown must not be mistaken for a fault and logged as one."""

    async def scenario():
        task = asyncio.create_task(main._run_bridge(EndlessService()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


class FakeServer:
    """Stands in for uvicorn: serves briefly, then returns as a signal would."""

    instances: list["FakeServer"] = []

    def __init__(self, config):
        self.config = config
        FakeServer.instances.append(self)

    async def serve(self):
        await asyncio.sleep(0.01)


@pytest.fixture
def fake_uvicorn(monkeypatch):
    FakeServer.instances = []
    monkeypatch.setattr(main.uvicorn, "Server", FakeServer)
    return FakeServer


def test_the_server_going_away_stops_the_bridge(fake_uvicorn):
    """Not the other way round: the server decides when the process is done."""
    service = TidyService()
    assert asyncio.run(main.serve(app=object(), service=service, port=8080)) == 0
    assert service.stopped


def test_a_bridge_that_stops_when_asked_is_never_cancelled(fake_uvicorn):
    """The regression that wedged the board on 2026-09-09.

    stop() only sets an event; cancelling in the next breath never yields to the
    loop, so the teardown did not run once. bleak's disconnect was awaited inside
    an already-cancelled task and never reached BlueZ, which held the LE link open
    after the process died -- and a connected board cannot advertise, so the next
    run could not find it. Every restart wedged the board.
    """
    service = TidyService()
    asyncio.run(main.serve(app=object(), service=service, port=8080))

    assert service.hung_up, "teardown never ran; the board is left connected"
    assert not service.cancelled


def test_a_teardown_that_overruns_is_still_cancelled(fake_uvicorn, monkeypatch, caplog):
    """A hung BLE stack must not gain the power to block shutdown."""
    monkeypatch.setattr(main, "SHUTDOWN_GRACE_SECONDS", 0.01)
    service = EndlessService()

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(main.serve(app=object(), service=service, port=8080)) == 0

    assert "did not stop" in caplog.text


def test_a_failing_bridge_does_not_end_the_server(fake_uvicorn, caplog):
    service = FailingService()
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(main.serve(app=object(), service=service, port=8080)) == 0
    assert "still being served" in caplog.text


def test_no_service_is_a_valid_way_to_run(fake_uvicorn):
    """The misconfigured appliance still has to answer on the port."""
    assert asyncio.run(main.serve(app=object(), service=None, port=8080)) == 0


def test_the_port_and_address_come_from_the_configuration(fake_uvicorn):
    asyncio.run(main.serve(app=object(), service=None, port=8123))
    config = fake_uvicorn.instances[0].config
    assert config.port == 8123
    assert config.host == "0.0.0.0"
    assert not config.access_log, "an event stream would flood the journal"


# --- the command line -----------------------------------------------------


def test_a_named_config_file_is_read_instead_of_the_boot_partition(
    on_disk, fake_uvicorn, tmp_path
):
    path = tmp_path / "test.conf"
    path.write_text(f"chesscom_username=nbaronmorgan\nweb_password={PASSWORD}\n")
    assert main.main(["--config", str(path), "--port", "8123"]) == 0
    assert fake_uvicorn.instances[0].config.port == 8123


def test_a_missing_config_file_still_starts_and_serves_the_problem(
    on_disk, fake_uvicorn, tmp_path, caplog
):
    """Exiting here would leave systemd restart-looping, which from the owner's
    side is indistinguishable from a dead appliance."""
    with caplog.at_level(logging.ERROR):
        assert main.main(["--config", str(tmp_path / "absent.conf")]) == 0
    assert "problem page" in caplog.text
    assert fake_uvicorn.instances, "it still bound the port"


def test_the_configured_port_is_used_when_the_flag_is_absent(
    on_disk, fake_uvicorn, tmp_path
):
    path = tmp_path / "test.conf"
    path.write_text(
        f"chesscom_username=nbaronmorgan\nweb_password={PASSWORD}\nweb_port=8200\n"
    )
    assert main.main(["--config", str(path)]) == 0
    assert fake_uvicorn.instances[0].config.port == 8200


def test_a_refused_port_is_explained_rather_than_traced(
    on_disk, monkeypatch, tmp_path, caplog
):
    """Port 80 without the capability is the overwhelmingly likely cause, and the
    fix is one line in a unit file -- so say it."""

    class Refusing(FakeServer):
        async def serve(self):
            raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(main.uvicorn, "Server", Refusing)
    path = tmp_path / "test.conf"
    path.write_text(f"chesscom_username=nbaronmorgan\nweb_password={PASSWORD}\n")
    with caplog.at_level(logging.ERROR):
        assert main.main(["--config", str(path)]) == 1
    assert "CAP_NET_BIND_SERVICE" in caplog.text
