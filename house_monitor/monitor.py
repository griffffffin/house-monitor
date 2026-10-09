"""The orchestrator: runs all scrapers, dedupes/filters results, sends the
notification email, and persists the seen-listings database."""

import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import aiofiles
import aiohttp

from .config import (
    BLACKLIST,
    DATA_FILE,
    DUPLICATE_LOOKBACK_DAYS,
    EMAIL_CONFIG,
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    HEALTH_MIN_CONSECUTIVE,
    HEALTH_STATE_FILE,
    HEALTH_UNMATCHED_MIN,
    INCOMPLETE_RETRY_DELAYS,
    LOG_FILE,
    SKIP_NO_PERSIST,
    TITLE_SUBSTRING_MIN_LEN,
)
from . import health
from .email_notifier import EmailNotifier
from .logging_setup import NOTICE, _fmt_count, log_notice

# parse_de_price / decode_utf8_or_latin1 aren't used directly in this module,
# but stay reachable from here too (e.g. for tests) because HouseMonitor
# itself doesn't use them — only the scrapers do.
from .models import Listing, decode_utf8_or_latin1, parse_de_price  # noqa: F401
from .scrapers import (
    BazarScraper,
    DerStandardScraper,
    DibeoScraper,
    DingDongScraper,
    FindheimScraper,
    FindMyHomeScraper,
    GoldgrubeScraper,
    HegerRealScraper,
    ImmobIlienNetScraper,
    ImmobilienDeScraper,
    ImmodirektScraper,
    ImmiScraper,
    ImmokralleScraper,
    ImmoLive24Scraper,
    ImmoScout24Scraper,
    LystioScraper,
    OhneMaklerScraper,
    PropyloScraper,
    RaiffeisenScraper,
    SonnbergerScraper,
    WillhabenScraper,
    WohnnetScraper,
)

# Per-scraper display name + unit for the final, alphabetically sorted
# console summary in run() (the "listings" group first, then "ads", each
# sorted by name) - the completion order of the concurrent gather() would
# otherwise be chaotic.
SCRAPER_SUMMARY_LABELS = {
    "ImmoScout24Scraper": ("ImmoScout24", "listings"),
    "DibeoScraper": ("Dibeo", "listings"),
    "FindMyHomeScraper": ("FindMyHome", "listings"),
    "WillhabenScraper": ("Willhaben", "listings"),
    "FindheimScraper": ("Findheim", "listings"),
    "WohnnetScraper": ("Wohnnet", "listings"),
    "DerStandardScraper": ("DerStandard", "listings"),
    "ImmodirektScraper": ("Immodirekt", "listings"),
    "RaiffeisenScraper": ("Raiffeisen", "listings"),
    "OhneMaklerScraper": ("OhneMakler", "ads"),
    "ImmobIlienNetScraper": ("Immobilien.net", "ads"),
    "ImmokralleScraper": ("Immokralle", "ads"),
    "ImmiScraper": ("Immi", "ads"),
    "BazarScraper": ("Bazar", "listings"),
    "ImmobilienDeScraper": ("Immobilien.de", "ads"),
    "GoldgrubeScraper": ("Goldgrube", "ads"),
    "ImmoLive24Scraper": ("ImmoLive24", "ads"),
    "DingDongScraper": ("DingDong", "ads"),
    "SonnbergerScraper": ("Sonnberger", "ads"),
    "HegerRealScraper": ("HegerReal", "listings"),
    "PropyloScraper": ("Propylo", "listings"),
    "LystioScraper": ("Lystio", "listings"),
}

