import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, WOHNNET_BASE_URL, WOHNNET_URL
from ..models import Listing, parse_de_price


class WohnnetScraper:
    """
    Wohnnet.at house listings (HTML, paginated).
    Listing: <a href="/immobilien/TYPE-REGION-kauf-ROOMS-ID" data-id="ID" data-title="...">
    Price: <b style="font-size: x-large">3.200 €</b> in col text-right text-nowrap
    Title: <p class="h4">...</p>
    Location: <i class="fas fa-map-marker-alt"> followed by text node
    Skip: listings with location "Deutschland"
    Pagination: &seite=N
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        # Each listing is an <a> with data-id attribute wrapping a .realty div
        listing_links = soup.find_all("a", attrs={"data-id": True, "data-title": True})
        results = []
        for a in listing_links:
            raw_id = a.get("data-id", "").strip()
            if not raw_id:
                continue
            listing_id = f"wn_{raw_id}"

            href = a.get("href", "")
            listing_url = href if href.startswith("http") else f"{WOHNNET_BASE_URL}{href}"

            # Title from data-title attribute (most reliable)
            title = a.get("data-title", "").strip()

            # Location: text after <i class="fas fa-map-marker-alt">
            # next_sibling may be whitespace; strip and check
            location = ""
            marker = a.find("i", class_="fa-map-marker-alt")
            if marker:
                sib = marker.next_sibling
                if sib:
                    location = str(sib).strip()

            # Skip non-Austrian listings
            if "Deutschland" in location or "Serbien" in location:
                continue

            # Price: inside <div class="col text-right text-nowrap">
            # Multiple <b style="font-size: x-large"> exist per card (area, rooms, price)
            # Only the one inside text-right text-nowrap div is the price
            price = 0.0
            price_div = a.find(
                "div", class_=lambda c: c and "text-right" in c and "text-nowrap" in c
            )
            if price_div:
                price_b = price_div.find("b", style=lambda s: s and "x-large" in s)
                if price_b:
                    price = parse_de_price(price_b.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1

        while True:
            url = f"{WOHNNET_URL}&seite={page}"
            logging.info(f"Wohnnet.at: fetching page {page} -> {url}")
            try:
                async with self.session.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept-Language": "de-DE,de;q=0.9",
                    },
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"Wohnnet.at: HTTP {resp.status} on page {page}.")
                        break
                    html = await resp.text()

                page_cards = self._parse_cards(html)

                if page == 1:
                    logging.info(f"Wohnnet.at DEBUG: listing_links={len(page_cards)}")

                found_on_page = 0
                for listing_id, title, listing_url, price in page_cards:
                    if listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)

                    # Client-side price filter
                    if price > 0 and not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                        continue

                    now = datetime.now().isoformat()
                    results.append(
                        Listing(
                            id=listing_id,
                            title=title,
                            price=price,
                            url=listing_url,
                            source="wohnnet.at",
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    found_on_page += 1

                logging.info(f"Wohnnet.at: {found_on_page} listings on page {page}.")

                soup = BeautifulSoup(html, "lxml")

                # Check for next page link: pagination nav has <a> buttons with seite=N
                # If there's a next page button beyond current page, continue
                next_page_links = soup.find_all("a", href=re.compile(rf"seite={page + 1}"))
                if not next_page_links:
                    logging.info("Wohnnet.at: no more results.")
                    break

                page += 1
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Wohnnet.at: error on page {page}: {e}")
                break

        logging.info(f"Wohnnet: {len(results)} listings")
        return results
