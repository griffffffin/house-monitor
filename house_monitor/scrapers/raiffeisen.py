import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, RAIFFEISEN_BASE_URL, RAIFFEISEN_SEARCH_URL
from ..models import Listing, parse_de_price


class RaiffeisenScraper:
    """
    Raiffeisen-Immobilien.at house listings (HTML, SSR).
    Cards: div.bg-white with <a href="/en/properties/buy/...">
    Title: h4 inside the card
    Price: dd inside dl.facts, after dt "Purchase price"
    Pagination: ?page=1,2,... — stop when 0 results
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        # Cards: <div class="bg-white flex flex-col relative group">
        cards = soup.find_all("div", class_=lambda c: c and "bg-white" in c and "flex-col" in c)
        results = []
        for card in cards:
            a_tag = card.find("a", href=re.compile(r"/en/properties/(buy|rent)/"))
            if not a_tag:
                continue

            href = a_tag.get("href", "")
            listing_url = href if href.startswith("http") else f"{RAIFFEISEN_BASE_URL}{href}"

            # Extract ID from URL: last path segment before query string
            url_path = href.split("?")[0].rstrip("/")
            raw_id = url_path.split("/")[-1]  # e.g. "0001009858" or "124567"
            listing_id = f"ri_{raw_id}"

            # Title from h4
            h4 = card.find("h4")
            title = h4.get_text(strip=True) if h4 else a_tag.get("title", "").strip()
            if not title:
                title = f"Raiffeisen #{raw_id}"

            # Price: find dt "Purchase price" then its sibling dd
            price = 0.0
            dl = card.find("dl")
            if dl:
                for dt in dl.find_all("dt"):
                    if "purchase price" in dt.get_text(strip=True).lower():
                        dd = dt.find_next_sibling("dd")
                        if dd:
                            # Format: "18.000,00 €" or "50.000,00 €"
                            price = parse_de_price(dd.get_text(strip=True))
                        break

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1

        logging.info(f"Raiffeisen-Immobilien: starting -> {RAIFFEISEN_SEARCH_URL}")

        while True:
            url = f"{RAIFFEISEN_SEARCH_URL}&page={page}"
            logging.info(f"Raiffeisen-Immobilien: fetching page {page} -> {url}")
            try:
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        logging.warning(
                            f"Raiffeisen-Immobilien: HTTP {resp.status} on page {page}."
                        )
                        break
                    html = await resp.text()

                cards = self._parse_cards(html)

                found_on_page = 0
                for listing_id, title, listing_url, price in cards:
                    if listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)

                    # Client-side min price filter (server only filters max)
                    if price > 0 and not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                        continue

                    # Skip 0-price entries (e.g. Leibrente — price on request)
                    if price == 0.0:
                        logging.info(f"Raiffeisen-Immobilien: skipped (price=0): {title}")
                        continue

                    now = datetime.now().isoformat()
                    results.append(
                        Listing(
                            id=listing_id,
                            title=title,
                            price=price,
                            url=listing_url,
                            source="raiffeisen-immobilien.at",
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    found_on_page += 1

                logging.info(
                    f"Raiffeisen-Immobilien: {found_on_page} listings on page {page} "
                    f"(cards found: {len(cards)})."
                )

                if found_on_page == 0 and len(cards) == 0:
                    logging.info("Raiffeisen-Immobilien: no more results.")
                    break
                elif found_on_page == 0:
                    logging.info("Raiffeisen-Immobilien: all cards already seen, stopping.")
                    break

                page += 1
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Raiffeisen-Immobilien: error on page {page}: {e}")
                break

        logging.info(f"Raiffeisen-Immobilien: {len(results)} listings")
        return results
