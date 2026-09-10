"""Tests for the boot-partition configuration.

The property that matters most is not parsing -- it is that a *bad* file produces
something the owner can read and act on. They have no screen and no shell, so a
config error that stops the service leaves them with a device that does nothing
and says nothing.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bridge.web.config import (  # noqa: E402
    MIN_PASSWORD_LENGTH,
    PATH_ENV_VAR,
    Config,
    load_config,
)

GOOD = "chesscom_username=nbaronmorgan\nweb_password=a-long-enough-password\n"


def write_conf(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "chessnut-bridge.conf"
    path.write_text(text)
    return path


# --- the happy path -------------------------------------------------------


def test_a_good_file_is_usable(tmp_path):
    config = load_config((write_conf(tmp_path, GOOD),))
    assert config.is_usable
    assert config.problems == ()
    assert config.chesscom_username == "nbaronmorgan"
    assert config.web_password == "a-long-enough-password"


def test_comments_and_blank_lines_are_ignored(tmp_path):
    text = "# my bridge\n\nchesscom_username=nbaronmorgan\n\n  # note\nweb_password=hunter22222\n"
    config = load_config((write_conf(tmp_path, text),))
    assert config.is_usable
    assert config.chesscom_username == "nbaronmorgan"


def test_surrounding_whitespace_is_forgiven(tmp_path):
    """Typed into a text editor on a laptop, so trailing spaces are certain."""
    text = "  chesscom_username = nbaronmorgan  \n\tweb_password = hunter22222 \n"
    config = load_config((write_conf(tmp_path, text),))
    assert config.chesscom_username == "nbaronmorgan"
    assert config.web_password == "hunter22222"


def test_keys_are_case_insensitive(tmp_path):
    config = load_config((write_conf(tmp_path, "CHESSCOM_USERNAME=nb\nWeb_Password=hunter22222\n"),))
    assert config.is_usable
    assert config.chesscom_username == "nb"


def test_a_password_containing_an_equals_sign_survives(tmp_path):
    """Passwords get pasted from generators, which love `=` padding."""
    config = load_config((write_conf(tmp_path, "chesscom_username=nb\nweb_password=ab=cd==ef\n"),))
    assert config.web_password == "ab=cd==ef"


def test_quotes_are_kept_rather_than_stripped(tmp_path):
    """Stripping them would make a password that really starts with a quote
    impossible to set, and silently log the owner out of their own device."""
    config = load_config((write_conf(tmp_path, 'chesscom_username=nb\nweb_password="quoted!!"\n'),))
    assert config.web_password == '"quoted!!"'


def test_optional_numbers_are_read(tmp_path):
    text = GOOD + "poll_seconds=45\nweb_port=8080\n"
    config = load_config((write_conf(tmp_path, text),))
    assert config.poll_seconds == 45.0
    assert config.web_port == 8080


def test_the_port_defaults_without_being_set(tmp_path):
    config = load_config((write_conf(tmp_path, GOOD),))
    assert config.web_port == 80
    assert config.poll_seconds is None


def test_the_source_is_reported_so_the_owner_knows_which_file(tmp_path):
    path = write_conf(tmp_path, GOOD)
    assert load_config((path,)).source == path


# --- a bad file explains itself -------------------------------------------


def test_a_missing_file_names_the_path_to_create(tmp_path):
    config = load_config((tmp_path / "absent.conf",))
    assert not config.is_usable
    assert "absent.conf" in " ".join(config.problems)
    assert "boot partition" in " ".join(config.problems)


def test_a_missing_password_is_refused_not_defaulted(tmp_path):
    """The UI can submit moves in real games; serving it open would be the worst
    outcome of a typo."""
    config = load_config((write_conf(tmp_path, "chesscom_username=nb\n"),))
    assert not config.is_usable
    assert config.web_password is None
    assert any("web_password" in problem for problem in config.problems)


def test_an_empty_password_is_treated_as_missing(tmp_path):
    config = load_config((write_conf(tmp_path, "chesscom_username=nb\nweb_password=\n"),))
    assert not config.is_usable
    assert config.web_password is None


def test_a_short_password_is_refused_and_not_carried(tmp_path):
    """Carrying it anyway would leave a usable-looking config with a password the
    guard would accept."""
    config = load_config((write_conf(tmp_path, "chesscom_username=nb\nweb_password=abc\n"),))
    assert not config.is_usable
    assert config.web_password is None
    assert str(MIN_PASSWORD_LENGTH) in " ".join(config.problems)


def test_a_missing_username_is_a_problem(tmp_path):
    config = load_config((write_conf(tmp_path, "web_password=hunter22222\n"),))
    assert not config.is_usable
    assert any("chesscom_username" in problem for problem in config.problems)


def test_every_problem_is_reported_not_just_the_first(tmp_path):
    """One round trip to the laptop per typo would be miserable."""
    config = load_config((write_conf(tmp_path, "poll_seconds=soon\n"),))
    joined = " ".join(config.problems)
    assert "chesscom_username" in joined
    assert "web_password" in joined
    assert "poll_seconds" in joined


def test_a_line_without_an_equals_sign_names_its_line_number(tmp_path):
    config = load_config((write_conf(tmp_path, GOOD + "this is just a note\n"),))
    assert any("line 3" in problem for problem in config.problems)


def test_a_bad_number_is_a_problem_rather_than_a_crash(tmp_path):
    config = load_config((write_conf(tmp_path, GOOD + "poll_seconds=often\n"),))
    assert not config.is_usable
    assert any("often" in problem for problem in config.problems)


def test_an_unknown_key_is_warned_about_but_not_fatal(tmp_path, caplog):
    """A stray line must not stop the appliance, but a misspelled `web_pasword`
    would otherwise leave the owner staring at a file that looks right."""
    text = GOOD + "web_pasword=typo\n"
    with caplog.at_level(logging.WARNING):
        config = load_config((write_conf(tmp_path, text),))
    assert config.is_usable
    assert "web_pasword" in config.unknown_keys
    assert "web_pasword" in caplog.text


def test_an_unreadable_file_is_reported_not_raised(tmp_path, monkeypatch):
    """A card pulled mid-write is the realistic cause, and it needs a different
    fix from a missing file."""
    path = write_conf(tmp_path, GOOD)
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("I/O error"))
    )
    config = load_config((path,))
    assert not config.is_usable
    assert any("I/O error" in problem for problem in config.problems)


def test_undecodable_bytes_do_not_crash_the_load(tmp_path):
    """FAT32 and a text editor that saved as UTF-16 is a plausible pairing."""
    path = tmp_path / "chessnut-bridge.conf"
    path.write_bytes(b"chesscom_username=nb\nweb_password=hunter2\xff\xfe2222\n")
    config = load_config((path,))
    assert isinstance(config, Config)


# --- where it looks -------------------------------------------------------


def test_the_first_existing_path_wins(tmp_path):
    """Both boot locations are searched, so a card prepared against either
    instruction works."""
    second = tmp_path / "second.conf"
    second.write_text("chesscom_username=second\nweb_password=hunter22222\n")
    first = tmp_path / "first.conf"
    first.write_text("chesscom_username=first\nweb_password=hunter22222\n")
    assert load_config((first, second)).chesscom_username == "first"


def test_a_missing_first_path_falls_through_to_the_second(tmp_path):
    second = write_conf(tmp_path, GOOD)
    config = load_config((tmp_path / "absent.conf", second))
    assert config.is_usable
    assert config.source == second


def test_the_environment_override_is_honoured(tmp_path, monkeypatch):
    path = write_conf(tmp_path, GOOD)
    monkeypatch.setenv(PATH_ENV_VAR, str(path))
    assert load_config().source == path


# --- not leaking the password --------------------------------------------


def test_the_config_does_not_render_its_password(tmp_path):
    """This object lands in log lines and exception context, and the whole point
    of the file is that it holds a password."""
    config = load_config((write_conf(tmp_path, "chesscom_username=nb\nweb_password=sekrit-password\n"),))
    rendered = f"{config!r} {config!s}"
    assert "sekrit-password" not in rendered
    assert "nb" in rendered, "non-secret settings stay debuggable"


def test_loading_does_not_log_the_password(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        load_config((write_conf(tmp_path, "chesscom_username=nb\nweb_password=sekrit-password\n"),))
    assert "sekrit-password" not in caplog.text


def test_a_problem_message_does_not_quote_the_password(tmp_path, caplog):
    """The short-password complaint is the one that would be tempted to."""
    with caplog.at_level(logging.DEBUG):
        config = load_config((write_conf(tmp_path, "chesscom_username=nb\nweb_password=sekrit\n"),))
    assert "sekrit" not in " ".join(config.problems)
    assert "sekrit" not in caplog.text
