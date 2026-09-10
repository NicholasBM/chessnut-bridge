"""Tests for signing in to chess.com with a password.

This module talks to an undocumented form on somebody else's website, so the
thing worth defending is not that the happy path works -- it is that **every way
it can go wrong produces the right specific complaint**. On an appliance with no
screen, "sign-in failed" is indistinguishable from "the appliance is broken", and
the four failures below need four different actions from the owner:

* wrong password      -> retype it
* 2FA / verification  -> nothing to retype; this account cannot be used this way
* Cloudflare          -> a network problem, not an account problem
* changed login form  -> *this code* is stale, and no owner action will help

The other property under test is that the password does not leak. It is checked
against reprs and log output rather than assumed from a code reading, because
this project has leaked live credentials four separate times.
"""

import http.cookiejar
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom.login import (  # noqa: E402
    MIN_RETRY_SECONDS,
    _verification_marker,
    REMEMBER_COOKIE,
    SESSION_COOKIE,
    BadCredentials,
    ChallengePresented,
    ChessComLogin,
    Credentials,
    LoginError,
    LoginFormChanged,
    LoginUnavailable,
    SessionIncomplete,
    VerificationRequired,
    find_csrf_token,
)

PASSWORD = "sekrit-chesscom-password"
USERNAME = "nbaronmorgan"

LOGIN_PAGE = (
    b"<!doctype html><html><body><form action='/login_check' method='post'>"
    b"<input type='hidden' name='_token' value='the-csrf-token-value'>"
    b"<input name='_username'><input name='_password' type='password'>"
    b"</form></body></html>"
)

#: Deliberately carries the furniture from a real chess.com page: the SEO meta
#: tag and the JS feature-flag array. A minimal fake landing page is what let a
#: catastrophic false positive through -- see the regression tests at the end.
REAL_PAGE_FURNITURE = (
    b'<meta name="p:domain_verify" content="314c7ba9469cc171a12a46b43e0e2aed" />'
    b'<meta name="google-site-verification" content="n7BdKb0xn1E9tRJXvmMxE3Y" />'
    b"<script>window.flags = ['recovery_turnstile_captcha', "
    b"'signup_issue_challenge_captcha', 'stockfish_release_a'];</script>"
)

HOME_PAGE = (
    b"<!doctype html><html><head>" + REAL_PAGE_FURNITURE + b"</head><body><h1>Home</h1>"
    b"<script>window.chesscom = {csrfToken: 'play-csrf-abc123'};</script>"
    b"</body></html>"
)


