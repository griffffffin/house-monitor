"""Configuration: price filter, blacklist, per-site URLs, and email settings."""

import os

DATA_FILE = "seen-houses.json"

BLACKLIST = [
    "urlaub",
    "sommerhaus",
    "badehütte",
    "reserviert",
    "stellplatz",
    "garagen",
    "weinkeller",
    "Erdkeller",
    "in Ungarn",
]

# Listings we skip but do NOT persist to the JSON database — if a "reserved"
# listing becomes available again, we'll still notify about it next run.
SKIP_NO_PERSIST = [
    "reserviert",
]

# Cross-platform duplicate detection (HouseMonitor._titles_similar): a title
# shorter than this (after normalization) only counts as "similar" on an exact
# match, never as a substring. Generic one-word titles like "Mobilheim" or
# "Haus" are contained in countless unrelated titles, and at a common round
# price they silently swallowed genuinely new listings/price drops.
TITLE_SUBSTRING_MIN_LEN = 20

# Only DB entries seen within this many days count as a live cross-platform
# twin in HouseMonitor._already_seen_elsewhere. A listing that vanished
# months ago can't be a copy of one that appears (or drops its price) today;
# without this window a stale entry could hide a new listing indefinitely.
# last_seen is refreshed for every fetched listing on every run, so live
# twins always stay well inside the window.
DUPLICATE_LOOKBACK_DAYS = 60

DATA_FILE = f"/opt/house-monitor/{DATA_FILE}"

if os.getenv("INVOCATION_ID"):  # Systemd service mode
    LOG_FILE = "/var/log/house-monitor/service.log"
else:
    LOG_FILE = "/opt/house-monitor/house-monitor.log"

