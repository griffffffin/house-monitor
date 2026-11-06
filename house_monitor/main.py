"""Entry point: runs the startup checks (house_monitor/startup.py), takes the
lock and starts HouseMonitor.

    python3 -m house_monitor.main          # the monitor (the service's ExecStart)
    python3 -m house_monitor.main --check  # only the startup checks

Exit codes: 0 — `--check` passed (the monitor itself never returns); 1 —
another monitor holds the lock; 78 (EX_CONFIG) — a configuration error: no
usable state directory, a lock file that can't be opened, missing mail
settings. The service unit doesn't restart on 78.
"""

import argparse
import asyncio
import os
import signal
import sys
from typing import List, Optional

from .monitor import HouseMonitor
from .startup import (
    EX_CONFIG,
    AlreadyRunning,
    ConfigError,
    lock_exclusively,
    run_startup_checks,
)


def _handle_sigterm(signum, frame) -> None:
    # systemd's KillMode=mixed sends SIGTERM on `systemctl stop`. Python has
    # no default handler for it (unlike SIGINT, which already raises
    # KeyboardInterrupt) - without this, the process would die immediately,
    # skipping HouseMonitor.run()'s `finally: await self._save_db()`.
    raise KeyboardInterrupt()


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python3 -m house_monitor.main",
        description="Watches real-estate sites and emails new and cheaper listings.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "run only the startup checks (state directory, lock file, mail settings) "
            f"and exit: 0 if they pass, {EX_CONFIG} if not. No network, no email, "
            "no state written."
        ),
    )
    return parser.parse_args(argv)


def _config_error(error: ConfigError) -> int:
    print(
        f"Configuration error: {error}. Exiting with {EX_CONFIG} (EX_CONFIG).",
        file=sys.stderr,
        flush=True,
    )
    return EX_CONFIG


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        startup = run_startup_checks(os.environ)
    except ConfigError as e:
        return _config_error(e)

    if args.check:
        startup.lock.close()
        print(
            f"Startup checks passed: state directory {startup.state.directory}, "
            f"lock file {startup.state.lock_file}, mail settings from {startup.email_source}.",
            flush=True,
        )
        return 0

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        lock_exclusively(startup.lock)
    except AlreadyRunning:
        startup.lock.close()
        print(
            f"Another instance is already running (lock: {startup.state.lock_file}). Exiting.",
            flush=True,
        )
        return 1
    except ConfigError as e:
        startup.lock.close()
        return _config_error(e)

    monitor = HouseMonitor(startup.state, startup.email_config)
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        startup.lock.close()  # releases the lock
    return 0


if __name__ == "__main__":
    sys.exit(main())
