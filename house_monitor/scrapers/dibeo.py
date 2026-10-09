import json
import logging
from datetime import datetime
from typing import List, Optional, Tuple

import aiohttp

from ..config import (
    DIBEO_API_URL,
    DIBEO_BASE_URL,
    DIBEO_PARAMS,
    DIBEO_PROBE_PARAMS,
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
)
from ..fetch import HTTPStatusError, fetch_text
from ..models import Listing


class DibeoScraper:
    """
    dibeo.at houses for sale, read from the site's own JSON API — the search
    page server-renders this very API response for its first 25 hits. The old
    HTML scraper stopped after page 1 (its &page=N pagination didn't advance),
    so it saw 29 of the 54 in-range houses on 2026-10-09.
    API:   GET /api/realEstate/list with DIBEO_PARAMS (category=HAUS,
           legalForm=KAUF, the price range — filtered by the server —
           sort=id,desc, size=100). Spring-style pages: `content` list, `last`
           flag; `page` is 0-indexed.
    Item:  id, title, minPrice/maxPrice (a range only for grouped project
           units — minPrice is used), pricePerSqMeter (true: the figure is a
           per-m² price, so the ad is skipped).
    ID:    dibeo_<id>; URL: https://www.dibeo.at/expose/<id> (as before).
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() ended on an error, i.e. the
        # returned list may be missing pages — the monitor's same-day retry
        # loop re-runs the sources that set this flag.
        self.incomplete = False
        # (items received, the site's own total) from the last complete
        # fetch — the daily error check compares the two (health.py).
        self.coverage: Optional[Tuple[int, int]] = None

    async def _get_page(self, params: dict) -> dict:
        status, body = await fetch_text(self.session, DIBEO_API_URL, params=params)
        if status != 200:
            raise HTTPStatusError(status)
        data = json.loads(body) if body.strip() else {}
        return data if isinstance(data, dict) else {}

    def _parse_items(self, data: dict) -> list:
        results = []
        for item in data.get("content") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if item.get("pricePerSqMeter"):
                logging.info(f"Dibeo.at: skipped per-m² price: {item.get('title')}")
                continue
            raw_id = item["id"]
            title = (item.get("title") or "").strip() or f"Dibeo #{raw_id}"
            try:
                price = float(item.get("minPrice") or 0)
            except (TypeError, ValueError):
                price = 0.0
            results.append((f"dibeo_{raw_id}", title, f"{DIBEO_BASE_URL}/expose/{raw_id}", price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        self.coverage = None
        received = reported = 0
        results: List[Listing] = []
        seen_ids: set = set()
        page = 0
        while True:
            logging.info(f"Dibeo.at: API page {page}")
            try:
                data = await self._get_page({**DIBEO_PARAMS, "page": str(page)})
            except HTTPStatusError as e:
                logging.warning(f"Dibeo.at: {e} on API page {page}")
                break
            except Exception as e:
                logging.error(f"Dibeo.at: error on API page {page}: {type(e).__name__}: {e}")
                self.incomplete = True
                break

            page_cards = self._parse_items(data)
            logging.info(f"Dibeo.at: {len(page_cards)} items on API page {page}")
            if page == 0:
                reported = int(data.get("totalElements") or 0)
            received += len(data.get("content") or [])
            for listing_id, title, url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                # The API already filtered the range; this is a safety net.
                if not (price > 0 and EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                    continue
                now = datetime.now().isoformat()
                results.append(
                    Listing(
                        id=listing_id,
                        title=title,
                        price=price,
                        url=url,
                        source="Dibeo.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )
            if data.get("last", True) or not page_cards:
                break
            page += 1

        if not self.incomplete:
            self.coverage = (received, reported)
        logging.info(f"Dibeo: {len(results)} listings")
        return results

    async def probe(self) -> list:
        """Page 0 of the same API search without the price range, parsed like
        a normal page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_items(await self._get_page({**DIBEO_PROBE_PARAMS, "page": "0"}))
