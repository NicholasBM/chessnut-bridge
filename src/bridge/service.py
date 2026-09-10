"""The bridge itself: board, chess.com, engine and writer wired into one object.

Everything below this module is a component that knows nothing about the others,
and everything above it is a *view*. ``board_doctor session`` already worked that
way against the engine -- "this is a view over the engine, nothing more" -- and
this extends that stance to the whole appliance, so the CLI and the web UI cannot
drift apart by reimplementing the wiring twice.

The service owns exactly one thing of its own: **the policy for what to do when a
write does not obviously succeed.** Everything else it delegates.

Why the write policy lives here
-------------------------------
:class:`~bridge.chesscom.write.MoveWriter` deliberately never retries, because a
POST that times out may still have been applied. That leaves a decision nobody
below this layer can make, since answering it needs both the write result and the
read path:

* **Accepted** -- poll again promptly so the engine sees the new position rather
  than waiting out the normal interval with a stale one.
* **Rejected** (the server understood and said no) -- surface chess.com's own
  words and stop. Re-sending identical bytes cannot help.
* **Session expired** -- surface "log in again". Do not keep submitting into a
  session that has gone.
* **Ambiguous** (timeout, network, 5xx) -- the genuinely hard one. Poll again and
  *look* at whether the move landed. That is the only way to distinguish "never
  sent" from "sent and the reply was lost", and it costs one cached GET.

After an ambiguous write the service does **not** resubmit on its own, even when
the poll shows the move did not land. The user's constraint for this project is
that the bridge must not silently make moves when its picture and the server's
disagree, and an unexplained failure is precisely that situation. So it surfaces
a resolvable state and waits for a person to press the button. On a 3-day game
that costs nothing; guessing wrong costs a move that cannot be taken back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

from .chesscom import login, public, write
from .chessnut import ble, protocol
from .state.secrets import (
    InMemoryCredentialStore,
    InMemorySessionStore,
    StoredSession,
)
from .state.store import InMemoryStore
from .sync.engine import Discontinuity, GameChoice, Snapshot, SyncEngine

log = logging.getLogger(__name__)

#: How soon to poll after a write, instead of waiting out the normal interval.
#: The server has just changed, so this is the one moment a fast poll is useful
#: rather than merely impatient.
_POLL_AFTER_WRITE_SECONDS = 3.0

#: How long the squares of a just-submitted move stay lit before going dark.
#: Long enough to read as confirmation that the move went in, short enough that
#: nobody mistakes it for the board asking for something. Asked for by the owner,
#: who could see the highlight of their own move but only for an instant.
LED_LINGER_SECONDS = 7.0


class WriteState(Enum):
    """What became of the last attempt to submit a move."""

    #: Nothing has been attempted.
    IDLE = "idle"
    #: A submission is in flight.
    SENDING = "sending"
    #: The server took it.
    ACCEPTED = "accepted"
    #: The server declined it. ``detail`` carries chess.com's own wording.
    REJECTED = "rejected"
    #: We do not know whether it was applied, and the re-read has not settled it.
    UNVERIFIED = "unverified"
    #: The re-read proved it never landed. Safe to send again, but only on a
    #: person's say-so.
    NOT_SENT = "not_sent"
    #: The session is gone; a human must log in.
    NEEDS_LOGIN = "needs_login"
    #: Something intercepted the request -- a Cloudflare challenge, most likely.
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WriteStatus:
    """The write path as a level, so a UI can never miss a transition."""

    state: WriteState = WriteState.IDLE
    #: chess.com's wording where we have it, ours otherwise. Safe to display.
    detail: str | None = None
    game_id: str | None = None
    move: str | None = None
    #: The FEN the move was derived from, so a stalled attempt can be retried
    #: against the same position it was decided in -- or abandoned when the
    #: position has moved on.
    fen: str | None = None
    at: float | None = None

    @property
    def needs_attention(self) -> bool:
        """Whether a person has to do something. Drives the UI's alert."""
        return self.state in (
            WriteState.REJECTED,
            WriteState.UNVERIFIED,
            WriteState.NOT_SENT,
            WriteState.NEEDS_LOGIN,
            WriteState.BLOCKED,
        )

    @property
    def is_retryable(self) -> bool:
        """Only NOT_SENT is provably safe to resend: the re-read showed the game
        untouched, so a resubmission cannot be a duplicate."""
        return self.state is WriteState.NOT_SENT


