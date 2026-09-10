"""Tests for the web UI as an HTTP surface.

What is worth defending here is not the HTML. It is:

* **Nothing works without the cookie**, including routes added later.
* **A misconfigured appliance serves an explanation, never an open UI** -- this
  page can submit moves in real games.
* **The irreversible action stays guarded at the HTTP layer too.** The service
  refuses to resend a move that was not proven unsent; a handler that turned that
  refusal into a 500, or worse into a success, would undo it.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from asgi_client import request  # noqa: E402

from bridge.chesscom import login as chesscom_login  # noqa: E402
from bridge.chesscom import public, write  # noqa: E402
from bridge.chessnut import ble  # noqa: E402
from bridge.service import BridgeService, WriteState, WriteStatus  # noqa: E402
from bridge.state.secrets import (  # noqa: E402
    InMemoryCredentialStore,
    InMemorySessionStore,
    StoredSession,
)
from bridge.sync.engine import SyncEngine  # noqa: E402
from bridge.web import auth  # noqa: E402
from bridge.web.app import create_app, create_misconfigured_app  # noqa: E402
from bridge.web.config import Config  # noqa: E402

PASSWORD = "a-long-enough-password"
SIGNING_KEY = b"device-key-material-for-tests"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class FakeBoard:
    def __init__(self):
        self.status = ble.BoardStatus()
        self._on_position = None
        self._on_status = None
        self._on_battery = None
        self.reconnects = 0

    async def run(self):
        await asyncio.Event().wait()

    def stop(self):
        pass

    def request_reconnect(self):
        self.reconnects += 1

    async def set_leds(self, squares):
        return True


class FakePublicClient:
    last_games: list = []

    async def fetch_games(self):
        return []


class FakeWriter:
    def __init__(self):
        self.calls = []

    async def submit(self, session, game_id, uci, fen, last_activity):
        self.calls.append((game_id, uci, fen, last_activity))
        return {}


def a_game(game_id: str = "1026053628") -> public.DailyGame:
    return public.DailyGame(
        id=game_id,
        url=f"https://www.chess.com/game/daily/{game_id}",
        fen=START_FEN,
        turn="white",
        my_color="white",
        move_by=None,
        time_control="1/259200",
        last_activity=1788861472,
    )


def a_config(**overrides) -> Config:
    values = {
        "chesscom_username": "nbaronmorgan",
        "web_password": PASSWORD,
        "source": Path("/boot/firmware/chessnut-bridge.conf"),
    }
    values.update(overrides)
    return Config(**values)


def make_service(writer=None, session=None) -> BridgeService:
    return BridgeService(
        username="nbaronmorgan",
        board=FakeBoard(),
        public_client=FakePublicClient(),
        writer=writer or FakeWriter(),
        session_store=InMemorySessionStore(session),
        engine=SyncEngine(settle_seconds=0),
    )


def a_session() -> StoredSession:
    return StoredSession(
        session=write.Session(cookies={"PHPSESSID": "x"}, csrf_token="c"),
        username="nbaronmorgan",
        stored_at=1788861472.0,
    )


@pytest.fixture
def service():
    return make_service(session=a_session())


@pytest.fixture
def app(service):
    return create_app(service, a_config(), signing_key=SIGNING_KEY)


@pytest.fixture
def signed_in():
    """A cookie the app will accept, minted the same way the app mints one."""
    signer = auth.TokenSigner(secret=auth.derive_signing_key(SIGNING_KEY))
    return {auth.COOKIE_NAME: signer.issue()}


# --- default-deny ---------------------------------------------------------


PROTECTED_GETS = ["/", "/events"]
PROTECTED_POSTS = [
    "/select-game",
    "/reconnect-board",
    "/retry-write",
    "/dismiss-write",
    "/chesscom/logout",
    "/chesscom/login",
    "/chesscom/forget",
    "/chesscom/retry-login",
    "/logout",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PROTECTED_GETS)
async def test_a_page_without_a_cookie_goes_to_the_login_form(app, path):
    reply = await request(app, path)
    assert reply.status == 303
    assert reply.location == "/login"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PROTECTED_POSTS)
async def test_an_action_without_a_cookie_is_refused_not_redirected(app, path):
    """A redirect would be followed by the browser and the owner would see the
    login page with no sign that their action was dropped."""
    reply = await request(app, path, method="POST", form={})
    assert reply.status == 403


@pytest.mark.asyncio
async def test_every_route_is_protected_unless_it_is_on_the_allowlist(app):
    """The guard is by path, so this is the test that keeps a future route from
    being added without one."""
    from bridge.web.app import PUBLIC_PATHS

    paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", None) and "{" not in route.path
    }
    for path in paths - set(PUBLIC_PATHS):
        reply = await request(app, path)
        assert reply.status in (303, 403, 405), f"{path} answered without a cookie"


@pytest.mark.asyncio
async def test_a_forged_cookie_is_refused(app):
    reply = await request(app, "/", cookies={auth.COOKIE_NAME: "9999999999.forged"})
    assert reply.status == 303


@pytest.mark.asyncio
async def test_a_cookie_from_another_device_key_is_refused(service):
    """Two appliances on one network must not accept each other's cookies."""
    other = auth.TokenSigner(secret=auth.derive_signing_key(b"a-different-device"))
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/", cookies={auth.COOKIE_NAME: other.issue()})
    assert reply.status == 303