# Listing.source (the raw, per-scraper value, e.g. "sonnberger.co.at") ->
# the short display name already used on the console (SCRAPER_SUMMARY_LABELS
# values). We use the same names in the email body's source headers, to stay
# consistent with the console summary.
SOURCE_DISPLAY_NAMES = {
    "ImmoScout24": "ImmoScout24",
    "Dibeo.at": "Dibeo",
    "FindMyHome.at": "FindMyHome",
    "willhaben.at": "Willhaben",
    "findheim.at": "Findheim",
    "wohnnet.at": "Wohnnet",
    "immobilien.derstandard.at": "DerStandard",
    "immodirekt.at": "Immodirekt",
    "raiffeisen-immobilien.at": "Raiffeisen",
    "ohne-makler.at": "OhneMakler",
    "immobilien.net": "Immobilien.net",
    "immokralle.com": "Immokralle",
    "immi.at": "Immi",
    "Bazar.at": "Bazar",
    "immobilien.de": "Immobilien.de",
    "goldgrube.at": "Goldgrube",
    "immolive24.at": "ImmoLive24",
    "ding-dong.at": "DingDong",
    "sonnberger.co.at": "Sonnberger",
    "hegerreal.at": "HegerReal",
    "at.propylo.com": "Propylo",
    "lystio.at": "Lystio",
}


