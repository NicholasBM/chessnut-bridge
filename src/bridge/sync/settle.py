"""Debouncing between the board and the reconciler: only act on a still board.

Why this exists, concretely. A real Chessnut GO was observed reporting square e8
as occupied and empty on alternating frames while nobody was touching the board
(2026-09-07): a marginal sensor, with the same piece reading perfectly steadily
on another square. Feeding that stream straight into reconciliation is unsafe,
and not merely noisy:

* Mid-move, a flicker combines with a genuinely lifted piece to make an occupancy
  that matches some *other* legal move. Reconciliation would find exactly one
  match, call it MOVE_READY, and submit a move the player never made. That is the
  precise failure the whole design exists to prevent.
* A flickering square makes the UI oscillate between IN_SYNC and MISMATCH, so a
  real disagreement is indistinguishable from noise.

The fix is to require the board to hold one position still before anything acts
on it. A correspondence game moves a few times a day, so waiting a couple of
seconds for the board to settle costs nothing at all -- this is one place where
the use case makes an otherwise awkward tradeoff free.

Flicker is also *counted* rather than merely filtered, because a square that
needs debouncing is a fault worth showing in the UI. Silently smoothing it over
would have hidden e8.

The cause is not necessarily a broken board, and the UI should not say it is. The
e8 flicker turned out to coincide with loose magnets sitting on the same table:
these boards sense pieces magnetically and take a reference reading of every
square at power-on, so a stray field during calibration leaves the affected
squares resting near their detection threshold. That reads as a hardware fault
and is cured by removing the magnets and power-cycling. So a flickering square
means "something is wrong at this square", and the likely causes are, in order:
a nearby magnet, a wrong power-on baseline, an unseated piece, a weakened piece
magnet, and only then a failed sensor.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Mapping

log = logging.getLogger(__name__)

#: How long the board must hold one position before it is trusted. Generous on
#: purpose: a 3-day game does not care, and the cost of acting on an unsettled
#: board is a wrong move in a real game.
SETTLE_SECONDS = 2.0

#: A square that changes more often than this while the board is supposedly at
#: rest is reported as unreliable rather than just debounced away.
FLICKER_THRESHOLD = 3

Occupancy = Mapping[str, str]


@dataclass
class SettleStatus:
    """What the UI needs to explain why the board is or is not being acted on."""

    settled: bool = False
    #: Squares that have changed repeatedly without the position ever settling.
    #: Non-empty means suspect hardware, not a suspect player.
    flickering: tuple[str, ...] = ()
    changes_while_unsettled: int = 0
    pending_for: float = 0.0

    @property
    def has_unreliable_squares(self) -> bool:
        return bool(self.flickering)


@dataclass
class PositionSettler:
    """Emits an occupancy only once the board has held it still.

    Deliberately clock-injectable and free of asyncio: the whole point is that it
    can be tested exhaustively without hardware or real time passing.
    """

    settle_seconds: float = SETTLE_SECONDS
    clock: Callable[[], float] = time.monotonic

    _candidate: dict[str, str] | None = field(default=None, init=False)
    _candidate_since: float = field(default=0.0, init=False)
    _emitted: dict[str, str] | None = field(default=None, init=False)
    _changes: int = field(default=0, init=False)
    _square_changes: Counter = field(default_factory=Counter, init=False)

    def update(self, occupancy: Occupancy) -> dict[str, str] | None:
        """Feed one frame. Returns the occupancy only when it is newly settled.

        Returns None while the board is still moving, and also for a position
        that has already been emitted -- callers get an edge, not a level, so
        reconciliation and any move submission happen once per real change.
        """
        current = dict(occupancy)
        now = self.clock()

        if self._candidate is None or current != self._candidate:
            if self._candidate is not None:
                self._changes += 1
                for square in _differing_squares(self._candidate, current):
                    self._square_changes[square] += 1
            self._candidate = current
            self._candidate_since = now
            return None

        if now - self._candidate_since < self.settle_seconds:
            return None

        if current == self._emitted:
            return None

        self._emitted = current
        # Reset the flicker tally: it describes the unsettled period that led to
        # this position, and keeping it would smear one bad episode across the
        # rest of the session.
        if self._changes:
            log.debug("settled after %d intermediate change(s)", self._changes)
        self._changes = 0
        self._square_changes.clear()
        return dict(current)

    @property
    def status(self) -> SettleStatus:
        now = self.clock()
        settled = (
            self._candidate is not None
            and now - self._candidate_since >= self.settle_seconds
        )
        return SettleStatus(
            settled=settled,
            flickering=tuple(
                sorted(
                    square
                    for square, count in self._square_changes.items()
                    if count >= FLICKER_THRESHOLD
                )
            ),
            changes_while_unsettled=self._changes,
            pending_for=0.0 if self._candidate is None else now - self._candidate_since,
        )

    @property
    def last_settled(self) -> dict[str, str] | None:
        """The most recently emitted position, or None if none ever settled."""
        return None if self._emitted is None else dict(self._emitted)

    def reset(self) -> None:
        """Forget everything -- used when the board reconnects or a game changes."""
        self._candidate = None
        self._candidate_since = 0.0
        self._emitted = None
        self._changes = 0
        self._square_changes.clear()


def _differing_squares(before: Occupancy, after: Occupancy) -> set[str]:
    return {
        square
        for square in set(before) | set(after)
        if before.get(square) != after.get(square)
    }
