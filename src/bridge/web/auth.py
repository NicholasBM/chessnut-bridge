"""Authenticating the owner to the appliance's own web UI.

Scope, so this is not mistaken for more than it is: the UI's password proves
"whoever is asking knows the secret from the SD card". That is all. It is not
protecting against someone holding the card, and it is not a chess.com
credential -- those live in :mod:`bridge.state.secrets`.

Three small pieces, each chosen for a reason worth keeping:

**Signed cookie rather than a session table.** The only fact to remember is one
bit -- this browser proved it knows the password -- so an HMAC over an expiry
timestamp needs no storage, survives a reboot, and cannot be forged. A session
dict would add state that has to be evicted, and eviction on a device that may be
power-cut is a source of bugs for no benefit here.

**The signing key comes from the device key file**, the same one that protects the
stored session, so cookies survive a power cut. If it were random per process,
every reboot would silently log the owner out -- and a 3am reboot they never saw
would look like the appliance forgetting its password.

**Brute force is slowed, never locked.** A hard lockout on a single-user appliance
is a denial of service anyone on the network can trigger by guessing wrong: lock
the owner out and the board stops being usable. So repeated failures push out the
next permitted attempt, capped, and a success clears it. Crucially the delay is
*reported* rather than slept through -- a handler sleeping on a Pi Zero 2 W ties
up a worker, which turns the defence into the attack.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

#: Name of the cookie carrying proof of login.
COOKIE_NAME = "chessnut_bridge_auth"

#: How long a browser stays logged in. Long on purpose: this is a household
#: appliance whose owner checks it a few times a week, and a UI that demands a
#: password every visit trains people to pick a short one.
DEFAULT_LIFETIME_SECONDS = 30 * 24 * 60 * 60

#: First delay after a wrong password, doubling per consecutive failure.
_LOCKOUT_BASE_SECONDS = 1.0
#: Ceiling on that delay. Five minutes makes online guessing hopeless while
#: leaving the owner a way back in without power-cycling anything.
_LOCKOUT_CAP_SECONDS = 300.0


def derive_signing_key(key_material: bytes) -> bytes:
    """A cookie-signing key from the device key, via HKDF-like separation.

    Not the device key itself: the same bytes must not both encrypt the chess.com
    session and sign cookies, or a flaw that leaks one would hand over the other.
    A distinct info string is the cheapest way to keep them independent.
    """
    return hmac.new(key_material, b"chessnut-bridge web cookie v1", hashlib.sha256).digest()


@dataclass
class TokenSigner:
    """Issues and verifies the login cookie. No stored state."""

    secret: bytes
    lifetime_seconds: float = DEFAULT_LIFETIME_SECONDS
    clock: Callable[[], float] = time.time

    def issue(self) -> str:
        expires = int(self.clock() + self.lifetime_seconds)
        return f"{expires}.{self._signature(expires)}"

    def verify(self, token: str | None) -> bool:
        """True only for a token this key signed and that has not expired.

        Every rejection returns the same False with no reason, on purpose: the
        caller has nothing useful to do with the difference, and reporting it
        would tell a guesser which half of the token to work on.
        """
        if not token or "." not in token:
            return False
        raw_expires, _, signature = token.partition(".")
        try:
            expires = int(raw_expires)
        except ValueError:
            return False
        if not hmac.compare_digest(signature, self._signature(expires)):
            return False
        return self.clock() < expires

    def _signature(self, expires: int) -> str:
        digest = hmac.new(self.secret, str(expires).encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")


@dataclass
class LoginGuard:
    """Checks the password, and makes repeated guessing pointless.

    Failures are counted for the appliance as a whole rather than per client
    address. On a home LAN a source address is trivially changed, so per-address
    counting mostly protects the attacker's convenience; and this appliance has
    exactly one user, so a global counter costs that user nothing they would
    notice while actually slowing a guesser down.
    """

    password: str | None
    clock: Callable[[], float] = time.time

    _failures: int = field(default=0, init=False)
    _next_attempt_at: float = field(default=0.0, init=False)

    @property
    def is_configured(self) -> bool:
        """False when no password was set. The app must then serve only an
        explanation -- never an unprotected UI."""
        return bool(self.password)

    def seconds_to_wait(self) -> float:
        """How long until another attempt is accepted. 0 when it is allowed now."""
        return max(0.0, self._next_attempt_at - self.clock())

    def check(self, attempt: str) -> bool:
        """Verify a password attempt, applying and updating the delay.

        Returns False both for a wrong password and for one offered too soon,
        because the caller's job is the same either way; ``seconds_to_wait`` is
        what distinguishes them for display.
        """
        if not self.password:
            # Not merely "no", but never a yes: an appliance with no password
            # configured must not be reachable by guessing the empty string.
            log.error("login attempted with no web_password configured")
            return False
        if self.seconds_to_wait() > 0:
            log.warning("login attempt refused; still waiting out earlier failures")
            return False

        # Constant time, so the response cannot be used to learn the password one
        # character at a time. Cheap here and free of caveats, unlike trying to
        # reason about whether it matters over a LAN.
        if secrets.compare_digest(attempt, self.password):
            self._failures = 0
            self._next_attempt_at = 0.0
            log.info("web login succeeded")
            return True

        self._failures += 1
        delay = min(
            _LOCKOUT_BASE_SECONDS * (2 ** (self._failures - 1)), _LOCKOUT_CAP_SECONDS
        )
        self._next_attempt_at = self.clock() + delay
        log.warning(
            "web login failed (%d consecutive); next attempt in %.0fs",
            self._failures,
            delay,
        )
        return False
