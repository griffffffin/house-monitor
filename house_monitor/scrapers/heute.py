import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, HEUTE_BASE_URL, HEUTE_PROBE_URL, HEUTE_URL
from ..fetch import fetch_page, fetch_text
from ..models import Listing, parse_de_price


class HeuteScraper:
    """
    heute.at/immobilien – the newspaper's real-estate search (HTML, paginated),
    fed with broker listings from the Justimmo CMS.
    Card: <a class="realty mn--realty-row" href="/immobilien/de/immobilie/<slug>/<id>">
    Title: h5.mn--realty-title
    Address: div.mn--realty-address (postcode + town, no street)
    Price: the .mn--realty-data-value whose sibling .mn--realty-data-label is
      "Kaufpreis" ("€ 16.500,00"). The label check is required because the
      same row holds the area ("21 m²", label "Wohnfläche") first — an unscoped
      parse would read that instead (the HegerReal/ImmoScout24 pitfall).
    ID: the last path segment of the href, a Justimmo object id (heute_<id>).
    Pagination: &p=N, 20 cards per page; past the last page the server answers
      HTTP 404. The search has only a maximum price, so "€ 1,00" placeholder
      ads and per-m² plot prices come along and are dropped by the client-side
      EUR_PRICE_FROM filter.
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
        for card in soup.select("a.realty.mn--realty-row"):
            href = card.get("href", "")
            if not href:
                continue
            listing_url = href if href.startswith("http") else f"{HEUTE_BASE_URL}{href}"
            raw_id = href.rstrip("/").split("/")[-1].split("?")[0]
            listing_id = f"heute_{raw_id}"

            title_el = card.find(class_="mn--realty-title")
            title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
            if not title:
                title = f"Heute.at #{raw_id}"

            price = 0.0
            for label in card.find_all(class_="mn--realty-data-label"):
                if label.get_text(strip=True).lower() == "kaufpreis":
                    value = label.find_previous_sibling(class_="mn--realty-data-value")
                    if value:
                        price = parse_de_price(value.get_text(strip=True))
                    break

            # Address (for the SITE_BLACKLIST check): "7063 Oggau am Neusiedler See"
            address = card.find(class_="mn--realty-address")
            location = address.get_text(" ", strip=True) if address else ""

            results.append((listing_id, title, listing_url, price, location))
        return results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        results = []
        seen_ids: set = set()
        page = 1

        while True:
            url = f"{HEUTE_URL}&p={page}"
            logging.info(f"Heute.at: fetching page {page} -> {url}")
            try:
                status, html = await fetch_text(self.session, url)
            except Exception as e:
                logging.error(f"Heute.at: error on page {page}: {type(e).__name__}: {e}")
                self.incomplete = True
                break

            if status != 200:
                if status == 404 and page > 1:
                    # This is how heute.at signals the end of pagination — not an error
                    logging.info(f"Heute.at: HTTP 404 (no more pages) - page {page}.")
                else:
                    logging.warning(f"Heute.at: HTTP {status} on page {page}.")
                break

            page_cards = self._parse_cards(html)
            logging.info(f"Heute.at: {len(page_cards)} cards on page {page}.")
            if not page_cards:
                break

            new_on_page = 0
            for listing_id, title, listing_url, price, location in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                new_on_page += 1

                if price == 0.0:
                    logging.info(f"Heute.at: skipped (price=0): {title}")
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
                        source="heute.at",
                        first_seen=now,
                        last_seen=now,
                        location=location,
                    )
                )

            # A page whose cards were all duplicates of earlier pages means the
            # pagination has looped back on itself — stop rather than spin.
            if new_on_page == 0:
                break

            page += 1
            await asyncio.sleep(2)

        logging.info(f"Heute.at: {len(results)} listings")
        return results

    async def probe(self) -> list:
        """Page 1 of the search without the price range, parsed like a normal
        page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_cards(await fetch_page(self.session, HEUTE_PROBE_URL))
