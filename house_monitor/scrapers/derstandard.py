import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import DERSTANDARD_BASE_URL, DERSTANDARD_URL, EUR_PRICE_FROM, EUR_PRICE_TO
from ..models import Listing, parse_de_price


class DerStandardScraper:
    """
    immobilien.derstandard.at house listings (HTML, paginated).
    Listing cards: <li class="sc-listing-card ...">
    Link/ID:  <a class="sc-listing-card-content-background-link" href="/detail/XXXXXXXX">
    Title:    <div class="sc-listing-card-title ...">
    Price:    <span class="ResultItemPrice-module-scss-module__...">€&nbsp;3.290</span>
              Fallback: <div class="sc-listing-card-footer-item-main">€&nbsp;3.290</div>
    Pagination: &page=2, &page=3 — stop when no listing cards found.
    Client-side price filter applied (server filter is reliable but we double-check).
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        # Each listing is an <li> with class "sc-listing-card"
        cards = soup.find_all("li", class_=lambda c: c and "sc-listing-card" in c)
        results = []
        for card in cards:
            # Background link contains the detail URL and serves as the canonical link
            bg_link = card.find(
                "a", class_=lambda c: c and "sc-listing-card-content-background-link" in c
            )
            if not bg_link:
                continue
            href = bg_link.get("href", "")
            if not href:
                continue

            # Normalize URL
            listing_url = href if href.startswith("http") else f"{DERSTANDARD_BASE_URL}{href}"

            # ID from href: /detail/XXXXXXXX  -> ds_XXXXXXXX
            raw_id = href.rstrip("/").split("/")[-1].split("?")[0]
            listing_id = f"ds_{raw_id}"

            # Title: div with class containing "sc-listing-card-title"
            title_el = card.find("div", class_=lambda c: c and "sc-listing-card-title" in c)
            title = (
                title_el.get_text(strip=True) if title_el else bg_link.get("aria-label", "").strip()
            )
            if not title:
                title = f"DerStandard #{raw_id}"

            # Price: first try the desktop price span (ResultItemPrice)
            price = 0.0
            price_el = card.find("span", class_=lambda c: c and "ResultItemPrice" in c)
            if not price_el:
                # Fallback: footer item marked as "main" (mobile view)
                price_el = card.find(
                    "div", class_=lambda c: c and "sc-listing-card-footer-item-main" in c
                )
            if price_el:
                # Format: "€ 3.290" or "€ 12.000" — dot = thousands separator
                price = parse_de_price(price_el.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1

        while True:
            url = DERSTANDARD_URL if page == 1 else f"{DERSTANDARD_URL}&page={page}"
            logging.info(f"DerStandard: fetching page {page} -> {url}")
            try:
                async with self.session.get(url) as resp:
                    if resp.status != 200:
                        if resp.status == 404 and page > 1:
                            # This is how DerStandard signals the end of pagination — not an error
                            logging.info(f"DerStandard: HTTP 404 (no more pages) - page {page}.")
                        else:
                            logging.warning(f"DerStandard: HTTP {resp.status} on page {page}.")
                        break
                    html = await resp.text()

                page_cards = self._parse_cards(html)

                logging.info(f"DerStandard: {len(page_cards)} cards found on page {page}.")

                if not page_cards:
                    logging.info("DerStandard: no listing cards found, stopping.")
                    break

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
                            source="immobilien.derstandard.at",
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    found_on_page += 1

                logging.info(f"DerStandard: {found_on_page} valid listings on page {page}.")

                if found_on_page == 0:
                    logging.info("DerStandard: no valid listings on page, stopping.")
                    break

                page += 1
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"DerStandard: error on page {page}: {e}")
                break

        logging.info(f"DerStandard: {len(results)} listings")
        return results
