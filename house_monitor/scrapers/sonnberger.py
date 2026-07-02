import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, SONNBERGER_BASE_URL, SONNBERGER_URL
from ..models import Listing, parse_de_price


class SonnbergerScraper:
    """
    sonnberger.co.at – a WordPress + Houzez theme real-estate site.
    There's no server-side price filter parameter (only ascending-price
    sorting via ?sortby=a_price), so filtering happens entirely client-side
    (same as Findheim). When a listing is "Reserviert"/"Verkauft"/"auf
    Anfrage" (reserved/sold/on request), the list view shows this text in
    place of the price, with no digits -> parse_de_price naturally returns
    0.0, which the existing price==0 client filter already excludes — no
    separate reserved/sold handling needed.
    Card: div.item-listing-wrap
    Title/URL: h2.item-title > a
    Price: .item-price (distinct from the similarly-named .item-price-wrap
      wrapper element)
    ID: the [data-listid] attribute inside the card (fallback: URL slug)
    Pagination: /immobilienart/haeuser/page/N/?sortby=a_price (standard
      WordPress pagination, N=2,3,...); the last page is followed by HTTP 404.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        cards = soup.find_all("div", class_="item-listing-wrap")
        results = []
        for card in cards:
            h2 = card.find("h2", class_="item-title")
            a = h2.find("a", href=True) if h2 else None
            if not a:
                continue
            listing_url = a["href"]
            title = _html.unescape(a.get_text(strip=True))

            id_tag = card.find(attrs={"data-listid": True})
            raw_id = id_tag.get("data-listid") if id_tag else None
            if not raw_id:
                raw_id = listing_url.rstrip("/").split("/")[-1]
            listing_id = f"sb_{raw_id}"

            price = 0.0
            price_el = card.find(class_="item-price")
            if price_el:
                price = parse_de_price(price_el.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 1

        while True:
            url = (
                SONNBERGER_URL
                if page == 1
                else (f"{SONNBERGER_BASE_URL}/wp/immobilienart/haeuser/page/{page}/?sortby=a_price")
            )
            logging.info(f"Sonnberger.co.at: fetching {url}")
            try:
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        if resp.status == 404 and page > 1:
                            # This is how Sonnberger signals the end of pagination — not an error
                            logging.info(
                                f"Sonnberger.co.at: HTTP 404 (no more pages) – page {page}"
                            )
                        else:
                            logging.warning(f"Sonnberger.co.at: HTTP {resp.status} – page {page}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"Sonnberger.co.at: fetch error {type(e).__name__}: {e}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"Sonnberger.co.at: {len(page_cards)} cards – page {page}")

            if not page_cards:
                break

            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)

                if price == 0.0:
                    logging.info(f"Sonnberger.co.at: skipped (price=0): {title}")
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
                        source="sonnberger.co.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            page += 1
            await asyncio.sleep(2)

        logging.info(f"Sonnberger: {len(results)} ads")
        return results
