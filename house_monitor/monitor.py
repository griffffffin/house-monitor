"""The orchestrator: runs all scrapers, dedupes/filters results, sends the
notification email, and persists the seen-listings database."""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiofiles
import aiohttp

from .config import (
    BLACKLIST,
    DATA_FILE,
    EMAIL_CONFIG,
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    LOG_FILE,
    SKIP_NO_PERSIST,
)
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
    ImmobIlienNetScraper,
    ImmobilienDeScraper,
    ImmodirektScraper,
    ImmiScraper,
    ImmokralleScraper,
    ImmoLive24Scraper,
    ImmoScout24Scraper,
    OhneMaklerScraper,
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
}


class HouseMonitor:
    def __init__(self):
        self._setup_logging()
        self.notifier = EmailNotifier(EMAIL_CONFIG)
        self.seen: Dict[str, Listing] = {}
        self.session: Optional[aiohttp.ClientSession] = None

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
            s = _re.sub(r"\s+", " ", s)
            return s

        a, b = _norm(t1), _norm(t2)
        return (a in b) if len(a) <= len(b) else (b in a)

    def _already_seen_elsewhere(
        self, listing: Listing, also_check: Optional[List[Listing]] = None
    ) -> bool:
        # Check the persistent DB
        for existing in self.seen.values():
            if existing.price == listing.price and self._titles_similar(
                existing.title, listing.title
            ):
                return True
        # Also check listings already queued for notification in THIS run
        if also_check:
            for other in also_check:
                if (
                    other.id != listing.id
                    and other.price == listing.price
                    and self._titles_similar(other.title, listing.title)
                ):
                    return True
        return False

    def _build_email_body(self, listings: List[Listing]) -> str:
        """Build the email body, grouped by source (modeled on the sibling
        product-monitor.py project's design: a decorated header per source
        plus a numbered list, rather than one flat, source-agnostic list).
        The body text itself is in Hungarian by design — it's the actual
        content of the notification email, not developer-facing output."""
        by_source: Dict[str, List[Listing]] = defaultdict(list)
        for listing in listings:
            display_name = SOURCE_DISPLAY_NAMES.get(listing.source, listing.source)
            by_source[display_name].append(listing)

        parts = ["-" * 148, "\n\n"]
        for source in sorted(by_source.keys(), key=str.lower):
            new_listings = sorted(
                (item for item in by_source[source] if not item.price_changed),
                key=lambda item: item.price,
            )
            changed_listings = sorted(
                (item for item in by_source[source] if item.price_changed),
                key=lambda item: item.price,
            )
            ordered = new_listings + changed_listings

            parts.extend(
                ["=" * 14, " " * 4, source, f" ({len(ordered)} db)", " " * 4, "=" * 14, "\n\n"]
            )

            for i, listing in enumerate(ordered, 1):
                price_int = (
                    int(listing.price) if listing.price == int(listing.price) else listing.price
                )
                price_fmt = _fmt_count(price_int) if isinstance(price_int, int) else price_int
                if listing.price_changed:
                    old_int = (
                        int(listing.old_price)
                        if listing.old_price == int(listing.old_price)
                        else listing.old_price
                    )
                    old_fmt = _fmt_count(old_int) if isinstance(old_int, int) else old_int
                    parts.append(f"{i}. {listing.title}\n")
                    parts.append(f"   Régi ár: {old_fmt} € -> Új ár: {price_fmt} €\n")
                else:
                    parts.append(f"{i}. {listing.title}\n")
                    parts.append(f"   Ár: {price_fmt} €\n")
                parts.append(f"   Link: {listing.url}\n\n")

            parts.append("-" * 148 + "\n\n")
        return "".join(parts)

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
        ]

        await self._load_db()

        try:
            while True:
                wait = self._seconds_until_1600()
                logging.info(f"Waiting {wait / 3600:.2f} hours until next run...")
                await asyncio.sleep(wait)

                log_notice("Searching...")
                db_changed = False
                to_notify: List[Listing] = []

                # Scrapers run concurrently (independent domains, no shared
                # state between them besides the common aiohttp session) ->
                # total run time is bounded by the single slowest scraper,
                # instead of the sum of all of them running sequentially.
                scraper_results = await asyncio.gather(
                    *(scraper.fetch_listings() for scraper in scrapers),
                    return_exceptions=True,
                )

                all_listings: List[Listing] = []
                summary_rows = []
                for scraper, result in zip(scrapers, scraper_results):
                    if isinstance(result, Exception):
                        logging.error(
                            f"{scraper.__class__.__name__}: unhandled error, "
                            f"this source is excluded from this run: {result}",
                            exc_info=result,
                        )
                        continue
                    all_listings.extend(result)
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

                if to_notify:
                    log_notice(f"Found {len(to_notify)} new/changed listings. Sending email...")
                    subject = f"Ingatlanok: {len(to_notify)} db"
                    body = self._build_email_body(to_notify)
                    success = await self.notifier.send(subject, body)

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
                        logging.error("Email failed! Will retry on the next run.")
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

        except asyncio.CancelledError:
            log_notice("Monitor cancelled.")
        finally:
            await self._save_db()
            if self.session:
                await self.session.close()
