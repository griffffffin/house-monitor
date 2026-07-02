"""Entry point: acquires a lock file (to prevent overlapping runs) and starts HouseMonitor."""

import asyncio
import fcntl
import sys

from .monitor import HouseMonitor

LOCK_FILE = "/tmp/house-monitor.lock"


def main() -> None:
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
