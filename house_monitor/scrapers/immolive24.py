import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import (
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    IMMOLIVE24_BASE_URL,
    IMMOLIVE24_SEARCH_DATA,
    IMMOLIVE24_SEARCH_URL,
)
from ..models import Listing, parse_de_price


class ImmoLive24Scraper:
    """
    at.immolive24.com – a Flynax-based aggregator portal.
    The first page must be fetched with POST (which sets the search filters
    in server-side session state keyed by PHPSESSID); subsequent pages are
    plain GET using the same session (cookie jar):
    /immobilien/search-results/indexN.html (N=2,3,...).
    The last page returns 0 cards (HTTP 200) — that's the stop signal.
    Card: div.item
    Title: the first div inside td.fields (a descriptive teaser text, e.g.
      "Kleines Einfamilienhaus mit Garten") — more informative than the
      h2 > a title ("Haus, PLZ, Ort"), so we use it, falling back to the
      plain address only when the teaser is missing.
    Price: span.miete ("Kaufpreis: € X.XXX,XX" — the class is named "miete"
      [rent] but the text says "Kaufpreis" [purchase price], since the search
      only queries Category_ID=228 [houses/buy]).
    ID: the number at the end of the href, "-<number>.html".
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("div", class_="item")
        results = []
        for card in cards:
            h2 = card.find("h2")
            a = h2.find("a", href=True) if h2 else None
            if not a:
                continue
            href = a["href"]
            m = re.search(r"-(\d+)\.html", href)
            if not m:
                continue
            raw_id = m.group(1)
            listing_id = f"il24_{raw_id}"
            listing_url = href.split("?")[0]

            address = _html.unescape(a.get_text(strip=True))
            title = address
            td = card.find("td", class_="fields")
            if td:
                teaser_div = td.find("div")
                if teaser_div:
                    teaser = _html.unescape(teaser_div.get_text(strip=True))
                    if teaser:
                        title = teaser

            price = 0.0
            price_span = card.find("span", class_="miete")
            if price_span:
                price = parse_de_price(price_span.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1

        while True:
            try:
                if page == 1:
                    logging.info(f"ImmoLive24.at: fetching {IMMOLIVE24_SEARCH_URL}")
                    async with self.session.post(
                        IMMOLIVE24_SEARCH_URL,
                        data=IMMOLIVE24_SEARCH_DATA,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            logging.warning(f"ImmoLive24.at: HTTP {resp.status} – page {page}")
                            break
                        html_text = await resp.text()
                else:
                    url = f"{IMMOLIVE24_BASE_URL}/immobilien/search-results/index{page}.html"
                    logging.info(f"ImmoLive24.at: fetching {url}")
                    async with self.session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            logging.warning(f"ImmoLive24.at: HTTP {resp.status} – page {page}")
                            break
                        html_text = await resp.text()
            except Exception as e:
                logging.error(f"ImmoLive24.at: fetch error {type(e).__name__}: {e}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"ImmoLive24.at: {len(page_cards)} cards – page {page}")

            if not page_cards:
                break

            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)

                if price == 0.0:
                    logging.info(f"ImmoLive24.at: skipped (price=0): {title}")
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
                        source="immolive24.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            page += 1
            await asyncio.sleep(2)

        logging.info(f"ImmoLive24: {len(results)} ads")
        return results