@pytest.mark.asyncio
async def test_health_is_reachable_without_a_cookie_and_says_nothing_else(app):
    """The one path anything on the network can reach, so it carries nothing
    about the account."""
    reply = await request(app, "/healthz")
    assert reply.status == 200
    assert reply.text == "ok"
    assert "nbaronmorgan" not in reply.text


# --- signing in -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_login_form_is_reachable(app):
    reply = await request(app, "/login")
    assert reply.status == 200
    assert "password" in reply.text.lower()


@pytest.mark.asyncio
async def test_the_login_form_explains_which_password_it_wants(app):
    """The obvious wrong guess is the Chess.com password."""
    reply = await request(app, "/login")
    assert "not your" in reply.text and "Chess.com" in reply.text


@pytest.mark.asyncio
async def test_the_right_password_issues_a_cookie_and_lands_on_the_page(app):
    reply = await request(app, "/login", method="POST", form={"password": PASSWORD})
    assert reply.status == 303
    assert reply.location == "/"
    token = reply.cookies.get(auth.COOKIE_NAME)
    assert token
    follow = await request(app, "/", cookies={auth.COOKIE_NAME: token})
    assert follow.status == 200


@pytest.mark.asyncio
async def test_a_wrong_password_issues_no_cookie(app):
    reply = await request(app, "/login", method="POST", form={"password": "nope"})
    assert reply.status == 401
    assert auth.COOKIE_NAME not in reply.cookies


@pytest.mark.asyncio
async def test_a_missing_password_field_is_not_a_crash(app):
    """Anything on the network can post an empty form."""
    reply = await request(app, "/login", method="POST", form={})
    assert reply.status == 401


@pytest.mark.asyncio
async def test_the_cookie_is_httponly_and_samesite_lax(app):
    """SameSite is what stops another site posting to the appliance on the
    owner's behalf, which is why there is no separate CSRF token."""
    reply = await request(app, "/login", method="POST", form={"password": PASSWORD})
    header = reply.header("set-cookie") or ""
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("samesite", "SameSite")
    assert "Secure" not in header, "plain HTTP on a LAN; Secure would never be sent"


@pytest.mark.asyncio
async def test_repeated_failures_start_refusing_early_with_429(service):
    """The delay is reported rather than slept through: a handler sleeping on a
    Pi Zero 2 W ties up a worker, which turns the defence into the attack."""
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    first = await request(app, "/login", method="POST", form={"password": "nope"})
    assert first.status == 401
    second = await request(app, "/login", method="POST", form={"password": PASSWORD})
    assert second.status == 429
    assert "Try again in" in second.text


