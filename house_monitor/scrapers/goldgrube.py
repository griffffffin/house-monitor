import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, GOLDGRUBE_BASE_URL, GOLDGRUBE_URLS
from ..fetch import fetch_bytes
from ..models import Listing, decode_utf8_or_latin1, parse_de_price


class GoldgrubeScraper:
    """
    goldgrube.at house listings (HTML, paginated).
    Card:  <article id="XXXXXX" class="twelvecol-xs-nm">
    Title: <h3 class="twelvecol-xs">
    URL:   <a class="detaillink detaillinklist" href="...">
    Price: <span class="price twelvecol-xs">€\xa0139.000,00</span>
           Dot = thousands separator, comma = decimal; empty -> skip
    Pagination: ?p=N (from the pagination div's links)
    Price filter: client-side only (the server offers no price filter)
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error (missing
        # pages possible) — picked up by the monitor's same-day retry loop.
        self.incomplete = False

    @staticmethod
    def _parse_price(span) -> float:
        # After latin-1 decoding, the € sign becomes the raw \x80 byte, so a
        # plain unicode € replace wouldn't work — instead we let the shared
        # parse_de_price parse whatever text get_text() returns.
        if not span:
            return 0.0
        return parse_de_price(span.get_text(strip=True))

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        articles = soup.find_all("article", class_="twelvecol-xs-nm")
        results = []
        for article in articles:
            raw_id = article.get("id", "").strip()
            if not raw_id or not raw_id.isdigit():
                continue
            listing_id = f"gg_{raw_id}"

            # URL
            a_tag = article.find("a", class_="detaillink")
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            if not href:
                continue
            listing_url = href if href.startswith("http") else f"{GOLDGRUBE_BASE_URL}{href}"

            # Title
            h3 = article.find("h3", class_="twelvecol-xs")
            title = h3.get_text(strip=True) if h3 else f"Goldgrube #{raw_id}"

            # Price
            price_span = article.find("span", class_="price")
            price = self._parse_price(price_span)

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        self.incomplete = False

        for base_url in GOLDGRUBE_URLS:
            page = 1
            logging.info(f"Goldgrube.at: starting → {base_url}")
            while True:
                url = base_url if page == 1 else f"{base_url}?p={page}"
                logging.info(f"Goldgrube.at: fetching page {page} → {url}")
                try:
                    status, raw, charset = await fetch_bytes(
                        self.session, url, timeout=aiohttp.ClientTimeout(total=30)
                    )
                    if status != 200:
                        logging.warning(f"Goldgrube.at: HTTP {status} – {url}")
                        break
                    html = decode_utf8_or_latin1(raw, charset)
                except Exception as e:
                    logging.error(f"Goldgrube.at: fetch error: {e}")
                    self.incomplete = True
                    break

                page_cards = self._parse_cards(html)
                logging.info(f"Goldgrube.at: {len(page_cards)} articles – page {page}")

                if not page_cards:
                    break

                found_on_page = 0
                for listing_id, title, listing_url, price in page_cards:
                    if listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)

                    if price == 0.0:
                        logging.info(f"Goldgrube.at: skipped (price unknown): {title}")
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
                            source="goldgrube.at",
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    found_on_page += 1

                logging.info(f"Goldgrube.at: {found_on_page} processed – page {page}")

                # Pagination: continue if there's a link to the next page
                soup = BeautifulSoup(html, "lxml")
                pagination = soup.find("div", class_="paginierung")
                if not pagination:
                    break
                next_link = pagination.find("a", href=re.compile(rf"\?p={page + 1}"))
                if not next_link:
                    break

                page += 1
                await asyncio.sleep(2)

        logging.info(f"Goldgrube: {len(results)} ads")
        return results
