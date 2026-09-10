"""Signing in to chess.com with a username and password, unattended.

Why this exists at all
----------------------
Every other way of getting a session onto the appliance needs a person: paste
cookies from a laptop's dev tools, or run a browser on a machine with no screen.
Both mean the bridge stops working weeks later, at the moment the session lapses,
with nobody watching. The owner's instruction was the obvious one -- *let it have
the password* -- and that is the only option that survives a session expiring
while they are away.

What that costs, stated plainly
-------------------------------
**This is a transcription of an undocumented flow, not an API.** chess.com's login
is a Symfony form: a ``_token`` in the page, then a POST to ``/login_check``. That
shape has been stable for years and is what a browser does, but nothing obliges it
to stay. So this module's real job -- exactly like :mod:`bridge.chesscom.write` --
is to fail *specifically* when reality stops matching, because "sign-in failed"
with no reason on a machine with no screen is the worst outcome available.

The failures worth telling apart, and why each is its own type:

* :class:`BadCredentials` -- the password is wrong. Nothing to retry.
* :class:`VerificationRequired` -- 2FA, a new-device email, or a captcha. A person
  must act, and no amount of retrying helps. **The likeliest reason this feature
  does not work on a given account**, so it says what to do rather than blaming
  the password.
* :class:`ChallengePresented` -- Cloudflare answered instead of the site. A
  network-shaped problem wearing an auth-shaped mask.
* :class:`LoginFormChanged` -- the ``_token`` or the form is not where this module
  expects. Means *this code* is stale, not that anything is wrong with the account,
  and it is the failure a future reader most needs pointed at.
* :class:`SessionIncomplete` -- the POST looked fine but no session cookie came
  back. Refusing here is the point: storing a useless session would turn a clear
  failure now into a mystified failed move later.
* :class:`LoginUnavailable` -- timeout, network, 5xx. Try again later.

Retrying is safe here, unlike the write path
--------------------------------------------
``write.py`` never retries because a POST that times out may still have applied a
move. Logging in has no such hazard: it is idempotent from our side, and a second
attempt costs nothing but a request. What it *can* cost is the account -- hammering
a login endpoint is how bot detection gets triggered -- so :data:`MIN_RETRY_SECONDS`
puts a floor under how often the appliance will try, and the caller is expected to
honour it rather than loop.

The password never appears in a log line, a traceback or a repr. That is enforced
by :class:`Credentials` refusing to render itself, because the single
best-evidenced accident in this project is credentials turning up somewhere nobody
meant to put them.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .write import USER_AGENT, Session

log = logging.getLogger(__name__)

LOGIN_URL = "https://www.chess.com/login"
LOGIN_CHECK_URL = "https://www.chess.com/login_check"

#: Cookie that means "we have a session at all". Everything else is a bonus.
SESSION_COOKIE = "PHPSESSID"

#: The long-lived one. Without it a session dies in hours rather than weeks, so
#: ``_remember_me`` is always requested -- an appliance that has to be re-taught
#: its password every day is not an appliance.
REMEMBER_COOKIE = "CHESSCOM_REMEMBERME"

#: Don't retry a failed sign-in more often than this. Not a correctness rule: it
#: is the difference between an appliance that quietly recovers and one that looks
#: like a credential-stuffing script to whatever watches that endpoint.
MIN_RETRY_SECONDS = 900.0

_TIMEOUT = 30.0

#: Path fragments that mean chess.com has routed us to a step only a person can
#: finish. The URL is the trustworthy signal: a real verification step *navigates*
#: somewhere, and a path cannot be contaminated by page furniture the way a body
#: can.
_VERIFICATION_PATHS = (
    "/login/verify",
    "/verify-email",
    "/verify_email",
    "/two-factor",
    "/two_factor",
    "/2fa",
)

#: Phrases that mean a prompt is being *shown*, rather than merely mentioned.
#:
#: Every entry is either several words of visible prose or a widget's own markup
#: hook, and that is a correction rather than a style choice. The first version of
#: this list held single words, and two of them matched every page chess.com
#: serves: ``"verification"`` inside ``<meta name="google-site-verification">`` in
#: the head, and ``"captcha"`` inside feature-flag names like
#: ``'signup_issue_challenge_captcha'`` in the JS bootstrap. Checked against a
#: real page on 2026-09-09, that made *every* sign-in -- including every
#: successful one -- report a two-factor prompt.
#:
#: The lesson kept here: a false positive on this list is not the harmless
#: direction. :class:`VerificationRequired` is not retryable, so the service
#: reacts by deleting the stored password. Broad matching costs more than it saves.
_VERIFICATION_PHRASES = (
    "two-factor authentication",
    "two factor authentication",
    "authenticator app",
    "verification code",
    "enter the code",
    "we sent a code",
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
    "data-sitekey",
)

#: Signs that something intercepted the request instead of chess.com answering it
#: -- overwhelmingly Cloudflare. Kept separate from the verification phrases
#: because the two need opposite reactions: an interception is a network-shaped
#: problem worth retrying later, while a verification prompt is terminal until a
#: person acts. Collapsing them would make a transient block delete the stored
#: password. All checked against a healthy login page on 2026-09-09.
_CHALLENGE_PHRASES = (
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-browser-verification",
    "challenge-platform",
    "cf-turnstile",
    "/cdn-cgi/challenge",
    "enable javascript and cookies to continue",
)

#: Head, scripts and comments: the three places every false positive came from.
#: SEO meta tags, JS feature-flag arrays and commented-out markup all talk about
#: verification and captchas on pages that are showing neither.
_HEAD_OR_SCRIPT = re.compile(
    r"<head\b.*?</head>|<script\b.*?</script>|<!--.*?-->", re.I | re.S
)

#: Both attribute orders, because a hidden input is written either way and a
#: single-order regex would turn a working site into :class:`LoginFormChanged`.
_TOKEN_PATTERNS = (
    re.compile(rb"""name=["']_token["'][^>]{0,200}?value=["']([^"']+)["']""", re.I),
    re.compile(rb"""value=["']([^"']+)["'][^>]{0,200}?name=["']_token["']""", re.I),
)


class LoginError(RuntimeError):
    """Signing in failed. ``for_display`` is safe to show; it never holds the
    password, because nothing here is ever given the password to render."""

    #: Whether trying again later, unattended, could plausibly work. False for
    #: anything a person has to fix, so the appliance does not sit in a loop
    #: re-POSTing a password that will never be accepted.
    retryable = False

    def __init__(self, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.status = status

    @property
    def for_display(self) -> str:
        return str(self)


class BadCredentials(LoginError):
    """chess.com rejected the username or password."""


class VerificationRequired(LoginError):
    """The account needs 2FA, an email confirmation, or a captcha solved.

    Terminal for an unattended appliance. This is the expected outcome on an
    account with two-factor authentication enabled, and the message says so
    rather than implying the password was wrong.
    """


class ChallengePresented(LoginError):
    """Cloudflare (or something like it) replied instead of the site.

    Its own type for the same reason ``write.ChallengePresented`` is: an HTML
    interception page and an application refusal look identical if you only read
    the status code, and they need opposite responses.
    """

    retryable = True


class LoginFormChanged(LoginError):
    """The login page is not the shape this module was written against.

    Means this code needs updating -- not that the owner did anything wrong.
    Worth its own type so that message reaches whoever is reading the journal a
    year from now.
    """


class SessionIncomplete(LoginError):
    """The sign-in appeared to work but produced no usable session cookie."""

    retryable = True


class LoginUnavailable(LoginError):
    """Timeout, network failure, or a 5xx. Nothing was learned."""

    retryable = True


@dataclass(frozen=True)
class Credentials:
    """A chess.com username and password, which never render themselves.

    The username alone is public and is shown in the UI; the password is not, and
    :meth:`__repr__` is the enforcement rather than a convention, because this
    object will end up in tracebacks.
    """

    username: str
    password: str

    def __repr__(self) -> str:
        return f"Credentials(username={self.username!r}, password=<redacted>)"

    __str__ = __repr__

    @property
    def is_populated(self) -> bool:
        return bool(self.username and self.password)


def find_csrf_token(html: bytes) -> str:
    """The ``_token`` hidden field from the login page.

    Raises :class:`LoginFormChanged` when it is absent, which is the honest
    reading: either the page changed shape or something served us a page that is
    not the login form at all.
    """
    for pattern in _TOKEN_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1).decode("utf-8", errors="replace")
    raise LoginFormChanged(
        "the chess.com login page has no _token field where this version expects "
        "one, so the sign-in form has probably changed and this code needs updating"
    )


def _looks_like_html(body: bytes, content_type: str) -> bool:
    if "text/html" in content_type.lower():
        return True
    return body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))


