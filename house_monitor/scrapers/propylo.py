import asyncio
import html as _html
import json
import logging
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup

from ..config import (
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    PROPYLO_PROBE_URL,
    PROPYLO_QUERY,
    PROPYLO_RESOLVE_ATTEMPTS,
    PROPYLO_RESOLVE_BACKOFF,
    PROPYLO_RESOLVE_DELAY,
    PROPYLO_URLS,
)
from ..fetch import fetch_page
from ..models import Listing, parse_de_price

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NEXT_DATA = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
# How an original ad page says it's gone or what it is.
_GONE_STATUSES = {404, 410}
_EXPIRED_REDIRECT_MARKERS = ("fromExpiredAdId", "entityRemoved")
_TYPE_MARKER = re.compile(
    r'"(?:@type|realEstateType|objectType|category|propertyType|estateType)"\s*:\s*"([A-Za-z]+)"'
)
_APARTMENT_TYPES = {"apartment", "wohnung", "flat", "condominium"}
_HOUSE_TYPES = {"house", "haus", "singlefamilyresidence"}
# How the original portals' ad URLs map onto our own DB ids.
_WILLHABEN_ID = re.compile(r"willhaben\.at/[^?#]*-(\d{6,})/?(?:[?#]|$)")
_WILLHABEN_AD_ID = re.compile(r"willhaben\.at/.*[?&]adId=(\d+)")
_WILLHABEN_CATEGORY = re.compile(r"willhaben\.at/iad/immobilien/d/([a-z-]+)/")
_DIBEO_ID = re.compile(r"dibeo\.at/expose/(\d+)")
_WOHNNET_ID = re.compile(r"wohnnet\.at/immobilien/[^?#]*-(\d{6,})(?:[/?#]|$)")
# The ImmoScout24 family (immobilienscout24.at / immodirekt / immobilien.net)
# shares a 24-hex expose id — the monitor's _object_key matches on it.
_OBJECT_KEY = re.compile(r"(?<![0-9a-z])[0-9a-f]{24}(?![0-9a-z])")


def origin_key(url: str) -> Optional[str]:
    """The id under which a scraped portal stores this original ad: a DB id
    (wh_/dibeo_/wn_) or an ImmoScout24-family expose id. None for portals we
    don't scrape (immowelt, urbanhome, …) — those ads are new to us."""
    for pattern, prefix in (
        (_WILLHABEN_ID, "wh_"),
        (_WILLHABEN_AD_ID, "wh_"),
        (_DIBEO_ID, "dibeo_"),
        (_WOHNNET_ID, "wn_"),
    ):
        m = pattern.search(url or "")
        if m:
            return prefix + m.group(1)
    m = _OBJECT_KEY.search((url or "").lower())
    return m.group(0) if m else None


# Propylo's cards carry no property type, so outside willhaben (whose ad
# URL names its category) the type is read from the original URL and the
# title: a house word in the title wins, otherwise any of these marks a
# non-house (apartments, parking). Offices and shops stay: the owner wants
# them (the other sources crawl Büro/Geschäftslokal categories too).
_HOUSE_WORDS = ("haus", "häus", "mobilheim", "bungalow", "hütte", "chalet")
_NOT_A_HOUSE_WORDS = (
    "wohnung",
    "garçonni",
    "garconni",
    "apartment",
    "appartement",
    "penthouse",
    "maisonette",
    "parkplatz",
    "tiefgarage",
)


def is_house(origin_url: str, title: str) -> bool:
    """Houses only (the owner's choice for this aggregator): Propylo lists
    every property type, apartments and parking spaces included. A willhaben
    target URL names its category exactly; for the other portals see
    _HOUSE_WORDS / _NOT_A_HOUSE_WORDS (an odd apartment can still slip
    through when neither its URL nor its title says what it is)."""
    m = _WILLHABEN_CATEGORY.search(origin_url or "")
    if m:
        return m.group(1).startswith(("haus", "ferien"))
    lowered_title = (title or "").lower()
    if any(word in lowered_title for word in _HOUSE_WORDS):
        return True
    text = f"{origin_url} {lowered_title}".lower()
    return not any(word in text for word in _NOT_A_HOUSE_WORDS)


