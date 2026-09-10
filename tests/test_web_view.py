"""Tests for what the page actually says.

The interesting behaviour is not formatting, it is *choice*: at any moment several
things are legitimately true, and the page has to pick the one the owner needs.
So most of these set up two or three simultaneous truths and assert which one
wins.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom.public import DailyGame  # noqa: E402
from bridge.chessnut import protocol  # noqa: E402
from bridge.chessnut.ble import BoardState, BoardStatus  # noqa: E402
from bridge.service import ServiceStatus, WriteState, WriteStatus  # noqa: E402
from bridge.sync.engine import GameChoice, Snapshot  # noqa: E402
from bridge.sync.reconcile import Reconciliation, SyncState  # noqa: E402
from bridge.web.view import (  # noqa: E402
    ATTENTION,
    ERROR,
    OK,
    STALE_READ_SECONDS,
    ago,
    render_view,
    until,
)

NOW = 1_788_900_000.0
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def a_game(game_id: str = "1026053628", turn: str = "white", move_by=None) -> DailyGame:
    return DailyGame(
        id=game_id,
        url=f"https://www.chess.com/game/daily/{game_id}",
        fen=START_FEN,
        turn=turn,
        my_color="white",
        move_by=move_by,
        time_control="1/259200",
        last_activity=1788861472,
    )


def a_status(
    *,
    board: BoardStatus | None = None,
    write: WriteStatus | None = None,
    logged_in: str | None = "nbaronmorgan",
    selected: str | None = "1026053628",
    reconciliation: Reconciliation | None = None,
    awaiting_setup: bool = False,
    reason: str = "",
    games_read_at: float | None = NOW,
    read_error: str | None = None,
    choices=(),
    battery: protocol.Battery | None = None,
) -> ServiceStatus:
    if board is None:
        board = BoardStatus(
            state=BoardState.CONNECTED,
            device_name="Chessnut GO",
            frames=100,
            last_frame_at=NOW,
        )
    if reconciliation is None and not awaiting_setup and not reason:
        reconciliation = Reconciliation(state=SyncState.IN_SYNC)
    return ServiceStatus(
        board=board,
        snapshot=Snapshot(
            board_connected=board.is_connected,
            board_known=True,
            game=a_game() if selected else None,
            reconciliation=reconciliation,
            reason=reason,
            evaluated_at=NOW,
            last_discontinuity=None,
            selected_game_id=selected,
            awaiting_setup=awaiting_setup,
        ),
        write=write or WriteStatus(),
        choices=tuple(choices),
        logged_in_as=logged_in,
        session_stored=bool(logged_in),
        games_read_at=games_read_at,
        read_error=read_error,
        battery=battery,
    )


# --- relative times -------------------------------------------------------


@pytest.mark.parametrize(
    "seconds, expected",
    [(0, "just now"), (5, "just now"), (30, "30 seconds ago"),
     (600, "10 minutes ago"), (7200, "2 hours ago"), (400000, "4 days ago")],
)
def test_ago_reads_naturally(seconds, expected):
    assert ago(NOW - seconds, NOW) == expected


@pytest.mark.parametrize(
    "seconds, expected",
    [(90, "1 minute ago"), (5400, "1 hour ago"), (129600, "1 day ago")],
)
def test_ago_is_singular_where_it_should_be(seconds, expected):
    """Every bucket boundary lands on exactly one of its unit, so this is the
    first thing a real page shows rather than an edge case: the appliance had
    been up a minute and said '1 minutes ago'."""
    assert ago(NOW - seconds, NOW) == expected


def test_ago_handles_never():
    assert ago(None, NOW) == "never"


def test_ago_does_not_go_backwards_on_clock_skew():
    """The board's timestamps and the page's clock are not the same source, so a
    'in -3 seconds' is a real possibility and looks like a bug to the reader."""
    assert ago(NOW + 5, NOW) == "just now"


@pytest.mark.parametrize(
    "seconds, expected",
    [(-1, "overdue"), (0, "overdue"), (1800, "30 minutes left"),
     (7200, "2 hours left"), (259200, "3 days left")],
)
def test_until_reads_naturally(seconds, expected):
    assert until(NOW + seconds, NOW) == expected


@pytest.mark.parametrize(
    "seconds, expected",
    [(60, "1 minute left"), (3600, "1 hour left"), (129600, "1 day left")],
)
def test_until_is_singular_where_it_should_be(seconds, expected):
    assert until(NOW + seconds, NOW) == expected


def test_a_deadline_inside_the_minute_is_not_reported_as_zero():
    """'0 minutes left' reads as expired, and this is exactly the moment the
    difference matters."""
    assert until(NOW + 30, NOW) == "less than a minute left"


def test_until_understates_rather_than_overstates():
    """Truncation has to fall on the side that makes someone move early."""
    assert until(NOW + 47 * 3600, NOW) == "1 day left"


def test_until_handles_no_deadline():
    assert until(None, NOW) == "no deadline"


# --- which message wins ---------------------------------------------------


def test_a_working_bridge_says_so_quietly():
    view = render_view(a_status(), NOW)
    assert view.severity == OK
    assert "matches" in view.headline


def test_a_stalled_write_outranks_everything_else():
    """It is the only item that concerns a move in a real game; the rest are
    prerequisites that have simply not been met."""
    view = render_view(
        a_status(
            write=WriteStatus(state=WriteState.NOT_SENT, move="e2e4"),
            board=BoardStatus(state=BoardState.DISCONNECTED),
            selected=None,
        ),
        NOW,
    )
    assert "never reached" in view.headline
    assert view.write_can_retry


def test_being_signed_out_outranks_an_unpinned_game():
    view = render_view(a_status(logged_in=None, selected=None), NOW)
    assert "Sign in to Chess.com" in view.headline
    assert not view.logged_in


def test_an_unpinned_game_outranks_a_sleeping_board():
    """Choosing a game is the prerequisite; a sleeping board is this appliance's
    normal resting state and cannot be the first thing to complain about."""
    view = render_view(
        a_status(selected=None, board=BoardStatus(state=BoardState.DISCONNECTED)), NOW
    )
    assert "Choose the game" in view.headline


def test_a_disconnected_board_is_attention_not_error():
    """A Chessnut GO sleeps as soon as we disconnect, so painting this red would
    train the owner to ignore red."""
    view = render_view(a_status(board=BoardStatus(state=BoardState.DISCONNECTED)), NOW)
    assert view.severity == ATTENTION
    assert "wake it" in view.board_hint


def test_unusable_readings_are_an_error_even_though_the_board_is_connected():
    """'Connected' on its own would be actively misleading when no position from
    this session can be trusted."""
    view = render_view(
        a_status(
            board=BoardStatus(
                state=BoardState.CONNECTED, frames=10, truncated_frames=10
            )
        ),
        NOW,
    )
    assert view.severity == ERROR
    assert not view.board_healthy
    assert "may be wrong" in view.board_hint


def test_a_connected_board_with_no_readings_yet_is_not_called_healthy():
    view = render_view(
        a_status(board=BoardStatus(state=BoardState.CONNECTED, frames=0)), NOW
    )
    assert not view.board_healthy
    assert "no positions" in view.board_hint


def test_awaiting_setup_asks_for_the_position_rather_than_reporting_a_fault():
    """Occupancy alone cannot tell 'build this position' from 'something is
    wrong'; only the owner's choice of game distinguishes them."""
    view = render_view(
        a_status(
            awaiting_setup=True,
            reconciliation=Reconciliation(state=SyncState.MISMATCH, lifted=("e2",)),
            reason="set up the position for game 1026053628 on the board",
        ),
        NOW,
    )
    assert "Set up" in view.headline
    assert view.awaiting_setup
    assert view.severity == ATTENTION


def test_a_read_failure_is_reported_as_an_error_with_its_own_words():
    view = render_view(a_status(read_error="cannot reach api.chess.com"), NOW)
    assert view.severity == ERROR
    assert view.read_hint == "cannot reach api.chess.com"


def test_a_stale_read_is_called_out_rather_than_left_as_a_timestamp():
    """Silence is how a stalled poll loop presents, and nobody reads a timestamp
    and does the subtraction."""
    view = render_view(a_status(games_read_at=NOW - STALE_READ_SECONDS - 1), NOW)
    assert "longer ago than it should be" in view.read_hint


def test_a_fresh_read_says_nothing_extra():
    view = render_view(a_status(games_read_at=NOW - 30), NOW)
    assert view.read_hint == ""
    assert "30 seconds ago" in view.read_summary


def test_no_read_yet_is_distinguished_from_a_stale_one():
    view = render_view(a_status(games_read_at=None), NOW)
    assert "never" in view.read_summary
    assert "first read" in view.read_hint


# --- the position panel ---------------------------------------------------


def test_an_opponent_move_is_explained_as_an_action():
    """This is the normal state of a 3-day game, so the wording has to be an
    instruction rather than a diagnosis."""
    view = render_view(
        a_status(
            reconciliation=Reconciliation(
                state=SyncState.OPPONENT_MOVE_PENDING, move="e7e5"
            )
        ),
        NOW,
    )
    assert "Play their move" in view.sync_summary
    assert view.highlight == ("e7", "e5")


def test_ambiguity_lists_the_candidates():
    view = render_view(
        a_status(
            reconciliation=Reconciliation(
                state=SyncState.AMBIGUOUS, candidates=("e2e4", "e2e3")
            )
        ),
        NOW,
    )
    assert "e2e4" in view.sync_detail and "e2e3" in view.sync_detail
    assert view.severity == ATTENTION


def test_a_mismatch_names_the_squares_in_the_owners_terms():
    view = render_view(
        a_status(
            reconciliation=Reconciliation(
                state=SyncState.MISMATCH, lifted=("e8",), added=("e6",), changed=("a1",)
            )
        ),
        NOW,
    )
    assert "missing from e8" in view.sync_detail
    assert "unexpected on e6" in view.sync_detail
    assert "wrong piece on a1" in view.sync_detail
    assert set(view.highlight) == {"a1", "e6", "e8"}


def test_setup_guidance_wins_over_the_reconciliation_highlight():
    """While building a position the lit squares should be the ones to act on."""
    view = render_view(
        a_status(
            awaiting_setup=True,
            reconciliation=Reconciliation(state=SyncState.MISMATCH, lifted=("d4",)),
            reason="set up the position",
        ),
        NOW,
    )
    assert view.highlight == ("d4",)


# --- the write panel ------------------------------------------------------


def test_only_a_proven_unsent_move_offers_to_resend():
    view = render_view(a_status(write=WriteStatus(state=WriteState.NOT_SENT)), NOW)
    assert view.write_can_retry


@pytest.mark.parametrize(
    "state",
    [WriteState.UNVERIFIED, WriteState.REJECTED, WriteState.BLOCKED,
     WriteState.NEEDS_LOGIN],
)
def test_no_other_failure_offers_to_resend(state):
    """The button being absent is the defence a person actually experiences."""
    view = render_view(a_status(write=WriteStatus(state=state)), NOW)
    assert not view.write_can_retry
    assert view.write_needs_attention


def test_an_unverified_write_says_it_will_not_retry_by_itself():
    view = render_view(a_status(write=WriteStatus(state=WriteState.UNVERIFIED)), NOW)
    assert "not be sent again automatically" in view.write_summary


def test_chesscoms_own_wording_is_passed_through():
    """It explains an unfamiliar refusal better than anything inferred from a
    status code."""
    view = render_view(
        a_status(
            write=WriteStatus(
                state=WriteState.REJECTED, detail="Oops! This game is already over."
            )
        ),
        NOW,
    )
    assert view.write_detail == "Oops! This game is already over."


def test_an_accepted_write_needs_no_attention():
    view = render_view(a_status(write=WriteStatus(state=WriteState.ACCEPTED)), NOW)
    assert not view.write_needs_attention
    assert not view.write_can_dismiss


# --- the game picker ------------------------------------------------------


def test_rows_carry_the_selection_and_the_hint():
    choices = (
        GameChoice(game=a_game("111"), matches_board=True, differences=0),
        GameChoice(game=a_game("1026053628", turn="black"), matches_board=False, differences=6),
    )
    view = render_view(a_status(choices=choices), NOW)
    assert [row.id for row in view.games] == ["111", "1026053628"]
    assert view.games[0].matches_board
    assert not view.games[0].is_selected
    assert view.games[1].is_selected
    assert view.games[1].turn == "their move"


def test_a_deadline_is_shown_in_the_row():
    choices = (
        GameChoice(game=a_game(move_by=int(NOW) + 7200), matches_board=False, differences=None),
    )
    view = render_view(a_status(choices=choices), NOW)
    assert view.games[0].deadline == "2 hours left"


def test_no_games_is_not_an_error():
    view = render_view(a_status(choices=()), NOW)
    assert view.games == ()


def test_the_battery_is_reported_when_known():
    view = render_view(
        a_status(battery=protocol.Battery(percent=80, charging=True)), NOW
    )
    assert view.battery == "80% (charging)"


def test_an_unknown_battery_says_unknown_rather_than_zero():
    """Zero would look like a flat board and send someone hunting for a cable."""
    assert render_view(a_status(battery=None), NOW).battery == "unknown"
