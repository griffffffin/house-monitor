import asyncio
import logging
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import DINGDONG_BASE_URL, DINGDONG_URL, EUR_PRICE_FROM, EUR_PRICE_TO
from ..models import Listing, parse_de_price


class DingDongScraper:
    """
    ding-dong.at – a Drupal Views-based portal (a partner of immokralle.com,
    but the cross-platform duplicate filter already handles any overlap).
    Card: table.views-table tbody tr
    Title: td.views-field-title > h2 > a
    Price: td.views-field-field-preis (e.g. "13.500,00 €")
    URL/ID: the href is a unique slug ("/immobilien/<slug>") — there's no
      numeric ID in the link, so the slug itself serves as the ID.
    Pagination: &page=N (0-indexed, page 1 is the URL with no parameter/page=0);
      stateless GET — no session/cookie needed, every page carries the full
      filter query string.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        import html as _html

        soup = BeautifulSoup(html_text, "lxml")
        rows = soup.select("table.views-table tbody tr")
        results = []
        for row in rows:
            title_td = row.find("td", class_=lambda c: c and "views-field-title" in c)
            h2 = title_td.find("h2") if title_td else None
            a = h2.find("a", href=True) if h2 else None
            if not a:
                continue
            href = a["href"]
            slug = href.rstrip("/").split("/")[-1]
            listing_id = f"dd_{slug}"
            listing_url = href if href.startswith("http") else DINGDONG_BASE_URL + href

            title = _html.unescape(a.get_text(strip=True)) or f"DingDong #{slug}"

            price = 0.0
            price_td = row.find("td", class_=lambda c: c and "views-field-field-preis" in c)
            if price_td:
                price = parse_de_price(price_td.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        page = 0

        while True:
            url = DINGDONG_URL if page == 0 else f"{DINGDONG_URL}&page={page}"
            logging.info(f"DingDong.at: fetching {url}")
            try:
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"DingDong.at: HTTP {resp.status} – page {page}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"DingDong.at: fetch error {type(e).__name__}: {e}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"DingDong.at: {len(page_cards)} cards – page {page}")

            if not page_cards:
                break

            for listing_id, title, listing_url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)

                if price == 0.0:
                    logging.info(f"DingDong.at: skipped (price=0): {title}")
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
                        source="ding-dong.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            page += 1
            await asyncio.sleep(2)

        logging.info(f"DingDong: {len(results)} ads")
        return results
