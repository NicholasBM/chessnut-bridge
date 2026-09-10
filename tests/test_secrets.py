"""Tests for encrypted session storage.

Three properties are worth defending, and they are not equally obvious:

1. **The ciphertext really is ciphertext.** A test that only round-trips would
   pass just as happily if the "encryption" were base64, so the secrets are
   searched for in the bytes on disk.
2. **An unreadable session is a logged-out session, not a dead appliance.** Every
   way the file can go wrong -- absent, truncated, wrong key, tampered, wrong
   version -- must end up at "log in again".
3. **The key and the data are separable.** That is the whole security argument:
   copying or pasting the data file must carry nothing usable.
"""

import base64
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.chesscom.write import Session  # noqa: E402
from bridge.state.secrets import (  # noqa: E402
    SECRETS_VERSION,
    DeviceKey,
    InMemorySessionStore,
    PassphraseKey,
    SecretsUnavailable,
    SessionStore,
    StoredSession,
)

SECRET_COOKIE = "sekrit-php-session-value"
SECRET_CSRF = "sekrit-csrf-token-value"


@pytest.fixture
def paths(tmp_path):
    """Key and data deliberately in different directories, as in production."""
    return tmp_path / "data" / "session.enc", tmp_path / "keys" / "session.key"


@pytest.fixture
def store(paths):
    data, key = paths
    return SessionStore(data, DeviceKey(key))


def a_stored_session() -> StoredSession:
    return StoredSession(
        session=Session(
            cookies={"PHPSESSID": SECRET_COOKIE, "cf_clearance": "sekrit-clearance"},
            csrf_token=SECRET_CSRF,
            play_client="obd95lu",
        ),
        username="nbaronmorgan",
        stored_at=1788861472.0,
    )


# --- round trip -----------------------------------------------------------


def test_a_session_survives_a_round_trip(store):
    store.save(a_stored_session())
    loaded = store.load()
    assert loaded is not None
    assert loaded.session.cookies == {
        "PHPSESSID": SECRET_COOKIE,
        "cf_clearance": "sekrit-clearance",
    }
    assert loaded.session.csrf_token == SECRET_CSRF
    assert loaded.session.play_client == "obd95lu"
    assert loaded.username == "nbaronmorgan"
    assert loaded.stored_at == 1788861472.0


def test_a_session_survives_a_new_store_object(paths):
    """The real case: the appliance rebooted."""
    data, key = paths
    SessionStore(data, DeviceKey(key)).save(a_stored_session())
    reloaded = SessionStore(data, DeviceKey(key)).load()
    assert reloaded is not None
    assert reloaded.session.cookies["PHPSESSID"] == SECRET_COOKIE


def test_nothing_stored_is_not_an_error(store):
    assert store.load() is None
    assert not store.exists


def test_exists_reflects_the_file_without_needing_a_key(paths):
    data, key = paths
    store = SessionStore(data, DeviceKey(key))
    assert not store.exists
    store.save(a_stored_session())
    assert store.exists


def test_saving_twice_replaces_rather_than_appends(store):
    store.save(a_stored_session())
    store.save(
        StoredSession(session=Session(cookies={"PHPSESSID": "second"}), username="other")
    )
    loaded = store.load()
    assert loaded is not None
    assert loaded.session.cookies == {"PHPSESSID": "second"}
    assert loaded.username == "other"


def test_clearing_forgets_the_session(store):
    store.save(a_stored_session())
    store.clear()
    assert store.load() is None
    assert not store.exists


def test_clearing_when_empty_is_not_an_error(store):
    store.clear()


# --- it is actually encrypted --------------------------------------------


def test_no_secret_appears_in_the_file_on_disk(store, paths):
    """The test that stops this being security theatre. A round-trip test alone
    would pass if the 'encryption' were base64."""
    store.save(a_stored_session())
    data, _ = paths
    blob = data.read_bytes()

    for secret in (SECRET_COOKIE, SECRET_CSRF, "sekrit-clearance"):
        assert secret.encode() not in blob
        # Also check the obvious encodings, so a lazy implementation cannot pass.
        assert base64.b64encode(secret.encode()) not in blob
        assert base64.urlsafe_b64encode(secret.encode()) not in blob
        assert secret.encode().hex().encode() not in blob


def test_the_username_is_not_readable_on_disk_either(store, paths):
    """It is not a credential, but it identifies the account, and there is no
    reason to leak it when everything else is encrypted."""
    store.save(a_stored_session())
    data, _ = paths
    assert b"nbaronmorgan" not in data.read_bytes()


def test_the_data_file_is_useless_without_the_key(paths, caplog):
    """The core of the security argument: the likely accident is the data being
    copied, pasted or backed up, and that copy must carry nothing."""
    data, key = paths
    SessionStore(data, DeviceKey(key)).save(a_stored_session())

    stolen_key = key.parent / "attacker.key"
    with caplog.at_level(logging.ERROR):
        assert SessionStore(data, DeviceKey(stolen_key)).load() is None
    assert "could not be decrypted" in caplog.text


