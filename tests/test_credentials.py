"""Tests for storing the chess.com password on the device.

This is the file that makes the appliance unattended, and also the file with the
most to lose, so the properties defended here are deliberately paranoid:

1. **The password is not on disk in the clear.** Checked by searching the bytes,
   because a round-trip test would pass just as happily if the "encryption" were
   base64.
2. **The key and the password are separable.** Copying the state directory without
   the key -- the likely accident -- must carry nothing usable. Same argument as
   the session store's, and it is the entire security case for storing a password
   at all.
3. **An unreadable file means "no password", not a dead appliance.** Every way it
   can go wrong must land on "ask the owner again" rather than on a service that
   will not start.
4. **Half a credential is never returned.** A username with no password cannot
   sign in, and returning one would produce a sign-in attempt guaranteed to fail.
"""

import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom.login import Credentials  # noqa: E402
from bridge.state.secrets import (  # noqa: E402
    SECRETS_VERSION,
    CredentialStore,
    DeviceKey,
    InMemoryCredentialStore,
)

PASSWORD = "sekrit-chesscom-password"
USERNAME = "nbaronmorgan"


@pytest.fixture
def paths(tmp_path):
    """Key and data in different directories, as in production."""
    return tmp_path / "data" / "credentials.enc", tmp_path / "keys" / "session.key"


@pytest.fixture
def store(paths):
    data, key = paths
    return CredentialStore(data, DeviceKey(key))


def some_credentials() -> Credentials:
    return Credentials(username=USERNAME, password=PASSWORD)


# --- the round trip ---------------------------------------------------------


def test_nothing_stored_reads_as_no_password(store):
    assert store.load() is None
    assert not store.exists


def test_a_password_survives_a_restart(store):
    store.save(some_credentials())
    assert store.exists
    reloaded = store.load()
    assert reloaded == some_credentials()


def test_a_new_store_object_reads_what_the_old_one_wrote(paths):
    """The real case: the appliance rebooted, and only the files remain."""
    data, key = paths
    CredentialStore(data, DeviceKey(key)).save(some_credentials())
    assert CredentialStore(data, DeviceKey(key)).load().password == PASSWORD


def test_clearing_forgets_the_password(store):
    store.save(some_credentials())
    store.clear()
    assert store.load() is None
    assert not store.exists


def test_clearing_a_store_that_has_nothing_is_not_an_error(store):
    store.clear()  # an owner can press the button twice


# --- it really is encrypted -------------------------------------------------


def test_the_password_is_not_on_disk_in_the_clear(store):
    store.save(some_credentials())
    raw = store.path.read_bytes()
    assert PASSWORD.encode() not in raw
    assert USERNAME.encode() not in raw
    # Not merely encoded, either.
    import base64

    assert base64.b64encode(PASSWORD.encode()) not in raw


def test_the_data_is_useless_without_the_key(paths, caplog):
    """The whole security argument: an rsync of the state directory carries
    nothing, because the key lives somewhere else."""
    data, key = paths
    CredentialStore(data, DeviceKey(key)).save(some_credentials())

    elsewhere = data.parent / "other.key"
    with caplog.at_level(logging.ERROR):
        assert CredentialStore(data, DeviceKey(elsewhere)).load() is None
    assert "could not be decrypted" in caplog.text


def test_tampering_is_detected_rather_than_decrypted_to_nonsense(store, caplog):
    store.save(some_credentials())
    blob = bytearray(store.path.read_bytes())
    blob[-6] = blob[-6] ^ 0xFF
    store.path.write_bytes(bytes(blob))
    with caplog.at_level(logging.ERROR):
        assert store.load() is None
    assert "could not be decrypted" in caplog.text


def test_the_file_is_not_readable_by_other_local_users(store):
    store.save(some_credentials())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


# --- every failure lands on "ask again" ------------------------------------


def test_a_truncated_file_reads_as_no_password(store, caplog):
    store.save(some_credentials())
    store.path.write_bytes(store.path.read_bytes()[:20])
    with caplog.at_level(logging.ERROR):
        assert store.load() is None


def test_a_file_that_is_not_json_reads_as_no_password(paths, caplog):
    from cryptography.fernet import Fernet

    data, key = paths
    source = DeviceKey(key)
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(Fernet(source.key()).encrypt(b"not json at all"))
    with caplog.at_level(logging.ERROR):
        assert CredentialStore(data, source).load() is None
    assert "not JSON" in caplog.text