class FakeResponse:
    """Enough of an HTTP response for the module under test."""

    def __init__(self, body: bytes, url: str, status: int = 200, content_type="text/html"):
        self._body = body
        self._url = url
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Serves canned replies and can drop cookies into the jar like a site would.

    The jar is the real :mod:`http.cookiejar` one the class under test reads, so
    the cookie-collection path is exercised rather than stubbed.
    """

    def __init__(self, jar: http.cookiejar.CookieJar, replies: list):
        self.jar = jar
        self.replies = list(replies)
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        cookies, response = reply
        for name, value in (cookies or {}).items():
            self.jar.set_cookie(_cookie(name, value))
        return response


def _cookie(name: str, value: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=".chess.com", domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    )


def a_login(replies: list) -> ChessComLogin:
    jar = http.cookiejar.CookieJar()
    return ChessComLogin(opener=FakeOpener(jar, replies), jar=jar)


def signed_in_replies(cookies=None, landing=HOME_PAGE, url="https://www.chess.com/home"):
    """The two-request happy path: the form, then a successful POST."""
    return [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        (
            cookies if cookies is not None else {SESSION_COOKIE: "abc", REMEMBER_COOKIE: "def"},
            FakeResponse(landing, url),
        ),
    ]


def credentials() -> Credentials:
    return Credentials(username=USERNAME, password=PASSWORD)


# --- the CSRF token ---------------------------------------------------------


def test_the_token_is_found_in_either_attribute_order():
    """A hidden input is written both ways, and a one-order regex would report a
    working site as a changed form."""
    assert find_csrf_token(LOGIN_PAGE) == "the-csrf-token-value"
    reversed_order = b"<input value='tok-2' type='hidden' name='_token'>"
    assert find_csrf_token(reversed_order) == "tok-2"


def test_a_page_without_a_token_is_reported_as_a_changed_form():
    with pytest.raises(LoginFormChanged) as caught:
        find_csrf_token(b"<html><body>nothing here</body></html>")
    # The message has to point at this code, not at the owner's password.
    assert "changed" in str(caught.value)
    assert "updating" in str(caught.value)


# --- the happy path --------------------------------------------------------


def test_a_successful_sign_in_returns_the_collected_cookies():
    session = a_login(signed_in_replies()).log_in_sync(credentials())
    assert session.cookies[SESSION_COOKIE] == "abc"
    assert session.cookies[REMEMBER_COOKIE] == "def"


def test_the_posted_form_carries_the_token_and_asks_to_be_remembered():
    """``_remember_me`` is not optional: without it the session dies in hours and
    an appliance that needs its password retyped daily is not an appliance."""
    client = a_login(signed_in_replies())
    client.log_in_sync(credentials())
    posted = client._opener.requests[1].data.decode()
    assert "_token=the-csrf-token-value" in posted
    assert "_remember_me=on" in posted
    assert f"_username={USERNAME}" in posted


def test_a_csrf_token_on_the_landing_page_is_picked_up_for_later_writes():
    session = a_login(signed_in_replies()).log_in_sync(credentials())
    assert session.csrf_token == "play-csrf-abc123"


def test_a_landing_page_without_a_csrf_token_still_signs_in():
    """The submit endpoint's need for a token was never established, so a missing
    one must not fail a sign-in that otherwise worked."""
    session = a_login(signed_in_replies(landing=b"<html>no token here</html>")).log_in_sync(
        credentials()
    )
    assert session.csrf_token is None
    assert session.cookies[SESSION_COOKIE] == "abc"


# --- the failures, each with its own name ----------------------------------


def test_landing_back_on_the_login_form_means_the_password_was_wrong():
    replies = [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        ({}, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
    ]
    with pytest.raises(BadCredentials):
        a_login(replies).log_in_sync(credentials())


@pytest.mark.parametrize(
    "marker",
    ["two-factor", "authenticator", "verification", "captcha"],
)
def test_a_verification_prompt_is_not_reported_as_a_bad_password(marker):
    """The likeliest real-world failure. Telling the owner their password is wrong
    when the account simply wants 2FA sends them to change something that was
    never broken."""
    body = f"<html><body>Please complete {marker} to continue</body></html>".encode()
    replies = [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        ({}, FakeResponse(body, "https://www.chess.com/login/verify")),
    ]
    with pytest.raises(VerificationRequired) as caught:
        a_login(replies).log_in_sync(credentials())
    assert not caught.value.retryable  # retrying cannot fix it


def test_a_verification_prompt_says_what_to_do_about_it():
    replies = [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        ({}, FakeResponse(
            b"<html><body>Two-factor authentication is required to continue."
            b"</body></html>",
            "https://www.chess.com/x",
        )),
    ]
    with pytest.raises(VerificationRequired) as caught:
        a_login(replies).log_in_sync(credentials())
    message = caught.value.for_display
    assert "two-factor" in message.lower() or "2fa" in message.lower()


def test_a_challenge_instead_of_the_login_page_is_its_own_failure():
    """An interception page and a wrong password look identical by status code and
    need opposite responses."""
    challenge = b"<html><body>Just a moment... Checking your browser</body></html>"
    replies = [(None, FakeResponse(challenge, "https://www.chess.com/login"))]
    with pytest.raises(ChallengePresented) as caught:
        a_login(replies).log_in_sync(credentials())
    assert caught.value.retryable


def test_an_html_403_is_a_challenge_rather_than_a_refusal():
    error = urllib.error.HTTPError(
        "https://www.chess.com/login", 403, "Forbidden",
        {"Content-Type": "text/html"}, None,
    )
    error.read = Mock(return_value=b"<html>Cloudflare</html>")
    with pytest.raises(ChallengePresented):
        a_login([error]).log_in_sync(credentials())


def test_a_sign_in_with_no_session_cookie_is_refused_rather_than_stored():
    """Storing a session with no PHPSESSID would turn a clear failure now into a
    baffling failed move later."""
    with pytest.raises(SessionIncomplete) as caught:
        a_login(signed_in_replies(cookies={})).log_in_sync(credentials())
    assert SESSION_COOKIE in str(caught.value)


def test_a_missing_remember_cookie_is_a_warning_not_a_failure(caplog):
    """The session works; it just will not last. 'It worked for a day and then
    stopped' is otherwise an unexplainable symptom."""
    with caplog.at_level(logging.WARNING):
        session = a_login(signed_in_replies(cookies={SESSION_COOKIE: "abc"})).log_in_sync(
            credentials()
        )
    assert session.cookies[SESSION_COOKIE] == "abc"
    assert REMEMBER_COOKIE in caplog.text
    assert "short-lived" in caplog.text


