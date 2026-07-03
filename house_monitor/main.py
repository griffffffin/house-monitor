"""Entry point: acquires a lock file (to prevent overlapping runs) and starts HouseMonitor."""

import asyncio
import fcntl
import signal
import sys

from .monitor import HouseMonitor

LOCK_FILE = "/tmp/house-monitor.lock"


def _handle_sigterm(signum, frame) -> None:
    # systemd's KillMode=mixed sends SIGTERM on `systemctl stop`. Python has
    # no default handler for it (unlike SIGINT, which already raises
    # KeyboardInterrupt) - without this, the process would die immediately,
    # skipping HouseMonitor.run()'s `finally: await self._save_db()`.
    raise KeyboardInterrupt()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance is already running (lock: /tmp/house-monitor.lock). Exiting.")
        sys.exit(1)

    monitor = HouseMonitor()
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
