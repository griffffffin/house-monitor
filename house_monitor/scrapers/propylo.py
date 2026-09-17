import asyncio
import html as _html
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, PROPYLO_QUERY, PROPYLO_URLS
from ..models import Listing, parse_de_price


class PropyloScraper:
    """
    at.propylo.com – a real-estate aggregator (it pulls listings from several
    popular Austrian portals), sales only; rentals live on the sister site
    at.flatspotter.com. Because it's an aggregator there's heavy overlap with the
    other scrapers, but the cross-platform duplicate detection
    (_already_seen_elsewhere) absorbs it.
    There's no country-wide listing page — listings are grouped per Bundesland,
    so we crawl all 9 state pages (config: PROPYLO_URLS), which together cover
    every listing in Austria. The site honors a server-side price filter +
    ascending-price sort (PROPYLO_QUERY), so the price range is pushed down to
    the server and results come sorted cheapest-first.
    Card: the <a href=".../verkaufsimmobilie/<id>"> element itself (the whole
      card is one link); title: its inner <h2>; price: div.price ("15.000 €",
      German format); URL: the (already absolute) href.
    ID: the numeric /verkaufsimmobilie/<id> (pro_<id>).
    Pagination: page 1 = the region URL, page N = region URL + "/N", with the
      query string appended AFTER the /N segment; past the last page the server
      returns HTTP 200 with 0 cards (no 404).
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        results = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/verkaufsimmobilie/(\d+)", href)
            if not m:
                continue
            listing_id = f"pro_{m.group(1)}"

            h2 = a.find("h2")
            title = _html.unescape(h2.get_text(strip=True)) if h2 else ""
            if not title:
                # a["title"] carries the same text and is a safe fallback
                title = _html.unescape(a.get("title", "")).strip()

            price = 0.0
            price_div = a.find("div", class_="price")
            if price_div:
                price = parse_de_price(price_div.get_text(strip=True))

            results.append((listing_id, title, href, price))
        return results

    async def _fetch_one_url(self, region_url: str) -> List[Listing]:
        url_results = []
        seen_ids: set = set()
        page = 1

        while True:
            path = region_url if page == 1 else f"{region_url}/{page}"
            url = f"{path}{PROPYLO_QUERY}"
            logging.info(f"Propylo.com: fetching {url}")
            try:
                async with self.session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        logging.warning(f"Propylo.com: HTTP {resp.status} – {url}")
                        break
                    html_text = await resp.text()
            except Exception as e:
                logging.error(f"Propylo.com: fetch error {type(e).__name__}: {e} – {url}")
                break

            page_cards = self._parse_cards(html_text)
            logging.info(f"Propylo.com: {len(page_cards)} cards – {url}")

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
                    logging.info(f"Propylo.com: skipped (price=0): {title}")
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
                        source="at.propylo.com",
                        first_seen=now,
                        last_seen=now,
                    )
                )

            # A page whose cards were all duplicates of an earlier page means the
            # pagination has run past the end and is repeating — stop.
            if new_on_page == 0:
                break

            page += 1
            await asyncio.sleep(2)

        return url_results

    async def fetch_listings(self) -> List[Listing]:
        # The 9 Bundesland pages are independent of each other, so we paginate
        # them concurrently and merge with a single shared seen_ids set (a
        # listing could in principle appear under two regions).
        per_url_results = await asyncio.gather(*(self._fetch_one_url(u) for u in PROPYLO_URLS))

        results = []
        seen_ids: set = set()
        for url_results in per_url_results:
            for listing in url_results:
                if listing.id in seen_ids:
                    continue
                seen_ids.add(listing.id)
                results.append(listing)

        logging.info(f"Propylo: {len(results)} listings")
        return results
