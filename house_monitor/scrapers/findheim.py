import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import FINDHEIM_BASE_URL, FINDHEIM_URL
from ..fetch import fetch_text
from ..models import Listing, parse_de_price


class FindheimScraper:
    """
    Findheim.at house listings (HTML, paginated).
    Listing links: <a href="/de/immobilie/slug-HASH">
    Card: div.group whose class list includes both "overflow-hidden" and
      "border" (the earlier "rounded-md" Tailwind class was replaced by
      "rounded-3xl" in the site's 2026 redesign, and the old
      z-[1]/right-0/bottom-0 price-container structure is gone too -
      the price now comes from the <p class="font-semibold ...">€ X</p>
      next to the title instead)
    Price: <p class="font-semibold ...">€ 59.999</p> inside the card
    Title: <h2> or <h3> inside the card
    Pagination: &page=N
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error, i.e. the
        # returned list may be missing pages — the monitor's same-day retry
        # loop re-runs the sources that set this flag.
        self.incomplete = False

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")

        # Cards: divs with both "group" and "overflow-hidden" (and "border")
        # in their class list - the old "rounded-md" check stopped working
        # because the site redesign changed the corner-rounding class to
        # "rounded-3xl" (see class docstring).
        all_divs = soup.find_all("div", class_="group")
        cards = [
            d
            for d in all_divs
            if "overflow-hidden" in d.get("class", []) and "border" in d.get("class", [])
        ]

        results = []
        for card in cards:
            # Find the main listing link (href="/de/immobilie/...")
            # Skip advertisement cards (utm_medium=advertisement in href)
            link = card.find("a", href=re.compile(r"/de/immobilie/"))
            if not link:
                continue
            href = link.get("href", "")
            # Skip ad/promoted listings
            if "utm_medium=advertisement" in href or "utm_campaign=" in href:
                continue
            # Strip any query params from URL for clean link
            clean_href = href.split("?")[0]
            listing_url = (
                clean_href if clean_href.startswith("http") else f"{FINDHEIM_BASE_URL}{clean_href}"
            )

            # Extract slug hash as ID (last part of URL path)
            slug = clean_href.rstrip("/").split("/")[-1]
            listing_id = f"fh_{slug[-8:]}" if len(slug) >= 8 else f"fh_{slug}"

            # Title: <h3> is the actual listing title (h2 is generic "Haus zum Kauf in...")
            h3 = card.find("h3")
            h2 = card.find("h2")
            title = (h3.get_text(strip=True) if h3 else None) or (
                h2.get_text(strip=True) if h2 else slug
            )

            # Price: <p class="font-semibold ...">€ 59.999</p> next to the
            # title (the old z-[1]/right-0/bottom-0 price-container structure
            # no longer exists, see class docstring)
            price = 0.0
            price_el = card.find("p", class_=lambda c: c and "font-semibold" in c)
            if price_el:
                price = parse_de_price(price_el.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1
        self.incomplete = False

        while True:
            url = f"{FINDHEIM_URL}&page={page}"
            logging.info(f"Findheim.at: fetching page {page} -> {url}")
            try:
                status, html = await fetch_text(
                    self.session, url, headers={"Accept-Language": "de-DE,de;q=0.9"}
                )
                if status != 200:
                    logging.warning(f"Findheim.at: HTTP {status} on page {page}.")
                    break

                page_cards = self._parse_cards(html)

                found_on_page = 0
                for listing_id, title, listing_url, price in page_cards:
                    if listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)

                    now = datetime.now().isoformat()
                    results.append(
                        Listing(
                            id=listing_id,
                            title=title,
                            price=price,
                            url=listing_url,
                            source="findheim.at",
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    found_on_page += 1

                logging.info(f"Findheim.at: {found_on_page} listings on page {page}.")

                if found_on_page == 0:
                    logging.info("Findheim.at: no more results.")
                    break

                page += 1
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Findheim.at: error on page {page}: {e}")
                self.incomplete = True
                break

        logging.info(f"Findheim: {len(results)} listings")
        return results
