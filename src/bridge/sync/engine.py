"""The level-driven core: holds the inputs, re-derives everything from them.

This module exists because of a bug caught on real hardware (2026-09-07). The
board dropped its BLE link and reconnected automatically, streamed valid frames,
and reported the correct position -- and the bridge said nothing at all, because
the position had not *changed* since before the drop. Everything downstream was
driven by transitions, so a discontinuity that produced no transition left the
whole system silently stale while looking perfectly healthy. The same hazard
applies to a Pi reboot, a WiFi outage, and a re-login.

Three rules follow from that, and they are the whole design:

1. **Inputs are levels, not events.** Callers push the current occupancy, the
   current game list, the current connection state. Nothing is remembered as
   "the change that happened"; state is re-derived from what is true now. Levels
   can be re-read after any interruption. Edges cannot be recovered once missed.

2. **Suppress at the output, never at the input.** ``reconcile()`` is a pure
   function of (FEN, occupancy), so recomputing it is free and always gives the
   same answer. Recomputing too often therefore costs nothing, while recomputing
   too rarely is precisely the bug. So evaluation is cheap and frequent, and the
   only thing deduplicated is ``take_submission()`` -- the single irreversible
   act. The earlier design deduplicated at the *earliest* point, which threw away
   information everything after it needed.

3. **Every discontinuity goes through one door.** ``Discontinuity`` enumerates
   them and ``_evaluate`` is the only thing that acts on them, so a newly
   discovered one has an obvious home rather than another ad-hoc check bolted
   into a callback. Startup is deliberately just another discontinuity, so there
   is no separate cold-start path to rot.

Fail-safe direction: losing the board discards the settled position rather than
retaining it. A remembered position is indistinguishable from a current one, and
acting on a stale board is how a move gets submitted that the player never made.
Trust has to be re-earned after every gap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Sequence

from bridge.chesscom.public import DailyGame
from bridge.state.store import BridgeState, InMemoryStore
from bridge.sync.reconcile import (
    Reconciliation,
    SyncState,
    occupancy_from_fen,
    reconcile,
)
from bridge.sync.settle import PositionSettler, SettleStatus

log = logging.getLogger(__name__)

#: How many squares may differ before a game is no longer a plausible match for
#: what is on the board. Two allows for a move in progress (a lifted piece and
#: its destination); four allows for a capture mid-move plus a nudge. Beyond that
#: the board is showing something else entirely. Only ever used to *hint* in the
#: game picker -- nothing is selected on the strength of it.
MAX_MATCH_DIFFERENCES = 4

#: How many consecutive polls a pinned game must be missing from the in-progress
#: list before we conclude it is over and unpin it. More than one because a
#: single poll can omit a game for reasons other than it finishing -- a malformed
#: entry is dropped by the parser, for instance -- and silently unpinning a live
#: game would leave the player thinking they were still connected to it.
_MISSING_POLLS_BEFORE_UNPIN = 2


class Discontinuity(Enum):
    """Something happened that invalidates anything we thought we knew.

    Every one of these must force a re-derivation even if no input appears to
    have changed -- that "appears" is exactly what went wrong before.
    """

    #: Process start. Listed as a discontinuity on purpose: cold start and
    #: reconnect then share one code path instead of two that can drift.
    STARTUP = "startup"
    BOARD_CONNECTED = "board_connected"
    BOARD_LOST = "board_lost"
    #: Network came back, so the game list may have moved on without us.
    NETWORK_RETURNED = "network_returned"
    GAME_SELECTED = "game_selected"
    GAME_FINISHED = "game_finished"
    #: The chess.com session was renewed, so anything derived under the old one
    #: is suspect.
    REAUTHENTICATED = "reauthenticated"


@dataclass(frozen=True)
class Submission:
    """A move cleared for sending. Handed out at most once per (position, move)."""

    game_id: str
    move: str
    #: The FEN the move was decided from. Part of the identity of the submission,
    #: so the same move in a repeated position is correctly treated as new.
    fen: str


@dataclass(frozen=True)
class GameChoice:
    """One row of the game picker.

    ``matches_board`` is a *hint* and nothing more. Detection can only ever tell
    you which game is already set up, which is the opposite of what you need when
    switching games -- and two games in the same opening are indistinguishable
    from occupancy anyway. So it highlights a likely row and never selects it.
    """

    game: DailyGame
    matches_board: bool
    differences: int | None


@dataclass(frozen=True)
class Snapshot:
    """Everything a UI or logger needs, valid to read at any moment.

    Deliberately a level: there is no "on_change" for this. A consumer that reads
    it can never have missed anything, which is the property the old edge-driven
    design lacked.
    """

    board_connected: bool
    #: True once a settled position has been read *since the last discontinuity*.
    board_known: bool
    game: DailyGame | None
    reconciliation: Reconciliation | None
    #: Why there is no reconciliation, in words fit to show a person.
    reason: str
    evaluated_at: float
    last_discontinuity: Discontinuity | None
    #: The pinned game id, even when that game is not in the current list.
    selected_game_id: str | None = None
    #: The board is showing some other position and the pinned game has not been
    #: set up yet. Distinguished from a plain MISMATCH by *intent*: the player
    #: deliberately chose this game, so the honest instruction is "build this
    #: position", not "something is wrong". Occupancy alone cannot tell these
    #: apart, which is why the engine has to remember the selection.
    awaiting_setup: bool = False

    @property
    def is_submittable(self) -> bool:
        return self.reconciliation is not None and self.reconciliation.is_submittable

    @property
    def setup_guidance(self) -> tuple[str, ...]:
        """Squares to act on to build the pinned position: light these up."""
        if not self.awaiting_setup or self.reconciliation is None:
            return ()
        return self.reconciliation.squares_to_highlight


@dataclass
class SyncEngine:
    """Owns the levels and re-derives the reconciliation from them.

    Owns the ``PositionSettler`` rather than taking one, so resetting it after a
    gap cannot be forgotten by a caller -- that omission is the original bug.
    """

    clock: Callable[[], float] = time.monotonic
    settle_seconds: float | None = None
    max_match_differences: int = MAX_MATCH_DIFFERENCES
    #: Where the pinned game is remembered. Defaults to memory only, so a caller
    #: that wants persistence has to say so rather than getting it by accident.
    store: object = field(default_factory=InMemoryStore)

    _settler: PositionSettler = field(init=False)
    _occupancy: dict[str, str] | None = field(default=None, init=False)
    _games: tuple[DailyGame, ...] = field(default=(), init=False)
    _game_id: str | None = field(default=None, init=False)
    _previous_fen: dict[str, str] = field(default_factory=dict, init=False)
    _board_connected: bool = field(default=False, init=False)
    _submitted: set[tuple[str, str, str]] = field(default_factory=set, init=False)
    _discontinuity: Discontinuity | None = field(default=None, init=False)
    _matched_since_selection: bool = field(default=False, init=False)
    _missing_polls: int = field(default=0, init=False)
    _snapshot: Snapshot = field(init=False)

    def __post_init__(self) -> None:
        self._settler = PositionSettler(
            clock=self.clock,
            **({} if self.settle_seconds is None else {"settle_seconds": self.settle_seconds}),
        )
        # A pinned game outlives the process on purpose: a power cut must not
        # silently un-pick the player's game. Loading it here rather than in a
        # separate start() keeps startup on the same path as every other
        # discontinuity.
        self._game_id = self.store.load().selected_game_id  # type: ignore[attr-defined]
        if self._game_id is not None:
            log.info("restored pinned game %s from persisted state", self._game_id)
        self._evaluate(Discontinuity.STARTUP)

    # --- levels in ---------------------------------------------------------

    def on_frame(self, occupancy: Mapping[str, str]) -> Snapshot:
        """Feed one raw board frame. Cheap to call at the board's full rate."""
        settled = self._settler.update(occupancy)
        if settled is None:
            # The settled level has not moved, so there is nothing to re-derive.
            # Safe to skip only because any *discontinuity* re-evaluates on its
            # own rather than waiting for an input to change.
            return self._snapshot
        self._occupancy = settled
        return self._evaluate()

    def set_board_connected(self, connected: bool) -> Snapshot:
        """Report the live BLE state. Idempotent -- only edges do work."""
        if connected == self._board_connected:
            return self._snapshot
        self._board_connected = connected
        # Reset on *both* directions. On loss because a remembered position must
        # never be mistaken for a current one; on connect because an unclean
        # transition could otherwise leave one behind.
        self._settler.reset()
        self._occupancy = None
        return self._evaluate(
            Discontinuity.BOARD_CONNECTED if connected else Discontinuity.BOARD_LOST
        )

    def observe_games(self, games: Sequence[DailyGame]) -> Snapshot:
        """Push the current game list. Retains previous FENs for move replay."""
        previous = {game.id: game for game in self._games}
        for game in games:
            was = previous.get(game.id)
            if was is not None and was.fen != game.fen:
                self._previous_fen[game.id] = was.fen
        self._games = tuple(games)

        if self._game_id is None:
            self._missing_polls = 0
            return self._evaluate()

        if self._game_id in {game.id for game in games}:
            self._missing_polls = 0
            return self._evaluate()

        # The pinned game is not in the in-progress list. Counted rather than
        # acted on immediately -- see _MISSING_POLLS_BEFORE_UNPIN.
        self._missing_polls += 1
        if self._missing_polls < _MISSING_POLLS_BEFORE_UNPIN:
            return self._evaluate()

        log.info("pinned game %s is over; unpinning", self._game_id)
        self._set_selection(None)
        return self._evaluate(Discontinuity.GAME_FINISHED)

    def select_game(self, game_id: str | None) -> Snapshot:
        """Pin a game until it finishes, or pass None to unpin.

        Nothing is ever selected automatically. Detection can only identify the
        game already on the board, and choosing the wrong game would mean
        submitting a perfectly legal move into a game the player was not playing
        -- worse than a wrong move, because nothing about it looks wrong.
        """
        self._set_selection(game_id)
        return self._evaluate(Discontinuity.GAME_SELECTED)

    def _set_selection(self, game_id: str | None) -> None:
        self._game_id = game_id
        self._matched_since_selection = False
        self._missing_polls = 0
        self.store.save(BridgeState(selected_game_id=game_id))  # type: ignore[attr-defined]

    def note(self, discontinuity: Discontinuity) -> Snapshot:
        """Report a discontinuity the engine cannot observe for itself."""
        return self._evaluate(discontinuity)

    # --- levels out -------------------------------------------------------

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    @property
    def settle_status(self) -> SettleStatus:
        """Read live rather than stored, so it cannot go stale in a Snapshot."""
        return self._settler.status

    def seconds_since_evaluated(self) -> float:
        """For staleness alarms: silence is the symptom this class of bug shows."""
        return self.clock() - self._snapshot.evaluated_at

    # --- the one edge -----------------------------------------------------

    def take_submission(self) -> Submission | None:
        """The single deduplicated output, and the only irreversible one.

        Every other repeat in this module is allowed through deliberately. This
        is the one place where doing something twice would be wrong, so this is
        the only place that suppresses. Keyed on the position as well as the move
        so a genuine repetition later in the game is not swallowed.
        """
        snapshot = self._snapshot
        if not snapshot.is_submittable or snapshot.game is None:
            return None
        assert snapshot.reconciliation is not None and snapshot.reconciliation.move
        key = (snapshot.game.id, snapshot.game.fen, snapshot.reconciliation.move)
        if key in self._submitted:
            return None
        self._submitted.add(key)
        return Submission(game_id=key[0], move=key[2], fen=key[1])

    # --- derivation -------------------------------------------------------

    def _evaluate(self, discontinuity: Discontinuity | None = None) -> Snapshot:
        """Re-derive the snapshot from the levels. Pure apart from the timestamp."""
        if discontinuity is not None:
            self._discontinuity = discontinuity
            log.debug("re-deriving after %s", discontinuity.value)

        game, reason = self._resolve_game()
        reconciliation = None
        awaiting_setup = False
        if not self._board_connected:
            reason = "board not connected"
        elif self._occupancy is None:
            reason = "waiting for the board to settle"
        elif game is not None:
            reconciliation = reconcile(
                game.fen, self._occupancy, previous_fen=self._previous_fen.get(game.id)
            )
            reason = ""
            # Once the board has genuinely shown this game, a later mismatch is a
            # real disagreement rather than a game that was never set up. Without
            # this latch, knocking the pieces over mid-game would be reported as
            # "set up your game", which is both wrong and alarming.
            if reconciliation.state in (
                SyncState.IN_SYNC,
                SyncState.MOVE_READY,
                SyncState.MOVE_IN_PROGRESS,
                SyncState.OPPONENT_MOVE_PENDING,
            ):
                self._matched_since_selection = True
            elif (
                reconciliation.state is SyncState.MISMATCH
                and not self._matched_since_selection
            ):
                awaiting_setup = True
                reason = f"set up the position for game {game.id} on the board"

        self._snapshot = Snapshot(
            board_connected=self._board_connected,
            board_known=self._occupancy is not None,
            game=game,
            reconciliation=reconciliation,
            reason=reason,
            evaluated_at=self.clock(),
            last_discontinuity=self._discontinuity,
            selected_game_id=self._game_id,
            awaiting_setup=awaiting_setup,
        )
        return self._snapshot

    def _resolve_game(self) -> tuple[DailyGame | None, str]:
        """Only ever the pinned game. Nothing is chosen on the board's behalf."""
        if self._game_id is None:
            return None, "no game selected -- pick one"
        for game in self._games:
            if game.id == self._game_id:
                return game, ""
        return None, f"waiting for game {self._game_id} in the game list"

    def choices(self) -> list[GameChoice]:
        """Rows for the game picker, most urgent first.

        Ordered by what the player actually wants to know with several games
        running -- "which one needs me?" -- so games where it is their turn come
        first, then by soonest deadline.
        """
        def key(choice: GameChoice) -> tuple:
            game = choice.game
            return (
                not game.is_my_turn,
                game.move_by if game.move_by is not None else float("inf"),
                game.id,
            )

        rows = [
            GameChoice(
                game=game,
                matches_board=(
                    self._occupancy is not None
                    and self._differences(game, self._occupancy)
                    <= self.max_match_differences
                ),
                differences=(
                    None
                    if self._occupancy is None
                    else self._differences(game, self._occupancy)
                ),
            )
            for game in self._games
        ]
        return sorted(rows, key=key)

    def _differences(self, game: DailyGame, observed: Mapping[str, str]) -> int:
        expected = occupancy_from_fen(game.fen)
        return sum(
            1
            for square in set(expected) | set(observed)
            if expected.get(square) != observed.get(square)
        )
