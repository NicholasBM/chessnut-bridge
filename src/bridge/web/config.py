"""Configuration the owner sets when flashing the SD card.

The appliance has no screen, no keyboard, and SSH is explicitly not part of normal
operation, so there is exactly one moment when a human can tell it anything before
it is running: while the card is in their laptop. Raspberry Pi OS already expects
that -- WiFi credentials go into a file on the boot partition -- so this reads its
own file from the same place. One more file in a directory they are already
editing costs nothing to explain.

Format is deliberately ``key=value`` with ``#`` comments, matching the Pi's own
boot files rather than introducing YAML: it needs no dependency, survives a text
editor that mangles indentation, and looks native next to ``config.txt``.

Two decisions worth keeping
---------------------------
**Loading never raises.** A typo in this file must produce a page that says what
is wrong, not a service that fails to start -- because a unit that will not start
is invisible to someone with no screen and no shell. Problems are collected and
carried on the object for the UI to render. Same asymmetry as
:mod:`bridge.state.store`.

**An absent password is a refusal, not a default.** The web UI holds the button
that submits moves into real games and the flow that captures a chess.com
session, so quietly serving it unprotected would be the worst outcome of a
misconfiguration. It is reported as a problem, and the app serves only an
explanation until it is fixed.

On secrecy: the password sits in plaintext on a FAT32 boot partition, and there
is no way around that -- it is the only channel the owner has. It is no worse
than what is already there: the device key that decrypts the stored chess.com
session is on the same card. Neither defends against someone holding the card,
which :mod:`bridge.state.secrets` already says plainly. What this does defend is
the network the appliance sits on.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Where Raspberry Pi OS mounts the boot partition. ``/boot/firmware`` is current
#: (bookworm onwards); ``/boot`` is where older images put it and where a
#: hand-written note is most likely to end up. Both are checked so a card
#: prepared against either instruction works.
DEFAULT_PATHS: tuple[Path, ...] = (
    Path("/boot/firmware/chessnut-bridge.conf"),
    Path("/boot/chessnut-bridge.conf"),
)

#: Overrides the search entirely, for development and tests.
PATH_ENV_VAR = "CHESSNUT_BRIDGE_CONF"

#: Shortest password accepted. Not a security boundary -- it is one field on a
#: LAN -- but four characters would be brute-forced faster than the login delay
#: can slow it down, and a two-character password is almost certainly a typo.
MIN_PASSWORD_LENGTH = 8

_KNOWN_KEYS = frozenset(
    {"chesscom_username", "web_password", "poll_seconds", "web_port"}
)


@dataclass(frozen=True)
class Config:
    """What the owner asked for, plus everything wrong with what they wrote."""

    chesscom_username: str | None = None
    #: Plaintext, as typed. Never logged, never rendered -- see ``__repr__``.
    web_password: str | None = None
    poll_seconds: float | None = None
    web_port: int = 80
    #: Which file this came from, so the UI can say where to go and fix it.
    source: Path | None = None
    #: Human-readable reasons the appliance cannot run normally. Displayable.
    problems: tuple[str, ...] = ()
    #: Keys that were not recognised. A warning rather than a problem: refusing
    #: to start over a stray line would be worse than ignoring it, but silently
    #: ignoring a misspelled ``web_pasword`` would leave the owner staring at a
    #: file that looks correct.
    unknown_keys: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """Whether the appliance can serve its normal UI."""
        return not self.problems

    def __repr__(self) -> str:
        """Redacts. This object ends up in log lines and exception context, and
        the whole point of the file is that it holds a password."""
        return (
            f"Config(chesscom_username={self.chesscom_username!r}, "
            f"web_password={'set' if self.web_password else 'unset'}, "
            f"poll_seconds={self.poll_seconds!r}, web_port={self.web_port!r}, "
            f"source={self.source!r}, problems={self.problems!r})"
        )

    __str__ = __repr__


def _parse(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Split ``key=value`` lines into values, unknown keys and complaints."""
    values: dict[str, str] = {}
    unknown: list[str] = []
    problems: list[str] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f"line {number} is not `key=value`: {line!r}")
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        # Only the surrounding whitespace goes: a password may legitimately
        # contain almost anything, and stripping quotes would make a password
        # that genuinely starts with one impossible to set.
        value = value.strip()
        if key not in _KNOWN_KEYS:
            unknown.append(key)
            continue
        values[key] = value

    return values, unknown, problems


def _number(
    values: dict[str, str], key: str, problems: list[str], cast
) -> float | int | None:
    raw = values.get(key)
    if not raw:
        return None
    try:
        return cast(raw)
    except ValueError:
        problems.append(f"{key} must be a number, not {raw!r}")
        return None


def load_config(paths: tuple[Path, ...] | None = None) -> Config:
    """Read the first config file that exists. Never raises.

    Returns a ``Config`` whose ``problems`` are empty only when the appliance can
    actually run. The caller is expected to serve those problems rather than
    treat them as a startup failure.
    """
    if paths is None:
        override = os.environ.get(PATH_ENV_VAR)
        paths = (Path(override),) if override else DEFAULT_PATHS

    found: Path | None = None
    text = ""
    problems: list[str] = []
    for path in paths:
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                found = path
                break
        except OSError as exc:
            # An unreadable file is worth naming: it is a different fix from a
            # missing one, and on a FAT32 boot partition it usually means the
            # card was pulled mid-write.
            problems.append(f"could not read {path}: {exc}")

    if found is None:
        wanted = paths[0]
        problems.append(
            f"no configuration file found -- create {wanted} on the SD card's "
            f"boot partition with `chesscom_username=` and `web_password=` lines"
        )
        return Config(problems=tuple(problems))

    values, unknown, parse_problems = _parse(text)
    problems.extend(parse_problems)

    username = values.get("chesscom_username") or None
    if not username:
        problems.append(f"chesscom_username is not set in {found}")

    password = values.get("web_password") or None
    if not password:
        problems.append(
            f"web_password is not set in {found} -- the web interface can submit "
            f"moves in your games, so it will not be served without one"
        )
    elif len(password) < MIN_PASSWORD_LENGTH:
        problems.append(
            f"web_password in {found} is shorter than "
            f"{MIN_PASSWORD_LENGTH} characters"
        )
        password = None

    poll = _number(values, "poll_seconds", problems, float)
    port = _number(values, "web_port", problems, int)

    if unknown:
        log.warning(
            "ignoring unrecognised setting(s) in %s: %s", found, ", ".join(unknown)
        )

    config = Config(
        chesscom_username=username,
        web_password=password,
        poll_seconds=poll,
        web_port=int(port) if port else 80,
        source=found,
        problems=tuple(problems),
        unknown_keys=tuple(unknown),
    )
    # Logged at info because the first question about a misbehaving appliance is
    # always "which config did it actually read". Safe: the repr redacts.
    log.info("loaded configuration: %s", config)
    return config