# SMTP credentials come from a shared environment file (several projects on
# the same host use the same Gmail account) - see EnvironmentFile=/opt/secrets.env
# in the systemd unit. The .get() fallback ensures a missing env var (e.g.
# during test runs) doesn't raise at import/collection time.
EMAIL_CONFIG = {
    "smtp_server": os.environ.get("SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
    "sender_email": os.environ.get("SENDER_EMAIL", ""),
    "sender_password": os.environ.get("SENDER_PASSWORD", ""),
    "recipient_email": os.environ.get("RECIPIENT_EMAIL", ""),
}

# Same-day retry for sources whose fetch ended on an error (see the scrapers'
# `incomplete` flag and HouseMonitor._scrape_and_notify): wait time before
# each retry pass, in seconds. The main 16:00 email is never delayed by this —
# only the failed sources are re-fetched, and anything new they find goes out
# in a separate follow-up email the same day.
INCOMPLETE_RETRY_DELAYS = (1800, 1800, 3600)

# --- Price filter — change here to affect ALL scrapers ---
EUR_PRICE_FROM = 3000
EUR_PRICE_TO = 70000

# used by: ImmoScout24Scraper
IMMOSCOUT_URL = (
    "https://www.immobilienscout24.at/regional/oesterreich/haus-kaufen"
    f"/geringster-preis-zuerst?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}"
)

# used by: ImmiScraper
IMMI_BASE_URL = "https://immi.at"
IMMI_SEARCH_URL = (
    f"{IMMI_BASE_URL}/Immobilien-Suche"
    f"?type%5B%5D=h&offer%5B%5D=k"
    f"&price_from={EUR_PRICE_FROM}&price_to={EUR_PRICE_TO}"
    f"&sort=preis_aufsteigend"
)

# used by: BazarScraper
BAZAR_API_URL = "https://www.bazar.at/api/article/l/07-ha-ka/v"
BAZAR_PARAMS = {
    "term": "",
    "allShops": "true",
    "price.from": str(EUR_PRICE_FROM),
    "price.to": str(EUR_PRICE_TO),
    "size": "20",
    "sort": "sort.price,asc",
}

# used by: DibeoScraper
# Dibeo's own JSON API (the search page embeds its response for the first 25
# hits); the server applies the price range, `page` is 0-indexed.
DIBEO_BASE_URL = "https://www.dibeo.at"
DIBEO_API_URL = f"{DIBEO_BASE_URL}/api/realEstate/list"
DIBEO_PARAMS = {
    "category": "HAUS",
    "legalForm": "KAUF",
    "price.from": str(EUR_PRICE_FROM),
    "price.to": str(EUR_PRICE_TO),
    "sort": "id,desc",
    "size": "100",
}

# used by: FindMyHomeScraper
FINDMYHOME_BASE_URL = "https://www.findmyhome.at"
# pp=100 -> request up to 100 results per page so everything fits on one page
FINDMYHOME_URL = (
    "https://www.findmyhome.at/index.php"
    f"?id=14&1=1&module=select&land=AT&lang=de&h_e=1&prv={EUR_PRICE_FROM}&prb={EUR_PRICE_TO}&pp=100"
)

# used by: ImmoLive24Scraper
IMMOLIVE24_BASE_URL = "https://at.immolive24.com"
IMMOLIVE24_SEARCH_URL = f"{IMMOLIVE24_BASE_URL}/immobilien/search-results.html"
# Category_ID=228 -> Houses/Buy (Flynax-based portal). The first page must be
# fetched with POST, which sets the search filters in server-side session state
# keyed by PHPSESSID; subsequent pages are plain GET using the same session.
IMMOLIVE24_SEARCH_DATA = (
    "action=search&post_form_key=immobilien_quick"
    "&f%5BCategory_ID%5D=228&f%5Bbundesland%5D=0"
    f"&f%5Bkaufpreis%5D%5Bfrom%5D={EUR_PRICE_FROM}&f%5Bkaufpreis%5D%5Bto%5D={EUR_PRICE_TO}"
)

# used by: DingDongScraper
DINGDONG_BASE_URL = "https://www.ding-dong.at"
# field_kategorie_tid=5 -> House, field_miete_kauf_value=kauf -> for sale
DINGDONG_URL = (
    f"{DINGDONG_BASE_URL}/immobilien"
    "?field_inserent_value=All&field_kategorie_tid=5&field_miete_kauf_value=kauf"
    f"&field_preis_value={EUR_PRICE_FROM}&field_preis_value_1={EUR_PRICE_TO}"
    "&order=field_preis&sort=asc"
)

# used by: SonnbergerScraper
SONNBERGER_BASE_URL = "https://sonnberger.co.at"
# WordPress + Houzez theme, no server-side price filter parameter -> filtering
# happens entirely client-side (the site only sorts ascending by price).
SONNBERGER_URL = f"{SONNBERGER_BASE_URL}/wp/immobilienart/haeuser/?sortby=a_price"

# used by: PropyloScraper
PROPYLO_BASE_URL = "https://at.propylo.com"
# Aggregator, sales only (rentals live on the sister site at.flatspotter.com).
# There's no country-wide listing page — listings are grouped per Bundesland, so
# these 9 state pages together cover all of Austria. The site DOES honor a
# server-side price filter + ascending-price sort via query params, so we push
# the price range down to the server. Pagination is a trailing /N path segment
# placed BEFORE the query string (page 1 has no suffix).
_PROPYLO_REGIONS = [
    "burgenland",
    "karnten",
    "niederosterreich",
    "oberosterreich",
    "land-salzburg",
    "steiermark",
    "tirol",
    "vorarlberg",
    "wien",
]
PROPYLO_QUERY = f"?price_min={EUR_PRICE_FROM}&price_max={EUR_PRICE_TO}&ordering=price_asc"
PROPYLO_URLS = [f"{PROPYLO_BASE_URL}/immobilien/{r}" for r in _PROPYLO_REGIONS]
# Each Propylo card links to /verkaufsimmobilie/<id>, which redirects to the
# original ad on another portal. New cards are resolved one at a time, this
# many seconds apart (the site answers bursts with HTTP 429); a 429 is
# retried after PROPYLO_RESOLVE_BACKOFF × attempt seconds.
PROPYLO_RESOLVE_DELAY = 1.5
PROPYLO_RESOLVE_ATTEMPTS = 4
PROPYLO_RESOLVE_BACKOFF = 10.0

# used by: LystioScraper
LYSTIO_BASE_URL = "https://lystio.at"
LYSTIO_API_URL = "https://api.lystio.at/tenement/search"
# The site's own search filter (its category pages embed it as
# "ssrSearchPayload"): type 3 = Haus, 20 = Büro, 5 = Gewerbe — the three
# category pages the owner picked, in one query; "rent" is the purchase price
# range for rentType "buy".
LYSTIO_FILTER = {
    "type": [3, 20, 5],
    "rentType": ["buy"],
    "rent": [EUR_PRICE_FROM, EUR_PRICE_TO],
    "rentScope": "rent",
    "showPriceOnRequest": True,
    "all": True,
}
LYSTIO_PAGE_SIZE = 100
LYSTIO_PROBE_FILTER = {k: v for k, v in LYSTIO_FILTER.items() if k != "rent"}

# used by: HegerRealScraper
HEGERREAL_BASE_URL = "https://www.hegerreal.at"
# Justimmo-backed broker site, no server-side price filter parameter -> the whole
# inventory (houses/apartments/rentals) is listed on one page, filtering happens
# client-side. Rentals carry a "Miete" (rent) row instead of "Kaufpreis", so they
# naturally parse to price 0.0 and get dropped by the existing price==0 filter.
HEGERREAL_URL = f"{HEGERREAL_BASE_URL}/aktuelle-immobilien"

# used by: RaiffeisenScraper
RAIFFEISEN_BASE_URL = "https://www.raiffeisen-immobilien.at"
RAIFFEISEN_SEARCH_URL = (
    f"{RAIFFEISEN_BASE_URL}/en/properties"
    f"?sales_type=buy&category%5B%5D=house&price_to={EUR_PRICE_TO}&sort=price_asc"
)

# used by: ImmodirektScraper
IMMODIREKT_BASE_URL = "https://www.immodirekt.at"
IMMODIREKT_URLS = [
    f"https://www.immodirekt.at/haeuser-kaufen/oesterreich?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}&sort=PRICE_ASC",
    f"https://www.immodirekt.at/geschaeftslokale-kaufen/oesterreich?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}&sort=PRICE_ASC",
    f"https://www.immodirekt.at/sonstige-wohnimmobilien-kaufen/oesterreich?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}&sort=PRICE_ASC",
    f"https://www.immodirekt.at/sonstige-gewerbeimmobilien-kaufen/oesterreich?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}&sort=PRICE_ASC",
]

# used by: ImmobIlienNetScraper
IMMOBILIEN_NET_BASE = "https://www.immobilien.net"
IMMOBILIEN_NET_URLS = [
    f"https://www.immobilien.net/haeuser-kaufen/oesterreich?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}&sort=PRICE_ASC",
    f"https://www.immobilien.net/sonstige-wohnimmobilien-kaufen/oesterreich?primaryPriceFrom={EUR_PRICE_FROM}&primaryPriceTo={EUR_PRICE_TO}&sort=PRICE_ASC",
]

# used by: ImmokralleScraper
IMMOKRALLE_BASE_URL = "https://www.immokralle.com"
IMMOKRALLE_URLS = [
    f"https://www.immokralle.com/immobilien/at?q_ty=1&q_pr_min={EUR_PRICE_FROM}&q_pr_max={EUR_PRICE_TO}&sort_by=ik_price_1&f[0]=ik_form:haus",
    f"https://www.immokralle.com/immobilien/at?q_ty=1&q_pr_min={EUR_PRICE_FROM}&q_pr_max={EUR_PRICE_TO}&sort_by=ik_price_1&f[0]=ik_form:gesch%C3%A4ftslokal",
]

# used by: OhneMaklerScraper
OHNE_MAKLER_BASE_URL = "https://www.ohne-makler.at"
OHNE_MAKLER_URLS = [
    f"https://www.ohne-makler.at/immobilien/haus-kaufen/?price_min={EUR_PRICE_FROM}&price_max={EUR_PRICE_TO}",
    f"https://www.ohne-makler.at/immobilien/lagerhalle-kaufen/?price_min={EUR_PRICE_FROM}&price_max={EUR_PRICE_TO}",
]

# used by: WillhabenScraper
WILLHABEN_BASE_URL = "https://www.willhaben.at"
WILLHABEN_URLS = [
    f"https://www.willhaben.at/iad/immobilien/haus-kaufen/haus-angebote?sort=3&PRICE_FROM={EUR_PRICE_FROM}&PRICE_TO={EUR_PRICE_TO}",
    f"https://www.willhaben.at/iad/immobilien/ferienimmobilien-kaufen/ferienimmobilien-angebote?sort=3&PRICE_FROM={EUR_PRICE_FROM}&PRICE_TO={EUR_PRICE_TO}",
]

# used by: FindheimScraper
FINDHEIM_BASE_URL = "https://findheim.at"
FINDHEIM_URL = (
    "https://findheim.at/de/immobilien"
    "?f%5BbuyRentAll%5D=buy"
    f"&f%5BpriceTo%5D={EUR_PRICE_TO}"
    "&f%5Btypes%5D%5B%5D=house"
    "&f%5Btypes%5D%5B%5D=commercial_leisure"
    "&sort=priceAsc"
)

# used by: WohnnetScraper
WOHNNET_BASE_URL = "https://www.wohnnet.at"
WOHNNET_URL = (
    f"https://www.wohnnet.at/immobilien/haeuser"
    f"?intention=kauf&preis={EUR_PRICE_FROM}-{EUR_PRICE_TO}&sortierung=guenstigste-zuerst"
)

# used by: DerStandardScraper
DERSTANDARD_BASE_URL = "https://immobilien.derstandard.at"
DERSTANDARD_URL = (
    f"https://immobilien.derstandard.at/suche/oesterreich/kaufen-haus"
    f"?priceFrom={EUR_PRICE_FROM}&priceTo={EUR_PRICE_TO}&sorting=priceAscending"
)

# used by: ImmobilienDeScraper
IMMOBILIEN_DE_BASE_URL = "https://www.immobilien.de"
# Rebuilt on Next.js in 2026; read through its documented public REST API
# (/api/docs) — the Austria page only server-renders the 24 newest houses and
# ignores every query parameter. POST {API}/estates/search with this filter;
# the server applies the price range and paginates by cursor.
IMMOBILIEN_DE_API_URL = f"{IMMOBILIEN_DE_BASE_URL}/api/rest"
IMMOBILIEN_DE_SEARCH = {
    "category": "ausland",
    "objectKind": "haus",
    "marketingType": "kauf",
    "country": "at",
    "priceMin": EUR_PRICE_FROM,
    "priceMax": EUR_PRICE_TO,
    "sort": "newest",
    "limit": 50,
}

# used by: GoldgrubeScraper
GOLDGRUBE_BASE_URL = "https://www.goldgrube.at"
GOLDGRUBE_URLS = [
    "https://www.goldgrube.at/immobilien/haeuser-kaufen/1201.html",
    "https://www.goldgrube.at/immobilien/ferienimmobilien-kaufen/1211.html",
    "https://www.goldgrube.at/immobilien/gewerbeimmobilien-kaufen/1209.html",
]

# ---------------------------------------------------------------------------
# Daily source health check (house_monitor/health.py)
# ---------------------------------------------------------------------------
# Alert by email once a source has looked broken on this many consecutive
# daily checks; repeated every day while it stays broken.
HEALTH_MIN_CONSECUTIVE = 2
# Consecutive-failure counters + the date of the last check, kept next to the
# seen-DB so a service restart doesn't reset the streak.
HEALTH_STATE_FILE = os.path.join(os.path.dirname(DATA_FILE), "health-check-state.json")
# A probe that errors is retried this many times, this many seconds apart.
HEALTH_PROBE_ATTEMPTS = 3
HEALTH_PROBE_RETRY_DELAY = 5.0

# Probe targets: page 1 of each source's search WITHOUT the price range.
# The real search URLs carry 3000-70000 € server-side, so a working site can
# legitimately return 0 results there for weeks; the unfiltered page can't be
# empty unless the site or our parser broke. Sources whose normal URL has no
# server-side price filter (Goldgrube, Sonnberger, HegerReal) reuse it.
IMMOSCOUT_PROBE_URL = (
    "https://www.immobilienscout24.at/regional/oesterreich/haus-kaufen/geringster-preis-zuerst"
)
DIBEO_PROBE_PARAMS = {k: v for k, v in DIBEO_PARAMS.items() if not k.startswith("price.")}
FINDMYHOME_PROBE_URL = (
    "https://www.findmyhome.at/index.php?id=14&1=1&module=select&land=AT&lang=de&h_e=1&pp=100"
)
WILLHABEN_PROBE_URL = "https://www.willhaben.at/iad/immobilien/haus-kaufen/haus-angebote?sort=3"
FINDHEIM_PROBE_URL = (
    "https://findheim.at/de/immobilien"
    "?f%5BbuyRentAll%5D=buy"
    "&f%5Btypes%5D%5B%5D=house"
    "&f%5Btypes%5D%5B%5D=commercial_leisure"
    "&sort=priceAsc"
)
# Default sort: Wohnnet also lists foreign (mostly German) properties, and its
# cheapest-first page 1 is entirely German ones, which _parse_cards skips by
# design — the probe would look broken even though the parser works.
WOHNNET_PROBE_URL = "https://www.wohnnet.at/immobilien/haeuser?intention=kauf"
DERSTANDARD_PROBE_URL = (
    "https://immobilien.derstandard.at/suche/oesterreich/kaufen-haus?sorting=priceAscending"
)
IMMODIREKT_PROBE_URL = "https://www.immodirekt.at/haeuser-kaufen/oesterreich?sort=PRICE_ASC"
RAIFFEISEN_PROBE_URL = (
    f"{RAIFFEISEN_BASE_URL}/en/properties?sales_type=buy&category%5B%5D=house&sort=price_asc"
)
OHNE_MAKLER_PROBE_URL = "https://www.ohne-makler.at/immobilien/haus-kaufen/"
IMMOBILIEN_NET_PROBE_URL = "https://www.immobilien.net/haeuser-kaufen/oesterreich?sort=PRICE_ASC"
IMMOKRALLE_PROBE_URL = (
    "https://www.immokralle.com/immobilien/at?q_ty=1&sort_by=ik_price_1&f[0]=ik_form:haus"
)
IMMI_PROBE_URL = (
    f"{IMMI_BASE_URL}/Immobilien-Suche?type%5B%5D=h&offer%5B%5D=k&sort=preis_aufsteigend"
)
# Bazar keeps price.from: sorted ascending without it, the first page is all
# 0 € "Fixpreis" ads, which would read as an unparseable price.
BAZAR_PROBE_PARAMS = {k: v for k, v in BAZAR_PARAMS.items() if k != "price.to"}
IMMOBILIEN_DE_PROBE_SEARCH = {
    k: v for k, v in IMMOBILIEN_DE_SEARCH.items() if not k.startswith("price")
}
IMMOLIVE24_PROBE_DATA = (
    "action=search&post_form_key=immobilien_quick&f%5BCategory_ID%5D=228&f%5Bbundesland%5D=0"
)
DINGDONG_PROBE_URL = (
    f"{DINGDONG_BASE_URL}/immobilien"
    "?field_inserent_value=All&field_kategorie_tid=5&field_miete_kauf_value=kauf"
    "&order=field_preis&sort=asc"
)
PROPYLO_PROBE_URL = f"{PROPYLO_BASE_URL}/immobilien/niederosterreich?ordering=price_asc"
