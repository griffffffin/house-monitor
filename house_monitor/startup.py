"""Startup checks: where the monitor keeps its state, its lock, its mail settings.

main() runs these before anything else. A failed check raises ConfigError,
which main() turns into exit code 78 (EX_CONFIG): the service unit doesn't
restart on it, so a broken setup stays visibly failed instead of retrying
every five minutes. Nothing here runs at import time — the tests and
scripts/live_smoke_check.py import the package too.

As a systemd service (INVOCATION_ID is set) the code directory is read-only
and the one writable place is the state directory systemd passes in
STATE_DIRECTORY (StateDirectory= in the unit); the mail settings come from the
systemd credential "smtp" ($CREDENTIALS_DIRECTORY/smtp, ImportCredential= in
the unit). On a manual run the state lives in the project root and the mail
settings in environment variables of the same names.
"""

import fcntl
import os
import tempfile
from dataclasses import dataclass
from typing import IO, Any, Dict, Mapping

from .config import DATA_FILE_NAME, HEALTH_STATE_FILE_NAME, LOCK_FILE_NAME, PROJECT_ROOT

# sysexits.h's "configuration error".
EX_CONFIG = 78

# The credential's name (the file in $CREDENTIALS_DIRECTORY) and its keys,
# which are also the manual run's environment variables.
SMTP_CREDENTIAL = "smtp"
SMTP_KEYS = ("SMTP_SERVER", "SMTP_PORT", "SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL")
# No email can go out without any of these (SMTP_PORT defaults to 587).
SMTP_REQUIRED_KEYS = ("SMTP_SERVER", "SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL")
SMTP_DEFAULT_SERVER = "smtp.gmail.com"  # manual runs only, as before
SMTP_DEFAULT_PORT = "587"


class ConfigError(Exception):
    """A startup check failed. The message says what is wrong and where —
    never a secret: no password, no line of the credential file."""


class AlreadyRunning(Exception):
    """Another monitor holds the lock of this state directory."""


def service_mode(environ: Mapping[str, str]) -> bool:
    """Running as the systemd service (systemd sets INVOCATION_ID)."""
    return bool(environ.get("INVOCATION_ID"))


@dataclass(frozen=True)
class StatePaths:
    """Every file the monitor writes — all in the one state directory (the
    atomic writes' .tmp files too, as data_file/health_state_file + ".tmp")."""

    directory: str
    data_file: str  # the seen-DB
    health_state_file: str  # the daily error checks' state (health.py)
    lock_file: str

    @classmethod
    def in_dir(cls, directory: str) -> "StatePaths":
        return cls(
            directory=directory,
            data_file=os.path.join(directory, DATA_FILE_NAME),
            health_state_file=os.path.join(directory, HEALTH_STATE_FILE_NAME),
            lock_file=os.path.join(directory, LOCK_FILE_NAME),
        )


def resolve_state_dir(environ: Mapping[str, str]) -> str:
    """The state directory. As a service: STATE_DIRECTORY, which must be
    exactly one existing, writable, absolute path. There is deliberately no
    fallback: the code directory is read-only there and /tmp is emptied on
    every start, so the seen-DB would be lost silently and the next run would
    email every ad as new. On a manual run: the project root."""
    if not service_mode(environ):
        return PROJECT_ROOT
    directory = environ.get("STATE_DIRECTORY", "")
    if not directory:
        raise ConfigError(
            "STATE_DIRECTORY is not set: the service keeps its seen-DB in the state "
            "directory systemd passes there (StateDirectory= in the unit)"
        )
    if ":" in directory:
        # systemd joins several StateDirectory= entries with ":".
        raise ConfigError(
            f"STATE_DIRECTORY names several directories ({directory}), the monitor needs one"
        )
    if not os.path.isabs(directory):
        raise ConfigError(f"STATE_DIRECTORY is not an absolute path ({directory})")
    if not os.path.isdir(directory):
        raise ConfigError(f"STATE_DIRECTORY {directory} is not an existing directory")
    try:
        # An unnamed file (O_TMPFILE) where the filesystem has it, else a
        # named one that is removed at once — nothing is left behind.
        with tempfile.TemporaryFile(dir=directory):
            pass
    except OSError as e:
        raise ConfigError(
            f"STATE_DIRECTORY {directory} is not writable ({e.strerror or e})"
        ) from None
    return directory


