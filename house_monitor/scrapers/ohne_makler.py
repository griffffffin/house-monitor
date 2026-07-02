import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, OHNE_MAKLER_BASE_URL, OHNE_MAKLER_URLS
from ..models import Listing, parse_de_price


class OhneMaklerScraper:
    """
    ohne-makler.at – commission-free house listings.
    Cards: div[id^="bookmark_"] -> the ID comes from bookmark_XXXXX
    Price: span.font-semibold.text-primary-500
    Title: h4 element
    URL: a[href] inside the card (/immobilie/XXXXX/)
    Pagination: ?page=N query param, as long as there are cards
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("div", id=lambda v: v and v.startswith("bookmark_"))
        results = []
        for card in cards:
            # ID
            raw_id = card["id"].replace("bookmark_", "").strip()
            listing_id = f"om_{raw_id}"

            # Link and title
            a_tag = card.find("a", href=True)
            if not a_tag:
                continue
            href = a_tag["href"]
            listing_url = href if href.startswith("http") else OHNE_MAKLER_BASE_URL + href

            h4 = card.find("h4")
            title = _html.unescape(h4.get_text(strip=True)) if h4 else f"OhneMakler #{raw_id}"

            # Price: the first span.font-semibold.text-primary-500
            price = 0.0
            price_span = card.find(
                "span", class_=lambda c: c and "font-semibold" in c and "text-primary-500" in c
            )
            if price_span:
                # e.g. "69.000 €" or "655.000 €"
                price = parse_de_price(price_span.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def _fetch_one_url(self, base_url: str) -> List[Listing]:
        url_results = []
        seen_ids: set = set()
        page = 1
        while True:
            url = f"{base_url}&page={page}" if page > 1 else base_url
            logging.info(f"OhneMakler.at: fetching {url}")
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logging.warning(f"OhneMakler.at: HTTP {resp.status} – {url}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"OhneMakler.at: fetch error {e}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"OhneMakler.at: {len(page_cards)} cards – {url}")

            if not page_cards:
                break

            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)

                # Client-side filter
                if price == 0.0:
                    logging.info(f"OhneMakler.at: skipped (price=0): {title}")
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
                        source="ohne-makler.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            # If we're on the last page (fewer than 20 cards), stop
            if len(page_cards) < 20:
                break
            page += 1
            await asyncio.sleep(2)

        return url_results

    async def fetch_listings(self) -> List[Listing]:
        # The two category URLs (haus-kaufen, lagerhalle-kaufen) are
        # independent of each other, so we paginate them concurrently
        # instead of sequentially.
        per_url_results = await asyncio.gather(*(self._fetch_one_url(u) for u in OHNE_MAKLER_URLS))

        results = []
        seen_ids: set = set()
        for url_results in per_url_results:
            for listing in url_results:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                results.append(listing)

        logging.info(f"OhneMakler: {len(results)} ads")
        return results