class PropyloScraper:
    """
    at.propylo.com – a real-estate aggregator (it pulls listings from several
    popular Austrian portals), sales only; rentals live on the sister site
    at.flatspotter.com. Because it's an aggregator there's heavy overlap with the
    other scrapers, but the cross-platform duplicate detection
    (_already_seen_elsewhere) absorbs it.
    There's no country-wide listing page — listings are grouped per Bundesland,
    so we crawl all 9 state pages (config: PROPYLO_URLS), which together cover
    every listing in Austria. The site honors a server-side price filter +
    ascending-price sort (PROPYLO_QUERY), so the price range is pushed down to
    the server and results come sorted cheapest-first.
    Card: the <a href=".../verkaufsimmobilie/<id>"> element itself (the whole
      card is one link); title: its inner <h2>; price: div.price ("15.000 €",
      German format); URL: the (already absolute) href.
    ID: the numeric /verkaufsimmobilie/<id> (pro_<id>).
    That card URL redirects to the original ad on another portal:
      screen_listings() resolves each NEW card (paced — bursts get HTTP 429),
      keeps houses only, drops copies of ads a scraped portal already has
      (by the original ad's id); on the very first pass it also checks the
      remaining originals and drops gone (expired) ones and those the
      original page types as an apartment; the rest are emailed with the
      original URL.
    Pagination: page 1 = the region URL, page N = region URL + "/N", with the
      query string appended AFTER the /N segment; past the last page the server
      returns HTTP 200 with 0 cards (no 404).
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error, i.e. the
        # returned list may be missing pages — the monitor's same-day retry
        # loop re-runs the sources that set this flag.
        self.incomplete = False
        # The last screen_listings(): per scraped portal, how many in-range
        # HOUSES Propylo resolved to an original ad our own scraper of that
        # portal never returned (only willhaben: its URL names the category).
        # The daily error check reports a steady count (health.py).
        self.unmatched_origins: Dict[str, int] = {}

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        results = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/verkaufsimmobilie/(\d+)", href)
            if not m:
                continue
            listing_id = f"pro_{m.group(1)}"

            h2 = a.find("h2")
            title = _html.unescape(h2.get_text(strip=True)) if h2 else ""
            if not title:
                # a["title"] carries the same text and is a safe fallback
                title = _html.unescape(a.get("title", "")).strip()

            price = 0.0
            price_div = a.find("div", class_="price")
            if price_div:
                price = parse_de_price(price_div.get_text(strip=True))

            results.append((listing_id, title, href, price))
        return results

    async def _fetch_one_url(self, region_url: str) -> List[Listing]:
        url_results = []
        seen_ids: set = set()
        page = 1

        while True:
            path = region_url if page == 1 else f"{region_url}/{page}"
            url = f"{path}{PROPYLO_QUERY}"
            logging.info(f"Propylo.com: fetching {url}")
            try:
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"Propylo.com: HTTP {resp.status} – {url}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"Propylo.com: fetch error {type(e).__name__}: {e} – {url}")
                self.incomplete = True
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"Propylo.com: {len(page_cards)} cards – {url}")

            # Past the last page the server returns HTTP 200 with an empty list.
            if not page_cards:
                break

            new_on_page = 0
            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                new_on_page += 1

                if price == 0.0:
                    logging.info(f"Propylo.com: skipped (price=0): {title}")
                    continue
                if not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                    continue

                now = datetime.now().isoformat()
                url_results.append(
                    Listing(
                        id=listing_id,
                        title=title,
                        price=price,
                        url=listing_url,
                        source="at.propylo.com",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            # A page whose cards were all duplicates of an earlier page means the
            # pagination has run past the end and is repeating — stop.
            if new_on_page == 0:
                break

            page += 1
            await asyncio.sleep(2)

        return url_results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        # The 9 Bundesland pages are independent of each other, so we paginate
        # them concurrently and merge with a single shared seen_ids set (a
        # listing could in principle appear under two regions).
        per_url_results = await asyncio.gather(*(self._fetch_one_url(u) for u in PROPYLO_URLS))

        results = []
        seen_ids: set = set()
        for url_results in per_url_results:
            for listing in url_results:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                results.append(listing)

        logging.info(f"Propylo: {len(results)} listings")
        return results

    async def probe(self) -> list:
        """Page 1 of the search without the price range, parsed like a normal
        page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_cards(await fetch_page(self.session, PROPYLO_PROBE_URL))

    async def _resolve(self, url: str) -> Tuple[bool, Optional[str]]:
        """Follow one card's redirect. Returns (resolved, original ad URL);
        the URL is None when Propylo serves the ad itself. resolved=False
        means "can't tell this run" (HTTP 429 after the retries, another
        status, a network error) — the card is retried on the next run."""
        for attempt in range(1, PROPYLO_RESOLVE_ATTEMPTS + 1):
            try:
                async with self.session.get(url, allow_redirects=False) as resp:
                    status, location = resp.status, resp.headers.get("Location", "")
            except Exception as e:
                logging.warning(f"Propylo.com: resolving {url} failed: {type(e).__name__}: {e}")
                return False, None
            if status in _REDIRECT_STATUSES and location:
                return True, location
            if status == 200:
                return True, None
            if status == 429 and attempt < PROPYLO_RESOLVE_ATTEMPTS:
                await asyncio.sleep(PROPYLO_RESOLVE_BACKOFF * attempt)
                continue
            logging.warning(f"Propylo.com: HTTP {status} resolving {url}")
            return False, None
        return False, None

    async def _check_original(self, url: str) -> Optional[str]:
        """Look at the original ad itself (no redirect follow). Propylo keeps
        listing ads for a while after they're gone: on the first full pass 14
        of 15 unknown willhaben houses had expired (and one was reserved), and
        ImmoScout24 / immowelt / nachrichten.at originals answered 410.
        Returns "gone" (404/410, an expiry redirect, or a willhaben ad that
        isn't active), "apartment" (the page's structured data types it as
        one — titles often don't say), "live", or None if it can't be told."""
        try:
            async with self.session.get(url, allow_redirects=False) as resp:
                status, location = resp.status, resp.headers.get("Location", "")
                body = await resp.text() if status == 200 else ""
        except Exception as e:
            logging.warning(f"Propylo.com: checking {url} failed: {type(e).__name__}: {e}")
            return None
        if status in _GONE_STATUSES:
            return "gone"
        if status in _REDIRECT_STATUSES:
            # willhaben answers an expired ad with a redirect to a search page.
            expired = "willhaben.at" in url or any(m in location for m in _EXPIRED_REDIRECT_MARKERS)
            return "gone" if expired else None
        if status != 200:
            return None
        if "willhaben.at" in url:
            m = _NEXT_DATA.search(body)
            try:
                details = json.loads(m.group(1))["props"]["pageProps"]["advertDetails"] if m else {}
            except (ValueError, KeyError, TypeError):
                return None
            ad_status = (details.get("advertStatus") or {}).get("id")
            if ad_status is None:
                return None
            return "live" if ad_status == "active" else "gone"
        kinds = {kind.lower() for kind in _TYPE_MARKER.findall(body)}
        if kinds & _APARTMENT_TYPES and not kinds & _HOUSE_TYPES:
            return "apartment"
        return "live"

    async def screen_listings(
        self,
        listings: List[Listing],
        stored_url: Callable[[str], Optional[str]],
        is_known: Callable[[str], bool],
        check_originals: bool = False,
    ) -> Tuple[List[Listing], List[Listing], List[Listing]]:
        """Sort this run's cards before the monitor's duplicate check.
        stored_url(listing_id) is the URL kept in the seen-DB (None if the card
        is new); is_known(key) says whether an original ad's origin_key() is
        already in the DB or in this run; check_originals makes it fetch each
        remaining candidate's original (_check_original) — the monitor sets it
        only on the very first pass, when Propylo's whole backlog of stale ads
        and untitled flats arrives at once; later runs only see the day's
        fresh cards and don't open the originals (the owner's choice).
        Returns (normal, silent, deferred):
          normal   – the usual new / price-change handling, with the original
                     ad's URL;
          silent   – stored without an email: not a house, or a copy of an ad
                     a scraped portal has (that portal reports it and its price
                     changes);
          deferred – unresolved this run; left out entirely, retried next run.
        """
        normal: List[Listing] = []
        silent: List[Listing] = []
        deferred: List[Listing] = []
        resolved_any = False
        self.unmatched_origins = {}
        for listing in listings:
            known_url = stored_url(listing.id)
            if known_url is not None:
                # Seen before: keep the original-ad URL resolved back then.
                if "propylo.com" not in known_url:
                    listing.url = known_url
                key = origin_key(listing.url)
                (silent if key and is_known(key) else normal).append(listing)
                continue

            if resolved_any:
                await asyncio.sleep(PROPYLO_RESOLVE_DELAY)
            resolved_any = True
            resolved, original = await self._resolve(listing.url)
            if not resolved:
                deferred.append(listing)
                continue
            if original:
                listing.url = original
            if not is_house(original or "", listing.title):
                logging.info(f"Propylo.com: not a house, stored silently: {listing.title}")
                silent.append(listing)
                continue
            key = origin_key(original or "")
            if key and is_known(key):
                logging.info(f"Propylo.com: copy of {key}, stored silently: {listing.title}")
                silent.append(listing)
                continue
            verdict = await self._check_original(original) if original and check_originals else None
            if verdict in ("gone", "apartment"):
                logging.info(
                    f"Propylo.com: original is {verdict}, stored silently: {listing.title}"
                )
                silent.append(listing)
                continue
            if key and key.startswith("wh_"):
                # A willhaben house our Willhaben scraper didn't bring.
                self.unmatched_origins["Willhaben"] = self.unmatched_origins.get("Willhaben", 0) + 1
            # verdict None (not checked, or couldn't be): rather email it than lose it.
            normal.append(listing)
        if deferred:
            logging.warning(f"Propylo.com: {len(deferred)} new card(s) unresolved, retried later")
        return normal, silent, deferred
