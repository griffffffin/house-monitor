import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import FINDHEIM_BASE_URL, FINDHEIM_PROBE_URL, FINDHEIM_URL
from ..fetch import fetch_page, fetch_text
from ..models import Listing, parse_de_price


class FindheimScraper:
    """
    Findheim.at house listings (HTML, paginated).
    Listing links: <a href="/de/immobilie/slug-HASH">
    Card (2026-10-31 markup): div.group holding two links to the same
      listing — the photo carousel and the text block. Any div.group whose
      listing links all point to one listing counts, so the card no longer
      hangs on Tailwind classes (the 07-01 redesign's "overflow-hidden" +
      "border" moved to the inner photo frame, which made every card vanish).
    Text block (the link that holds the <p>s):
      price  p.font-bold ("€ 9.400"; the 07-01 markup: p.font-semibold)
      facts  p.font-medium ("2 Zimmer · 40 m² · Grund 291 m²", unused)
      h2     "Haus kaufen in 7522 Strem" -> the address (postcode + town)
      title  p.truncate, the ad's own title (the 07-01 markup: h3)
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
        results = []
        for card in soup.find_all("div", class_="group"):
            links = card.find_all("a", href=re.compile(r"/de/immobilie/"))
            # Exactly one listing: not a card at all (no link), or a block
            # wrapping several cards, otherwise.
            if len({a["href"] for a in links}) != 1:
                continue
            href = links[0]["href"]
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

            # The text block: the link with the <p>s (the other one is the photo)
            body = next((a for a in links if a.find("p")), card)

            # Title: the ad's own title (h2 is the generic "Haus kaufen in …")
            title_el = body.find("p", class_="truncate") or card.find("h3")
            heading = card.find("h2")
            title = " ".join(title_el.get_text(" ", strip=True).split()) if title_el else ""
            if not title:
                title = heading.get_text(" ", strip=True) if heading else slug

            price = 0.0
            price_el = body.find("p", class_="font-bold") or body.find("p", class_="font-semibold")
            if price_el:
                price = parse_de_price(price_el.get_text(strip=True))

            # Address (for the SITE_BLACKLIST check): "Haus kaufen in 7522 Strem"
            # -> "7522 Strem"; "… in keine Angabe" (no address given) -> "".
            location = ""
            if heading:
                _, _, place = heading.get_text(" ", strip=True).partition(" in ")
                if place[:1].isdigit():
                    location = place

            results.append((listing_id, title, listing_url, price, location))
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
                for listing_id, title, listing_url, price, location in page_cards:
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
                            location=location,
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

    async def probe(self) -> list:
        """Page 1 of the search without the price range, parsed like a normal
        page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_cards(await fetch_page(self.session, FINDHEIM_PROBE_URL))
