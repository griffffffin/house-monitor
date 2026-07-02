"""Live smoke check: runs every real scraper against the live sites and
reports how many listings each one found - no price/blacklist filtering,
no email, no database writes.

This is deliberately kept OUT of the pytest suite (tests/test_house_monitor.py
is unit-only, no network calls, fast and deterministic by design). This
script is the opposite: it exists specifically to catch what unit tests
structurally cannot - a live site silently changing its HTML/JSON out from
under a scraper (see the Findheim incident in CLAUDE.md, where a site
redesign caused months of silent zero-result runs before anyone noticed).

Run from the project root:
    python3 -m scripts.live_smoke_check

Caveat: most scrapers apply their own price-range filter (EUR_PRICE_FROM/TO)
internally before returning results, so "0 listings" here can mean either
"this site's selectors broke" OR "genuinely nothing in the 3000-70000 EUR
band right now" - it's not a perfect oracle. Treat a 0 (or a big drop from
a source's usual count) as a prompt to check that source manually, not as
proof of breakage by itself.
"""

import asyncio
import logging
import time

import aiohttp

from house_monitor.monitor import SCRAPER_SUMMARY_LABELS
from house_monitor.scrapers import (
    BazarScraper,
    DerStandardScraper,
    DibeoScraper,
    DingDongScraper,
    FindheimScraper,
    FindMyHomeScraper,
    GoldgrubeScraper,
    ImmobIlienNetScraper,
    ImmobilienDeScraper,
    ImmodirektScraper,
    ImmiScraper,
    ImmokralleScraper,
    ImmoLive24Scraper,
    ImmoScout24Scraper,
    OhneMaklerScraper,
    RaiffeisenScraper,
    SonnbergerScraper,
    WillhabenScraper,
    WohnnetScraper,
)


async def _run_one(scraper) -> tuple:
    name = scraper.__class__.__name__
    start = time.monotonic()
    try:
        results = await scraper.fetch_listings()
        return (name, len(results), None, time.monotonic() - start)
    except Exception as e:
        return (name, 0, f"{type(e).__name__}: {e}", time.monotonic() - start)


async def main():
    # INFO-level so the per-page "N cards found" trail is visible when a
    # source gets flagged below - that's the actual diagnostic signal.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Same headers as the real production session (house_monitor/monitor.py's
    # run()) - several sites block the aiohttp default User-Agent outright,
    # so using different headers here would produce false "broken" positives.
    async with aiohttp.ClientSession(
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        timeout=aiohttp.ClientTimeout(total=30),
    ) as session:
        scrapers = [
            ImmoScout24Scraper(session),
            DibeoScraper(session),
            FindMyHomeScraper(session),
            WillhabenScraper(session),
            FindheimScraper(session),
            WohnnetScraper(session),
            DerStandardScraper(session),
            ImmodirektScraper(session),
            RaiffeisenScraper(session),
            OhneMaklerScraper(session),
            ImmobIlienNetScraper(session),
            ImmokralleScraper(session),
            ImmiScraper(session),
            BazarScraper(session),
            ImmobilienDeScraper(session),
            GoldgrubeScraper(session),
            ImmoLive24Scraper(session),
            DingDongScraper(session),
            SonnbergerScraper(session),
        ]

        print(f"Running {len(scrapers)} scrapers live, no filters applied by this script...\n")
        results = await asyncio.gather(*(_run_one(s) for s in scrapers))

    results.sort(key=lambda r: r[1])  # suspicious (0-result) ones float to the top

    name_width = max(len(SCRAPER_SUMMARY_LABELS.get(n, (n, ""))[0]) for n, _, _, _ in results)
    suspicious = []
    for cls_name, count, error, elapsed in results:
        label = SCRAPER_SUMMARY_LABELS.get(cls_name, (cls_name, "listings"))[0]
        if error:
            flag = "  ERROR"
            suspicious.append((label, error))
        elif count == 0:
            flag = "  <- 0 results, check manually"
            suspicious.append((label, "0 results"))
        else:
            flag = ""
        print(f"{label:<{name_width}}  {count:>4}  ({elapsed:5.1f}s){flag}")

    print()
    if suspicious:
        print(f"{len(suspicious)} source(s) worth a manual look:")
        for label, reason in suspicious:
            print(f"  - {label}: {reason}")
    else:
        print("All sources returned at least one listing.")


if __name__ == "__main__":
    asyncio.run(main())
