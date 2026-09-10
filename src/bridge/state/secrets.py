"""Encrypted storage for the chess.com session, and an honest account of its limits.

What this actually defends against
----------------------------------
On a Pi with no TPM or secure element, **unattended restart and secrecy from
someone holding the SD card are mutually exclusive.** If the appliance can come
back from a 3am power cut on its own, the key must be readable by the appliance
without a human, which means it is readable by anyone with the card. No amount of
cipher choice changes that; pretending otherwise would be worse than plaintext,
because it would invite misplaced confidence.

So this module is built against the threats that are actually likely here, in
rough order of probability:

* **Copying.** ``rsync``, a backup, a ``tar`` of the project directory, or a
  ``git init`` followed by a push. The key lives in a *different directory* from
  the ciphertext by default, so the usual accident -- copying the data -- carries
  nothing usable.
* **Sharing.** Pasting a file, a screenshot, or a log into a chat. This project
  has leaked live credentials four separate times that way, which makes it the
  best-evidenced threat in the whole design. Ciphertext pasted into a chat is
  inert.
* **Casual reading.** Anything that walks the filesystem, and a mode-0600 file
  keeps other local users out.

It does **not** defend against: root on the running device, a stolen SD card, or
anyone who can read both files. Those need a passphrase nobody stores, which
costs unattended reboot -- see :class:`PassphraseKey`, which is implemented and
ready if that trade is ever worth making.

Design notes
------------
``cryptography``'s Fernet does the encryption. It is authenticated (so a modified
file fails loudly instead of decrypting to nonsense), versioned, and hard to
misuse -- no nonce for us to reuse and no mode to pick wrongly. A prebuilt
aarch64 wheel exists, so the Pi does not compile Rust to get it.

Writes reuse the atomic temp-file-and-rename dance from
:mod:`bridge.state.store`, for the same reason: an appliance loses power
mid-write eventually. A half-written session file would mean an unexplained
logout.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..chesscom.login import Credentials
from ..chesscom.write import Session

log = logging.getLogger(__name__)

#: Bumped only for a format a previous version could not read.
SECRETS_VERSION = 1

#: Owner read/write only. Checked on load as well as set on save, because a file
#: that has become world-readable is worth a warning rather than silence.
_SECRET_MODE = 0o600


class KeySource:
    """Where the encryption key comes from. Swappable so the security model can
    change without touching the storage format."""

    def key(self) -> bytes:
        """A urlsafe-base64 32-byte key, as Fernet wants."""
        raise NotImplementedError


class DeviceKey(KeySource):
    """A random key kept in a file on the device. The default.

    Generated on first use with mode 0600. Belongs in a *different directory*
    from the encrypted data, which is the entire point: it makes the likely
    accident -- copying or sharing the data file -- harmless, because the copy
    cannot be read without a second file nobody thought to take.

    Being on the same device as the data, it is no defence against physical
    possession. That is a deliberate, stated trade for surviving a power cut
    without a human present.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def key(self) -> bytes:
        try:
            existing = self.path.read_bytes().strip()
        except FileNotFoundError:
            return self._create()
        except OSError as exc:
            raise SecretsUnavailable(f"cannot read key file {self.path}: {exc}") from exc

        if not existing:
            log.warning("key file %s is empty; generating a new key", self.path)
            return self._create()
        self._warn_if_permissive()
        return existing

    def _create(self) -> bytes:
        """Generate and write a key, atomically.

        Written via a temp file and renamed rather than opened in place, which
        makes the zero-byte key file impossible in the first place instead of
        merely recoverable -- an interrupted first boot is exactly how one would
        arise. The mode is tightened on the temp file before any bytes are
        written, so the key is never briefly readable by anyone else.

        Not safe against two processes racing to create a key at the same instant:
        the loser's key would win the rename and orphan anything the winner had
        already encrypted. The appliance runs this as a single systemd unit, and
        the alternative (``O_EXCL`` on the final path) cannot replace the empty
        file this method exists to handle.
        """
        key = Fernet.generate_key()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "wb",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            os.chmod(handle.name, _SECRET_MODE)
            with handle as tmp:
                tmp.write(key + b"\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        log.info("generated a new session key at %s", self.path)
        return key

    def _warn_if_permissive(self) -> None:
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            log.warning(
                "key file %s is mode %o; it should be %o so other local users "
                "cannot read it",
                self.path,
                mode,
                _SECRET_MODE,
            )


class PassphraseKey(KeySource):
    """A key derived from a passphrase that is never stored.

    The strong option, and the one that costs unattended reboot: after a power
    cut the appliance comes back locked and stays locked until someone opens the
    web UI and types the passphrase. For 3-day correspondence games that may well
    be an acceptable price, which is why this is implemented rather than
    described -- but it is not the default, because "the bridge silently stopped
    working while I was away" is the failure this whole appliance exists to avoid.

    scrypt with a stored random salt. Deliberately expensive: n=2**14 keeps
    derivation to a fraction of a second even on a Zero 2 W while making a
    guessing attack on a weak passphrase pay for every attempt.
    """

    #: Tuned for a Pi Zero 2 W: high enough to hurt an attacker, low enough that
    #: unlocking the UI does not feel broken.
    SCRYPT_N = 2**14
    SCRYPT_R = 8
    SCRYPT_P = 1

    def __init__(self, passphrase: str, salt: bytes):
        if not passphrase:
            raise ValueError("passphrase must not be empty")
        if len(salt) < 16:
            raise ValueError("salt must be at least 16 bytes")
        self._passphrase = passphrase
        self._salt = salt

    def key(self) -> bytes:
        kdf = Scrypt(
            salt=self._salt,
            length=32,
            n=self.SCRYPT_N,
            r=self.SCRYPT_R,
            p=self.SCRYPT_P,
        )
        return base64.urlsafe_b64encode(kdf.derive(self._passphrase.encode()))

    def __repr__(self) -> str:
        # The passphrase must not reach a traceback.
        return f"PassphraseKey(passphrase=<redacted>, salt={len(self._salt)} bytes)"

    __str__ = __repr__


class SecretsUnavailable(RuntimeError):
    """The store could not be read or written for a reason that is not "empty"."""


@dataclass(frozen=True)
class StoredSession:
    """A session plus the non-secret facts the UI wants to show about it."""

    session: Session
    #: Whose account this is, so the UI can say who is logged in.
    username: str | None = None
    #: Unix seconds when it was captured, so the UI can say how old it is. A
    #: session's age is the best available hint at whether a failure means
    #: "expired" -- there is no way to test one without using it.
    stored_at: float | None = None

    def __repr__(self) -> str:
        return (
            f"StoredSession(session={self.session!r}, "
            f"username={self.username!r}, stored_at={self.stored_at!r})"
        )


class SessionStore:
    """Encrypted at rest, atomic on write, and empty rather than fatal on read."""

    def __init__(self, path: str | os.PathLike[str], key_source: KeySource):
        self.path = Path(path)
        self._key_source = key_source

    @property
    def exists(self) -> bool:
        """Whether anything is stored. Cheap, and needs no key."""
        return self.path.exists()

    def load(self) -> StoredSession | None:
        """Decrypt the stored session, or None if there is not a usable one.

        Never raises for a bad file. A session that cannot be read is exactly
        equivalent to not being logged in -- a state the UI already has to handle
        -- whereas raising here would make a corrupt file stop the appliance
        booting. The file is left in place, because deleting the only copy of
        something we merely failed to parse destroys the evidence.
        """
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.error("cannot read session file %s (%s); treating as logged out", self.path, exc)
            return None

        self._warn_if_permissive()

        try:
            key = self._key_source.key()
        except SecretsUnavailable as exc:
            log.error("%s; treating as logged out", exc)
            return None

        try:
            plaintext = Fernet(key).decrypt(blob)
        except InvalidToken:
            # Wrong key or a tampered file. Both mean the same thing to us, and
            # neither is worth guessing about.
            log.error(
                "session file %s could not be decrypted -- the key may not match "
                "or the file may be damaged; treating as logged out and leaving "
                "the file in place",
                self.path,
            )
            return None
        except Exception as exc:  # noqa: BLE001 -- a malformed key raises other types
            log.error("session file %s is unusable (%s); treating as logged out", self.path, exc)
            return None

        return self._decode(plaintext)

    def _decode(self, plaintext: bytes) -> StoredSession | None:
        try:
            raw = json.loads(plaintext)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error("decrypted session is not JSON (%s); treating as logged out", exc)
            return None
        if not isinstance(raw, dict):
            log.error("decrypted session is not an object; treating as logged out")
            return None

        version = raw.get("version")
        if version != SECRETS_VERSION:
            log.error(
                "session file has version %r, expected %d; ignoring rather than "
                "guessing at its fields",
                version,
                SECRETS_VERSION,
            )
            return None

        cookies = raw.get("cookies")
        if not isinstance(cookies, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()
        ):
            log.error("stored cookies are not string pairs; treating as logged out")
            return None

        return StoredSession(
            session=Session(
                cookies=cookies,
                csrf_token=_optional_str(raw.get("csrf_token")),
                play_client=_optional_str(raw.get("play_client")),
            ),
            username=_optional_str(raw.get("username")),
            stored_at=raw.get("stored_at") if isinstance(raw.get("stored_at"), (int, float)) else None,
        )

    def save(self, stored: StoredSession) -> None:
        """Encrypt and write atomically. Raises on a real failure.

        Unlike :meth:`load`, failures here are not swallowed: silently failing to
        persist a login would mean the one manual step in this whole appliance
        quietly stops sticking, and nobody would find out until the session
        expired.
        """
        payload = json.dumps(
            {
                "version": SECRETS_VERSION,
                "cookies": dict(stored.session.cookies),
                "csrf_token": stored.session.csrf_token,
                "play_client": stored.session.play_client,
                "username": stored.username,
                "stored_at": stored.stored_at,
            }
        ).encode()

        blob = Fernet(self._key_source.key()).encrypt(payload)
        _write_secret_atomically(self.path, blob)
        log.info("stored an encrypted chess.com session at %s", self.path)

    def clear(self) -> None:
        """Forget the session. Used by an explicit log-out in the UI."""
        self.path.unlink(missing_ok=True)
        log.info("cleared the stored chess.com session")

    def _warn_if_permissive(self) -> None:
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            log.warning("session file %s is mode %o; expected %o", self.path, mode, _SECRET_MODE)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _write_secret_atomically(path: Path, blob: bytes) -> None:
    """Write ciphertext via a temp file and a rename.

    Shared by both stores because an appliance loses power mid-write eventually,
    and a half-written secret presents as an unexplained logout rather than as an
    error. The mode is tightened before any bytes are written, so the file is
    never briefly readable by another local user.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        os.chmod(handle.name, _SECRET_MODE)
        with handle as tmp:
            tmp.write(blob)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


class CredentialStore:
    """The chess.com password, encrypted, so the appliance can sign itself in.

    Why this exists, and the trade being made
    -----------------------------------------
    Storing a session is enough to submit moves *today*. Storing the password is
    what lets the appliance recover on its own when that session lapses -- weeks
    later, with nobody watching, which is the whole point of an appliance. The
    owner asked for that explicitly.

    It is a real step up in what a compromise costs. A leaked session cookie can
    be killed by signing out everywhere; a leaked password is the account. The
    protection is identical to the session's -- Fernet, mode 0600, key in a
    different directory -- and identically limited: it does **not** defend against
    anyone holding the SD card. :mod:`bridge.state.secrets`' opening note applies
    unchanged, and this class does not pretend to improve on it.

    Kept in its own file rather than added to the session blob, so that "forget my
    password but stay signed in" and "sign out but keep signing back in" are both
    expressible, and so an owner who wants the weaker-but-smaller exposure can
    simply delete this one file.
    """

    def __init__(self, path: str | os.PathLike[str], key_source: KeySource):
        self.path = Path(path)
        self._key_source = key_source

    @property
    def exists(self) -> bool:
        """Whether a password is stored. Cheap, and needs no key."""
        return self.path.exists()

    def load(self) -> Credentials | None:
        """Decrypt the stored credentials, or None if there are not usable ones.

        Never raises, for the same reason :meth:`SessionStore.load` does not: a
        file we cannot read is equivalent to having no password stored, which is a
        state the appliance already handles, whereas raising would stop it booting
        over something recoverable.
        """
        try:
            blob = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.error(
                "cannot read credentials file %s (%s); treating as no stored password",
                self.path,
                exc,
            )
            return None

        self._warn_if_permissive()

        try:
            key = self._key_source.key()
        except SecretsUnavailable as exc:
            log.error("%s; treating as no stored password", exc)
            return None

        try:
            plaintext = Fernet(key).decrypt(blob)
        except InvalidToken:
            log.error(
                "credentials file %s could not be decrypted -- the key may not "
                "match or the file may be damaged; treating as no stored password "
                "and leaving the file in place",
                self.path,
            )
            return None
        except Exception as exc:  # noqa: BLE001 -- a malformed key raises other types
            log.error(
                "credentials file %s is unusable (%s); treating as no stored password",
                self.path,
                exc,
            )
            return None

        return self._decode(plaintext)

    def _decode(self, plaintext: bytes) -> Credentials | None:
        try:
            raw = json.loads(plaintext)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error("decrypted credentials are not JSON (%s); ignoring", exc)
            return None
        if not isinstance(raw, dict):
            log.error("decrypted credentials are not an object; ignoring")
            return None
        if raw.get("version") != SECRETS_VERSION:
            log.error(
                "credentials file has version %r, expected %d; ignoring rather "
                "than guessing at its fields",
                raw.get("version"),
                SECRETS_VERSION,
            )
            return None

        username = _optional_str(raw.get("username"))
        password = _optional_str(raw.get("password"))
        if not username or not password:
            # Half a credential cannot sign in, and storing one is a bug worth
            # naming rather than a state to carry around.
            log.error("stored credentials are incomplete; ignoring them")
            return None
        return Credentials(username=username, password=password)

    def save(self, credentials: Credentials) -> None:
        """Encrypt and write atomically. Raises on a real failure.

        Not swallowed, like the session's: silently failing to keep the password
        would mean the appliance stops recovering from an expired session and
        nobody would find out until it had already stopped sending moves.
        """
        if not credentials.is_populated:
            raise ValueError("refusing to store empty chess.com credentials")
        payload = json.dumps(
            {
                "version": SECRETS_VERSION,
                "username": credentials.username,
                "password": credentials.password,
            }
        ).encode()
        blob = Fernet(self._key_source.key()).encrypt(payload)
        _write_secret_atomically(self.path, blob)
        # Deliberately says nothing about the password, not even its length.
        log.info("stored encrypted chess.com credentials at %s", self.path)

    def clear(self) -> None:
        """Forget the password. The appliance will stop signing itself in."""
        self.path.unlink(missing_ok=True)
        log.info("cleared the stored chess.com credentials")

    def _warn_if_permissive(self) -> None:
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            log.warning(
                "credentials file %s is mode %o; expected %o",
                self.path,
                mode,
                _SECRET_MODE,
            )


class InMemoryCredentialStore:
    """Forgets on exit. For tests, and for anyone who declines to store it."""

    def __init__(self, credentials: Credentials | None = None):
        self._credentials = credentials

    @property
    def exists(self) -> bool:
        return self._credentials is not None

    def load(self) -> Credentials | None:
        return self._credentials

    def save(self, credentials: Credentials) -> None:
        if not credentials.is_populated:
            raise ValueError("refusing to store empty chess.com credentials")
        self._credentials = credentials

    def clear(self) -> None:
        self._credentials = None


class InMemorySessionStore:
    """Forgets on exit. For tests, and for a --no-persist mode."""

    def __init__(self, stored: StoredSession | None = None):
        self._stored = stored

    @property
    def exists(self) -> bool:
        return self._stored is not None

    def load(self) -> StoredSession | None:
        return self._stored

    def save(self, stored: StoredSession) -> None:
        self._stored = stored

    def clear(self) -> None:
        self._stored = None
