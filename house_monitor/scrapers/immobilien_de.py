import logging
from datetime import datetime
from typing import List, Optional, Tuple

import aiohttp

from ..config import (
    EUR_PRICE_FROM,
    EUR_PRICE_TO,
    IMMOBILIEN_DE_API_URL,
    IMMOBILIEN_DE_BASE_URL,
    IMMOBILIEN_DE_PROBE_SEARCH,
    IMMOBILIEN_DE_SEARCH,
)
from ..fetch import HTTPStatusError, fetch_page
from ..models import Listing


class ImmobilienDeScraper:
    """
    immobilien.de – German real estate portal, Austrian houses, read through
    its documented public REST API (/api/docs; the site was rebuilt on Next.js
    in 2026, and its Austria page only server-renders the 24 newest houses
    with every query parameter ignored).
    Search: POST /api/rest/estates/search with a JSON filter (IMMOBILIEN_DE_SEARCH:
            Austria, houses, purchase, the price range) — the server filters,
            keyset pagination via the opaque `cursor` / `nextCursor` (null when
            exhausted).
    CSRF:   double-submit cookie: any GET (/health/check) sets `csrf-token`,
            echoed back as the `x-csrf-token` header.
    Item:   legacyId, title, purchasePrice ("16900.00", null if on request),
            country.
    ID:     imde_<legacyId> (the same numbering as the old /ausland/<id> URLs)
    URL:    https://www.immobilien.de/expose/<legacyId>
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

    async def _csrf_token(self) -> str:
        """Let the API set its CSRF cookie and return the value to echo back."""
        await fetch_page(self.session, f"{IMMOBILIEN_DE_API_URL}/health/check")
        jar = getattr(self.session, "cookie_jar", ())
        return next((cookie.value for cookie in jar if cookie.key == "csrf-token"), "")

    async def _search(self, body: dict, token: str) -> dict:
        async with self.session.post(
            f"{IMMOBILIEN_DE_API_URL}/estates/search",
            json=body,
            headers={"x-csrf-token": token},
        ) as resp:
            if resp.status != 200:
                raise HTTPStatusError(resp.status)
            data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}

    def _parse_items(self, data: dict) -> list:
        results = []
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not item.get("legacyId"):
                continue
            if item.get("country") != "at":
                continue
            raw_id = item["legacyId"]
            title = (item.get("title") or "").strip() or f"Immobilien.de #{raw_id}"
            try:
                price = float(item.get("purchasePrice") or 0)
            except (TypeError, ValueError):
                price = 0.0
            results.append(
                (f"imde_{raw_id}", title, f"{IMMOBILIEN_DE_BASE_URL}/expose/{raw_id}", price)
            )
        return results

    async def fetch_listings(self) -> List[Listing]:
        self.incomplete = False
        self.coverage = None
        received = reported = 0
        results: List[Listing] = []
        seen_ids: set = set()
        # count=True makes the API include the search's `total`.
        body = dict(IMMOBILIEN_DE_SEARCH, count=True)
        page = 1
        try:
            token = await self._csrf_token()
            while True:
                logging.info(f"Immobilien.de: API search page {page}")
                data = await self._search(body, token)
                page_cards = self._parse_items(data)
                logging.info(f"Immobilien.de: {len(page_cards)} items – page {page}")
                if page == 1:
                    reported = int(data.get("total") or 0)
                received += len(data.get("items") or [])
                for listing_id, title, listing_url, price in page_cards:
                    if listing_id in seen_ids:
                        continue
                    seen_ids.add(listing_id)
                    if price == 0.0:
                        logging.info(f"Immobilien.de: skipped (price=0): {title}")
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
                            url=listing_url,
                            source="immobilien.de",
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                if not data.get("nextCursor") or not page_cards:
                    break
                body["cursor"] = data["nextCursor"]
                page += 1
        except HTTPStatusError as e:
            logging.warning(f"Immobilien.de: {e} on API page {page}")
        except Exception as e:
            logging.error(f"Immobilien.de: fetch error {type(e).__name__}: {e}")
            self.incomplete = True

        if not self.incomplete:
            self.coverage = (received, reported)
        logging.info(f"Immobilien.de: {len(results)} ads")
        return results

    async def probe(self) -> list:
        """Page 1 of the same API search without the price range, parsed like
        a normal page — for the daily source health check (house_monitor/health.py)."""
        return self._parse_items(
            await self._search(IMMOBILIEN_DE_PROBE_SEARCH, await self._csrf_token())
        )
