import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, IMMODIREKT_BASE_URL, IMMODIREKT_URLS
from ..models import Listing, parse_de_price


class ImmodirektScraper:
    """
    Immodirekt.at (ImmoScout24 AT portal) house listings (HTML, SSR).
    Pagination: ?page=N appended to the base URL.
    Card:  <section class="_98L38">
    Title: <h2 class="_2jNcY">
    Price: <div class="_1-CSS"> containing <span class="_1xxDl">Kaufpreis...</span>
             -> <span class="_2Pe1d">48.500,00</span>
    URL/ID: <a href="/immobilie/PLZ-ORT/slug-HEXID/"> - the ID is the 24-char hex slug
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("section", class_="_98L38")
        results = []
        for card in cards:
            # Extract URL and ID
            a = card.find("a", href=re.compile(r"/immobilie/"))
            if not a:
                continue
            href = a.get("href", "")
            if not href:
                continue
            listing_url = f"{IMMODIREKT_BASE_URL}{href}" if href.startswith("/") else href

            # ID: 24-character hex string at the end of the slug
            m = re.search(r"-([a-f0-9]{24})/?$", href)
            raw_id = m.group(1) if m else href.rstrip("/").split("/")[-1]
            listing_id = f"imd_{raw_id}"

            # Title (unescape HTML entities, e.g. &amp; -> &)
            h2 = card.find("h2", class_="_2jNcY")
            title = _html.unescape(h2.get_text(strip=True)) if h2 else f"Immodirekt #{raw_id}"

            # Price: the last "Kaufpreis" row's _2Pe1d value.
            # Some cards have a duplicated "Kaufpreis" row: first "-", second the real price.
            price = 0.0
            for div in card.find_all("div", class_="_1-CSS"):
                label = div.find("span", class_="_1xxDl")
                if label and "Kaufpreis" in label.get_text():
                    val = div.find("span", class_="_2Pe1d")
                    if val:
                        p_str = val.get_text(strip=True)
                        if p_str == "-":
                            continue  # skip the placeholder row, keep looking
                        parsed = parse_de_price(p_str)
                        if parsed > 0:
                            price = parsed
                            break  # found the real price

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()

        for base_url in IMMODIREKT_URLS:
            page = 1
            logging.info(f"Immodirekt.at: starting -> {base_url}")
            while True:
                url = base_url if page == 1 else f"{base_url}&page={page}"
                logging.info(f"Immodirekt.at: fetching page {page} -> {url}")
                try:
                    async with self.session.get(url) as resp:
                        if resp.status != 200:
                            logging.warning(f"Immodirekt.at: HTTP {resp.status} on page {page}.")
                            break
                        html = await resp.text()

                    page_cards = self._parse_cards(html)
                    logging.info(f"Immodirekt.at: {len(page_cards)} cards on page {page}.")

                    if not page_cards:
                        logging.info("Immodirekt.at: no more cards, stopping.")
                        break

                    found_on_page = 0
                    for listing_id, title, listing_url, price in page_cards:
                        if listing_id in seen_ids:
                            continue
                        seen_ids.add(listing_id)

                        # Client-side price filter: skip unknown (0) prices and out-of-range ones
                        if price == 0.0:
                            logging.info(f"Immodirekt.at: skipped (price=0, unknown): {title}")
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
                                source="immodirekt.at",
                                first_seen=now,
                                last_seen=now,
                            )
                        )
                        found_on_page += 1

                    logging.info(f"Immodirekt.at: {found_on_page} processed on page {page}.")

                    if found_on_page == 0:
                        logging.info("Immodirekt.at: no new results, stopping.")
                        break

                    page += 1
                    await asyncio.sleep(2)

                except Exception as e:
                    logging.error(f"Immodirekt.at: error on page {page}: {e}", exc_info=True)
                    break

        logging.info(f"Immodirekt: {len(results)} listings")
        return results
