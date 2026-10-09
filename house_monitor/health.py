"""Daily source health check: tells "nothing in the price range right now"
apart from "the site or our parser broke".

The search URLs carry the 3000-70000 € range server-side, so a working source
can legitimately return 0 results for weeks. A plain "0 results" rule would
therefore cry wolf daily, while a site redesign that silently breaks a
selector also shows up as nothing but "0 results" (the Findheim case in
CLAUDE.md). So a source that returned 0 listings for the day (after the
same-day retries) is checked further. It counts as broken today if
  - every fetch of the day ended on an error, or
  - its probe (scraper.probe(): page 1 of the same search WITHOUT the price
    range, parsed by the scraper's own code) still fails after retries, or
  - the probe page has no card with a readable price: 0 cards means the card
    selector broke, cards without any price mean the price selector broke.
Once a source has been broken on HEALTH_MIN_CONSECUTIVE consecutive daily
checks, an alert email goes out — every day while it stays broken.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .config import HEALTH_PROBE_ATTEMPTS, HEALTH_PROBE_RETRY_DELAY


def format_error(e: BaseException) -> str:
    """The exception's text prefixed with its type — some exceptions (asyncio
    timeouts) stringify to '', which would leave an empty reason."""
    msg = str(e)
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


async def probe_source(scraper: Any) -> Tuple[Optional[list], Optional[str]]:
    """Run scraper.probe(), retrying a failed attempt (a one-off blip is not
    a broken source). Returns (cards, None) on success, (None, error) if
    every attempt failed."""
    name = type(scraper).__name__
    error = ""
    for attempt in range(1, HEALTH_PROBE_ATTEMPTS + 1):
        try:
            return await scraper.probe(), None
        except Exception as e:
            error = format_error(e)
            logging.warning(
                f"{name}: probe attempt {attempt}/{HEALTH_PROBE_ATTEMPTS} failed: {error}"
            )
            if attempt < HEALTH_PROBE_ATTEMPTS:
                await asyncio.sleep(HEALTH_PROBE_RETRY_DELAY)
    return None, error


async def diagnose(scraper: Any, day_count: int, fetch_failed: bool) -> Optional[str]:
    """None if the source is healthy today, otherwise why it looks broken —
    in Hungarian, since it goes straight into the alert email."""
    if day_count > 0:
        return None
    if fetch_failed:
        return "a mai lekérés minden próbálkozásnál hibával állt le (pl. időtúllépés)"
    cards, error = await probe_source(scraper)
    if cards is None:
        return f"0 találat, és a szűrés nélküli próba-lekérés is hibára futott ({error})"
    if not cards:
        return (
            "0 találat, és a szűrés nélküli oldalon sem talált egy hirdetést sem "
            "(valószínűleg megváltozott az oldal szerkezete)"
        )
    if not any(card[3] > 0 for card in cards):
        return (
            f"0 találat; a szűrés nélküli oldalon {len(cards)} hirdetés van, "
            "de egyikből sem tudja kiolvasni az árat"
        )
    # The site and the parser work — there's just nothing in the price range.
    return None


def next_failure_counts(previous: Dict[str, int], broken: List[str]) -> Dict[str, int]:
    """Consecutive broken-day counters after today's check: today's broken
    sources go up by one, every other source drops out (= back to 0)."""
    return {name: previous.get(name, 0) + 1 for name in broken}


def load_state(path: str) -> Dict[str, Any]:
    """The persisted state; {} if missing or unreadable. The worst case of a
    lost state is an alert one day later, never a false one."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logging.error(f"Error loading {path}: {e}")
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: str, state: Dict[str, Any]) -> None:
    """Atomic write (temp file + os.replace), like the seen-DB."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_alert(alerts: List[Tuple[str, int, str]]) -> Tuple[str, str]:
    """(subject, body) of the alert email for (source, broken days, reason)
    entries. In Hungarian, like the listing emails."""
    subject = f"Ingatlanok: {len(alerts)} forrás lehet, hogy elromlott"
    lines = [
        "Ezek a források egymást követő napokon 0 találatot hoztak, és nem azért, "
        "mert nincs hirdetés az ársávban — lehet, hogy elromlottak:\n\n"
    ]
    for name, days, reason in alerts:
        lines.append(f"- {name} ({days}. napja): {reason}\n")
    lines.append(
        "\nAmíg így marad, minden nap jön erről egy levél. "
        "Részletek: /var/log/house-monitor/service.log\n"
    )
    return subject, "".join(lines)