@dataclass(frozen=True)
class ServiceStatus:
    """Everything a view needs, in one consistent read."""

    board: ble.BoardStatus
    snapshot: Snapshot
    write: WriteStatus
    choices: tuple[GameChoice, ...]
    logged_in_as: str | None
    session_stored: bool
    #: None until the first successful poll; otherwise unix seconds.
    games_read_at: float | None
    #: Why the read path is unhappy, if it is. Displayable.
    read_error: str | None
    battery: protocol.Battery | None
    #: Whether a password is stored, i.e. whether the appliance can sign itself
    #: back in unattended. Distinct from being signed in *now*. Defaulted so that
    #: a caller constructing a status by hand -- every test that renders a view --
    #: does not have to know about a capability it is not exercising.
    credentials_stored: bool = False
    #: Why the last sign-in attempt failed, in chess.com's terms where possible.
    #: Displayable; never contains the password.
    login_error: str | None = None


@dataclass
class BridgeService:
    """Runs the bridge. Every method is safe to call from a request handler."""

    username: str
    board: ble.ChessnutBoard | None = None
    public_client: public.PublicClient | None = None
    writer: write.MoveWriter = field(default_factory=write.MoveWriter)
    session_store: object = field(default_factory=InMemorySessionStore)
    state_store: object = field(default_factory=InMemoryStore)
    credential_store: object = field(default_factory=InMemoryCredentialStore)
    #: Built fresh per attempt, never reused: a login client carries a cookie jar,
    #: and a jar left over from a previous attempt is how a "successful" sign-in
    #: ends up holding a stale session that only fails when a real move needs it.
    login_factory: Callable[[], login.ChessComLogin] = login.ChessComLogin
    engine: SyncEngine | None = None
    poll_interval: float = public.DEFAULT_POLL_SECONDS
    clock: Callable[[], float] = time.time

    _observed: tuple[public.DailyGame, ...] = field(default=(), init=False)
    _write: WriteStatus = field(default_factory=WriteStatus, init=False)
    _read_error: str | None = field(default=None, init=False)
    _games_read_at: float | None = field(default=None, init=False)
    _login_error: str | None = field(default=None, init=False)
    _last_login_at: float | None = field(default=None, init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _poll_now: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _board_was_connected: bool = field(default=False, init=False)
    _submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _lit: tuple[str, ...] | None = field(default=None, init=False)
    _clear_leds_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = SyncEngine(store=self.state_store)
        if self.public_client is None:
            self.public_client = public.PublicClient(self.username)
        if self.board is None:
            self.board = ble.ChessnutBoard(
                on_position=self._on_position,
                on_status=self._on_board_status,
                on_battery=self._on_battery,
            )
        else:
            # A board handed in for testing still has to be wired up, or the
            # service would sit deaf with no obvious reason why.
            self.board._on_position = self._on_position  # type: ignore[attr-defined]
            self.board._on_status = self._on_board_status  # type: ignore[attr-defined]
            self.board._on_battery = self._on_battery  # type: ignore[attr-defined]

    # --- what a view reads -------------------------------------------------

    @property
    def status(self) -> ServiceStatus:
        """One consistent read of everything. Cheap; call it per request."""
        stored = self._stored_session()
        assert self.engine is not None and self.board is not None
        return ServiceStatus(
            board=self.board.status,
            snapshot=self.engine.snapshot,
            write=self._write,
            choices=tuple(self.engine.choices()),
            logged_in_as=stored.username if stored else None,
            session_stored=bool(stored),
            credentials_stored=self._stored_credentials() is not None,
            login_error=self._login_error,
            games_read_at=self._games_read_at,
            read_error=self._read_error,
            battery=self.board.status.battery,
        )

    def _stored_session(self) -> StoredSession | None:
        try:
            return self.session_store.load()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 -- a view must never 500 over this
            log.error("could not read the stored session: %s", exc)
            return None

    def _stored_credentials(self) -> login.Credentials | None:
        try:
            return self.credential_store.load()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 -- a view must never 500 over this
            log.error("could not read the stored credentials: %s", exc)
            return None

    # --- what a view does --------------------------------------------------

    def select_game(self, game_id: str | None) -> Snapshot:
        """Pin a game, or unpin with None. Persisted by the engine's store."""
        assert self.engine is not None
        log.info("game selection changed to %r", game_id)
        # A new selection makes any stalled write irrelevant: it belonged to a
        # game the player is no longer pointing at.
        self._write = WriteStatus()
        return self.engine.select_game(game_id)

    def log_in(self, stored: StoredSession) -> None:
        """Store a captured session and tell the engine its world changed."""
        assert self.engine is not None
        self.session_store.save(stored)  # type: ignore[attr-defined]
        self._write = WriteStatus()
        # Anything derived under the old session is suspect, and the engine has a
        # door for exactly this.
        self.engine.note(Discontinuity.REAUTHENTICATED)
        self.request_poll()

    async def sign_in(
        self, username: str, password: str, *, remember: bool = True
    ) -> None:
        """Sign in to chess.com with a password. Raises :class:`login.LoginError`.

        The password is stored (encrypted) when ``remember`` is set, which is the
        default and the point of the feature: it is what lets the appliance sign
        itself back in weeks later when the session lapses, with nobody present.
        Passing ``remember=False`` gets a session now and nothing kept, for an
        owner who would rather come back and do this by hand.

        Deliberately *not* storing the password until the login succeeds. Keeping a
        password that chess.com has just refused would mean the appliance retrying
        a wrong password on a timer, which is how an account gets locked.
        """
        credentials = login.Credentials(username=username.strip(), password=password)
        session = await self.login_factory().log_in(credentials)
        self._last_login_at = self.clock()

        if remember:
            try:
                self.credential_store.save(credentials)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                # The session is good, so this is a degradation rather than a
                # failure: moves will work until it lapses, then need a human.
                log.error(
                    "signed in but could not store the password (%s); the appliance "
                    "will not be able to sign itself back in later",
                    exc,
                )
        self._login_error = None
        self.log_in(
            StoredSession(
                session=session, username=credentials.username, stored_at=self.clock()
            )
        )
        log.info("signed in to chess.com as %s", credentials.username)

    async def _sign_in_from_store(self, *, force: bool = False) -> bool:
        """Try to sign in again using the stored password. Never raises.

        Returns whether there is a usable session afterwards. This is the whole
        unattended-recovery path, so its two refusals matter more than its success:

        * **No stored password** -- nothing to do, and saying so is what puts
          "sign in" on the page rather than leaving it silently stuck.
        * **Too soon since the last attempt** -- :data:`login.MIN_RETRY_SECONDS`
          keeps a permanently-failing credential from becoming a machine POSTing a
          password at chess.com in a loop, which is how the account itself gets
          into trouble. ``force`` is for a person pressing a button, where the
          request is deliberate and one attempt is what they asked for.
        """
        credentials = self._stored_credentials()
        if credentials is None:
            return False

        since = None if self._last_login_at is None else self.clock() - self._last_login_at
        if not force and since is not None and since < login.MIN_RETRY_SECONDS:
            log.info(
                "not signing in again yet: last attempt was %.0fs ago and the floor "
                "is %.0fs",
                since,
                login.MIN_RETRY_SECONDS,
            )
            return False

        self._last_login_at = self.clock()
        try:
            session = await self.login_factory().log_in(credentials)
        except login.LoginError as exc:
            self._login_error = exc.for_display
            log.error("could not sign in to chess.com: %s", exc)
            if not exc.retryable:
                # A wrong password, or an account wanting 2FA, will never succeed
                # unattended. Forgetting it stops the appliance retrying for ever
                # and makes the page ask for a new one, which is the only thing
                # that can actually resolve this.
                log.error(
                    "forgetting the stored password because that failure cannot "
                    "resolve itself; the page will ask for it again"
                )
                try:
                    self.credential_store.clear()  # type: ignore[attr-defined]
                except Exception as clear_exc:  # noqa: BLE001
                    log.error("could not clear the stored password: %s", clear_exc)
            return False
        except Exception as exc:  # noqa: BLE001 -- must not take a loop down
            self._login_error = f"unexpected failure signing in: {exc}"
            log.exception("unexpected failure signing in to chess.com")
            return False

        self._login_error = None
        self.log_in(
            StoredSession(
                session=session, username=credentials.username, stored_at=self.clock()
            )
        )
        log.info("signed back in to chess.com as %s", credentials.username)
        return True

    async def sign_in_again(self) -> bool:
        """Sign in now using the stored password, ignoring the retry floor."""
        return await self._sign_in_from_store(force=True)

    def forget_credentials(self) -> None:
        """Forget the stored password but keep the current session."""
        self.credential_store.clear()  # type: ignore[attr-defined]
        log.info("forgot the stored chess.com password at the owner's request")

    def log_out(self) -> None:
        """Sign out of chess.com and forget the password.

        Both, because forgetting only the session would have the appliance sign
        straight back in on the next move, and a "sign out" button that does not
        sign you out is worse than no button.
        """
        self.session_store.clear()  # type: ignore[attr-defined]
        try:
            self.credential_store.clear()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            log.error("could not clear the stored password: %s", exc)
        self._login_error = None
        self._write = WriteStatus(
            state=WriteState.NEEDS_LOGIN, detail="logged out", at=self.clock()
        )

    def request_poll(self) -> None:
        """Ask the poll loop to go now. Used after a login or a write."""
        self._poll_now.set()

    def reconnect_board(self) -> None:
        """Drop the BLE link so the run loop rebuilds it.

        Exposed because a board that has wandered off is the most common thing
        needing a nudge, and the whole point of the web UI is not having to SSH in
        to run ``bluetoothctl``.
        """
        assert self.board is not None
        log.info("board reconnect requested")
        self.board.request_reconnect()

    def clear_write_alert(self) -> None:
        """Acknowledge a failed write without resending it."""
        self._write = WriteStatus()

    async def retry_write(self) -> WriteStatus:
        """Resend a move the re-read proved never landed.

        Refuses in any other state. ``NOT_SENT`` is the only one where a resend is
        provably not a duplicate, and this is the single irreversible action the
        UI can trigger, so the guard is a real one rather than a comment.
        """
        stalled = self._write
        if not stalled.is_retryable:
            raise RuntimeError(
                f"a write in state {stalled.state.value} must not be resent; "
                "only a move proven not to have landed can be"
            )
        assert stalled.game_id and stalled.move and stalled.fen
        return await self._submit(stalled.game_id, stalled.move, stalled.fen)

    # --- board callbacks ---------------------------------------------------

    async def _on_position(self, frame: protocol.BoardFrame) -> None:
        assert self.engine is not None
        snapshot = self.engine.on_frame(frame.occupancy)
        await self._maybe_submit(snapshot)
        await self._drive_leds(snapshot)

    async def _on_board_status(self, status: ble.BoardStatus) -> None:
        """Tell the engine about connect/disconnect edges, and only those.

        A reconnect must re-derive the position from nothing: the settler emits
        edges, so carrying a pre-disconnect position across the gap makes an
        unchanged board indistinguishable from no board at all. That was observed
        live, and it is the same hazard as a reboot.
        """
        assert self.engine is not None
        if status.is_connected != self._board_was_connected:
            self._board_was_connected = status.is_connected
            # A board that slept came back dark, whatever we last sent it — so
            # there is nothing left to linger over either.
            self._lit = None
            self._clear_leds_at = None
            self.engine.set_board_connected(status.is_connected)

    async def _on_battery(self, battery: protocol.Battery) -> None:
        log.info("board battery %d%%%s", battery.percent,
                 " (charging)" if battery.charging else "")

    async def _drive_leds(self, snapshot: Snapshot) -> None:
        """Light what the snapshot says to light, and nothing else.

        The empty set is a real instruction, not "no instruction": once a move is
        submitted there is nothing left to highlight, and the LED command has no
        "leave as you were" — so skipping the write left the last move's two
        squares lit until the board slept. Observed live 2026-09-09.

        The clear is delayed by :data:`LED_LINGER_SECONDS` though. The squares go
        empty the instant the move is accepted, which is exactly the moment the
        owner wants to see them: the lit pair is the appliance's only way of
        saying "that one, and it went in". Deferring the *clear* is safe in a way
        that deferring a *light* would not be — there is nothing newer to show,
        because a non-empty set cancels the deadline immediately.

        Written only on change. Frames arrive continuously, and one BLE write per
        frame to say the same thing is a waste of the radio the Zero 2 W is also
        using for WiFi.
        """
        assert self.board is not None
        squares = tuple(snapshot.setup_guidance or (
            snapshot.reconciliation.squares_to_highlight
            if snapshot.reconciliation
            else ()
        ))
        if squares == self._lit:
            self._clear_leds_at = None
            return
        if squares:
            # Something to show now outranks anything we were waiting to erase.
            self._clear_leds_at = None
        elif self._lit:
            # Frame-driven rather than a timer task: frames arrive about ten times
            # a second while the board is awake, so the deadline is checked often
            # enough, and a board that stops sending is a board on its way to
            # sleeping dark anyway.
            if self._clear_leds_at is None:
                self._clear_leds_at = self.clock() + LED_LINGER_SECONDS
            if self.clock() < self._clear_leds_at:
                return
            self._clear_leds_at = None
        try:
            wrote = await self.board.set_leds(squares)
        except Exception as exc:  # noqa: BLE001 -- LEDs are cosmetic
            log.debug("could not set LEDs: %s", exc)
        else:
            # A dropped write returns False rather than raising. Recording it as
            # lit anyway would suppress the retry on the next frame.
            if wrote:
                self._lit = squares

    # --- the write path ----------------------------------------------------

    async def _maybe_submit(self, snapshot: Snapshot) -> None:
        """Submit if and only if the engine hands out a submission."""
        assert self.engine is not None
        submission = self.engine.take_submission()
        if submission is None:
            return
        await self._submit(submission.game_id, submission.move, submission.fen)

    async def _submit(self, game_id: str, uci: str, fen: str) -> WriteStatus:
        """One attempt, then apply the policy in this module's docstring."""
        async with self._submit_lock:
            stored = self._stored_session()
            if stored is None and await self._sign_in_from_store():
                # No session, but a stored password: the ordinary state after the
                # session has lapsed, and the reason the password is kept at all.
                stored = self._stored_session()
            if stored is None:
                self._write = WriteStatus(
                    state=WriteState.NEEDS_LOGIN,
                    detail=(
                        self._login_error
                        or "no chess.com session stored; log in to submit moves"
                    ),
                    game_id=game_id, move=uci, fen=fen, at=self.clock(),
                )
                log.error("cannot submit %s: not logged in", uci)
                return self._write

            last_activity = self._last_activity_for(game_id)
            if last_activity is None:
                # Without it the optimistic-concurrency guard cannot be set, and
                # sending a guessed value would throw away the protection that
                # makes this whole path safe.
                self._write = WriteStatus(
                    state=WriteState.UNVERIFIED,
                    detail=(
                        "cannot submit yet: the game's last-activity timestamp is "
                        "not known, so the safety check on the move cannot be set"
                    ),
                    game_id=game_id, move=uci, fen=fen, at=self.clock(),
                )
                self.request_poll()
                return self._write

            self._write = WriteStatus(
                state=WriteState.SENDING, game_id=game_id, move=uci, fen=fen,
                at=self.clock(),
            )
            try:
                await self.writer.submit(
                    stored.session, game_id, uci, fen, last_activity
                )
            except write.SessionExpired as exc:
                # The one failure worth handling automatically. A 401 is *proof*
                # the move was not applied -- the server refused the request
                # before looking at its body -- so signing in and sending once
                # more cannot be a duplicate. This is the exception that keeps the
                # no-retry rule intact: everywhere else the outcome is unknown,
                # here it is known, and the difference is the whole reason the
                # password is stored.
                #
                # Forced past the retry floor deliberately: the stored session is
                # now definitely dead, and sitting on a ready move for fifteen
                # minutes to be polite would strand it. Bounded to one extra
                # attempt per submission, so it cannot become a loop.
                if await self._sign_in_from_store(force=True):
                    refreshed = self._stored_session()
                    if refreshed is not None:
                        log.info("signed in again; sending %s once more", uci)
                        return await self._submit_once(
                            refreshed, game_id, uci, fen, last_activity
                        )
                self._write = self._failed(WriteState.NEEDS_LOGIN, exc, game_id, uci, fen)
            except write.ChallengePresented as exc:
                self._write = self._failed(WriteState.BLOCKED, exc, game_id, uci, fen)
            except write.MoveRejected as exc:
                self._write = self._failed(WriteState.REJECTED, exc, game_id, uci, fen)
                # Our picture of the game is wrong; the read path is how we find
                # out how.
                self.request_poll()
            except write.WriteUnavailable as exc:
                self._write = self._failed(WriteState.UNVERIFIED, exc, game_id, uci, fen)
                log.error(
                    "submission of %s to game %s is unverified (%s); re-reading "
                    "to find out whether it landed -- not resending",
                    uci, game_id, exc,
                )
                self.request_poll()
            else:
                self._write = WriteStatus(
                    state=WriteState.ACCEPTED, game_id=game_id, move=uci, fen=fen,
                    at=self.clock(),
                )
                log.info("submitted %s to game %s", uci, game_id)
                self.request_poll()
            return self._write

    async def _submit_once(
        self,
        stored: StoredSession,
        game_id: str,
        uci: str,
        fen: str,
        last_activity: int,
    ) -> WriteStatus:
        """The same policy, with no further sign-in. Called after a re-sign-in.

        Separate from :meth:`_submit` so that "try again once with a fresh session"
        cannot recurse. A second 401 here means something is wrong that a third
        login will not fix, and it becomes an ordinary NEEDS_LOGIN for a person to
        look at. Assumes the caller holds ``_submit_lock``.
        """
        self._write = WriteStatus(
            state=WriteState.SENDING, game_id=game_id, move=uci, fen=fen,
            at=self.clock(),
        )
        try:
            await self.writer.submit(stored.session, game_id, uci, fen, last_activity)
        except write.SessionExpired as exc:
            self._write = self._failed(WriteState.NEEDS_LOGIN, exc, game_id, uci, fen)
        except write.ChallengePresented as exc:
            self._write = self._failed(WriteState.BLOCKED, exc, game_id, uci, fen)
        except write.MoveRejected as exc:
            self._write = self._failed(WriteState.REJECTED, exc, game_id, uci, fen)
            self.request_poll()
        except write.WriteUnavailable as exc:
            self._write = self._failed(WriteState.UNVERIFIED, exc, game_id, uci, fen)
            log.error(
                "submission of %s to game %s is unverified (%s); re-reading to find "
                "out whether it landed -- not resending",
                uci, game_id, exc,
            )
            self.request_poll()
        else:
            self._write = WriteStatus(
                state=WriteState.ACCEPTED, game_id=game_id, move=uci, fen=fen,
                at=self.clock(),
            )
            log.info("submitted %s to game %s", uci, game_id)
            self.request_poll()
        return self._write

    def _failed(
        self, state: WriteState, exc: write.WriteError, game_id: str, uci: str, fen: str
    ) -> WriteStatus:
        return WriteStatus(
            state=state,
            # chess.com's own words where it gave any. Paraphrasing loses the
            # only information that explains an unfamiliar failure.
            detail=exc.for_display,
            game_id=game_id, move=uci, fen=fen, at=self.clock(),
        )

    def _last_activity_for(self, game_id: str) -> int | None:
        """The concurrency guard for a game, from the poll the engine also saw.

        Read from what this service last observed rather than from the client's
        own cache, so the timestamp sent with a move cannot come from a different
        read than the position the move was derived from. In production the two
        are the same fetch; keeping them the same *by construction* means a future
        change cannot quietly make them differ.
        """
        for game in self._observed:
            if game.id == game_id and game.last_activity is not None:
                return game.last_activity
        return None

    def _resolve_unverified(self, games: list[public.DailyGame]) -> None:
        """Decide whether an ambiguous write actually landed.

        Called on every poll while a write is unverified. The evidence is the
        game's own FEN: if it still shows the position the move was derived from
        and it is still our turn, the move demonstrably never arrived. Anything
        else means it did, or that the game has moved on so far that resending
        would be wrong anyway.
        """
        stalled = self._write
        if stalled.state is not WriteState.UNVERIFIED or not stalled.fen:
            return

        game = next((g for g in games if g.id == stalled.game_id), None)
        if game is None:
            # The game left the in-progress list: finished, or aborted. Either
            # way there is nothing left to resend into.
            self._write = WriteStatus(
                state=WriteState.REJECTED,
                detail="the game is no longer in progress, so the move was not resent",
                game_id=stalled.game_id, move=stalled.move, at=self.clock(),
            )
            return

        if game.fen == stalled.fen and game.is_my_turn:
            self._write = WriteStatus(
                state=WriteState.NOT_SENT,
                detail=(
                    "the move never reached chess.com -- the game is untouched and "
                    "it is still your turn, so it is safe to send again"
                ),
                game_id=stalled.game_id, move=stalled.move, fen=stalled.fen,
                at=self.clock(),
            )
            log.warning("move %s to game %s never landed", stalled.move, stalled.game_id)
        else:
            self._write = WriteStatus(
                state=WriteState.ACCEPTED,
                detail="the move did reach chess.com; the reply was lost, not the move",
                game_id=stalled.game_id, move=stalled.move, at=self.clock(),
            )
            log.info("move %s to game %s did land after all", stalled.move, stalled.game_id)

    # --- the loops ---------------------------------------------------------

    def _observe(self, games: list[public.DailyGame]) -> None:
        assert self.engine is not None
        self._games_read_at = self.clock()
        self._read_error = None
        self._observed = tuple(games)
        self._resolve_unverified(games)
        self.engine.observe_games(games)

    async def _poll_loop(self) -> None:
        """Poll the public API, honouring an out-of-band request to go now.

        Not reusing :func:`public.watch_games` because that owns its own sleep,
        and the service needs to be able to cut a wait short after a write. The
        backoff behaviour is the same.
        """
        assert self.public_client is not None
        backoff = public._ERROR_BACKOFF_START

        while not self._stop.is_set():
            delay = self.poll_interval
            try:
                games = await self.public_client.fetch_games()
            except public.RateLimited as exc:
                delay = exc.retry_after or backoff
                backoff = min(backoff * 2, public._ERROR_BACKOFF_CAP)
                self._read_error = f"rate-limited by chess.com; retrying in {delay:.0f}s"
                log.warning("%s", self._read_error)
            except public.ChessComError as exc:
                delay = backoff
                backoff = min(backoff * 2, public._ERROR_BACKOFF_CAP)
                self._read_error = str(exc)
                log.warning("read path unavailable (%s); retrying in %.0fs", exc, delay)
            else:
                backoff = public._ERROR_BACKOFF_START
                if games is not None:
                    self._observe(games)
                else:
                    # A 304. Unchanged is not unknown, and the last-read clock
                    # should still move so a view can tell polling is alive.
                    self._games_read_at = self.clock()
                    self._read_error = None

            if self._write.state is WriteState.ACCEPTED and delay > _POLL_AFTER_WRITE_SECONDS:
                delay = _POLL_AFTER_WRITE_SECONDS

            self._poll_now.clear()
            waiters = [asyncio.create_task(self._stop.wait()),
                       asyncio.create_task(self._poll_now.wait())]
            try:
                done, pending = await asyncio.wait(
                    waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
            except asyncio.CancelledError:
                for task in waiters:
                    task.cancel()
                raise

    async def run(self) -> None:
        """Run until :meth:`stop`. Neither loop failing takes the other down."""
        assert self.board is not None
        self._stop.clear()
        board_task = asyncio.create_task(self.board.run(), name="board")
        poll_task = asyncio.create_task(self._poll_loop(), name="poll")
        try:
            await asyncio.gather(board_task, poll_task)
        except asyncio.CancelledError:
            raise
        finally:
            self._stop.set()
            for task in (board_task, poll_task):
                task.cancel()
            await asyncio.gather(board_task, poll_task, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()
        self._poll_now.set()
        if self.board is not None:
            self.board.stop()