@pytest.mark.asyncio
async def test_a_password_with_url_punctuation_in_it_still_works(service):
    """The form body is parsed here rather than by Starlette, which asserts
    ``python-multipart`` is installed before it will parse even a urlencoded body.
    A space arrives as ``+`` and punctuation as ``%xx``, so this is the test that
    keeps that decision from quietly mangling somebody's password."""
    awkward = "two words +50% & more"
    app = create_app(
        service, a_config(web_password=awkward), signing_key=SIGNING_KEY
    )
    reply = await request(app, "/login", method="POST", form={"password": awkward})
    assert reply.status == 303
    assert reply.cookies.get(auth.COOKIE_NAME)


@pytest.mark.asyncio
async def test_an_already_signed_in_browser_is_sent_onward(app, signed_in):
    reply = await request(app, "/login", cookies=signed_in)
    assert reply.status == 303
    assert reply.location == "/"


@pytest.mark.asyncio
async def test_signing_out_clears_the_cookie(app, signed_in):
    reply = await request(app, "/logout", method="POST", form={}, cookies=signed_in)
    assert reply.status == 303
    header = reply.header("set-cookie") or ""
    assert auth.COOKIE_NAME in header
    assert 'Max-Age=0' in header or '""' in header or "expires" in header.lower()


# --- the status page ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_page_renders_at_boot_before_anything_has_happened(app, signed_in):
    """No board, no poll, no game. A view that cannot render then is useless
    exactly when it is most needed."""
    reply = await request(app, "/", cookies=signed_in)
    assert reply.status == 200
    assert "Chess board bridge" in reply.text


@pytest.mark.asyncio
async def test_the_page_names_the_config_file_it_is_running_from(app, signed_in):
    """First question about a misbehaving appliance is which config it read."""
    reply = await request(app, "/", cookies=signed_in)
    assert "chessnut-bridge.conf" in reply.text


@pytest.mark.asyncio
async def test_the_page_does_not_leak_the_web_password(app, signed_in):
    reply = await request(app, "/", cookies=signed_in)
    assert PASSWORD not in reply.text


@pytest.mark.asyncio
async def test_the_page_lists_the_games_with_follow_buttons(service, signed_in):
    service._observe([a_game("111"), a_game("222")])
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/", cookies=signed_in)
    assert "Game 111" in reply.text and "Game 222" in reply.text
    assert reply.text.count("/select-game") == 2


@pytest.mark.asyncio
async def test_the_page_says_nothing_is_chosen_automatically(service, signed_in):
    """The reasoning matters to the owner: two games in one opening look identical
    to the board."""
    service._observe([a_game()])
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/", cookies=signed_in)
    assert "Nothing is chosen for you" in reply.text


# --- actions --------------------------------------------------------------


@pytest.mark.asyncio
async def test_following_a_game_pins_it(service, signed_in):
    service._observe([a_game()])
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(
        app, "/select-game", method="POST",
        form={"game_id": "1026053628"}, cookies=signed_in,
    )
    assert reply.status == 303
    assert service.engine.snapshot.selected_game_id == "1026053628"


@pytest.mark.asyncio
async def test_an_empty_game_id_unpins_rather_than_pinning_nothing(service, signed_in):
    """The Stop button posts an empty value, and pinning the empty string would
    leave the engine following a game that cannot exist."""
    service.select_game("1026053628")
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    await request(app, "/select-game", method="POST", form={"game_id": ""}, cookies=signed_in)
    assert service.engine.snapshot.selected_game_id is None


@pytest.mark.asyncio
async def test_reconnecting_the_board_reaches_the_transport(service, signed_in):
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/reconnect-board", method="POST", form={}, cookies=signed_in)
    assert reply.status == 303
    assert service.board.reconnects == 1


