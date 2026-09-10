"""Turning the service's state into words for a person.

Kept out of the templates and out of the request handlers, because this is where
the actual judgement lives -- *which* of eight true statements is the one the
owner needs -- and that judgement deserves tests that do not involve HTTP.

The ordering in :func:`_headline` is the design decision in this module. At any
moment several things are legitimately true at once ("no game pinned", "board
asleep", "not logged in"), and a page that lists all of them equally makes the
owner do the triage. So there is one headline, chosen by how much it blocks play,
with everything else still available further down.

One thing it deliberately does *not* do is treat a disconnected board as an
error. A Chessnut GO sleeps as soon as we disconnect and only advertises for a
minute or two after being physically woken, so "not connected" is the normal
resting state of this appliance between sessions. Painting it red would train the
owner to ignore red.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..chessnut import ble
from ..service import ServiceStatus, WriteState
from ..sync.reconcile import SyncState

#: How stale the read path may get before the UI says so. The poll interval is a
#: minute or two, so this is several missed polls rather than one slow one.
STALE_READ_SECONDS = 600.0

#: Severities, in the order the page uses them. Only ``error`` means "something
#: is broken"; ``attention`` means "a person needs to do something ordinary".
OK = "ok"
ATTENTION = "attention"
ERROR = "error"


_SYNC_WORDS: dict[SyncState, str] = {
    SyncState.IN_SYNC: "The board matches the game.",
    SyncState.OPPONENT_MOVE_PENDING: (
        "Your opponent has moved. Play their move on the board to catch up."
    ),
    SyncState.MOVE_IN_PROGRESS: "A move is in progress on the board.",
    SyncState.MOVE_READY: "A move is ready to send.",
    SyncState.AMBIGUOUS: (
        "The board matches more than one legal move, so nothing was sent."
    ),
    SyncState.MISMATCH: (
        "The board and the game disagree, and no single move explains it."
    ),
}

_WRITE_WORDS: dict[WriteState, str] = {
    WriteState.IDLE: "",
    WriteState.SENDING: "Sending your move to Chess.com.",
    WriteState.ACCEPTED: "Your move was sent.",
    WriteState.REJECTED: "Chess.com would not accept the move.",
    WriteState.UNVERIFIED: (
        "A move was sent but Chess.com did not confirm it. Checking whether it "
        "arrived -- it will not be sent again automatically."
    ),
    WriteState.NOT_SENT: "A move never reached Chess.com and can be sent again.",
    WriteState.NEEDS_LOGIN: "Sign in to Chess.com to send moves.",
    WriteState.BLOCKED: "Something intercepted the request to Chess.com.",
}


def _count(quantity: float, unit: str) -> str:
    """``2 hours``, but ``1 hour``. Every bucket below can land on one -- 90
    seconds is one minute, 36 hours is one day -- so this is not a rare case."""
    whole = int(quantity)
    return f"{whole} {unit}" if whole == 1 else f"{whole} {unit}s"


def ago(then: float | None, now: float) -> str:
    """A rough relative time. Rough on purpose: 'about a minute ago' is what the
    owner wants, and a ticking seconds counter would force a faster SSE stream
    than anything else on the page needs."""
    if then is None:
        return "never"
    seconds = max(0.0, now - then)
    if seconds < 10:
        return "just now"
    if seconds < 90:
        return f"{_count(seconds, 'second')} ago"
    minutes = seconds / 60
    if minutes < 90:
        return f"{_count(minutes, 'minute')} ago"
    hours = minutes / 60
    if hours < 36:
        return f"{_count(hours, 'hour')} ago"
    return f"{_count(hours / 24, 'day')} ago"


def until(deadline: float | None, now: float) -> str:
    """How long is left, for a move deadline.

    Truncates rather than rounds, so 47 hours reads as one day left. For a
    deadline that is the safe direction to be wrong in: it makes the owner move
    sooner than they had to, never later than they could.
    """
    if deadline is None:
        return "no deadline"
    seconds = deadline - now
    if seconds <= 0:
        return "overdue"
    if seconds < 60:
        # Truncating here would say "0 minutes left", which reads as expired.
        return "less than a minute left"
    hours = seconds / 3600
    if hours < 1:
        return f"{_count(seconds / 60, 'minute')} left"
    if hours < 36:
        return f"{_count(hours, 'hour')} left"
    return f"{_count(hours / 24, 'day')} left"


@dataclass(frozen=True)
class GameRow:
    """One row of the picker, already in words."""

    id: str
    url: str
    opponent_to_move: bool
    is_selected: bool
    matches_board: bool
    deadline: str
    turn: str


@dataclass(frozen=True)
class View:
    """Everything the page shows, with no service or BLE types left in it."""

    headline: str
    severity: str
    board_summary: str
    board_hint: str
    board_healthy: bool
    chesscom_summary: str
    logged_in: bool
    #: Whether a password is stored, so the page can offer "forget it" and say
    #: that a lapsed session will fix itself.
    credentials_remembered: bool
    #: Why the last sign-in failed, if it did. Displayable.
    login_error: str
    #: What the appliance will do about not being signed in, in words. Empty when
    #: signed in.
    chesscom_hint: str
    read_summary: str
    read_hint: str
    sync_summary: str
    sync_detail: str
    highlight: tuple[str, ...]
    awaiting_setup: bool
    write_summary: str
    write_detail: str
    write_needs_attention: bool
    write_can_retry: bool
    write_can_dismiss: bool
    selected_game_id: str | None
    games: tuple[GameRow, ...] = ()
    battery: str = "unknown"


def _board_words(status: ServiceStatus, now: float) -> tuple[str, str, bool]:
    board = status.board
    if board.state is ble.BoardState.CONNECTED:
        rejected = board.truncated_frames + board.rejected_frames
        summary = f"Connected to {board.device_name or 'the board'}."
        if board.frames == 0:
            return (
                summary,
                "Connected but no positions have arrived yet.",
                False,
            )
        if rejected:
            # Worth being blunt: no position from this session can be trusted, so
            # a reassuring "connected" alone would be actively misleading.
            return (
                summary,
                f"{rejected} of {board.frames} readings could not be understood, "
                "so the position shown may be wrong.",
                False,
            )
        return summary, f"Last reading {ago(board.last_frame_at, now)}.", True

    if board.state is ble.BoardState.SCANNING:
        return "Looking for the board.", "Tap a piece on the board to wake it.", False
    if board.state is ble.BoardState.CONNECTING:
        return "Connecting to the board.", "", False

    hint = "Tap a piece on the board to wake it -- it sleeps when disconnected."
    if board.last_error:
        hint = f"{hint} Last attempt: {board.last_error}"
    return "Not connected to the board.", hint, False


def _read_words(status: ServiceStatus, now: float) -> tuple[str, str]:
    if status.read_error:
        return "Cannot read from Chess.com.", status.read_error
    read_at = status.games_read_at
    summary = f"Games last read {ago(read_at, now)}."
    if read_at is None:
        return summary, "Waiting for the first read of your games."
    if now - read_at > STALE_READ_SECONDS:
        # Silence is how a stalled poll loop presents, so it needs saying out
        # loud rather than being inferred from a timestamp nobody reads.
        return summary, "That is longer ago than it should be; the connection may be down."
    return summary, ""


def _sync_words(status: ServiceStatus) -> tuple[str, str]:
    snapshot = status.snapshot
    if snapshot.awaiting_setup:
        return (
            "Set up this game's position on the board.",
            snapshot.reason or "The board is showing a different position.",
        )
    if snapshot.reconciliation is None:
        return snapshot.reason or "Nothing to compare yet.", ""

    reconciliation = snapshot.reconciliation
    summary = _SYNC_WORDS.get(reconciliation.state, reconciliation.state.value)
    detail = ""
    if reconciliation.state is SyncState.AMBIGUOUS and reconciliation.candidates:
        detail = "Could be: " + ", ".join(reconciliation.candidates)
    elif reconciliation.state is SyncState.MISMATCH:
        parts = []
        if reconciliation.lifted:
            parts.append("missing from " + ", ".join(reconciliation.lifted))
        if reconciliation.added:
            parts.append("unexpected on " + ", ".join(reconciliation.added))
        if reconciliation.changed:
            parts.append("wrong piece on " + ", ".join(reconciliation.changed))
        detail = "; ".join(parts)
    elif reconciliation.move:
        detail = reconciliation.move
    return summary, detail


def _chesscom_words(status: ServiceStatus) -> tuple[str, str]:
    """How the chess.com side stands, and what will happen about it.

    The hint carries the distinction the owner actually cares about when they see
    "not signed in": whether they have to do something, or whether the appliance
    is going to sort it out by itself. Those look identical without being told
    apart, and one of them is worth getting out of bed for.
    """
    if status.logged_in_as:
        summary = f"Signed in as {status.logged_in_as}."
        if status.credentials_stored:
            return summary, "Its password is stored, so a lapsed session signs back in on its own."
        return summary, "No password is stored, so this will need signing in again when it lapses."

    if status.credentials_stored:
        return (
            "Not signed in to Chess.com.",
            "A password is stored, so the next move will sign in first.",
        )
    return (
        "Not signed in to Chess.com.",
        "Moves cannot be sent until you sign in.",
    )


def _headline(status: ServiceStatus, board_healthy: bool) -> tuple[str, str]:
    """The one thing to say, ordered by how much it stops play.

    A stalled write comes first because it is the only item that concerns a move
    in a real game; everything below it is a prerequisite that has simply not been
    met yet, which is ordinary rather than alarming.
    """
    write = status.write
    if write.needs_attention:
        severity = ERROR if write.state in (WriteState.BLOCKED, WriteState.REJECTED) else ATTENTION
        return _WRITE_WORDS[write.state], severity
    if not status.logged_in_as:
        return "Sign in to Chess.com to send moves.", ATTENTION
    if status.snapshot.selected_game_id is None:
        return "Choose the game to follow.", ATTENTION
    if not status.board.is_connected:
        return "Waiting for the board.", ATTENTION
    if not board_healthy:
        return "The board is connected but its readings are not usable.", ERROR
    if status.read_error:
        return "Cannot read from Chess.com.", ERROR
    if status.snapshot.awaiting_setup:
        return "Set up this game's position on the board.", ATTENTION
    reconciliation = status.snapshot.reconciliation
    if reconciliation is None:
        return status.snapshot.reason or "Getting ready.", ATTENTION
    if reconciliation.state in (SyncState.AMBIGUOUS, SyncState.MISMATCH):
        return _SYNC_WORDS[reconciliation.state], ATTENTION
    return _SYNC_WORDS.get(reconciliation.state, "Ready."), OK


def _rows(status: ServiceStatus, now: float) -> tuple[GameRow, ...]:
    selected = status.snapshot.selected_game_id
    return tuple(
        GameRow(
            id=choice.game.id,
            url=choice.game.url,
            opponent_to_move=not choice.game.is_my_turn,
            is_selected=choice.game.id == selected,
            matches_board=choice.matches_board,
            deadline=until(choice.game.move_by, now),
            turn="your move" if choice.game.is_my_turn else "their move",
        )
        for choice in status.choices
    )


def render_view(status: ServiceStatus, now: float | None = None) -> View:
    """Everything the page needs, as strings. Pure apart from the clock."""
    now = time.time() if now is None else now
    board_summary, board_hint, board_healthy = _board_words(status, now)
    read_summary, read_hint = _read_words(status, now)
    sync_summary, sync_detail = _sync_words(status)
    chesscom_summary, chesscom_hint = _chesscom_words(status)
    headline, severity = _headline(status, board_healthy)

    battery = "unknown"
    if status.battery is not None:
        battery = f"{status.battery.percent}%"
        if status.battery.charging:
            battery += " (charging)"

    reconciliation = status.snapshot.reconciliation
    return View(
        headline=headline,
        severity=severity,
        board_summary=board_summary,
        board_hint=board_hint,
        board_healthy=board_healthy,
        chesscom_summary=chesscom_summary,
        logged_in=bool(status.logged_in_as),
        credentials_remembered=status.credentials_stored,
        login_error=status.login_error or "",
        chesscom_hint=chesscom_hint,
        read_summary=read_summary,
        read_hint=read_hint,
        sync_summary=sync_summary,
        sync_detail=sync_detail,
        highlight=(
            status.snapshot.setup_guidance
            or (reconciliation.squares_to_highlight if reconciliation else ())
        ),
        awaiting_setup=status.snapshot.awaiting_setup,
        write_summary=_WRITE_WORDS.get(status.write.state, ""),
        # chess.com's own wording where there is any: it explains an unfamiliar
        # refusal better than anything inferred from a status code.
        write_detail=status.write.detail or "",
        write_needs_attention=status.write.needs_attention,
        write_can_retry=status.write.is_retryable,
        write_can_dismiss=status.write.needs_attention,
        selected_game_id=status.snapshot.selected_game_id,
        games=_rows(status, now),
        battery=battery,
    )