def _visible(text: str) -> str:
    """The page with its head, scripts and comments removed.

    Not a parser and not trying to be: it only has to stop the machinery that
    talks *about* verification from being mistaken for a page that is *asking* for
    it. See :data:`_VERIFICATION_PHRASES` for what that cost when it was missing.
    """
    return _HEAD_OR_SCRIPT.sub(" ", text)


def _verification_marker(final_url: str, body: str = "") -> str | None:
    """Which human-only step this page represents, if any. Named, for the message.

    The URL is consulted first because it is evidence rather than inference: a
    verification step redirects, and a path has no room for SEO tags or feature
    flags. The body is a fallback for a prompt rendered in place, and is read only
    after the furniture has been stripped out of it.
    """
    path = urllib.parse.urlparse(final_url).path.lower()
    for fragment in _VERIFICATION_PATHS:
        if fragment in path:
            return fragment.strip("/").replace("_", "-")

    lowered = _visible(body).lower()
    for phrase in _VERIFICATION_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def _challenge_marker(body: str) -> str | None:
    """Whether something intercepted this instead of chess.com answering it.

    Matched against the raw body rather than the visible text: an interception
    page *is* mostly script and markup hooks, so stripping those would throw away
    the only evidence there is.
    """
    lowered = body.lower()
    for phrase in _CHALLENGE_PHRASES:
        if phrase in lowered:
            return phrase
    return None


