import asyncio
import logging
from datetime import datetime
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

from ..config import (
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    SONNBERGER_PROBE_URL,
    SONNBERGER_URL,
)
from ..fetch import fetch_page, fetch_text
from ..models import Listing, parse_de_price


class SonnbergerScraper:
    """
    sonnberger.at/immobilien – the broker's own site (WordPress with its own
    estate plugin, server-rendered HTML). It moved there from sonnberger.co.at
    (a WordPress + Houzez theme) around 2026-10-28; the old pages redirect.
    The search filters server-side: houses for sale in the price range,
    cheapest first (SONNBERGER_URL).
    Card: article.sb-estate-card[data-sb-estate-id]
    Title/URL: h2 > a, its .sb-estate-card__title-name + __title-subtitle
      ("ANPACKEN" + "Sanierungshaus mit Garten …"), joined with " – " like the
      old site's titles.
    Address: p.sb-estate-card__location (postcode + town, no street)
    Price: p.sb-estate-card__price ("€ 117.000"; "Preis auf Anfrage" -> 0.0)
    ID: data-sb-estate-id (sb_<id>)
    Reserved: the old site showed "Reserviert" in place of the price, so those
      cards parsed to 0.0 and were left out — and never saved, so they still
      came through once they were for sale again. The new cards keep the
      price and carry .sb-estate-card__status--reserved instead; they are left
      out here for the same effect. ("Kaufanbot im Haus" — an offer is in —
      stays: the house is still for sale.)
    Pagination: "load more" — ?sb_page=N returns the first 12·N cards, and the
      next page's link, a[data-sb-load-more], is missing on the last one.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error, i.e. the
        # returned list may be missing pages — the monitor's same-day retry
        # loop re-runs the sources that set this flag.
        self.incomplete = False

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        results = []
        for card in soup.select("article[data-sb-estate-id]"):
            link = card.select_one("h2 a[href]")
            if not link:
                continue
            listing_url = link["href"]
            parts = [
                " ".join(el.get_text(" ", strip=True).split())
                for el in link.select(
                    ".sb-estate-card__title-name, .sb-estate-card__title-subtitle"
                )
            ]
            title = " – ".join(part for part in parts if part)
            if not title:
                title = " ".join(link.get_text(" ", strip=True).split())

            if card.select_one(".sb-estate-card__status--reserved"):
                logging.info(f"Sonnberger.at: skipped (reserved): {title}")
                continue

            listing_id = f"sb_{card['data-sb-estate-id']}"

            price_el = card.select_one(".sb-estate-card__price")
            price = parse_de_price(price_el.get_text(strip=True)) if price_el else 0.0

            # Address (for the SITE_BLACKLIST check): "4400 Steyr"
            address = card.select_one(".sb-estate-card__location")
            location = address.get_text(" ", strip=True) if address else ""

            results.append((listing_id, title, listing_url, price, location))
        return results

    @staticmethod
    def _next_page_url(html_text: str) -> Optional[str]:
        """The "load more" link's target, or None on the last page."""
        link = BeautifulSoup(html_text, "lxml").select_one("a[data-sb-load-more][href]")
        return str(link["href"]) if link else None

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        results = []
        seen_ids: set = set()
        url: Optional[str] = SONNBERGER_URL
        page = 1

        while url:
            logging.info(f"Sonnberger.at: fetching {url}")
            try:
                status, html_text = await fetch_text(self.session, url)
            except Exception as e:
                logging.error(f"Sonnberger.at: fetch error {type(e).__name__}: {e}")
                self.incomplete = True
                break

            if status != 200:
                logging.warning(f"Sonnberger.at: HTTP {status} – page {page}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"Sonnberger.at: {len(page_cards)} cards – page {page}")

            # Every page repeats the earlier pages' cards, so only the new ones count.
            new_on_page = 0
            for listing_id, title, listing_url, price, location in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                new_on_page += 1

                if price == 0.0:
                    logging.info(f"Sonnberger.at: skipped (price=0): {title}")
                    continue
                if not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                    continue

                now = datetime.now().isoformat()
                results.append(
                    Listing(
                        id=listing_id,
                        title=title,
                        price=price,
                        url=listing_url,
                        source="sonnberger.at",
                        first_seen=now,
                        last_seen=now,
                        location=location,
                    )
                )

            # A page without a new card means the pagination is going round
            # in circles — stop rather than spin.
            if new_on_page == 0:
                break

            url = self._next_page_url(html_text)
            page += 1
            if url:
                await asyncio.sleep(2)

        logging.info(f"Sonnberger: {len(results)} ads")
        return results

    async def probe(self) -> list:
        """Page 1 of the search without the price range, parsed like a normal
        page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_cards(await fetch_page(self.session, SONNBERGER_PROBE_URL))
