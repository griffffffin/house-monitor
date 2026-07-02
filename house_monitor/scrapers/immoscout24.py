import asyncio
import logging
from datetime import datetime
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

from ..config import IMMOSCOUT_URL
from ..models import Listing, parse_de_price


class ImmoScout24Scraper:
    """immobilienscout24.at house listings (server-rendered HTML)."""

    BASE_URL = "https://www.immobilienscout24.at"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

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
            price_elements = item.select('ul[class*="PriceKeyFacts"] li')
            for el in price_elements:
                text = el.text.strip()
                if "€" in text and "/m²" not in text:
                    parsed = parse_de_price(text)
                    if parsed > 0:
                        price = parsed
                        break

            results.append((listing_id, title, url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
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

                    for listing_id, title, url, price in page_cards:
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
                break

        logging.info(f"ImmoScout24: {len(results)} listings")
        return results