def test_an_unknown_version_is_refused_not_guessed_at(paths, caplog):
    """A future format must not be half-read by an older build."""
    from cryptography.fernet import Fernet

    data, key = paths
    source = DeviceKey(key)
    data.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": SECRETS_VERSION + 1, "username": USERNAME, "password": PASSWORD}
    ).encode()
    data.write_bytes(Fernet(source.key()).encrypt(payload))
    with caplog.at_level(logging.ERROR):
        assert CredentialStore(data, source).load() is None
    assert "version" in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"version": SECRETS_VERSION, "username": USERNAME},
        {"version": SECRETS_VERSION, "password": PASSWORD},
        {"version": SECRETS_VERSION, "username": "", "password": PASSWORD},
        {"version": SECRETS_VERSION, "username": USERNAME, "password": ""},
    ],
)
def test_half_a_credential_is_never_returned(paths, payload, caplog):
    """Returning one would produce a sign-in attempt certain to fail, and a
    "wrong password" message for a password that was never there."""
    from cryptography.fernet import Fernet

    data, key = paths
    source = DeviceKey(key)
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(Fernet(source.key()).encrypt(json.dumps(payload).encode()))
    with caplog.at_level(logging.ERROR):
        assert CredentialStore(data, source).load() is None


def test_an_unreadable_file_reads_as_no_password(store, caplog):
    store.save(some_credentials())
    os.chmod(store.path, 0o000)
    try:
        with caplog.at_level(logging.ERROR):
            assert store.load() is None
    finally:
        os.chmod(store.path, 0o600)


def test_saving_empty_credentials_is_refused(store):
    """A blank password would be stored, retried on a timer, and rejected every
    time. Better to refuse it at the door."""
    with pytest.raises(ValueError):
        store.save(Credentials(username=USERNAME, password=""))
    with pytest.raises(ValueError):
        store.save(Credentials(username="", password=PASSWORD))
    assert not store.exists


# --- nothing leaks into the logs -------------------------------------------


def test_saving_and_loading_does_not_log_the_password(store, caplog):
    with caplog.at_level(logging.DEBUG):
        store.save(some_credentials())
        store.load()
    assert PASSWORD not in caplog.text


def test_a_decryption_failure_does_not_log_the_ciphertext(paths, caplog):
    data, key = paths
    CredentialStore(data, DeviceKey(key)).save(some_credentials())
    blob = data.read_bytes()
    with caplog.at_level(logging.DEBUG):
        CredentialStore(data, DeviceKey(data.parent / "other.key")).load()
    assert blob.decode(errors="replace") not in caplog.text


def test_a_world_readable_file_is_warned_about(store, caplog):
    store.save(some_credentials())
    os.chmod(store.path, 0o644)
    with caplog.at_level(logging.WARNING):
        store.load()
    assert "expected" in caplog.text


# --- the in-memory stand-in behaves the same -------------------------------


def test_the_in_memory_store_matches_the_real_one():
    store = InMemoryCredentialStore()
    assert store.load() is None
    assert not store.exists
    store.save(some_credentials())
    assert store.exists
    assert store.load() == some_credentials()
    store.clear()
    assert store.load() is None


def test_the_in_memory_store_also_refuses_empty_credentials():
    with pytest.raises(ValueError):
        InMemoryCredentialStore().save(Credentials(username=USERNAME, password=""))


def test_the_in_memory_store_can_start_populated():
    """Used by tests that need an appliance which already knows its password."""
    assert InMemoryCredentialStore(some_credentials()).load() == some_credentials()


# --- the two stores are independent ---------------------------------------


def test_the_password_file_is_separate_from_the_session_file(tmp_path):
    """So that "forget my password but stay signed in" is expressible, and so an
    owner can delete one file to opt out of unattended sign-in."""
    from bridge.chesscom.write import Session
    from bridge.state.secrets import SessionStore, StoredSession

    key = DeviceKey(tmp_path / "keys" / "session.key")
    sessions = SessionStore(tmp_path / "data" / "session.enc", key)
    credentials = CredentialStore(tmp_path / "data" / "credentials.enc", key)

    sessions.save(StoredSession(session=Session(cookies={"PHPSESSID": "x"})))
    credentials.save(some_credentials())

    credentials.clear()
    assert credentials.load() is None
    assert sessions.load() is not None  # still signed in
