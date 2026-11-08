#!/usr/bin/env python3
"""Check that the installed packages match the pins in requirements.txt.

The service runs the Pi's /usr/bin/python3 with its apt packages (no venv), and
requirements.txt pins exactly those versions, so that a dev venv and CI test
what production runs. This check notices when the two drift apart, e.g. after
an apt upgrade on the Pi. Run it there with the system Python:

    cd /opt/house-monitor && /usr/bin/python3 -s scripts/check_pinned_deps.py

(-s leaves out ~/.local, which the service user, who has no home, doesn't see
either.) It also checks the interpreter's version against .python-version, the
version CI tests with.

It also looks for the optional packages aiohttp starts using by itself when
they are importable (aiodns, Brotli, ...): one installed but not pinned puts
production and the dev venv on different code paths, so it counts as drift.

Exit codes: 0 = everything matches, 1 = drift (a version differs, a pinned
package is missing, an automatically used package is installed but not pinned,
or the Python version differs), 2 = the requirements or
.python-version file can't be read or contains something other than exact
pins. Every requirements line must be `name==version`: a range or a marker
would make "matches" ambiguous, so the check refuses it instead of skipping it.

Standard library only, and nothing from house_monitor is imported: the point
is to report a broken or missing dependency, not to crash on it.
"""

import argparse
import importlib.metadata
import importlib.util
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIREMENTS = REPO_ROOT / "requirements.txt"
DEFAULT_PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"

_PIN_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)==([A-Za-z0-9.+!_-]+)$")
_PYTHON_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

# (module, distribution) pairs aiohttp switches to on its own as soon as the
# module is importable: aiodns makes c-ares its default DNS resolver (the Pi has
# it via apt, and it behaves differently from the threaded getaddrinfo one),
# Brotli/brotlicffi add `br` response decoding, backports.zstd adds zstd before
# Python 3.14. Installed on one side only, such a package silently changes what
# the dev venv tests compared to production, so an importable one must be pinned.
AUTO_USED = (
    ("aiodns", "aiodns"),
    ("brotli", "Brotli"),
    ("brotlicffi", "brotlicffi"),
    ("backports.zstd", "backports.zstd"),
)


class Row(NamedTuple):
    name: str
    pinned: str
    installed: Optional[str]

    @property
    def ok(self) -> bool:
        return self.installed == self.pinned


def normalize(name: str) -> str:
    """PEP 503 name normalization: `typing_extensions` == `Typing-Extensions`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(text: str) -> Dict[str, str]:
    """`name==version` lines -> {name: version}, in file order.

    Comments and blank lines are skipped. Anything else raises ValueError.
    """
    pins: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _PIN_RE.match(line)
        if not match:
            raise ValueError(f"line {lineno}: not an exact `name==version` pin: {line!r}")
        name, version = match.groups()
        key = normalize(name)
        if key in seen:
            raise ValueError(f"line {lineno}: {name} is already pinned on line {seen[key]}")
        seen[key] = lineno
        pins[name] = version
    return pins


def parse_python_version(text: str) -> str:
    """The first non-comment line of .python-version: `3.14` or `3.14.4`."""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not _PYTHON_VERSION_RE.match(line):
            raise ValueError(f"not a major.minor[.micro] Python version: {line!r}")
        return line
    raise ValueError("no Python version in the file")


def installed_version(name: str) -> Optional[str]:
    """The version of the distribution this interpreter would import, or None."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):  # e.g. the parent package of a dotted name is missing
        return False


def unpinned_auto_used(
    pins: Dict[str, str], is_importable: Callable[[str], bool] = importable
) -> List[str]:
    """The AUTO_USED distributions that are importable here but not pinned."""
    pinned = {normalize(name) for name in pins}
    return [
        dist
        for module, dist in AUTO_USED
        if is_importable(module) and normalize(dist) not in pinned
    ]


def python_matches(expected: str, actual: Sequence[object]) -> bool:
    """`3.14` matches any 3.14.x; `3.14.4` only 3.14.4."""
    wanted = [int(part) for part in expected.split(".")]
    return list(actual[: len(wanted)]) == wanted


def check_pins(
    pins: Dict[str, str], lookup: Callable[[str], Optional[str]] = installed_version
) -> List[Row]:
    return [Row(name, version, lookup(name)) for name, version in pins.items()]


def format_report(
    rows: List[Row],
    python_expected: str,
    python_actual: Sequence[object],
    executable: str,
    unpinned: Sequence[str] = (),
) -> str:
    actual = ".".join(str(part) for part in python_actual[:3])
    python_status = "OK" if python_matches(python_expected, python_actual) else "MISMATCH"
    lines = [
        f"Python {actual} ({executable}), expected {python_expected}: {python_status}",
        "",
        f"{'Package':<20}{'Pinned':<12}{'Installed':<16}Status",
        "-" * 54,
    ]
    for row in rows:
        installed = row.installed if row.installed is not None else "(missing)"
        status = "OK" if row.ok else ("MISSING" if row.installed is None else "DIFFERS")
        lines.append(f"{row.name:<20}{row.pinned:<12}{installed:<16}{status}")
    for dist in unpinned:
        lines.append(f"{dist:<20}{'(none)':<12}{'(importable)':<16}UNPINNED")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--python-version-file", type=Path, default=DEFAULT_PYTHON_VERSION_FILE)
    args = parser.parse_args(argv)

    try:
        pins = parse_pins(args.requirements.read_text(encoding="utf-8"))
        python_expected = parse_python_version(args.python_version_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not pins:
        print(f"ERROR: no pins in {args.requirements}", file=sys.stderr)
        return 2

    rows = check_pins(pins)
    unpinned = unpinned_auto_used(pins)
    print(format_report(rows, python_expected, sys.version_info, sys.executable, unpinned))

    drift = [row for row in rows if not row.ok]
    python_ok = python_matches(python_expected, sys.version_info)
    print()
    if not drift and not unpinned and python_ok:
        print(f"All {len(rows)} pins match, and the Python version too.")
        return 0
    problems = []
    if drift:
        problems.append(
            f"{len(drift)} of {len(rows)} packages differ from {args.requirements.name}"
        )
    if unpinned:
        problems.append(
            f"installed but not pinned, and aiohttp uses it by itself: {', '.join(unpinned)}"
        )
    if not python_ok:
        problems.append(f"Python is not {python_expected} ({args.python_version_file.name})")
    print("DRIFT: " + "; ".join(problems) + ".")
    return 1


if __name__ == "__main__":
    sys.exit(main())
