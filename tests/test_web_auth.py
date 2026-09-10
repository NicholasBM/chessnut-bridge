"""Tests for the web UI's own authentication.

Two things are being defended, and only one of them is the password check:

1. **A cookie must not be forgeable**, or the password is decoration.
2. **The owner must not be lockable out of their own appliance.** A hard lockout
   on a single-user device is a denial of service anyone on the network can
   trigger by guessing wrong, and it would stop the board working.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.web.auth import (  # noqa: E402
    _LOCKOUT_CAP_SECONDS,
    LoginGuard,
    TokenSigner,
    derive_signing_key,
)

PASSWORD = "a-long-enough-password"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- the signing key ------------------------------------------------------


def test_the_signing_key_is_not_the_device_key_itself():
    """One secret must not both encrypt the chess.com session and sign cookies:
    a flaw leaking either would otherwise hand over both."""
    material = b"device-key-material"
    assert derive_signing_key(material) != material


def test_the_signing_key_is_stable_for_the_same_device_key():
    """If it changed, every reboot would silently log the owner out -- and a 3am
    reboot they never saw would look like the appliance forgetting its password."""
    assert derive_signing_key(b"same") == derive_signing_key(b"same")


def test_different_device_keys_give_different_signing_keys():
    assert derive_signing_key(b"a") != derive_signing_key(b"b")


# --- tokens ---------------------------------------------------------------


def test_a_freshly_issued_token_verifies():
    signer = TokenSigner(secret=b"s")
    assert signer.verify(signer.issue())


def test_a_token_from_another_key_is_refused():
    """The whole point: knowing the format must not be enough."""
    issued = TokenSigner(secret=b"one").issue()
    assert not TokenSigner(secret=b"two").verify(issued)


def test_an_expiry_cannot_be_extended_without_the_key():
    """The obvious forgery: keep the signature, push the timestamp out."""
    clock = FakeClock()
    signer = TokenSigner(secret=b"s", lifetime_seconds=10, clock=clock)
    _, _, signature = signer.issue().partition(".")
    forged = f"{int(clock.now) + 99999}.{signature}"
    assert not signer.verify(forged)


def test_a_token_expires():
    clock = FakeClock()
    signer = TokenSigner(secret=b"s", lifetime_seconds=100, clock=clock)
    token = signer.issue()
    clock.advance(99)
    assert signer.verify(token)
    clock.advance(2)
    assert not signer.verify(token)


@pytest.mark.parametrize(
    "token",
    [None, "", "nonsense", "no-dot-here", ".", "abc.def", "notanumber.sig", "123"],
)
def test_a_malformed_token_is_refused_rather_than_raising(token):
    """These arrive from anything on the network, so a crash here would be a
    trivial way to take the appliance's UI down."""
    assert not TokenSigner(secret=b"s").verify(token)


def test_a_token_survives_a_new_signer_over_the_same_key():
    """The real case: the appliance rebooted while the browser kept its cookie."""
    token = TokenSigner(secret=b"s").issue()
    assert TokenSigner(secret=b"s").verify(token)


def test_the_token_carries_no_secret():
    signer = TokenSigner(secret=b"sekrit-signing-key")
    assert "sekrit-signing-key" not in signer.issue()


# --- the password check ---------------------------------------------------


def test_the_right_password_is_accepted():
    assert LoginGuard(password=PASSWORD).check(PASSWORD)


def test_a_wrong_password_is_refused():
    assert not LoginGuard(password=PASSWORD).check("wrong-password")


def test_an_unconfigured_guard_reports_itself():
    guard = LoginGuard(password=None)
    assert not guard.is_configured


@pytest.mark.parametrize("attempt", ["", " ", "None", "null"])
def test_no_password_configured_means_nothing_is_accepted(attempt, caplog):
    """Not merely 'no': an appliance with no password set must not be reachable
    by guessing the empty string."""
    with caplog.at_level(logging.ERROR):
        assert not LoginGuard(password=None).check(attempt)
    assert "no web_password configured" in caplog.text


def test_a_near_miss_is_refused():
    assert not LoginGuard(password=PASSWORD).check(PASSWORD[:-1])
    assert not LoginGuard(password=PASSWORD).check(PASSWORD + "x")


# --- slowing guesses without locking anyone out --------------------------


def test_a_failure_delays_the_next_attempt():
    clock = FakeClock()
    guard = LoginGuard(password=PASSWORD, clock=clock)
    guard.check("wrong")
    assert guard.seconds_to_wait() > 0


def test_the_right_password_is_refused_while_waiting():
    """Otherwise the delay would be advice rather than a defence."""
    clock = FakeClock()
    guard = LoginGuard(password=PASSWORD, clock=clock)
    guard.check("wrong")
    assert not guard.check(PASSWORD)


def test_the_delay_expires_and_the_owner_gets_back_in():
    clock = FakeClock()
    guard = LoginGuard(password=PASSWORD, clock=clock)
    guard.check("wrong")
    clock.advance(guard.seconds_to_wait() + 0.1)
    assert guard.seconds_to_wait() == 0
    assert guard.check(PASSWORD)


def test_consecutive_failures_lengthen_the_delay():
    clock = FakeClock()
    guard = LoginGuard(password=PASSWORD, clock=clock)
    delays = []
    for _ in range(4):
        clock.advance(_LOCKOUT_CAP_SECONDS)  # clear the previous wait
        guard.check("wrong")
        delays.append(guard.seconds_to_wait())
    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


def test_the_delay_is_capped_so_the_owner_is_never_locked_out():
    """A permanent lockout is a denial of service anyone on the network could
    trigger by guessing wrong, and it would stop the board working."""
    clock = FakeClock()
    guard = LoginGuard(password=PASSWORD, clock=clock)
    for _ in range(50):
        clock.advance(_LOCKOUT_CAP_SECONDS)
        guard.check("wrong")
    assert guard.seconds_to_wait() <= _LOCKOUT_CAP_SECONDS


def test_a_success_clears_the_penalty():
    """Otherwise one fat-fingered attempt would still be slowing the owner down
    on their next visit."""
    clock = FakeClock()
    guard = LoginGuard(password=PASSWORD, clock=clock)
    guard.check("wrong")
    guard.check("wrong")
    clock.advance(_LOCKOUT_CAP_SECONDS)
    assert guard.check(PASSWORD)
    assert guard.seconds_to_wait() == 0

    clock.advance(1)
    guard.check("wrong")
    # Back to the first delay, not where the earlier run left off.
    fresh = LoginGuard(password=PASSWORD, clock=clock)
    fresh.check("wrong")
    assert guard.seconds_to_wait() == pytest.approx(fresh.seconds_to_wait())


def test_the_password_is_never_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        guard = LoginGuard(password="sekrit-password")
        guard.check("also-sekrit-guess")
        guard.check("sekrit-password")
    assert "sekrit-password" not in caplog.text
    assert "also-sekrit-guess" not in caplog.text
