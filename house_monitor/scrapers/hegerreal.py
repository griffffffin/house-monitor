import asyncio
import html as _html
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, HEGERREAL_BASE_URL, HEGERREAL_URL
from ..models import Listing, parse_de_price


class HegerRealScraper:
    """
    hegerreal.at – a broker site backed by the Justimmo real-estate CMS.
    The whole inventory (houses, apartments, rentals) is on one paginated list
    with no server-side price filter, so filtering happens client-side (same as
    Sonnberger/Findheim). Rentals carry a "Miete" (rent) row instead of a
    "Kaufpreis" one, so they parse to price 0.0 and are dropped by the existing
    price==0 filter — no separate rent handling needed; likewise "erfolgreich
    vermittelt" (already-sold) listings have no price row -> 0.0 -> skipped.
    Card: div.realty-wrapper
    Title/URL: h3 > a (href="/objekt/<id>?from=...")
    Price: the short-info <li> whose .list-item-desc == "Kaufpreis"; its sibling
      .list-item-value holds the German-formatted price. The label check is
      required because a sibling <li> holds the area ("Fläche ca. 65,38 m²") —
      an unscoped parse would grab that instead.
    ID: the numeric /objekt/<id> from the URL (heger_<id>).
    Pagination: page 1 = /aktuelle-immobilien, page N = /aktuelle-immobilien/p/N;
      past the last page the server returns HTTP 200 with 0 cards (no 404).
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("div", class_="realty-wrapper")
        results = []
        for card in cards:
            h3 = card.find("h3")
            a = h3.find("a", href=True) if h3 else None
            if not a:
                continue
            listing_url = a["href"]
            title = _html.unescape(a.get_text(strip=True))

            m = re.search(r"/objekt/(\d+)", listing_url)
            raw_id = m.group(1) if m else listing_url.rstrip("/").split("/")[-1].split("?")[0]
            listing_id = f"heger_{raw_id}"

            price = 0.0
            for li in card.find_all("li"):
                desc = li.find(class_="list-item-desc")
                if desc and "kaufpreis" in desc.get_text(strip=True).lower():
                    val = li.find(class_="list-item-value")
                    if val:
                        price = parse_de_price(val.get_text(strip=True))
                    break

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1

        while True:
            url = HEGERREAL_URL if page == 1 else f"{HEGERREAL_URL}/p/{page}"
            logging.info(f"HegerReal.at: fetching {url}")
            try:
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"HegerReal.at: HTTP {resp.status} – page {page}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"HegerReal.at: fetch error {type(e).__name__}: {e}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"HegerReal.at: {len(page_cards)} cards – page {page}")

            # Past the last page the server returns HTTP 200 with an empty list.
            if not page_cards:
                break

            new_on_page = 0
            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                new_on_page += 1

                if price == 0.0:
                    logging.info(f"HegerReal.at: skipped (price=0): {title}")
                    continue
                if not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                    continue

                # Make the relative /objekt/... href absolute for the email link.
                full_url = (
                    listing_url
                    if listing_url.startswith("http")
                    else f"{HEGERREAL_BASE_URL}{listing_url}"
                )

                now = datetime.now().isoformat()
                results.append(
                    Listing(
                        id=listing_id,
                        title=title,
                        price=price,
                        url=full_url,
                        source="hegerreal.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            # A page whose cards were all duplicates of earlier pages means the
            # pagination has looped back on itself — stop rather than spin.
            if new_on_page == 0:
                break

            page += 1
            await asyncio.sleep(2)

        logging.info(f"HegerReal: {len(results)} listings")
        return results