@pytest.mark.asyncio
async def test_signing_out_of_chesscom_forgets_the_session(service, signed_in):
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    await request(app, "/chesscom/logout", method="POST", form={}, cookies=signed_in)
    assert service.status.logged_in_as is None


@pytest.mark.asyncio
async def test_dismissing_an_alert_does_not_resend(service, signed_in):
    writer = FakeWriter()
    service.writer = writer
    service._write = WriteStatus(
        state=WriteState.NOT_SENT, game_id="1026053628", move="e2e4", fen=START_FEN
    )
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    await request(app, "/dismiss-write", method="POST", form={}, cookies=signed_in)
    assert writer.calls == []
    assert service.status.write.state is WriteState.IDLE


# --- the one irreversible action -----------------------------------------


@pytest.mark.asyncio
async def test_a_proven_unsent_move_can_be_resent_from_the_page(service, signed_in):
    writer = FakeWriter()
    service.writer = writer
    service._observe([a_game()])
    service._write = WriteStatus(
        state=WriteState.NOT_SENT, game_id="1026053628", move="e2e4", fen=START_FEN
    )
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/retry-write", method="POST", form={}, cookies=signed_in)
    assert reply.status == 303
    assert writer.calls == [("1026053628", "e2e4", START_FEN, 1788861472)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [WriteState.UNVERIFIED, WriteState.ACCEPTED, WriteState.REJECTED, WriteState.IDLE],
)
async def test_any_other_state_is_a_conflict_not_a_resend(service, signed_in, state):
    """Reaching here means the state changed between the button being drawn and
    pressed, which is exactly when a resend would be a duplicate."""
    writer = FakeWriter()
    service.writer = writer
    service._write = WriteStatus(
        state=state, game_id="1026053628", move="e2e4", fen=START_FEN
    )
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/retry-write", method="POST", form={}, cookies=signed_in)
    assert reply.status == 409
    assert writer.calls == []


@pytest.mark.asyncio
async def test_the_resend_button_is_absent_unless_it_is_safe(service, signed_in):
    """The defence a person actually experiences is the button not being there."""
    service._write = WriteStatus(state=WriteState.UNVERIFIED, move="e2e4")
    app = create_app(service, a_config(), signing_key=SIGNING_KEY)
    reply = await request(app, "/", cookies=signed_in)
    assert "/retry-write" not in reply.text
    assert "/dismiss-write" in reply.text


# --- a misconfigured appliance -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/", "/login", "/events", "/healthz", "/anything"])
async def test_an_unconfigured_appliance_explains_itself_on_every_path(path):
    """Including paths a bookmark might go straight to."""
    app = create_misconfigured_app(
        Config(problems=("web_password is not set in /boot/firmware/chessnut-bridge.conf",))
    )
    reply = await request(app, path)
    assert reply.status == 503
    assert "not configured" in reply.text
    assert "web_password" in reply.text


@pytest.mark.asyncio
async def test_a_config_with_no_password_never_yields_a_working_ui(service):
    """The worst outcome of a typo would be a working-but-open UI, so
    create_app itself refuses rather than trusting its caller to check."""
    app = create_app(
        service,
        Config(chesscom_username="nbaronmorgan", problems=("no web_password",)),
        signing_key=SIGNING_KEY,
    )
    reply = await request(app, "/login", method="POST", form={"password": ""})
    assert reply.status == 503


@pytest.mark.asyncio
async def test_the_problem_page_says_how_to_fix_it():
    app = create_misconfigured_app(Config(problems=("no configuration file found",)))
    reply = await request(app, "/")
    assert "SD card" in reply.text
    assert "chessnut-bridge.conf" in reply.text


# --- the update stream ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_stream_sends_the_status_immediately(service, signed_in):
    app = create_app(
        service, a_config(), signing_key=SIGNING_KEY, stream_interval=0.001
    )
    # One chunk, then the client hangs up like a closed tab -- so what this asserts
    # is that the first event arrives without waiting out an interval.
    reply = await request(app, "/events", cookies=signed_in, stream_chunks=1)
    assert reply.header("content-type").startswith("text/event-stream")
    assert "event: status" in reply.text
    assert "data:" in reply.text


