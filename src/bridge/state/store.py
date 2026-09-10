"""Durable state that must survive a reboot, written crash-safely.

Only one thing lives here so far: which game the player pinned. That is enough to
justify a real store rather than a variable, because the whole point of the choice
is that a power cut must not silently un-pick your game -- if it did, the
appliance would come back up with no game selected, or worse, guess one.

Deliberately a small JSON file rather than SQLite. SQLite is still the right
answer for the move log and game history that come later (concurrent readers,
range queries), but for a handful of scalar settings it buys nothing over an
atomic file write and costs a schema. When the move log arrives this module keeps
its interface and grows a SQLite backend behind it.

Two failure modes are handled deliberately, because both happen on an appliance
that loses power mid-write:

* A **truncated or corrupt** file must not stop the bridge booting. A pinned game
  is a convenience; refusing to start because we cannot read it would turn a
  trivial fault into a dead appliance. So a corrupt file is logged loudly and
  treated as empty, and the original is kept for inspection rather than deleted.
* A **partially written** file must be impossible in the first place, which is
  why writes go to a temporary file, get fsynced, and are then renamed over the
  target. Rename within a directory is atomic, so a reader sees either the whole
  old file or the whole new one.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Bumped only for changes a previous version could not read. An *unknown*
#: version is treated as unreadable rather than guessed at, so a downgrade cannot
#: silently reinterpret a newer file's fields.
STATE_VERSION = 1


@dataclass(frozen=True)
class BridgeState:
    """Everything that must outlive the process."""

    #: The game the player pinned, kept until that game finishes. None means
    #: "nothing selected", which is a real state the UI must handle -- never a
    #: cue to pick something automatically.
    selected_game_id: str | None = None


class StateStore:
    """Loads and atomically saves :class:`BridgeState`."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> BridgeState:
        """Read the state. Never raises -- an unreadable file reads as empty."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return BridgeState()
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.error(
                "state file %s is unreadable (%s); starting with no pinned game. "
                "the file is left in place for inspection",
                self.path,
                exc,
            )
            return BridgeState()

        if not isinstance(raw, dict):
            log.error("state file %s is not an object; ignoring", self.path)
            return BridgeState()

        version = raw.get("version")
        if version != STATE_VERSION:
            log.error(
                "state file %s has version %r, expected %d; ignoring its contents "
                "rather than guessing at them",
                self.path,
                version,
                STATE_VERSION,
            )
            return BridgeState()

        game_id = raw.get("selected_game_id")
        if game_id is not None and not isinstance(game_id, str):
            log.error("selected_game_id in %s is not a string; ignoring", self.path)
            game_id = None
        return BridgeState(selected_game_id=game_id)

    def save(self, state: BridgeState) -> None:
        """Write atomically: temp file, fsync, rename. Raises on a real IO error.

        Unlike ``load``, this does not swallow failures. Silently failing to
        persist would mean the pinned game quietly stops surviving reboots, and
        nothing would ever notice.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": STATE_VERSION, **asdict(state)}, indent=2)

        handle = tempfile.NamedTemporaryFile(
            "w",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle as tmp:
                tmp.write(payload + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise


class InMemoryStore:
    """A store that forgets on exit. For tests, and for a --no-persist mode."""

    def __init__(self, state: BridgeState | None = None):
        self._state = state or BridgeState()

    def load(self) -> BridgeState:
        return self._state

    def save(self, state: BridgeState) -> None:
        self._state = state