def test_a_network_failure_is_retryable_and_says_nothing_was_learned():
    with pytest.raises(LoginUnavailable) as caught:
        a_login([urllib.error.URLError("no route to host")]).log_in_sync(credentials())
    assert caught.value.retryable


def test_a_server_error_is_retryable():
    error = urllib.error.HTTPError(
        "https://www.chess.com/login", 502, "Bad Gateway", {}, None
    )
    error.read = Mock(return_value=b"")
    with pytest.raises(LoginUnavailable) as caught:
        a_login([error]).log_in_sync(credentials())
    assert caught.value.retryable


def test_rate_limiting_is_retryable_and_named_as_such():
    error = urllib.error.HTTPError(
        "https://www.chess.com/login", 429, "Too Many Requests", {}, None
    )
    error.read = Mock(return_value=b"")
    with pytest.raises(LoginUnavailable) as caught:
        a_login([error]).log_in_sync(credentials())
    assert "rate-limit" in str(caught.value)


def test_empty_credentials_are_refused_without_a_request():
    client = a_login([])
    with pytest.raises(BadCredentials):
        client.log_in_sync(Credentials(username="", password=""))
    assert client._opener.requests == []  # nothing was sent


# --- the password must not leak --------------------------------------------


def test_credentials_do_not_render_the_password():
    creds = credentials()
    assert PASSWORD not in repr(creds)
    assert PASSWORD not in str(creds)
    assert PASSWORD not in f"{creds}"
    assert USERNAME in repr(creds)  # the username is public and worth showing


def test_a_successful_sign_in_does_not_log_the_password(caplog):
    with caplog.at_level(logging.DEBUG):
        a_login(signed_in_replies()).log_in_sync(credentials())
    assert PASSWORD not in caplog.text


def test_a_failed_sign_in_does_not_log_or_report_the_password(caplog):
    replies = [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        ({}, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
    ]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BadCredentials) as caught:
            a_login(replies).log_in_sync(credentials())
    assert PASSWORD not in caplog.text
    assert PASSWORD not in caught.value.for_display


def test_credentials_in_a_traceback_do_not_carry_the_password():
    """Reprs end up in tracebacks, which end up pasted into chats."""
    creds = credentials()
    try:
        raise RuntimeError(f"failed with {creds!r}")
    except RuntimeError as exc:
        assert PASSWORD not in str(exc)


# --- the politeness floor --------------------------------------------------


def test_there_is_a_floor_under_retry_frequency():
    """Not a correctness rule: it is what keeps unattended recovery from looking
    like a credential-stuffing script to whatever watches that endpoint."""
    assert MIN_RETRY_SECONDS >= 300


def test_every_failure_declares_whether_retrying_could_help():
    """The service decides whether to keep trying from this flag alone, so a type
    that forgot to set it would either loop for ever or give up too early."""
    for failure in (
        BadCredentials, VerificationRequired, LoginFormChanged,
        ChallengePresented, SessionIncomplete, LoginUnavailable, LoginError,
    ):
        assert isinstance(failure("x").retryable, bool)


# --- regression: the false "you need 2FA" ----------------------------------
#
# Found on 2026-09-09, on the first attempt against the live site, by an owner
# whose account has no 2FA at all. Two mistakes compounded:
#
# 1. ``"verification"`` was matched as a bare substring, and every chess.com page
#    carries ``<meta name="google-site-verification">`` in its head. Likewise
#    ``"captcha"`` against feature-flag names in their JS bootstrap.
# 2. That check ran *before* the session-cookie check, so a sign-in that had
#    actually succeeded was discarded on the strength of the substring.
#
# The result was a feature that could not work for anybody, on any account, and
# which then deleted the stored password because VerificationRequired is not
# retryable. The tests below are what would have caught it: fixtures with real
# page furniture in them, and an explicit assertion about the *order* of evidence.


