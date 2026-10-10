import asyncio
import logging
from datetime import datetime
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

from ..config import IMMOSCOUT_PROBE_URL, IMMOSCOUT_URL
from ..fetch import fetch_page
from ..models import Listing, parse_de_price


class ImmoScout24Scraper:
    """immobilienscout24.at house listings (server-rendered HTML)."""

    BASE_URL = "https://www.immobilienscout24.at"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error, i.e. the
        # returned list may be missing pages — the monitor's same-day retry
        # loop re-runs the sources that set this flag.
        self.incomplete = False

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "html.parser")
        items = soup.select('ol[data-testid="results-items"] > li')
        results = []
        for item in items:
            link_tag = item.find("a", href=True)
            if not link_tag:
                continue

            url = link_tag["href"]
            if not url.startswith("http"):
                url = self.BASE_URL + url

            listing_id = "is24_" + url.split("/")[-1].split("?")[0]

            title_tag = item.find("h2")
            title = title_tag.text.strip() if title_tag else "No title"

            price = 0.0
            # Current markup: one <li class="PriceKeyFact-price-key-fact-…"> per
            # fact (price, €/m²); before the ~2026-08 redesign the <li>s sat in
            # a <ul class="PriceKeyFacts…">. Both are accepted.
            price_elements = item.select('li[class*="PriceKeyFact"], ul[class*="PriceKeyFacts"] li')
            for el in price_elements:
                text = el.text.strip()
                if "€" in text and "/m²" not in text:
                    parsed = parse_de_price(text)
                    if parsed > 0:
                        price = parsed
                        break

            # Address (for the SITE_BLACKLIST check): "Street, 1220 Wien"
            address = item.find("address")
            location = address.get_text(" ", strip=True) if address else ""

            results.append((listing_id, title, url, price, location))
        return results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        results = []
        next_url: Optional[str] = IMMOSCOUT_URL
        page = 1

        while next_url:
            logging.info(f"ImmoScout24: fetching page {page} -> {next_url}")
            try:
                async with self.session.get(next_url) as response:
                    if response.status != 200:
                        logging.error(f"ImmoScout24: blocked with HTTP {response.status}")
                        break

                    html = await response.text()
                    page_cards = self._parse_cards(html)
                    if not page_cards:
                        logging.warning("ImmoScout24: no listing elements found on page.")
                        break

                    logging.info(f"ImmoScout24: {len(page_cards)} elements on page {page}.")

                    for listing_id, title, url, price, location in page_cards:
                        if price > 0:
                            now = datetime.now().isoformat()
                            results.append(
                                Listing(
                                    id=listing_id,
                                    title=title,
                                    price=price,
                                    url=url,
                                    source="ImmoScout24",
                                    first_seen=now,
                                    last_seen=now,
                                    location=location,
                                )
                            )

                    soup = BeautifulSoup(html, "html.parser")
                    next_link = soup.select_one('a[rel~="next"]')
                    if next_link and "href" in next_link.attrs:
                        next_url = self.BASE_URL + next_link["href"]
                        page += 1
                        await asyncio.sleep(2)
                    else:
                        logging.info("ImmoScout24: no more pages.")
                        next_url = None

            except Exception as e:
                logging.error(f"ImmoScout24: error on page {page}: {e}")
                self.incomplete = True
                break

        logging.info(f"ImmoScout24: {len(results)} listings")
        return results

    async def probe(self) -> list:
        """Page 1 of the search without the price range, parsed like a normal
        page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_cards(await fetch_page(self.session, IMMOSCOUT_PROBE_URL))