class ChessComLogin:
    """Performs the sign-in. Owns a cookie jar and nothing else.

    A fresh jar per instance on purpose: reusing cookies from a previous attempt
    is how a "successful" login ends up carrying a stale session that fails on
    first use, which is precisely the failure this module exists to make loud.
    """

    def __init__(
        self,
        opener: Any | None = None,
        jar: http.cookiejar.CookieJar | None = None,
    ) -> None:
        self._jar = jar if jar is not None else http.cookiejar.CookieJar()
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    # --- the flow ----------------------------------------------------------

    def log_in_sync(self, credentials: Credentials) -> Session:
        """Sign in and return the session. Raises a specific error otherwise.

        Two requests: the form (for its ``_token`` and its cookies) and the POST.
        The username is logged; the password is not passed to anything that
        formats it.
        """
        if not credentials.is_populated:
            raise BadCredentials("a chess.com username and password are both needed")

        log.info("signing in to chess.com as %s", credentials.username)
        token = self._fetch_token()
        return self._post_credentials(credentials, token)

    def _fetch_token(self) -> str:
        body, content_type, _url, _status = self._request(
            urllib.request.Request(
                LOGIN_URL,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
            ),
            what="the chess.com login page",
        )
        # A challenge here is indistinguishable from the login page by status
        # alone -- both are 200 HTML -- so the missing ``_token`` is what decides
        # it, and the marker only sharpens the message. Requiring both is what
        # keeps a healthy page that merely *mentions* something from being called
        # an interception.
        text = body.decode("utf-8", errors="replace")
        marker = _challenge_marker(text) or _verification_marker("", text)
        if marker and b"_token" not in body:
            raise ChallengePresented(
                f"the chess.com login page came back as a {marker} challenge rather "
                "than a form, so signing in from this device is being blocked"
            )
        del content_type
        return find_csrf_token(body)

    def _post_credentials(self, credentials: Credentials, token: str) -> Session:
        form = urllib.parse.urlencode(
            {
                "_username": credentials.username,
                "_password": credentials.password,
                "_token": token,
                # Requested every time: see REMEMBER_COOKIE. A session that dies
                # in hours defeats the point of storing a password at all.
                "_remember_me": "on",
                "_target_path": "https://www.chess.com/home",
            }
        ).encode()

        request = urllib.request.Request(
            LOGIN_CHECK_URL,
            data=form,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html,application/xhtml+xml",
                "Origin": "https://www.chess.com",
                "Referer": LOGIN_URL,
            },
            method="POST",
        )
        body, content_type, final_url, status = self._request(
            request, what="the chess.com sign-in"
        )

        text = body.decode("utf-8", errors="replace")
        cookies = self.cookies()

        # Success is established *first*, and deliberately before any failure
        # heuristic runs. A session cookie on a page that is not the login form is
        # positive evidence that chess.com signed us in; every check below it is an
        # inference about somebody else's HTML.
        #
        # The original order asked "does this page mention verification?" before
        # "did we get a session?", and threw away real sessions on the strength of
        # a substring. Observed on 2026-09-09: `google-site-verification` in the
        # head of the landing page reported a successful sign-in as a 2FA prompt,
        # and because that error is not retryable the stored password was then
        # deleted. Evidence must outrank inference, in that order, permanently.
        if SESSION_COOKIE in cookies and not _is_login_page(final_url):
            if REMEMBER_COOKIE not in cookies:
                # Not fatal: the session works, it just will not last. Said out
                # loud because "it worked for a day and then stopped" is otherwise
                # a baffling symptom.
                log.warning(
                    "signed in but chess.com sent no %s cookie; the session will be "
                    "short-lived and will need signing in again sooner",
                    REMEMBER_COOKIE,
                )
            log.info(
                "signed in to chess.com as %s (%d cookies)",
                credentials.username,
                len(cookies),
            )
            return Session(cookies=cookies, csrf_token=_csrf_from(text))

        marker = _verification_marker(final_url, text)
        if marker:
            raise VerificationRequired(
                f"chess.com wants extra verification ({marker}) before it will sign "
                "this device in. Two-factor authentication and new-device checks "
                "cannot be completed by the appliance -- sign in once in a browser, "
                "or turn 2FA off for this account, then try again",
                status=status,
            )

        # Landing back on the login form is how a Symfony login failure presents:
        # it redirects to the form and re-renders it with an error flash.
        if _is_login_page(final_url) or (
            _looks_like_html(body, content_type) and b"_token" in body and "login" in final_url
        ):
            raise BadCredentials(
                "chess.com did not accept that username and password",
                status=status,
            )

        raise SessionIncomplete(
            f"signing in returned no {SESSION_COOKIE} cookie (got "
            f"{', '.join(sorted(cookies)) or 'nothing'}), so there is no session "
            "to store. Nothing was saved rather than saving something unusable",
            status=status,
        )

    # --- plumbing ----------------------------------------------------------

    def _request(
        self, request: urllib.request.Request, *, what: str
    ) -> tuple[bytes, str, str, int]:
        """One request, with every failure mapped to a specific type."""
        try:
            with self._opener.open(request, timeout=_TIMEOUT) as response:
                body = response.read()
                headers = getattr(response, "headers", None)
                content_type = headers.get("Content-Type", "") if headers else ""
                final_url = response.geturl()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            body = exc.read() or b""
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            raise self._classify(exc.code, body, content_type, what) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LoginUnavailable(f"could not reach {what}: {exc}") from exc
        return body, content_type, final_url, status

    def _classify(
        self, status: int, body: bytes, content_type: str, what: str
    ) -> LoginError:
        text = body.decode("utf-8", errors="replace")
        # Interception is tested first: it is the retryable one, and mistaking it
        # for a verification prompt would have the service throw the password away
        # over what is really a network problem.
        challenge = _challenge_marker(text)
        if challenge:
            return ChallengePresented(
                f"something intercepted {what} and answered with a {challenge} page "
                "rather than chess.com; this is a network-level block, not a problem "
                "with the account",
                status=status,
            )
        marker = _verification_marker("", text)
        if marker:
            return VerificationRequired(
                f"chess.com wants extra verification ({marker}) before signing in; "
                "this cannot be completed by the appliance",
                status=status,
            )
        if status in (401, 403) and not _looks_like_html(body, content_type):
            return BadCredentials(
                f"chess.com refused the sign-in (HTTP {status})", status=status
            )
        if status in (403, 503) and _looks_like_html(body, content_type):
            return ChallengePresented(
                f"an HTML page rather than the site answered {what} (HTTP {status}); "
                "the request was probably intercepted before reaching chess.com",
                status=status,
            )
        if status == 429:
            return LoginUnavailable(
                f"chess.com is rate-limiting sign-in attempts (HTTP {status}); "
                "waiting rather than trying again immediately",
                status=status,
            )
        if status >= 500:
            return LoginUnavailable(
                f"chess.com had trouble with {what} (HTTP {status})", status=status
            )
        return LoginError(f"HTTP {status} from {what}", status=status)

    def cookies(self) -> dict[str, str]:
        """Every chess.com cookie the jar has collected."""
        return {
            cookie.name: cookie.value or ""
            for cookie in self._jar
            if "chess.com" in (cookie.domain or "")
        }

    async def log_in(self, credentials: Credentials) -> Session:
        """Async wrapper: urllib blocks, so it runs off the event loop."""
        return await asyncio.to_thread(self.log_in_sync, credentials)


def _is_login_page(url: str) -> bool:
    return urllib.parse.urlparse(url).path.rstrip("/") in ("/login", "/login_check")


_CSRF_IN_PAGE = re.compile(
    rb"""["']?csrf(?:_token|Token)["']?\s*[:=]\s*["']([A-Za-z0-9_\-]{8,})["']""", re.I
)


def _csrf_from(text: str) -> str | None:
    """A CSRF token for later move submissions, if the page happens to carry one.

    Best-effort and optional. ``write.Session`` treats the token as optional
    because whether the submit endpoint requires it was never established, so a
    miss here is not worth failing a sign-in over -- and guessing a wrong one
    would be worse than sending none.
    """
    match = _CSRF_IN_PAGE.search(text.encode())
    return match.group(1).decode() if match else None
