import asyncio
import logging
import re
from datetime import datetime
from typing import List

import aiohttp

from ..config import BAZAR_API_URL, BAZAR_PARAMS, BAZAR_PROBE_PARAMS
from ..fetch import HTTPStatusError
from ..models import Listing


class BazarScraper:
    """
    Bazar.at house listings, served via a JSON REST API (no HTML parsing).
    Response shape: {"content": [{"id", "common": {"title", "price": {"price"}}, "path"}], "last": bool}
    Some listings' "path" points to dibeo.at - those URLs are normalized to
    the clean /expose/<ID> format.
    Pagination: &page=N (0-indexed), stop when the response's "last" is true.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error, i.e. the
        # returned list may be missing pages — the monitor's same-day retry
        # loop re-runs the sources that set this flag.
        self.incomplete = False

    def _parse_items(self, data: dict) -> list:
        listings = data.get("content", [])
        results = []
        for hit in listings:
            if not isinstance(hit, dict) or "common" not in hit:
                continue

            listing_id = str(hit.get("id", ""))
            if not listing_id:
                continue

            listing_id = f"bazar_{listing_id}"
            common = hit.get("common", {})
            title = common.get("title", "No title")

            price_raw = (common.get("price") or {}).get("price", 0) or 0
            try:
                price = float(price_raw)
            except (ValueError, TypeError):
                price = 0.0

            path = hit.get("path", "")
            if path.startswith("http"):
                url = path
            elif path.startswith("/"):
                url = f"https://www.bazar.at{path}"
            else:
                url = path

            # If the URL points to dibeo.at, normalize to clean /expose/<ID> format
            if "dibeo.at" in url:
                m_id = re.search(r"[/?](\d{6,})", url)
                if m_id:
                    url = f"https://www.dibeo.at/expose/{m_id.group(1)}"

            results.append((listing_id, title, url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        results = []
        page = 0

        while True:
            params = {**BAZAR_PARAMS, "page": str(page)}
            logging.info(f"Bazar.at: fetching page {page + 1}")
            try:
                async with self.session.get(BAZAR_API_URL, params=params) as response:
                    if response.status != 200:
                        logging.error(f"Bazar.at: blocked with HTTP {response.status}")
                        break

                    data = await response.json(content_type=None)

                    if not data.get("content", []):
                        logging.info("Bazar.at: no more listings.")
                        break

                    items = self._parse_items(data)
                    logging.info(f"Bazar.at: {len(items)} listings on page {page + 1}.")

                    for listing_id, title, url, price in items:
                        if price > 0:
                            now = datetime.now().isoformat()
                            results.append(
                                Listing(
                                    id=listing_id,
                                    title=title,
                                    price=price,
                                    url=url,
                                    source="Bazar.at",
                                    first_seen=now,
                                    last_seen=now,
                                )
                            )

                    if data.get("last", True):
                        logging.info("Bazar.at: no more pages.")
                        break

                    page += 1
                    await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Bazar.at: error on page {page + 1}: {e}")
                self.incomplete = True
                break

        logging.info(f"Bazar: {len(results)} listings")
        return results

    async def probe(self) -> list:
        """Page 1 of the API search without the price range, parsed like a
        normal page — for the daily source health check (house_monitor/health.py)."""
        async with self.session.get(BAZAR_API_URL, params=BAZAR_PROBE_PARAMS) as response:
            if response.status != 200:
                raise HTTPStatusError(response.status)
            data = await response.json(content_type=None)
        return self._parse_items(data)
