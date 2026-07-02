import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, IMMOBILIEN_DE_BASE_URL, IMMOBILIEN_DE_URLS
from ..models import Listing, parse_de_price


class ImmobilienDeScraper:
    """
    immobilien.de – German real estate portal, Austrian listings.
    Cards:      a.lr-card[href]
    Title:      .lr-card__title
    Price:      .lr-card__price-amount (e.g. "208.000 €", dot = thousands separator)
    Pagination: &block=N (starting at 1; stop when .pg-nav__next is missing)
    ID prefix:  imde_
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.select("a.lr-card[href]")
        results = []
        for card in cards:
            href = card.get("href", "")
            if not href:
                continue

            listing_url = href if href.startswith("http") else f"{IMMOBILIEN_DE_BASE_URL}{href}"

            # ID: e.g. /ausland/9667674 -> "imde_9667674"
            raw_id = href.rstrip("/").split("/")[-1].split("?")[0]
            listing_id = f"imde_{raw_id}"

            # Title
            title_el = card.select_one(".lr-card__title")
            title = title_el.get_text(strip=True) if title_el else f"Immobilien.de #{raw_id}"

            # Price: "208.000 €" -> dot=thousands, no decimals
            price = 0.0
            price_el = card.select_one(".lr-card__price-amount")
            if price_el:
                price = parse_de_price(price_el.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def _fetch_one_url(self, base_url: str) -> List[Listing]:
        url_results = []
        seen_ids: set = set()
        block = 1
        logging.info(f"Immobilien.de: starting -> {base_url}")

        while True:
            url = base_url if block == 1 else f"{base_url}&block={block}"
            logging.info(f"Immobilien.de: fetching block {block} -> {url}")

            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        logging.warning(f"Immobilien.de: HTTP {resp.status} – {url}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"Immobilien.de: fetch error {type(e).__name__}: {e}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"Immobilien.de: {len(page_cards)} cards – block {block}")

            if not page_cards:
                break

            found_on_block = 0
            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)

                if price == 0.0:
                    logging.info(f"Immobilien.de: skipped (price=0): {title}")
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
                        source="immobilien.de",
                        first_seen=now,
                        last_seen=now,
                    )
                )
                found_on_block += 1

            logging.info(f"Immobilien.de: {found_on_block} processed, block {block}.")

            # Pagination: continue as long as there's a "next" arrow
            soup = BeautifulSoup(html_text, "lxml")
            if not soup.select_one(".pg-nav__next"):
                logging.info("Immobilien.de: no more pages.")
                break

            block += 1
            await asyncio.sleep(2)

        return url_results

    async def fetch_listings(self) -> List[Listing]:
        # The four category URLs (rendite, gastronomie_hotel, freizeit, haus)
        # are independent of each other, so we paginate them concurrently
        # instead of sequentially.
        per_url_results = await asyncio.gather(
            *(self._fetch_one_url(u) for u in IMMOBILIEN_DE_URLS)
        )

        results = []
        seen_ids: set = set()
        for url_results in per_url_results:
            for listing in url_results:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                results.append(listing)

        logging.info(f"Immobilien.de: {len(results)} ads")
        return results