def test_tampering_is_detected_rather_than_decrypted_to_nonsense(paths, caplog):
    """Fernet is authenticated. A flipped byte must fail loudly."""
    data, key = paths
    store = SessionStore(data, DeviceKey(key))
    store.save(a_stored_session())

    blob = bytearray(data.read_bytes())
    blob[-1] ^= 0x01
    data.write_bytes(bytes(blob))

    with caplog.at_level(logging.ERROR):
        assert store.load() is None
    assert "could not be decrypted" in caplog.text


# --- surviving a bad file ------------------------------------------------


def test_a_truncated_file_reads_as_logged_out(store, paths):
    data, _ = paths
    store.save(a_stored_session())
    data.write_bytes(data.read_bytes()[:20])
    assert store.load() is None


def test_a_garbage_file_reads_as_logged_out(store, paths):
    data, _ = paths
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"not encrypted, not even close")
    assert store.load() is None


def test_an_empty_file_reads_as_logged_out(store, paths):
    data, _ = paths
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"")
    assert store.load() is None


def test_a_bad_file_is_kept_for_inspection(store, paths):
    """Deleting the only copy of something we merely failed to parse destroys
    the evidence."""
    data, _ = paths
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(b"mystery bytes")
    store.load()
    assert data.read_bytes() == b"mystery bytes"


def test_an_unknown_version_is_refused_not_guessed_at(paths, caplog):
    """A downgrade must not reinterpret a newer file's fields."""
    data, key = paths
    from cryptography.fernet import Fernet

    source = DeviceKey(key)
    payload = json.dumps({"version": SECRETS_VERSION + 1, "cookies": {"a": "b"}}).encode()
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(Fernet(source.key()).encrypt(payload))

    with caplog.at_level(logging.ERROR):
        assert SessionStore(data, source).load() is None
    assert "version" in caplog.text


def test_wrongly_typed_cookies_are_refused(paths):
    data, key = paths
    from cryptography.fernet import Fernet

    source = DeviceKey(key)
    payload = json.dumps({"version": SECRETS_VERSION, "cookies": {"a": 1}}).encode()
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(Fernet(source.key()).encrypt(payload))
    assert SessionStore(data, source).load() is None


def test_a_non_object_payload_is_refused(paths):
    data, key = paths
    from cryptography.fernet import Fernet

    source = DeviceKey(key)
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_bytes(Fernet(source.key()).encrypt(b"[1,2,3]"))
    assert SessionStore(data, source).load() is None


def test_optional_fields_may_be_absent(paths):
    """A session captured without a CSRF token is still worth storing."""
    data, key = paths
    store = SessionStore(data, DeviceKey(key))
    store.save(StoredSession(session=Session(cookies={"PHPSESSID": "x"})))
    loaded = store.load()
    assert loaded is not None
    assert loaded.session.csrf_token is None
    assert loaded.username is None


# --- atomicity ------------------------------------------------------------


def test_a_failed_write_leaves_the_previous_session_intact(store, monkeypatch):
    store.save(a_stored_session())

    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.save(StoredSession(session=Session(cookies={"PHPSESSID": "new"})))

    loaded = store.load()
    assert loaded is not None
    assert loaded.session.cookies["PHPSESSID"] == SECRET_COOKIE


def test_a_failed_write_leaves_no_temp_file_holding_secrets(store, paths, monkeypatch):
    """A stray temp file per crash would accumulate copies of the session."""
    data, _ = paths
    store.save(a_stored_session())
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.save(a_stored_session())
    assert [p.name for p in data.parent.iterdir()] == [data.name]


def test_save_failures_are_not_swallowed(store, monkeypatch):
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        store.save(a_stored_session())


