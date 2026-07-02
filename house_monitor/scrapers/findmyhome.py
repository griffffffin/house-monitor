import asyncio
import logging
import re
from datetime import datetime
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup

from ..config import FINDMYHOME_BASE_URL, FINDMYHOME_URL
from ..models import Listing, parse_de_price


class FindMyHomeScraper:
    """
    FindMyHome.at is a classic server-rendered PHP site.
    Listings are in <h3 class="obj_list"> tags with links like /5549779.
    Price is in a <strong>Kauf: </strong><br>30.000,- € pattern.
    Pagination: <link rel="next"> in <head> with &entry=10, &entry=20, ...
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _parse_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "html.parser")
        title_tags = soup.select("h3.obj_list a")
        results = []
        for link in title_tags:
            href = link.get("href", "")
            if not href:
                continue

            # Absolute URL
            if href.startswith("http"):
                full_url = href
            else:
                full_url = FINDMYHOME_BASE_URL + href

            # ID: last path segment, e.g. /5549779 -> 5549779
            raw_id = href.strip("/").split("/")[-1].split("?")[0]
            if not raw_id.isdigit():
                continue
            listing_id = f"fmh_{raw_id}"

            # Title: text of the <a> tag, strip trailing "..."
            title = link.get_text(strip=True).rstrip(".")

            # Price: find the parent listing block and look for "Kauf:"
            # Walk up to the row-level div and search within it
            price = 0.0
            parent = link.find_parent("div", class_=re.compile(r"col-xs-12.*col-sm-9"))
            if parent:
                # Find the price row: contains "Kauf:"
                price_divs = parent.find_all("div", class_=re.compile(r"col-xs-4"))
                for div in price_divs:
                    text = div.get_text()
                    if "Kauf" in text or "€" in text:
                        price = parse_de_price(text)
                        if price > 0:
                            break

            results.append((listing_id, title, full_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        next_url: Optional[str] = FINDMYHOME_URL
        page = 1

        while next_url:
            logging.info(f"FindMyHome.at: fetching page {page} -> {next_url}")
            try:
                async with self.session.get(next_url) as response:
                    if response.status != 200:
                        logging.error(f"FindMyHome.at: blocked with HTTP {response.status}")
                        break

                    # FindMyHome uses ISO-8859-1 encoding
                    raw = await response.read()
                    html = raw.decode("iso-8859-1", errors="replace")
                    page_cards = self._parse_cards(html)
                    if not page_cards:
                        logging.info("FindMyHome.at: no listings found on page.")
                        break

                    found_on_page = 0
                    for listing_id, title, full_url, price in page_cards:
                        if not title:
                            continue
                        if price > 0:
                            now = datetime.now().isoformat()
                            results.append(
                                Listing(
                                    id=listing_id,
                                    title=title,
                                    price=price,
                                    url=full_url,
                                    source="FindMyHome.at",
                                    first_seen=now,
                                    last_seen=now,
                                )
                            )
                            found_on_page += 1

                    logging.info(f"FindMyHome.at: {found_on_page} listings on page {page}.")

                    # Stop when all results collected (parse total count)
                    soup = BeautifulSoup(html, "html.parser")
                    total_count = 0
                    found_text = soup.find(string=re.compile(r"\d+ Immobilien"))
                    if found_text:
                        m = re.search(r"(\d+)", str(found_text))
                        if m:
                            total_count = int(m.group(1))

                    if total_count > 0 and len(results) >= total_count:
                        logging.info(f"FindMyHome.at: all {total_count} listings collected. Done.")
                        next_url = None
                    else:
                        next_link = soup.find("link", rel="next")
                        if next_link and next_link.get("href"):
                            next_url = next_link["href"]
                            page += 1
                            await asyncio.sleep(2)
                        else:
                            logging.info("FindMyHome.at: no more pages.")
                            next_url = None

            except Exception as e:
                logging.error(f"FindMyHome.at: error on page {page}: {e}")
                break

        logging.info(f"FindMyHome: {len(results)} listings")
        return results