def test_seo_meta_tags_are_not_a_two_factor_prompt():
    """`google-site-verification` is in the head of every page chess.com serves."""
    page = (
        '<html><head><meta name="google-site-verification" content="abc123" />'
        '<meta name="p:domain_verify" content="def456" /></head>'
        "<body><h1>Home</h1></body></html>"
    )
    assert _verification_marker("https://www.chess.com/home", page) is None


def test_javascript_feature_flag_names_are_not_a_captcha():
    """Flag *names* ship on every page; they say nothing about what is displayed."""
    page = (
        "<html><head><script>window.flags = ['recovery_turnstile_captcha', "
        "'signup_issue_challenge_captcha'];</script></head><body>Home</body></html>"
    )
    assert _verification_marker("https://www.chess.com/home", page) is None


def test_a_successful_sign_in_survives_a_page_full_of_the_old_markers():
    """The whole bug, end to end: this landing page trips every old marker, and
    the sign-in must still succeed because a session cookie was granted."""
    session = a_login(signed_in_replies()).log_in_sync(credentials())
    assert session.cookies[SESSION_COOKIE] == "abc"
    assert b"google-site-verification" in HOME_PAGE  # the fixture really is dirty
    assert b"captcha" in HOME_PAGE


def test_a_granted_session_outranks_any_page_heuristic():
    """Evidence before inference, asserted directly.

    Even a landing page that genuinely says "two-factor authentication" cannot
    revoke a session that chess.com demonstrably issued -- chess.com would not
    have set the cookie if it still wanted a second factor. Getting this order
    wrong is what discarded a real session, so it is pinned here.
    """
    shouting = b"<html><body>two-factor authentication is available</body></html>"
    session = a_login(signed_in_replies(landing=shouting)).log_in_sync(credentials())
    assert session.cookies[SESSION_COOKIE] == "abc"


def test_a_real_prompt_with_no_session_is_still_reported():
    """The tightening must not go so far that a genuine prompt is missed."""
    replies = [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        ({}, FakeResponse(
            b"<html><body><p>Enter the code we sent to your email.</p>"
            b"<label>Verification code</label></body></html>",
            "https://www.chess.com/login/verify",
        )),
    ]
    with pytest.raises(VerificationRequired) as caught:
        a_login(replies).log_in_sync(credentials())
    assert "login/verify" in str(caught.value)


@pytest.mark.parametrize(
    "path",
    ["/login/verify", "/verify-email", "/two-factor", "/2fa"],
)
def test_the_url_alone_is_enough_to_recognise_a_verification_step(path):
    """A redirect is the reliable signal: a path cannot contain page furniture."""
    assert _verification_marker(f"https://www.chess.com{path}", "") is not None


@pytest.mark.parametrize(
    "phrase",
    ["two-factor authentication", "authenticator app", "verification code",
     "g-recaptcha", "cf-turnstile"],
)
def test_a_prompt_rendered_in_the_page_body_is_still_caught(phrase):
    body = f"<html><body><div>{phrase}</div></body></html>"
    assert _verification_marker("https://www.chess.com/login_check", body) == phrase


def test_a_prompt_hidden_in_a_script_tag_does_not_count():
    """Scripts describe capability, not what the page is asking for right now."""
    body = f"<html><body><script>var x = 'verification code';</script></body></html>"
    assert _verification_marker("https://www.chess.com/home", body) is None


def test_a_session_cookie_on_the_login_page_is_not_success():
    """Symfony hands out a PHPSESSID to anonymous visitors too, so the cookie on
    its own is not the test -- landing away from the form is the other half."""
    replies = [
        (None, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
        ({SESSION_COOKIE: "anon"}, FakeResponse(LOGIN_PAGE, "https://www.chess.com/login")),
    ]
    with pytest.raises(BadCredentials):
        a_login(replies).log_in_sync(credentials())
