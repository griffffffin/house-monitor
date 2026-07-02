import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import DIBEO_URL
from ..models import Listing, parse_de_price


class DibeoScraper:
    """
    Dibeo.at house listings (HTML, server-rendered).
    Cards: a[href*="/expose/"]
    ID: the numeric segment before "/acvblr" in the href, if present,
        otherwise the last path segment.
    Title: h2 inside the card
    Price: isolated from the card's full text via a "€ X" / "X €" regex
        (needed because the title can contain other numbers, e.g. a postal
        code), then converted with the shared parse_de_price() helper
    Pagination: &page=N, following rel="next"; also stops early if a page
    returns fewer than 5 new (not-already-seen) listings.
    """

    BASE_URL = "https://www.dibeo.at"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "html.parser")
        items = soup.select('a[href*="/expose/"]')
        results = []
        for item in items:
            href = item.get("href", "")
            if not href.startswith("http"):
                href = self.BASE_URL + href

            parts = href.rstrip("/").split("/")
            try:
                acvblr_idx = parts.index("acvblr")
                raw_id = parts[acvblr_idx - 1]
            except (ValueError, IndexError):
                raw_id = parts[-1].split("?")[0]

            listing_id = f"dibeo_{raw_id}"
            href = f"{self.BASE_URL}/expose/{raw_id}"

            title_tag = item.find("h2")
            title = title_tag.text.strip() if title_tag else "No title"

            # Dibeo price format: "€ 1.499.000" or "€ 1.230,61" (dot=thousands,
            # comma=decimal). The card's full text mixes the title and price
            # together (e.g. a street/postal-code number could appear before
            # the price), so the match must stay anchored next to the "€"
            # sign - a plain parse_de_price(item_text) could pick up the
            # wrong number. Once the right substring is isolated, parse_de_price
            # does the actual German-format-to-float conversion.
            price = 0.0
            item_text = item.get_text()
            price_match = re.search(r"€\s*([\d][0-9.,]*)", item_text)
            if not price_match:
                price_match = re.search(r"([\d][0-9.,]*)\s*€", item_text)
            if price_match:
                price = parse_de_price(price_match.group(1))

            results.append((listing_id, title, href, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        page = 1
        seen_ids: set = set()

        # Parse price bounds from DIBEO_URL (price.from / price.to params)
        from urllib.parse import urlparse, parse_qs

        _qs = parse_qs(urlparse(DIBEO_URL).query)
        _price_from = float(_qs.get("price.from", ["0"])[0])
        _price_to = float(_qs.get("price.to", ["1e18"])[0])

        while True:
            url = DIBEO_URL if page == 1 else f"{DIBEO_URL}&page={page}"
            logging.info(f"Dibeo.at: fetching page {page} -> {url}")
            try:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        logging.error(f"Dibeo.at: blocked with HTTP {response.status}")
                        break

                    html = await response.text()
                    page_cards = self._parse_cards(html)
                    if not page_cards:
                        logging.info("Dibeo.at: no listing elements found, stopping.")
                        break

                    found_on_page = 0
                    for listing_id, title, href, price in page_cards:
                        if listing_id in seen_ids:
                            continue
                        seen_ids.add(listing_id)

                        if not title or title == "No title":
                            continue

                        if price > 0 and _price_from <= price <= _price_to:
                            now = datetime.now().isoformat()
                            results.append(
                                Listing(
                                    id=listing_id,
                                    title=title,
                                    price=price,
                                    url=href,
                                    source="Dibeo.at",
                                    first_seen=now,
                                    last_seen=now,
                                )
                            )
                            found_on_page += 1

                    logging.info(f"Dibeo.at: {found_on_page} new listings on page {page}.")

                    soup = BeautifulSoup(html, "html.parser")
                    next_link = soup.select_one('a[rel~="next"]')
                    if next_link:
                        page += 1
                        await asyncio.sleep(2)
                    else:
                        if found_on_page < 5:
                            logging.info("Dibeo.at: no more pages.")
                            break
                        page += 1
                        await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Dibeo.at: error on page {page}: {e}")
                break

        logging.info(f"Dibeo: {len(results)} listings")
        return results
