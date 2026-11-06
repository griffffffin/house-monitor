"""Entry point: runs the startup checks (house_monitor/startup.py), takes the
lock and starts HouseMonitor.

Exit codes: 1 — another monitor holds the lock; 78 (EX_CONFIG) — a
configuration error: no usable state directory, or a lock file that can't
be opened. The service unit doesn't restart on 78.
"""

import asyncio
import os
import signal
import sys

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


def _config_error(error: ConfigError) -> int:
    print(
        f"Configuration error: {error}. Exiting with {EX_CONFIG} (EX_CONFIG).",
        file=sys.stderr,
        flush=True,
    )
    return EX_CONFIG


def main() -> int:
    try:
        startup = run_startup_checks(os.environ)
    except ConfigError as e:
        return _config_error(e)

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

    monitor = HouseMonitor(startup.state)
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        startup.lock.close()  # releases the lock
    return 0


if __name__ == "__main__":
    sys.exit(main())
