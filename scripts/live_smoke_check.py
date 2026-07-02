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

Exit code: non-zero only if a scraper actually raised (a real fetch/parse
error) - a clean 0-result is reported but doesn't fail the run by itself,
since that's a judgment call this script can't make on its own (see the
caveat above). Also runs on a weekly schedule via
.github/workflows/live-check.yml, which writes the summary to the GitHub
Actions job summary in addition to stdout.
"""

import asyncio
import logging
import os
import sys
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
    zero_result = []
    errored = []
    summary_lines = []
    for cls_name, count, error, elapsed in results:
        label = SCRAPER_SUMMARY_LABELS.get(cls_name, (cls_name, "listings"))[0]
        if error:
            flag = "  ERROR"
            errored.append((label, error))
        elif count == 0:
            flag = "  <- 0 results, check manually"
            zero_result.append(label)
        else:
            flag = ""
        line = f"{label:<{name_width}}  {count:>4}  ({elapsed:5.1f}s){flag}"
        print(line)
        summary_lines.append(line)

    print()
    if errored or zero_result:
        print(f"{len(errored) + len(zero_result)} source(s) worth a manual look:")
        for label, reason in errored:
            print(f"  - {label}: {reason}")
        for label in zero_result:
            print(f"  - {label}: 0 results")
    else:
        print("All sources returned at least one listing.")

    # In CI (scheduled runs), surface the summary prominently regardless of
    # exit status, and any exit-code interpretation. A 0-result source isn't
    # necessarily broken (see the module docstring), so it's reported but
    # doesn't fail the run by itself - only an actual fetch error does, since
    # that's an unambiguous technical failure rather than a market-conditions
    # judgment call.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("### Live smoke check results\n\n```\n")
            f.write("\n".join(summary_lines))
            f.write("\n```\n")
            if zero_result:
                f.write(
                    f"\n**0-result sources** (not necessarily broken - "
                    f"could be no inventory in range right now): "
                    f"{', '.join(zero_result)}\n"
                )
            if errored:
                error_labels = ", ".join(label for label, _ in errored)
                f.write(f"\n**Sources with fetch errors**: {error_labels}\n")

    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
