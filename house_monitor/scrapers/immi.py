import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, IMMI_SEARCH_URL
from ..models import Listing, parse_de_price


class ImmiScraper:
    """
    Immi.at house listings (HTML, server-rendered).
    Cards: section.teasers article.teaser, id attribute holds the raw ID
    Title: .description h3, falls back to the first span inside the h2 link
    Price: .infos > div > strong, e.g. "€ 60.000"
    Pagination: &page=N, stop once "Seite X von Y" reports the last page
    """

    BASE_URL = "https://immi.at"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "html.parser")
        articles = soup.select("section.teasers article.teaser")
        results = []
        for article in articles:
            raw_id = article.get("id", "")  # e.g. "immo_7-5542219"
            listing_id = f"immi_{raw_id}"

            # URL
            link = article.select_one("h2 > a")
            if not link:
                continue
            href = link.get("href", "")
            listing_url = self.BASE_URL + href if href.startswith("/") else href

            # Title: h3 inside the description, fallback: first span in the h2 link
            title_tag = article.select_one(".description h3")
            if title_tag and title_tag.text.strip():
                title = title_tag.text.strip()
            else:
                spans = link.select("span")
                title = spans[0].text.strip() if spans else "No title"

            # Price: <strong>€ 60.000</strong> inside .infos > div
            # Excludes the coloredBox strong (it has a class attribute)
            price = 0.0
            price_strong = article.select_one(".infos > div > strong")
            if price_strong:
                price = parse_de_price(price_strong.text)

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        page = 1

        while True:
            url = f"{IMMI_SEARCH_URL}&page={page}"
            logging.info(f"Immi.at: fetching page {page} -> {url}")

            try:
                async with self.session.get(url) as response:
                    if response.status != 200:
                        logging.error(f"Immi.at: HTTP {response.status} on page {page}")
                        break

                    html = await response.text()
                    page_cards = self._parse_cards(html)
                    if not page_cards:
                        logging.info(f"Immi.at: no listings on page {page}, stopping.")
                        break

                    logging.info(f"Immi.at: {len(page_cards)} listings on page {page}.")

                    for listing_id, title, listing_url, price in page_cards:
                        if price <= 0:
                            logging.debug(f"Immi.at: could not parse price: {listing_id}")
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
                                source="immi.at",
                                first_seen=now,
                                last_seen=now,
                            )
                        )

                    # Pagination: "Seite X von Y" (page X of Y) -> stop once X >= Y
                    soup = BeautifulSoup(html, "html.parser")
                    page_info = soup.select_one("nav.pagination > div:first-child")
                    if page_info:
                        m_pg = re.search(r"Seite\s+(\d+)\s+von\s+(\d+)", page_info.text)
                        if m_pg:
                            cur_pg = int(m_pg.group(1))
                            tot_pg = int(m_pg.group(2))
                            if cur_pg >= tot_pg:
                                logging.info(f"Immi.at: last page ({cur_pg}/{tot_pg}), stopping.")
                                break
                    else:
                        # Couldn't find the page number -> stop, to be safe
                        break

                    page += 1
                    await asyncio.sleep(1)

            except Exception as e:
                logging.error(f"Immi.at: error on page {page}: {e}")
                break

        logging.info(f"Immi: {len(results)} ads")
        return results
