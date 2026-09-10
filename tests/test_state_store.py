"""Tests for durable state, focused on the two ways an appliance loses power.

A bridge that sits powered on indefinitely will eventually be unplugged mid-write.
So the two behaviours worth defending are: a half-written file must be impossible,
and an unreadable file must not stop the thing booting.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.state.store import (  # noqa: E402
    STATE_VERSION,
    BridgeState,
    InMemoryStore,
    StateStore,
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


def test_a_saved_selection_comes_back(path):
    StateStore(path).save(BridgeState(selected_game_id="1022853950"))
    assert StateStore(path).load().selected_game_id == "1022853950"


def test_nothing_saved_yet_is_not_an_error(path):
    assert StateStore(path).load() == BridgeState()
    assert not path.exists(), "reading must not create the file"


def test_the_directory_is_created_on_demand(tmp_path):
    nested = tmp_path / "var" / "lib" / "bridge" / "state.json"
    StateStore(nested).save(BridgeState(selected_game_id="1"))
    assert StateStore(nested).load().selected_game_id == "1"


def test_unpinning_persists_as_none_not_as_absence(path):
    store = StateStore(path)
    store.save(BridgeState(selected_game_id="1022853950"))
    store.save(BridgeState(selected_game_id=None))
    assert StateStore(path).load().selected_game_id is None


# --- surviving a bad file --------------------------------------------------


def test_a_corrupt_file_reads_as_empty_rather_than_crashing(path, caplog):
    """A pinned game is a convenience; refusing to boot over it is not.

    Turning a trivial fault into a dead appliance would be much worse than
    forgetting which game was selected.
    """
    path.write_text("{ this is not json")
    with caplog.at_level("ERROR"):
        assert StateStore(path).load() == BridgeState()
    assert "unreadable" in caplog.text


def test_a_corrupt_file_is_kept_for_inspection(path):
    path.write_text("{ truncated")
    StateStore(path).load()
    assert path.read_text() == "{ truncated", "must not silently delete evidence"


def test_a_file_that_is_not_an_object_is_ignored(path):
    path.write_text("[1, 2, 3]")
    assert StateStore(path).load() == BridgeState()


def test_an_unknown_version_is_refused_not_guessed_at(path, caplog):
    """A downgrade must not reinterpret a newer file's fields."""
    path.write_text(json.dumps({"version": STATE_VERSION + 1, "selected_game_id": "9"}))
    with caplog.at_level("ERROR"):
        assert StateStore(path).load().selected_game_id is None
    assert "version" in caplog.text


def test_a_missing_version_is_refused(path):
    path.write_text(json.dumps({"selected_game_id": "9"}))
    assert StateStore(path).load().selected_game_id is None


def test_a_wrongly_typed_game_id_is_ignored(path):
    path.write_text(json.dumps({"version": STATE_VERSION, "selected_game_id": 1234}))
    assert StateStore(path).load().selected_game_id is None


# --- atomicity ------------------------------------------------------------


def test_a_failed_write_leaves_the_previous_state_intact(path, monkeypatch):
    """The point of the temp-file dance: no half-written state is ever visible."""
    store = StateStore(path)
    store.save(BridgeState(selected_game_id="good"))

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", explode)
    with pytest.raises(OSError):
        store.save(BridgeState(selected_game_id="bad"))

    assert StateStore(path).load().selected_game_id == "good"


def test_a_failed_write_leaves_no_temporary_files_behind(path, monkeypatch):
    store = StateStore(path)
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.save(BridgeState(selected_game_id="x"))
    assert list(path.parent.iterdir()) == [], "a stray temp file per crash would pile up"


def test_save_failures_are_not_swallowed(path, monkeypatch):
    """Unlike load. Silently failing to persist would go unnoticed forever."""
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        StateStore(path).save(BridgeState(selected_game_id="x"))


def test_the_written_file_is_readable_json(path):
    """So a human debugging the appliance over SSH can just cat it."""
    StateStore(path).save(BridgeState(selected_game_id="1022853950"))
    written = json.loads(path.read_text())
    assert written == {"version": STATE_VERSION, "selected_game_id": "1022853950"}


# --- the test double ------------------------------------------------------


def test_the_in_memory_store_round_trips():
    store = InMemoryStore()
    assert store.load() == BridgeState()
    store.save(BridgeState(selected_game_id="7"))
    assert store.load().selected_game_id == "7"