@pytest.mark.asyncio
async def test_the_stream_is_not_cached(service, signed_in):
    app = create_app(service, a_config(), signing_key=SIGNING_KEY, stream_interval=0.001)
    reply = await request(app, "/events", cookies=signed_in, stream_chunks=1)
    assert reply.header("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_the_stream_carries_the_same_markup_the_page_does(service, signed_in):
    """One description of the state, not two that can drift apart."""
    service._observe([a_game("777")])
    app = create_app(service, a_config(), signing_key=SIGNING_KEY, stream_interval=0.001)
    page = await request(app, "/", cookies=signed_in)
    stream = await request(app, "/events", cookies=signed_in, stream_chunks=1)
    assert "Game 777" in page.text
    assert "Game 777" in stream.text
    assert 'id="status"' in stream.text


def test_multi_line_markup_is_encoded_as_one_event():
    """A body that skipped the per-line prefix would silently truncate at the
    first newline, and HTML is full of them."""
    from bridge.web.app import _sse

    encoded = _sse("status", "<div>\n<p>hello</p>\n</div>")
    assert encoded.startswith("event: status\n")
    assert encoded.endswith("\n\n")
    assert encoded.count("data: ") == 3
    assert encoded.count("\n\n") == 1, "a blank line mid-event would end it early"


# --- signing in to chess.com ----------------------------------------------
#
# The form that takes the owner's real chess.com password. Two things matter here
# beyond "it works": the password must never come back in the HTML, and each
# failure must reach the page in words the owner can act on -- a 2FA prompt and a
# typo are not the same problem.


class FakeLogin:
    def __init__(self, session=None, raising=None):
        self.session = session or write.Session(cookies={"PHPSESSID": "fresh"})
        self.raising = raising
        self.attempts = []

    async def log_in(self, credentials):
        self.attempts.append(credentials)
        if self.raising is not None:
            raise self.raising
        return self.session


def app_with_login(raising=None, session=None, credentials=None):
    """An app whose service signs in through a fake, plus the pieces to inspect."""
    svc = make_service(session=session)
    svc.credential_store = InMemoryCredentialStore(credentials)
    fake = FakeLogin(raising=raising)
    svc.login_factory = lambda: fake
    return create_app(svc, a_config(), signing_key=SIGNING_KEY), svc, fake


@pytest.mark.asyncio
async def test_the_sign_in_form_is_served_to_a_signed_in_owner(signed_in):
    app, _svc, _fake = app_with_login()
    reply = await request(app, "/chesscom/login", cookies=signed_in)
    assert reply.status == 200
    assert "Chess.com password" in reply.text


@pytest.mark.asyncio
async def test_the_form_is_prefilled_with_the_configured_username(signed_in):
    """The username is already on the SD card; making them retype it is a chance
    to get it wrong."""
    app, _svc, _fake = app_with_login()
    reply = await request(app, "/chesscom/login", cookies=signed_in)
    assert "nbaronmorgan" in reply.text


@pytest.mark.asyncio
async def test_the_form_says_what_happens_to_the_password(signed_in):
    """This is the screen where the owner decides to hand over a real account
    password, and the moment the trade-off is worth stating."""
    app, _svc, _fake = app_with_login()
    reply = await request(app, "/chesscom/login", cookies=signed_in)
    assert "encrypted" in reply.text
    assert "two-factor" in reply.text
    assert "SD card" in reply.text


@pytest.mark.asyncio
async def test_a_successful_sign_in_stores_it_and_returns_to_the_page(signed_in):
    app, svc, fake = app_with_login()
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "sekrit", "remember": "on"},
    )
    assert reply.status == 303
    assert reply.location == "/"
    assert fake.attempts[0].password == "sekrit"
    assert svc.status.credentials_stored


