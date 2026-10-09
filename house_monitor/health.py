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
The same end-of-day check also counts the monitor's own errors of the day
(email not sent, DB not saved, the daily run or this check crashing). Every
finding keeps a streak of consecutive days; once a streak reaches
HEALTH_MIN_CONSECUTIVE days it goes into the "HIBÁK" section of the next
16:00 email (subject flagged with "hiba"), every day while it lasts. The
monitor not having run at all for that many days is reported the same way
when it starts again.
"""

import asyncio
import json
import logging
import os
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .config import HEALTH_MIN_CONSECUTIVE, HEALTH_PROBE_ATTEMPTS, HEALTH_PROBE_RETRY_DELAY

# Email labels of the monitor's own error kinds (streak keys "internal:<kind>").
INTERNAL_ERROR_LABELS = {
    "email_send": "Email-küldés",
    "db_save": "Adatbázis-mentés",
    "run_crash": "Napi futás",
    "health_check": "Napi hibaellenőrzés",
}


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


def update_streaks(
    previous: Dict[str, Any], findings: Dict[str, str], today: str
) -> Dict[str, Any]:
    """Consecutive-day streaks after today's check. `findings` maps a key
    ("source:<name>" or "internal:<kind>") to today's reason. A key found
    today continues its streak if it was last found yesterday, otherwise
    starts at 1; keys not found today drop out (= back to 0)."""
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    streaks = {}
    for key, reason in findings.items():
        prev = previous.get(key) or {}
        days = prev.get("days", 0) + 1 if prev.get("last") == yesterday else 1
        streaks[key] = {"days": days, "last": today, "reason": reason}
    return streaks


def due_alerts(streaks: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The streaks long enough to report, as email alert entries."""
    alerts = []
    for key, streak in sorted(streaks.items()):
        if streak["days"] < HEALTH_MIN_CONSECUTIVE:
            continue
        kind, _, name = key.partition(":")
        label = INTERNAL_ERROR_LABELS.get(name, name) if kind == "internal" else name
        alerts.append({"label": label, "days": streak["days"], "reason": streak["reason"]})
    return alerts


def missed_days(last_run: Optional[str], today: str) -> List[str]:
    """The dates strictly between the last daily run and today."""
    if not last_run:
        return []
    day, end = date.fromisoformat(last_run) + timedelta(days=1), date.fromisoformat(today)
    missed = []
    while day < end:
        missed.append(day.isoformat())
        day += timedelta(days=1)
    return missed


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


def email_subject(listings: int, alerts: int) -> str:
    """The 16:00 email's subject — it names "hiba" whenever errors are in it."""
    if not alerts:
        return f"Ingatlanok: {listings} db"
    if not listings:
        return f"Ingatlanok: {alerts} hiba"
    return f"Ingatlanok: {listings} db + {alerts} hiba"


def build_alert_section(alerts: List[Dict[str, Any]]) -> str:
    """The email's "HIBÁK" section (empty without alerts). In Hungarian, like
    the listing emails."""
    if not alerts:
        return ""
    lines = [
        "=" * 148 + "\n",
        f"HIBÁK — legalább {HEALTH_MIN_CONSECUTIVE} napja fennállnak; amíg így marad, "
        "minden nap jelezzük:\n\n",
    ]
    for alert in alerts:
        days = f" ({alert['days']}. napja)" if alert.get("days") else ""
        lines.append(f"- {alert['label']}{days}: {alert['reason']}\n")
    lines.append("\nRészletek: /var/log/house-monitor/service.log\n")
    return "".join(lines)