def open_lock_file(path: str) -> IO[str]:
    """The lock file, opened and created if missing, but not locked yet. A
    lock file that can't be opened is a configuration error, never "already
    running" — while the lock sat in the shared /tmp, any local user could
    create it first, and the monitor exited saying another one was running."""
    try:
        return open(path, "a", encoding="ascii")
    except OSError as e:
        raise ConfigError(f"cannot open the lock file {path} ({e.strerror or e})") from None


def lock_exclusively(lock: IO[str]) -> None:
    """Take the lock without waiting; AlreadyRunning if another monitor holds it."""
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise AlreadyRunning(lock.name) from None
    except OSError as e:
        raise ConfigError(f"cannot lock {lock.name} ({e.strerror or e})") from None


def parse_credential(text: str) -> Dict[str, str]:
    """The SMTP settings in a credential file: one KEY=VALUE per line, the
    key matched exactly, the value taken literally — no quote, `$` or `\\`
    processing, no `export` (a password may hold `=`, `#`, quotes, spaces).
    Lines starting with `#` and empty lines are skipped, a trailing \\r is
    cut, other keys are ignored, a later line wins. The same rules as the
    host's other mail senders."""
    settings: Dict[str, str] = {}
    # Not splitlines(): it would also split a value at \\v, \\f, \\x1c-\\x1e,
    # \\x85 or \\u2028.
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key in SMTP_KEYS:
            settings[key] = value
    return settings


def read_credential(path: str) -> Dict[str, str]:
    """parse_credential() of the file at `path`; ConfigError if it can't be read."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise ConfigError(
            f"the mail credential {path} does not exist "
            f"(ImportCredential={SMTP_CREDENTIAL} in the unit)"
        ) from None
    except OSError as e:
        raise ConfigError(f"cannot read the mail credential {path} ({e.strerror or e})") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # The exception's own text quotes a byte of the file.
        raise ConfigError(f"the mail credential {path} is not UTF-8 text") from None
    return parse_credential(text)


def load_email_config(environ: Mapping[str, str]) -> Dict[str, Any]:
    """The EmailNotifier settings. As a service: the credential, which must
    give SMTP_SERVER, SENDER_EMAIL, SENDER_PASSWORD and RECIPIENT_EMAIL —
    without email the monitor would mark ads as seen without telling anyone.
    On a manual run: the environment, as before (no check)."""
    if service_mode(environ):
        directory = environ.get("CREDENTIALS_DIRECTORY", "")
        if not directory:
            raise ConfigError(
                "CREDENTIALS_DIRECTORY is not set: the service reads its mail settings "
                f"from the systemd credential {SMTP_CREDENTIAL} (ImportCredential= in the unit)"
            )
        path = os.path.join(directory, SMTP_CREDENTIAL)
        settings = read_credential(path)
        missing = [key for key in SMTP_REQUIRED_KEYS if not settings.get(key)]
        if missing:
            raise ConfigError(
                f"the mail credential {path} gives no value for {', '.join(missing)}; "
                "without email the monitor would mark ads as seen without telling anyone"
            )
        server = settings["SMTP_SERVER"]
    else:
        settings = {key: environ[key] for key in SMTP_KEYS if key in environ}
        server = settings.get("SMTP_SERVER", SMTP_DEFAULT_SERVER)
    port = settings.get("SMTP_PORT") or SMTP_DEFAULT_PORT
    if not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        raise ConfigError("SMTP_PORT is not a port number")
    return {
        "smtp_server": server,
        "smtp_port": int(port),
        "sender_email": settings.get("SENDER_EMAIL", ""),
        "sender_password": settings.get("SENDER_PASSWORD", ""),
        "recipient_email": settings.get("RECIPIENT_EMAIL", ""),
    }


@dataclass
class Startup:
    """What the checks found: the state's place, the mail settings, and the
    opened, not yet taken lock file."""

    state: StatePaths
    email_config: Dict[str, Any]
    lock: IO[str]


def run_startup_checks(environ: Mapping[str, str]) -> Startup:
    """The startup gates, in order: the state directory, the mail settings,
    the lock file. Raises ConfigError. They make no request, send nothing and
    write no state; the only file they may leave behind is the empty lock
    file, created when missing (the state directory's write check leaves
    nothing)."""
    state = StatePaths.in_dir(resolve_state_dir(environ))
    email_config = load_email_config(environ)
    lock = open_lock_file(state.lock_file)
    return Startup(state, email_config, lock)
