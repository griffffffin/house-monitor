import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, IMMOKRALLE_URLS
from ..models import Listing, parse_de_price


class ImmokralleScraper:
    """
    immokralle.com – an aggregator portal.
    Cards: li.immo[data-id]
    Title: h2
    Price: div.price
    URL: a.anzeigen_link[href]
    Pagination: &page=N (0-indexed: page 1 = no param, page 2 = page=1)
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select("li.immo[data-id]")
        results = []
        for card in cards:
            uid = card.get("data-id", "").strip()
            if not uid:
                continue
            a_tag = card.find("a", class_="anzeigen_link")
            if not a_tag:
                continue
            listing_url = a_tag.get("href", "")
            if not listing_url or listing_url.startswith("javascript"):
                continue
            h2 = card.find("h2")
            title = _html.unescape(h2.get_text(strip=True)) if h2 else f"Immokralle #{uid}"
            price = 0.0
            price_div = card.find("div", class_="price")
            if price_div:
                price = parse_de_price(price_div.get_text(strip=True))
            results.append((uid, title, listing_url, price))
        return results

    async def _fetch_one_url(self, base_url: str) -> List[Listing]:
        """We use urllib instead of aiohttp here, because aiohttp always
        percent-encodes the [ ] characters in the URL, which the server rejects."""
        import urllib.request as _urllib

        url_results = []
        seen_ids: set = set()
        ik_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-AT,de;q=0.9,en;q=0.7",
        }

        def _fetch_url(url: str) -> str | None:
            """Synchronous urllib fetch — doesn't percent-encode the [ ] characters."""
            req = _urllib.Request(url, headers=ik_headers)
            try:
                with _urllib.urlopen(req, timeout=30) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except Exception as e:
                logging.error(f"Immokralle.com: urllib error: {type(e).__name__}: {e} – {url}")
                return None

        loop = asyncio.get_event_loop()

        page = 0
        while True:
            url = base_url if page == 0 else f"{base_url}&page={page}"
            logging.info(f"Immokralle.com: fetching {url}")
            html_text = await loop.run_in_executor(None, _fetch_url, url)

            if not html_text:
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"Immokralle.com: {len(page_cards)} cards – page {page}")

            if not page_cards:
                break

            for uid, title, listing_url, price in page_cards:
                listing_id = f"ik_{uid}"
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                if price == 0.0:
                    logging.info(f"Immokralle.com: skipped (price=0): {title}")
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
                        source="immokralle.com",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            page += 1
            await asyncio.sleep(2)

        return url_results

    async def fetch_listings(self) -> List[Listing]:
        # The two category URLs (haus, geschaftslokal) are independent of
        # each other, so we paginate them concurrently instead of sequentially.
        per_url_results = await asyncio.gather(*(self._fetch_one_url(u) for u in IMMOKRALLE_URLS))

        results = []
        seen_ids: set = set()
        for url_results in per_url_results:
            for listing in url_results:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                results.append(listing)

        logging.info(f"Immokralle: {len(results)} ads")
        return results
