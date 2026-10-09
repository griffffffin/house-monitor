import logging
from datetime import datetime
from typing import List, Optional, Tuple

import aiohttp

from ..config import (
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    LYSTIO_API_URL,
    LYSTIO_BASE_URL,
    LYSTIO_FILTER,
    LYSTIO_PAGE_SIZE,
    LYSTIO_PROBE_FILTER,
)
from ..fetch import HTTPStatusError
from ..models import Listing


class LystioScraper:
    """
    lystio.at – houses, offices and commercial property for sale, read from
    the site's own search API (the same call its Next.js pages make; each
    category page embeds the payload as "ssrSearchPayload").
    Search: POST https://api.lystio.at/tenement/search with
            {"filter": LYSTIO_FILTER, "sort": …, "paging": {"page", "pageSize"}}
            — one query covers the three category pages the owner picked
            (type 3 = Haus, 20 = Büro, 5 = Gewerbe; the Gewerbe page also
            shows some apartments, mostly court auctions, which come along),
            the price range is applied by the server, pages are 1-indexed and
            the response's paging.pageCount says when to stop.
    Item:   id, title (projectTitle as a fallback), rentDisplay = [min, max,
            …] — the purchase price; min ≠ max only for a project card that
            groups several units, where min is used — and pathSegments
            (["kaufen", "haus", "steiermark", "leibnitz"]) for the URL. The
            per-m² price is a separate field (rentPerDisplay), never read.
    ID:     lys_<id>; URL: https://lystio.at/<pathSegments…>/<id>
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

    async def _search(self, search_filter: dict, page: int, page_size: int) -> dict:
        body = {
            "filter": search_filter,
            "sort": {"relevance": "desc"},
            "paging": {"page": page, "pageSize": page_size},
        }
        async with self.session.post(LYSTIO_API_URL, json=body) as resp:
            if resp.status != 200:
                raise HTTPStatusError(resp.status)
            data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}

    def _parse_items(self, data: dict) -> list:
        results = []
        for item in data.get("res") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            raw_id = item["id"]
            title = (item.get("title") or item.get("projectTitle") or "").strip()
            title = title or f"Lystio #{raw_id}"
            display = item.get("rentDisplay") or []
            try:
                price = float(display[0] or 0) if display else 0.0
            except (TypeError, ValueError):
                price = 0.0
            path = "/".join(str(segment) for segment in item.get("pathSegments") or [])
            url = f"{LYSTIO_BASE_URL}/{path}/{raw_id}" if path else LYSTIO_BASE_URL
            results.append((f"lys_{raw_id}", title, url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        self.coverage = None
        received = reported = 0
        results: List[Listing] = []
        seen_ids: set = set()
        page = 1
        while True:
            logging.info(f"Lystio: API page {page}")
            try:
                data = await self._search(LYSTIO_FILTER, page, LYSTIO_PAGE_SIZE)
            except HTTPStatusError as e:
                logging.warning(f"Lystio: {e} on API page {page}")
                break
            except Exception as e:
                logging.error(f"Lystio: error on API page {page}: {type(e).__name__}: {e}")
                self.incomplete = True
                break

            page_cards = self._parse_items(data)
            logging.info(f"Lystio: {len(page_cards)} items on API page {page}")
            if page == 1:
                # cardCount, not totalCount: a project card groups several units.
                reported = int((data.get("paging") or {}).get("cardCount") or 0)
            received += len(data.get("res") or [])
            for listing_id, title, url, price in page_cards:
                if listing_id in seen_ids:
                    continue
                seen_ids.add(listing_id)
                if price == 0.0:
                    logging.info(f"Lystio: skipped (price=0): {title}")
                    continue
                # The API already filtered the range; this is a safety net.
                if not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                    continue
                now = datetime.now().isoformat()
                results.append(
                    Listing(
                        id=listing_id,
                        title=title,
                        price=price,
                        url=url,
                        source="lystio.at",
                        first_seen=now,
                        last_seen=now,
                    )
                )
            page_count = (data.get("paging") or {}).get("pageCount") or 0
            if not page_cards or page >= page_count:
                break
            page += 1

        if not self.incomplete:
            self.coverage = (received, reported)
        logging.info(f"Lystio: {len(results)} listings")
        return results

    async def probe(self) -> list:
        """Page 1 of the same API search without the price range, parsed like
        a normal page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_items(await self._search(LYSTIO_PROBE_FILTER, 1, 20))