@pytest.mark.asyncio
async def test_leaving_the_box_unticked_stores_no_password(signed_in):
    app, svc, _fake = app_with_login()
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "sekrit"},
    )
    assert reply.status == 303
    assert svc.status.logged_in_as == "nbaronmorgan"
    assert not svc.status.credentials_stored


@pytest.mark.asyncio
async def test_a_wrong_password_re_renders_the_form_with_chesscoms_words(signed_in):
    app, _svc, _fake = app_with_login(
        raising=chesscom_login.BadCredentials("chess.com did not accept that password")
    )
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "wrong"},
    )
    assert reply.status == 401
    assert "did not accept" in reply.text


@pytest.mark.asyncio
async def test_the_password_is_never_echoed_back_into_the_page(signed_in):
    """Retyping it is a small cost next to a page that holds it in its HTML --
    where a screenshot or a saved page would carry it."""
    app, _svc, _fake = app_with_login(
        raising=chesscom_login.BadCredentials("nope")
    )
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "sekrit-not-in-html"},
    )
    assert "sekrit-not-in-html" not in reply.text


@pytest.mark.asyncio
async def test_a_two_factor_account_is_told_why_rather_than_blamed(signed_in):
    app, _svc, _fake = app_with_login(
        raising=chesscom_login.VerificationRequired("chess.com wants two-factor")
    )
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "right-password"},
    )
    assert reply.status == 409
    assert "two-factor" in reply.text
    assert "cannot be completed by the appliance" in reply.text


@pytest.mark.asyncio
async def test_a_temporary_failure_is_distinguished_from_a_refusal(signed_in):
    app, _svc, _fake = app_with_login(
        raising=chesscom_login.LoginUnavailable("chess.com is unreachable")
    )
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "sekrit"},
    )
    assert reply.status == 502
    assert "unreachable" in reply.text


@pytest.mark.asyncio
async def test_an_empty_form_asks_again_without_calling_chesscom(signed_in):
    app, _svc, fake = app_with_login()
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": ""},
    )
    assert reply.status == 400
    assert fake.attempts == []


@pytest.mark.asyncio
async def test_an_unexpected_failure_does_not_500_with_a_traceback(signed_in):
    """A form that dies with a stack trace on a machine with no screen tells the
    owner nothing at all."""
    app, _svc, _fake = app_with_login(raising=RuntimeError("boom"))
    reply = await request(
        app, "/chesscom/login", method="POST", cookies=signed_in,
        form={"username": "nbaronmorgan", "password": "sekrit"},
    )
    assert reply.status == 500
    assert "boom" not in reply.text
    assert "went wrong" in reply.text


@pytest.mark.asyncio
async def test_forgetting_the_password_keeps_the_session(signed_in):
    app, svc, _fake = app_with_login(
        session=a_session(),
        credentials=chesscom_login.Credentials(username="nbaronmorgan", password="p"),
    )
    reply = await request(app, "/chesscom/forget", method="POST", cookies=signed_in, form={})
    assert reply.status == 303
    assert not svc.status.credentials_stored
    assert svc.status.logged_in_as == "nbaronmorgan"


@pytest.mark.asyncio
async def test_trying_the_stored_password_now_is_a_post(signed_in):
    """State-changing, so it cannot be a GET that a browser prefetch would fire."""
    app, _svc, fake = app_with_login(
        credentials=chesscom_login.Credentials(username="nbaronmorgan", password="p")
    )
    reply = await request(app, "/chesscom/retry-login", method="POST", cookies=signed_in, form={})
    assert reply.status == 303
    assert len(fake.attempts) == 1


@pytest.mark.asyncio
async def test_the_status_page_offers_sign_in_when_signed_out(signed_in):
    app, _svc, _fake = app_with_login(session=None)
    reply = await request(app, "/", cookies=signed_in)
    assert "/chesscom/login" in reply.text
    # The old placeholder promised nothing would work; it must be gone.
    assert "not wired up yet" not in reply.text
