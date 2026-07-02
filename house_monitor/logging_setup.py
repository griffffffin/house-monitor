"""A custom log level plus small formatting helpers for readable console output."""

import logging

# NOTICE sits between INFO and WARNING and marks the "significant" milestone
# messages (run start/end, total result count, email outcome, per-scraper
# summary counts). In interactive mode the console only shows NOTICE+ (plus
# warnings/errors); the detailed per-page/per-listing INFO logs still go to
# the log file (or, under systemd, to service.log) at full detail.
NOTICE = 25
logging.addLevelName(NOTICE, "NOTICE")


def log_notice(msg: str) -> None:
    logging.log(NOTICE, msg)


def _fmt_count(n: int) -> str:
    """Format a number with a space as the thousands separator (e.g. 4226 -> '4 226')."""
    return f"{n:,}".replace(",", " ")