class HouseMonitor:
    def __init__(self):
        self._setup_logging()
        self.notifier = EmailNotifier(EMAIL_CONFIG)
        self.seen: Dict[str, Listing] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        # Scraper class name -> how many listings its latest fetch returned
        # (a retry overwrites the main run's count) — for the health check.
        self.day_counts: Dict[str, int] = {}
        # The monitor's own errors today (kind -> detail), counted by the
        # end-of-day check into multi-day streaks (see health.py).
        self.today_errors: Dict[str, str] = {}
        # Whether the last _scrape_and_notify() email went out.
        self.last_email_ok = True
        # Today's Propylo cross-check (portal -> in-range houses our own
        # scraper of it never returned) and the sources that only came
        # through the same-day retry — both for the end-of-day check.
        self.day_unmatched: Dict[str, int] = {}
        self.day_late: List[Any] = []

    def _setup_logging(self):
        logger = logging.getLogger()
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        if os.getenv("INVOCATION_ID"):
            # Systemd service: stdout only (journald captures it)
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        else:
            # Manual run: the console only shows NOTICE+ messages, in a
            # compact "HH:MM message" format. Detailed per-page/per-listing
            # INFO logs still go to the file only, with the full
            # date+level format, for debugging.
            console_formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M")
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(console_formatter)
            console_handler.setLevel(NOTICE)
            console_handler.flush = lambda: sys.stdout.flush()
            logger.addHandler(console_handler)
            try:
                file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except Exception as e:
                print(f"WARNING: Could not open log file {LOG_FILE}: {e}", flush=True)

        logging.info(f"Logging initialized. LOG_FILE={LOG_FILE}")

    async def _load_db(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            async with aiofiles.open(DATA_FILE, "r", encoding="utf-8") as f:
                raw = await f.read()
            data = json.loads(raw)
        except Exception as e:
            logging.error(f"Error loading {DATA_FILE}: {e}")
            return

        # Load entry-by-entry: a single corrupt/incompatible entry doesn't
        # wipe out the whole database (otherwise every already-seen house
        # would look "new" on the next run, causing a flood of duplicate emails).
        loaded: Dict[str, Listing] = {}
        skipped = 0
        for lid, v in data.items():
            try:
                loaded[lid] = Listing(**v)
            except Exception as e:
                skipped += 1
                logging.error(f"Skipping corrupt entry '{lid}' in {DATA_FILE}: {e}")
        self.seen = loaded
        log_notice(
            f"Loaded {_fmt_count(len(self.seen))} listings from {DATA_FILE}"
            + (f" ({skipped} corrupt entries skipped)." if skipped else ".")
        )

    async def _save_db(self):
        try:
            to_save = {}
            for lid, listing in self.seen.items():
                d = asdict(listing)
                d.pop("price_changed", None)
                d.pop("old_price", None)
                to_save[lid] = d

            # Atomic write: write to a temp file first, then rename it to the
            # final name via os.replace(). This way, if the write is
            # interrupted (power loss, kill), the existing DATA_FILE is left
            # intact rather than truncated/corrupted.
            tmp_file = f"{DATA_FILE}.tmp"
            async with aiofiles.open(tmp_file, "w", encoding="utf-8") as f:
                await f.write(json.dumps(to_save, ensure_ascii=False, indent=2))
            os.replace(tmp_file, DATA_FILE)
        except Exception as e:
            logging.error(f"Error saving database: {e}")
            self.today_errors["db_save"] = (
                f"nem sikerült menteni a {DATA_FILE} fájlt ({health.format_error(e)})"
            )

    def _seconds_until_1600(self) -> float:
        now = datetime.now()
        target = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _titles_similar(self, t1: str, t2: str) -> bool:
        def _norm(s: str) -> str:
            import html as _html
            import re as _re

            s = _html.unescape(s)  # &amp; -> &, &quot; -> " etc.
            s = s.lower().strip()
            s = s.replace("–", "-").replace("—", "-")
            for _apos in ["’", "‘", "´", "`", "ʹ", "ʼ", "ʹ", "`"]:
                s = s.replace(_apos, "'")
            # Drop double-quote variants entirely: the same listing can be
            # re-syndicated with or without quotes around a word (e.g.
            # 'Tiny House "Igluhut"' from one portal vs 'Tiny House Igluhut'
            # from another), which otherwise breaks the substring match below
            # and lets a cross-platform duplicate through.
            for _quote in ['"', "“", "”", "„", "‟", "«", "»", "″"]:
                s = s.replace(_quote, "")
            # Emphasis punctuation varies between re-syndications of the same
            # listing ("Kein Hauptwohnsitz" vs "Kein Hauptwohnsitz!"); drop it
            # so those still compare equal under the exact-match rule below.
            s = s.replace("!", "").replace("?", "")
            s = _re.sub(r"\s+", " ", s)
            return s.strip(" .,;:")

        import re as _re

        a, b = _norm(t1), _norm(t2)
        if a == b:
            return True
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        # A short, generic title ("Mobilheim", "Haus") is a substring of
        # countless unrelated titles — only allow a substring match when the
        # shorter title is specific enough, and only on whole-word boundaries
        # (so "TG-Platz Nr. 4" doesn't match "TG-Platz Nr. 42").
        if len(short) < TITLE_SUBSTRING_MIN_LEN:
            return False
        return _re.search(r"(?<!\w)" + _re.escape(short) + r"(?!\w)", long_) is not None

    def _object_key(self, listing: Listing) -> Optional[str]:
        """A stable cross-platform object identifier read from the URL: the
        24-hex-char ImmoScout24-family expose ObjectId. immodirekt.at,
        immobilienscout24.at and immobilien.net (and Immokralle when it
        re-lists an IS24 expose) all carry the SAME id in the link, even
        though each scraper stores its own prefixed listing id (imd_/is24_/
        ik_/…) — Immokralle, for instance, keys on alleskralle's own data-id,
        not the expose hex. Matching on this catches the same object even when
        the syndicated titles diverge (translation/quoting/truncation)."""
        import re as _re

        m = _re.search(r"(?<![0-9a-z])[0-9a-f]{24}(?![0-9a-z])", (listing.url or "").lower())
        return m.group(0) if m else None

    def _origin_lookup(
        self, run_listings: List[Listing], own_sources: set
    ) -> Callable[[str], bool]:
        """is_known(key) for an aggregator's screen_listings(): whether an
        original ad's key — a DB id (wh_123) or an ImmoScout24-family expose
        id — belongs to an ad already in the seen-DB or in this run. The
        aggregator's own entries don't count (they carry the original's URL)."""
        others = [
            listing
            for listing in list(self.seen.values()) + run_listings
            if listing.source not in own_sources
        ]
        ids = {listing.id for listing in others}
        keys = {key for key in (self._object_key(listing) for listing in others) if key}
        return lambda key: key in ids or key in keys

    def _already_seen_elsewhere(
        self, listing: Listing, also_check: Optional[List[Listing]] = None
    ) -> bool:
        cand_key = self._object_key(listing)
        cutoff = datetime.now() - timedelta(days=DUPLICATE_LOOKBACK_DAYS)
        # Check the persistent DB
        for existing in self.seen.values():
            if existing.id == listing.id:
                continue
            # Skip stale entries: a listing nobody has seen for weeks is not a
            # live copy of this one. An unparseable timestamp is kept (the
            # conservative choice — at worst we hide a duplicate as before).
            try:
                if datetime.fromisoformat(existing.last_seen) < cutoff:
                    continue
            except (TypeError, ValueError):
                pass
            # Robust layer: same underlying object id in the URL (price/title
            # need not match — a shared 24-hex expose id is definitive).
            if cand_key and self._object_key(existing) == cand_key:
                return True
            if existing.price == listing.price and self._titles_similar(
                existing.title, listing.title
            ):
                return True
        # Also check listings already queued for notification in THIS run
        if also_check:
            for other in also_check:
                if other.id == listing.id:
                    continue
                if cand_key and self._object_key(other) == cand_key:
                    return True
                if other.price == listing.price and self._titles_similar(
                    other.title, listing.title
                ):
                    return True
        return False

    def _build_email_body(self, listings: List[Listing]) -> str:
        """Build the email body as a single flat, price-ordered list (new
        listings first, then price changes, each ascending by price), with
        the source name shown inline per listing rather than as a per-source
        grouping header. The body text itself is in Hungarian by design —
        it's the actual content of the notification email, not
        developer-facing output."""
        new_listings = sorted(
            (item for item in listings if not item.price_changed),
            key=lambda item: item.price,
        )
        changed_listings = sorted(
            (item for item in listings if item.price_changed),
            key=lambda item: item.price,
        )
        ordered = new_listings + changed_listings

        parts = ["-" * 148, "\n\n"]
        for i, listing in enumerate(ordered, 1):
            display_name = SOURCE_DISPLAY_NAMES.get(listing.source, listing.source)
            price_int = int(listing.price) if listing.price == int(listing.price) else listing.price
            price_fmt = _fmt_count(price_int) if isinstance(price_int, int) else price_int
            if listing.price_changed:
                old_int = (
                    int(listing.old_price)
                    if listing.old_price == int(listing.old_price)
                    else listing.old_price
                )
                old_fmt = _fmt_count(old_int) if isinstance(old_int, int) else old_int
                parts.append(f"{i}. {listing.title} ({display_name})\n")
                parts.append(f"   Régi ár: {old_fmt} € -> Új ár: {price_fmt} €\n")
            else:
                parts.append(f"{i}. {listing.title} ({display_name})\n")
                parts.append(f"   Ár: {price_fmt} €\n")
            parts.append(f"   Link: {listing.url}\n\n")
            parts.append("-" * 148 + "\n\n")
        return "".join(parts)

    async def _scrape_and_notify(
        self, scrapers: List[Any], alerts: Optional[List[Dict[str, Any]]] = None
    ) -> List[Any]:
        """One full fetch -> filter -> notify -> persist cycle over the given
        scrapers. `alerts` (the day's due error alerts, see health.py) go into
        the same email, after the listings — an email goes out for them even
        with no new listing. Returns the scrapers whose fetch failed or was
        cut short (unhandled exception from the gather, or the scraper set its
        `incomplete` flag after an in-loop error), so the caller can re-run
        just those later the same day."""
        db_changed = False
        to_notify: List[Listing] = []
        failed: List[Any] = []

        # Scrapers run concurrently (independent domains, no shared
        # state between them besides the common aiohttp session) ->
        # total run time is bounded by the single slowest scraper,
        # instead of the sum of all of them running sequentially.
        scraper_results = await asyncio.gather(
            *(scraper.fetch_listings() for scraper in scrapers),
            return_exceptions=True,
        )

        all_listings: List[Listing] = []
        ok_results: List[Any] = []
        summary_rows = []
        for scraper, result in zip(scrapers, scraper_results):
            if isinstance(result, BaseException):
                logging.error(
                    f"{scraper.__class__.__name__}: unhandled error, "
                    f"this source is excluded from this run: {result}",
                    exc_info=result,
                )
                failed.append(scraper)
                self.day_counts[scraper.__class__.__name__] = 0
                continue
            self.day_counts[scraper.__class__.__name__] = len(result)
            if getattr(scraper, "incomplete", False):
                # The scraper returned partial results after an in-loop
                # error (missing pages possible): process what it did get
                # now, and queue the source for the same-day retry pass.
                failed.append(scraper)
            all_listings.extend(result)
            ok_results.append((scraper, result))
            label = SCRAPER_SUMMARY_LABELS.get(scraper.__class__.__name__)
            if label:
                name, unit = label
                summary_rows.append((name, unit, len(result)))

        # On the console, show the "listings" group first, then "ads",
        # alphabetically by name within each group — the completion
        # order of the concurrent gather() would otherwise be chaotic.
        # Column alignment: the name field width matches the length of
        # "Immobilien.net" (the reference — a longer name would break
        # alignment), counts are right-aligned (so the "listings"/"ads"
        # word also lines up in a column), with a space as the
        # thousands separator.
        name_width = len("Immobilien.net")
        rows_with_count_str = [
            (name, unit, _fmt_count(count)) for name, unit, count in summary_rows
        ]
        num_width = max((len(cs) for _, _, cs in rows_with_count_str), default=0)

        for unit in ("listings", "ads"):
            for name, _unit, count_str in sorted(
                (row for row in rows_with_count_str if row[1] == unit),
                key=lambda row: row[0].lower(),
            ):
                log_notice(f"{name:<{name_width}}: {count_str:>{num_width}} {unit}")

        log_notice(f"Total listings fetched: {len(all_listings)}")

        # Source-specific screening ahead of the duplicate check: an
        # aggregator (Propylo) resolves its new cards to the original ads,
        # keeps houses only and drops copies of ads a scraped portal has.
        for scraper, result in ok_results:
            if not hasattr(scraper, "screen_listings"):
                continue
            # Blacklisted titles are left to the loop below (stored silently
            # there) — no point resolving them.
            to_screen = [
                listing
                for listing in result
                if not any(
                    word.lower() in listing.title.lower() for word in SKIP_NO_PERSIST + BLACKLIST
                )
            ]
            own_sources = {listing.source for listing in result}
            # The very first pass (none of this source's cards in the DB yet)
            # carries the aggregator's whole backlog, stale ads included: only
            # then are the original ads opened and checked.
            first_pass = not any(listing.source in own_sources for listing in self.seen.values())
            normal, silent, deferred = await scraper.screen_listings(
                to_screen,
                lambda lid: self.seen[lid].url if lid in self.seen else None,
                self._origin_lookup(all_listings, own_sources),
                check_originals=first_pass,
            )
            dropped = {id(listing) for listing in silent + deferred}
            all_listings = [listing for listing in all_listings if id(listing) not in dropped]
            for listing in silent:
                existing = self.seen.get(listing.id)
                if existing:
                    listing.first_seen = existing.first_seen
                self.seen[listing.id] = listing
                db_changed = True
            if deferred and scraper not in failed:
                failed.append(scraper)
            for portal, count in getattr(scraper, "unmatched_origins", {}).items():
                self.day_unmatched[portal] = self.day_unmatched.get(portal, 0) + count

        for listing in all_listings:
            title_lower = listing.title.lower()

            # SKIP_NO_PERSIST: skip but do NOT write to the database —
            # if the "reserved" status changes, we'll notify on the next run.
            if any(word.lower() in title_lower for word in SKIP_NO_PERSIST):
                logging.info(f"Temporary skip (no-persist): {listing.title}")
                continue

            if any(word.lower() in title_lower for word in BLACKLIST):
                if listing.id not in self.seen:
                    logging.info(f"Blacklisted listing hidden: {listing.title}")
                    self.seen[listing.id] = listing
                    db_changed = True
                continue

            if listing.id in self.seen:
                existing = self.seen[listing.id]
                if existing.price != listing.price:
                    listing.price_changed = True
                    listing.old_price = existing.price
                    # The scraper stamps first_seen with "now"; keep the real
                    # one, since the duplicate branch below stores this object
                    # directly (the post-email update restores it only for
                    # listings that went out).
                    listing.first_seen = existing.first_seen
                    # Also filter cross-platform duplicates on price drops
                    if self._already_seen_elsewhere(listing, also_check=to_notify):
                        logging.info(
                            f"Price-drop duplicate hidden: {listing.title} ({listing.price}€)"
                        )
                        self.seen[listing.id] = listing
                        db_changed = True
                    else:
                        to_notify.append(listing)
            else:
                # Findheim's server-side price filter is unreliable — filter client-side
                if listing.source == "findheim.at" and listing.price > 0:
                    if not (EUR_PRICE_FROM <= listing.price <= EUR_PRICE_TO):
                        logging.info(
                            f"Price filter ({listing.source}): {listing.title} ({listing.price}€) excluded"
                        )
                        self.seen[listing.id] = listing
                        db_changed = True
                        continue

                if self._already_seen_elsewhere(listing, also_check=to_notify):
                    logging.info(
                        f"Cross-platform duplicate hidden: {listing.title} ({listing.price}€)"
                    )
                    self.seen[listing.id] = listing
                    db_changed = True
                else:
                    to_notify.append(listing)

        alerts = alerts or []
        success = True
        if to_notify or alerts:
            log_notice(
                f"Found {len(to_notify)} new/changed listings and {len(alerts)} error alerts. "
                "Sending email..."
            )
            subject = health.email_subject(len(to_notify), len(alerts))
            body = (self._build_email_body(to_notify) if to_notify else "") + (
                health.build_alert_section(alerts)
            )
            success = await self.notifier.send(subject, body)
            if not success:
                logging.error("Email failed! Will retry on the next run.")
                self.today_errors["email_send"] = (
                    "nem sikerült elküldeni az emailt (SMTP-hiba, részletek a naplóban)"
                )
        self.last_email_ok = success

        if to_notify:
            if success:
                log_notice("Email sent. Updating database.")
                now_ts = datetime.now().isoformat()
                for listing in all_listings:
                    listing.last_seen = now_ts
                    if listing.id in self.seen:
                        # Preserve original first_seen
                        listing.first_seen = self.seen[listing.id].first_seen
                    self.seen[listing.id] = listing
                db_changed = True
        else:
            log_notice("No new findings.")
            # No new listings but still update last_seen for all fetched ones
            now_ts = datetime.now().isoformat()
            for listing in all_listings:
                if listing.id in self.seen:
                    self.seen[listing.id].last_seen = now_ts
            db_changed = True

        if db_changed:
            await self._save_db()

        log_notice("Run complete.")
        return failed

    def _start_of_day(self) -> List[Dict[str, Any]]:
        """Record today's run and return the error alerts due in today's 16:00
        email: what the last end-of-day check left pending, plus a missed-run
        notice once the monitor hadn't run for HEALTH_MIN_CONSECUTIVE+ days."""
        self.day_unmatched = {}
        self.day_late = []
        try:
            today = date.today().isoformat()
            state = health.load_state(HEALTH_STATE_FILE)
            missed = health.missed_days(state.get("last_run_date"), today)
            if len(missed) >= HEALTH_MIN_CONSECUTIVE:
                logging.warning(f"The monitor didn't run on {len(missed)} day(s): {missed}")
                state["missed_runs"] = {
                    "label": "Kimaradt futás",
                    "days": None,
                    "reason": f"a monitor {len(missed)} napig nem futott "
                    f"({missed[0]} – {missed[-1]}), pl. a Pi újraindult vagy állt a szolgáltatás",
                }
            state["last_run_date"] = today
            health.save_state(HEALTH_STATE_FILE, state)
            alerts = list(state.get("pending_alerts") or [])
            if state.get("missed_runs"):
                alerts.append(state["missed_runs"])
            return alerts
        except Exception as e:
            logging.error(f"Start-of-day check failed: {e}", exc_info=True)
            self.today_errors["health_check"] = (
                f"a napi hibaellenőrzés hibával leállt ({health.format_error(e)})"
            )
            return []

    def _clear_reported_alerts(self) -> None:
        """The alerts made it into an email: don't carry them over."""
        try:
            state = health.load_state(HEALTH_STATE_FILE)
            state["pending_alerts"] = []
            state.pop("missed_runs", None)
            health.save_state(HEALTH_STATE_FILE, state)
        except Exception as e:
            logging.error(f"Could not clear the reported alerts: {e}", exc_info=True)

    async def _end_of_day_check(
        self, scrapers: List[Any], still_failed: List[Any], check_sources: bool = True
    ) -> None:
        """Once a day, after the same-day retries: diagnose the sources (see
        health.py) and count them, together with today's own errors, into
        multi-day streaks; the streaks of HEALTH_MIN_CONSECUTIVE+ days become
        the alerts of the next 16:00 email. Never raises — a failing check
        must not take the monitoring loop down with it."""
        try:
            today = date.today().isoformat()
            state = health.load_state(HEALTH_STATE_FILE)
            if state.get("last_check_date") == today:
                # E.g. a second run the same day: counting it again would
                # turn one bad day into "two consecutive" ones.
                logging.info("End-of-day check already ran today, skipping.")
                return

            findings: Dict[str, str] = {}
            if check_sources:
                self._source_findings_without_probe(scrapers, state, findings)
                reasons = await asyncio.gather(
                    *(
                        health.diagnose(
                            scraper,
                            self.day_counts.get(scraper.__class__.__name__, 0),
                            scraper in still_failed,
                        )
                        for scraper in scrapers
                    )
                )
                for scraper, reason in zip(scrapers, reasons):
                    if reason:
                        name = self._display_name(scraper)
                        findings[f"source:{name}"] = reason
                        # A source that broke outright isn't "dropping" too.
                        findings.pop(f"drop:{name}", None)
            for kind, detail in self.today_errors.items():
                findings[f"internal:{kind}"] = detail

            streaks = health.update_streaks(state.get("streaks") or {}, findings, today)
            state["last_check_date"] = today
            state["streaks"] = streaks
            state["pending_alerts"] = health.due_alerts(streaks)
            health.save_state(HEALTH_STATE_FILE, state)
            self.today_errors = {}

            for key, streak in sorted(streaks.items()):
                logging.warning(
                    f"End-of-day check: {key} (day {streak['days']}): {streak['reason']}"
                )
            if not streaks:
                logging.info("End-of-day check: no problems.")
        except Exception as e:
            logging.error(f"End-of-day check failed: {e}", exc_info=True)
            self.today_errors = {
                "health_check": f"a napi hibaellenőrzés hibával leállt ({health.format_error(e)})"
            }

    @staticmethod
    def _display_name(scraper: Any) -> str:
        label = SCRAPER_SUMMARY_LABELS.get(scraper.__class__.__name__)
        return label[0] if label else scraper.__class__.__name__

    def _source_findings_without_probe(
        self, scrapers: List[Any], state: Dict[str, Any], findings: Dict[str, str]
    ) -> None:
        """The day's partial-loss findings that need no request: a sharp drop
        against the source's own daily counts, too few items against the
        site's own total, the Propylo cross-check, and late sources. Also adds
        today's counts to the per-source history kept in `state`."""
        history = state.get("count_history") or {}
        for scraper in scrapers:
            name = self._display_name(scraper)
            count = self.day_counts.get(scraper.__class__.__name__, 0)
            reason = health.drop_reason(history.get(name, []), count)
            if reason:
                findings[f"drop:{name}"] = reason
            history[name] = health.push_history(history.get(name, []), count)
            coverage = getattr(scraper, "coverage", None)
            if coverage:
                reason = health.coverage_reason(*coverage)
                if reason:
                    findings[f"coverage:{name}"] = reason
        state["count_history"] = history
        for portal, count in self.day_unmatched.items():
            if count >= HEALTH_UNMATCHED_MIN:
                findings[f"missing:{portal}"] = (
                    f"a Propylón ma {count} olyan ársávba eső ház jelent meg a(z) {portal} "
                    f"oldaláról, amit a saját {portal}-scraperünk nem hozott — lehet, hogy "
                    "kimaradnak hirdetések"
                )
        for scraper in self.day_late:
            findings[f"late:{self._display_name(scraper)}"] = (
                "16:00-kor hibával állt le, csak az újrapróbálásból jött meg "
                "(késve, külön emailben)"
            )

    @staticmethod
    def _memory_snapshot() -> str:
        """The process's resident and swapped-out memory, from /proc (Linux).
        Logged at the start of each daily run: the first requests of the 16:00
        run time out together while the process — idle for 24 hours, partly
        swapped out — pages back in, so this tells whether swap is the cause."""
        try:
            with open("/proc/self/status", encoding="ascii") as f:
                fields = dict(line.split(":", 1) for line in f if ":" in line)
            return f"VmRSS={fields['VmRSS'].strip()}, VmSwap={fields['VmSwap'].strip()}"
        except Exception as e:
            return f"unavailable ({type(e).__name__})"

    async def _daily_run(self, scrapers: List[Any]) -> List[Any]:
        """The 16:00 run with its same-day retries; returns the sources still
        failing at the end."""
        logging.info(f"Process memory at run start: {self._memory_snapshot()}")
        alerts = self._start_of_day()
        log_notice("Searching...")
        failed = await self._scrape_and_notify(scrapers, alerts=alerts)
        if alerts and self.last_email_ok:
            self._clear_reported_alerts()
        failed_at_1600 = list(failed)

        # Same-day retry: re-run ONLY the failed sources after a wait. The
        # main email above already went out from the healthy sources,
        # undelayed; anything new the retried sources find goes out in a
        # separate follow-up email. The seen-DB and the cross-platform
        # duplicate filter guarantee nothing already notified gets emailed
        # twice.
        for delay in INCOMPLETE_RETRY_DELAYS:
            if not failed:
                break
            names = ", ".join(s.__class__.__name__ for s in failed)
            log_notice(
                f"{len(failed)} incomplete source(s) ({names}), retrying in {delay // 60} min..."
            )
            await asyncio.sleep(delay)
            log_notice("Retrying incomplete sources...")
            failed = await self._scrape_and_notify(failed)
        if failed:
            names = ", ".join(s.__class__.__name__ for s in failed)
            log_notice(
                f"Still incomplete after retries: {names} — giving up until the next scheduled run."
            )
        self.day_late = [scraper for scraper in failed_at_1600 if scraper not in failed]
        return failed

    async def run(self):
        log_notice("Monitor has started.")

        self.session = aiohttp.ClientSession(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            # Default timeout for every scraper call (some used to rely on
            # aiohttp's 300s default, which could block the whole run for a
            # long time on a slow-responding site). Some scrapers pass an
            # explicit ClientTimeout per-call, which overrides this.
            timeout=aiohttp.ClientTimeout(total=30),
        )

        scrapers = [
            ImmoScout24Scraper(self.session),
            DibeoScraper(self.session),
            FindMyHomeScraper(self.session),
            WillhabenScraper(self.session),
            FindheimScraper(self.session),
            WohnnetScraper(self.session),
            DerStandardScraper(self.session),
            ImmodirektScraper(self.session),
            RaiffeisenScraper(self.session),
            OhneMaklerScraper(self.session),
            ImmobIlienNetScraper(self.session),
            ImmokralleScraper(self.session),
            ImmiScraper(self.session),
            BazarScraper(self.session),
            ImmobilienDeScraper(self.session),
            GoldgrubeScraper(self.session),
            ImmoLive24Scraper(self.session),
            DingDongScraper(self.session),
            SonnbergerScraper(self.session),
            HegerRealScraper(self.session),
            PropyloScraper(self.session),
            LystioScraper(self.session),
        ]

        await self._load_db()

        try:
            while True:
                wait = self._seconds_until_1600()
                logging.info(f"Waiting {wait / 3600:.2f} hours until next run...")
                await asyncio.sleep(wait)

                failed: List[Any] = []
                try:
                    failed = await self._daily_run(scrapers)
                    await self._end_of_day_check(scrapers, failed)
                except Exception as e:
                    # A crash used to end the process (systemd restarted it,
                    # and the day was lost silently); now it's logged, counted
                    # as an error of the day, and the loop waits for tomorrow.
                    logging.error(f"Daily run crashed: {e}", exc_info=True)
                    self.today_errors["run_crash"] = (
                        f"a napi futás hibával leállt ({health.format_error(e)})"
                    )
                    await self._end_of_day_check(scrapers, failed, check_sources=False)

        except asyncio.CancelledError:
            log_notice("Monitor cancelled.")
        finally:
            await self._save_db()
            if self.session:
                await self.session.close()