# --- file permissions -----------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_key_file_is_not_readable_by_others(paths):
    data, key = paths
    SessionStore(data, DeviceKey(key)).save(a_stored_session())
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_data_file_is_not_readable_by_others(paths):
    data, key = paths
    SessionStore(data, DeviceKey(key)).save(a_stored_session())
    assert stat.S_IMODE(data.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_world_readable_key_is_warned_about(paths, caplog):
    """Not fatal -- refusing to work would be worse -- but never silent."""
    data, key = paths
    store = SessionStore(data, DeviceKey(key))
    store.save(a_stored_session())
    key.chmod(0o644)

    with caplog.at_level(logging.WARNING):
        assert store.load() is not None
    assert "should be" in caplog.text


# --- the key sources ------------------------------------------------------


def test_a_device_key_is_stable_across_reads(tmp_path):
    """If it changed, every reboot would silently log you out."""
    source = DeviceKey(tmp_path / "k")
    assert source.key() == source.key()
    assert DeviceKey(tmp_path / "k").key() == source.key()


def test_two_device_keys_differ(tmp_path):
    assert DeviceKey(tmp_path / "a").key() != DeviceKey(tmp_path / "b").key()


def test_a_device_key_is_created_on_demand(tmp_path):
    path = tmp_path / "nested" / "deeper" / "k"
    assert DeviceKey(path).key()
    assert path.exists()


def test_an_empty_key_file_is_replaced_rather_than_used(tmp_path, caplog):
    """An interrupted first boot could leave a zero-byte key file behind."""
    path = tmp_path / "k"
    path.write_bytes(b"")
    with caplog.at_level(logging.WARNING):
        assert len(DeviceKey(path).key()) > 30
    assert "empty" in caplog.text


def test_generating_a_key_leaves_no_temp_file_behind(tmp_path):
    """A stray temp file would be a spare copy of the key, at whatever mode the
    crash left it."""
    path = tmp_path / "k"
    DeviceKey(path).key()
    assert [p.name for p in tmp_path.iterdir()] == ["k"]


def test_an_unreadable_key_file_is_reported_not_guessed(tmp_path):
    path = tmp_path / "keydir"
    path.mkdir()  # a directory where a file should be
    with pytest.raises(SecretsUnavailable):
        DeviceKey(path).key()


def test_an_unreadable_key_makes_the_store_read_as_logged_out(tmp_path, caplog):
    data = tmp_path / "session.enc"
    data.write_bytes(b"anything")
    keydir = tmp_path / "keydir"
    keydir.mkdir()
    with caplog.at_level(logging.ERROR):
        assert SessionStore(data, DeviceKey(keydir)).load() is None


def test_a_passphrase_key_is_deterministic():
    salt = b"0123456789abcdef"
    assert PassphraseKey("correct horse", salt).key() == PassphraseKey(
        "correct horse", salt
    ).key()


def test_a_different_passphrase_gives_a_different_key():
    salt = b"0123456789abcdef"
    assert PassphraseKey("a", salt).key() != PassphraseKey("b", salt).key()


def test_a_different_salt_gives_a_different_key():
    assert (
        PassphraseKey("same", b"0123456789abcdef").key()
        != PassphraseKey("same", b"fedcba9876543210").key()
    )


def test_a_passphrase_key_works_as_a_store_key(tmp_path):
    """The whole point of the abstraction: the storage format does not change."""
    data = tmp_path / "session.enc"
    source = PassphraseKey("correct horse battery staple", b"0123456789abcdef")
    SessionStore(data, source).save(a_stored_session())
    loaded = SessionStore(
        data, PassphraseKey("correct horse battery staple", b"0123456789abcdef")
    ).load()
    assert loaded is not None
    assert loaded.session.cookies["PHPSESSID"] == SECRET_COOKIE


def test_the_wrong_passphrase_reads_as_logged_out(tmp_path):
    data = tmp_path / "session.enc"
    salt = b"0123456789abcdef"
    SessionStore(data, PassphraseKey("right", salt)).save(a_stored_session())
    assert SessionStore(data, PassphraseKey("wrong", salt)).load() is None


def test_an_empty_passphrase_is_refused():
    with pytest.raises(ValueError):
        PassphraseKey("", b"0123456789abcdef")


def test_a_short_salt_is_refused():
    with pytest.raises(ValueError):
        PassphraseKey("x", b"short")


# --- not leaking through reprs -------------------------------------------


def test_a_passphrase_key_does_not_render_its_passphrase():
    source = PassphraseKey("sekrit-passphrase", b"0123456789abcdef")
    assert "sekrit-passphrase" not in f"{source!r} {source!s}"


def test_a_stored_session_does_not_render_its_secrets():
    """It contains a Session, whose repr redacts -- this checks the containing
    type did not undo that by rendering the fields itself."""
    rendered = repr(a_stored_session())
    assert SECRET_COOKIE not in rendered
    assert SECRET_CSRF not in rendered
    assert "nbaronmorgan" in rendered, "non-secret metadata stays debuggable"


def test_saving_and_loading_does_not_log_secrets(store, caplog):
    with caplog.at_level(logging.DEBUG):
        store.save(a_stored_session())
        store.load()
    for secret in (SECRET_COOKIE, SECRET_CSRF, "sekrit-clearance"):
        assert secret not in caplog.text


def test_a_decryption_failure_does_not_log_the_ciphertext(paths, caplog):
    """Ciphertext in a log is not a disaster, but logs get pasted into chats and
    a blob of unexplained base64 invites exactly that."""
    data, key = paths
    SessionStore(data, DeviceKey(key)).save(a_stored_session())
    blob = data.read_bytes()

    with caplog.at_level(logging.DEBUG):
        SessionStore(data, DeviceKey(key.parent / "other.key")).load()
    assert blob.decode(errors="replace") not in caplog.text


# --- the test double -----------------------------------------------------


def test_the_in_memory_store_round_trips():
    store = InMemorySessionStore()
    assert store.load() is None
    assert not store.exists
    store.save(a_stored_session())
    assert store.exists
    loaded = store.load()
    assert loaded is not None
    assert loaded.session.cookies["PHPSESSID"] == SECRET_COOKIE
    store.clear()
    assert store.load() is None
