import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, IMMOBILIEN_NET_BASE, IMMOBILIEN_NET_URLS
from ..models import Listing, parse_de_price


class ImmobIlienNetScraper:
    """
    immobilien.net – the ImmoScout24 AT portal.
    Cards: li._98L38
    Title: h2._3r8AR
    Price: h4.D1pOB
    URL/ID: the slug from a._2BVPu[href]
    Pagination: ?page=N (1-indexed)
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("li", class_="_98L38")
        results = []
        for card in cards:
            a = card.find("a", class_="_2BVPu")
            if not a:
                continue
            href = a.get("href", "")
            if not href or not href.startswith("/immobilie/"):
                continue
            slug = href.rstrip("/").split("/")[-1]
            listing_id = f"inet_{slug}"
            listing_url = IMMOBILIEN_NET_BASE + href

            h2 = card.find("h2", class_="_3r8AR")
            title = _html.unescape(h2.get_text(strip=True)) if h2 else f"ImmobNet #{slug[:20]}"

            price = 0.0
            h4 = card.find("h4", class_="D1pOB")
            if h4:
                price = parse_de_price(h4.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()

        for base_url in IMMOBILIEN_NET_URLS:
            page = 1
            while True:
                url = base_url if page == 1 else f"{base_url}&page={page}"
                logging.info(f"Immobilien.net: fetching {url}")
                try:
                    async with self.session.get(
                        url, timeout=aiohttp.ClientTimeout(total=30)
                    ) as resp:
                        if resp.status != 200:
                            logging.warning(f"Immobilien.net: HTTP {resp.status} – {url}")
                            break
                        html_text = await resp.text()
                except Exception as e:
                    logging.error(f"Immobilien.net: fetch error {type(e).__name__}: {e}")
                    break

                page_cards = self._parse_cards(html_text)
                logging.info(f"Immobilien.net: {len(page_cards)} cards – page {page}")

                if not page_cards:
                    break

                for listing_id, title, listing_url, price in page_cards:
                    if listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)
                    if price == 0.0:
                        logging.info(f"Immobilien.net: skipped (price=0): {title}")
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
                            source="immobilien.net",
                            first_seen=now,
                            last_seen=now,
                        )
                    )

                page += 1
                await asyncio.sleep(2)

        logging.info(f"Immobilien.net: {len(results)} ads")
        return results
