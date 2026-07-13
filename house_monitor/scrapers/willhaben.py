import asyncio
import json
import logging
import re
from datetime import datetime
from typing import List

import aiohttp
from bs4 import BeautifulSoup

from ..config import EUR_PRICE_FROM, EUR_PRICE_TO, WILLHABEN_BASE_URL, WILLHABEN_URLS
from ..fetch import fetch_text
from ..models import Listing, parse_de_price


class WillhabenScraper:
    """
    Willhaben.at house listings.
    Willhaben is a Next.js app — the data arrives server-side-rendered inside
    the <script id="__NEXT_DATA__"> JSON blob, not in the rendered DOM.

    Structure: __NEXT_DATA__ -> props.pageProps.searchResult.advertSummaryList.advertSummary[]
    Each advertSummary contains:
      - id: listing ID
      - description: title
      - advertStatus.statusId: "active" / "inactive" etc.
      - attributes.attribute[]: [{name:"PRICE", values:["39000"]}, {name:"URL_SLUG",...}]
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        # True when the last fetch_listings() hit an error on some page
        # (missing results possible) — picked up by the monitor's same-day
        # retry loop.
        self.incomplete = False

    def _extract_attr(self, attributes: list, name: str) -> str:
        """Extracts the value of the named attribute from the attributes list."""
        for attr in attributes:
            if attr.get("name") == name:
                vals = attr.get("values", [])
                return vals[0] if vals else ""
        return ""

    def _parse_html_fallback_cards(self, html_text: str) -> list:
        soup = BeautifulSoup(html_text, "lxml")
        card_divs = soup.find_all("div", id=re.compile(r"^\d{8,}$"))
        results = []
        for card in card_divs:
            raw_id = card.get("id", "")
            if not raw_id.isdigit():
                continue
            listing_id = f"wh_{raw_id}"

            a_tag = card.find("a", attrs={"data-testid": f"search-result-entry-header-{raw_id}"})
            if not a_tag:
                continue
            href = a_tag.get("href", "")
            if not href:
                continue
            listing_url = href if href.startswith("http") else f"{WILLHABEN_BASE_URL}{href}"
            if "/andere-laender/" in listing_url:
                continue

            h = card.find(["h3", "h2"])
            if h:
                for svg in h.find_all("svg"):
                    svg.decompose()
                title = h.get_text(separator=" ", strip=True)
            else:
                title = f"Willhaben #{raw_id}"

            price_el = soup.find(attrs={"data-testid": f"search-result-entry-price-{raw_id}"})
            price = 0.0
            if price_el:
                price = parse_de_price(price_el.get_text(strip=True))

            results.append((listing_id, title, listing_url, price))
        return results

    def _parse_json_adverts(self, adverts: list) -> list:
        results = []
        for advert in adverts:
            raw_id = str(advert.get("id", ""))
            if not raw_id:
                continue
            listing_id = f"wh_{raw_id}"

            # Status: only active listings
            status = str(advert.get("advertStatus", {}).get("statusId", ""))
            if status and status.lower() not in ("active", ""):
                logging.info(f"Willhaben.at: inactive status='{status}' id={raw_id}, continuing.")

            attrs = advert.get("attributes", {}).get("attribute", [])

            # URL slug -> full URL
            slug = self._extract_attr(attrs, "URL_SLUG")
            if not slug:
                # Fallback: SEO_URL
                slug = self._extract_attr(attrs, "SEO_URL")
            if not slug:
                continue
            # URL_SLUG can be in "/iad/immobilien/..." or "immobilien/..." format
            if slug.startswith("http"):
                listing_url = slug
            elif slug.startswith("/"):
                listing_url = f"{WILLHABEN_BASE_URL}{slug}"
            else:
                listing_url = f"{WILLHABEN_BASE_URL}/iad/{slug}"

            # Filter out foreign listings
            if "/andere-laender/" in listing_url:
                continue

            # Title
            title = advert.get("description", "").strip()
            if not title:
                title = f"Willhaben #{raw_id}"

            # Price
            price_str = self._extract_attr(attrs, "PRICE")
            if not price_str:
                price_str = self._extract_attr(attrs, "PRICE_FOR_DISPLAY")
            price = parse_de_price(price_str)

            # Some Willhaben listings' PRICE attribute is a
            # per-square-meter price (e.g. € 6,700/m²) instead of
            # the total purchase price. If the property's size is
            # known and price × area > EUR_PRICE_TO, it's
            # definitely a per-m² price -> exclude it.
            if price > 0:
                living_area = 0.0
                for area_attr in (
                    "ESTATE_SIZE",
                    "PROPERTY_SIZE_LIVING_AREA",
                    "LIVING_AREA",
                    "USABLE_AREA",
                ):
                    area_str = self._extract_attr(attrs, area_attr)
                    if area_str:
                        try:
                            living_area = float(area_str.replace(",", "."))
                        except ValueError:
                            pass
                        if living_area > 0:
                            break
                if living_area > 0 and price * living_area > EUR_PRICE_TO:
                    logging.info(
                        f"Willhaben.at: excluded per-m² price "
                        f"(price={price:.0f}€/m², area={living_area:.0f}m², "
                        f"total≈{price * living_area:.0f}€): id={raw_id}"
                    )
                    continue

            results.append((listing_id, title, listing_url, price))
        return results

    async def fetch_listings(self) -> List[Listing]:
        results = []
        seen_ids: set = set()
        self.incomplete = False

        for base_url in WILLHABEN_URLS:
            page = 1
            logging.info(f"Willhaben.at: starting URL -> {base_url}")
            while True:
                url = f"{base_url}&page={page}"
                logging.info(f"Willhaben.at: fetching page {page} -> {url}")
                try:
                    status, html = await fetch_text(self.session, url)
                    if status != 200:
                        logging.warning(f"Willhaben.at: HTTP {status} on page {page}.")
                        break

                    # --- Primary: extract the __NEXT_DATA__ JSON ---
                    next_data_match = re.search(
                        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
                    )
                    adverts = []
                    if next_data_match:
                        try:
                            nd = json.loads(next_data_match.group(1))
                            adverts = (
                                nd.get("props", {})
                                .get("pageProps", {})
                                .get("searchResult", {})
                                .get("advertSummaryList", {})
                                .get("advertSummary", [])
                            )
                            logging.info(
                                f"Willhaben.at: __NEXT_DATA__ -> {len(adverts)} listings in the JSON."
                            )
                        except Exception as e:
                            logging.warning(f"Willhaben.at: __NEXT_DATA__ parse error: {e}")

                    # --- Secondary fallback: HTML DOM cards ---
                    if not adverts:
                        logging.info("Willhaben.at: __NEXT_DATA__ empty, falling back to HTML DOM.")
                        page_cards = self._parse_html_fallback_cards(html)
                        logging.info(f"Willhaben.at: HTML fallback -> {len(page_cards)} cards.")
                        if not page_cards:
                            logging.info("Willhaben.at: no more results on this URL.")
                            break
                        # HTML DOM processing
                        found_on_page = 0
                        for listing_id, title, listing_url, price in page_cards:
                            if listing_id in seen_ids:
                                continue
                            seen_ids.add(listing_id)
                            if price > 0 and not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                                continue
                            now = datetime.now().isoformat()
                            results.append(
                                Listing(
                                    id=listing_id,
                                    title=title,
                                    price=price,
                                    url=listing_url,
                                    source="willhaben.at",
                                    first_seen=now,
                                    last_seen=now,
                                )
                            )
                            found_on_page += 1
                        logging.info(f"Willhaben.at: HTML fallback -> {found_on_page} processed.")
                        if found_on_page == 0:
                            break
                        page += 1
                        await asyncio.sleep(2)
                        continue

                    # --- __NEXT_DATA__ processing ---
                    found_on_page = 0
                    for listing_id, title, listing_url, price in self._parse_json_adverts(adverts):
                        if listing_id in seen_ids:
                            continue
                        seen_ids.add(listing_id)

                        # Client-side price filter
                        if price > 0 and not (EUR_PRICE_FROM <= price <= EUR_PRICE_TO):
                            continue

                        now = datetime.now().isoformat()
                        results.append(
                            Listing(
                                id=listing_id,
                                title=title,
                                price=price,
                                url=listing_url,
                                source="willhaben.at",
                                first_seen=now,
                                last_seen=now,
                            )
                        )
                        found_on_page += 1

                    logging.info(f"Willhaben.at: {found_on_page} listings on page {page}.")

                    if found_on_page == 0 and len(adverts) == 0:
                        logging.info("Willhaben.at: no more results for this URL.")
                        break
                    elif found_on_page == 0:
                        logging.info(
                            "Willhaben.at: page had adverts but all already seen, stopping."
                        )
                        break

                    page += 1
                    await asyncio.sleep(2)

                except Exception as e:
                    logging.error(f"Willhaben.at: error on page {page}: {e}", exc_info=True)
                    self.incomplete = True
                    break

        logging.info(f"Willhaben: {len(results)} listings")
        return results
