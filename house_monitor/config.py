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
    "in Ungarn",
]

# Listings we skip but do NOT persist to the JSON database — if a "reserved"
# listing becomes available again, we'll still notify about it next run.
SKIP_NO_PERSIST = [
    "reserviert",
]

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
    "smtp_server": os.environ.get("HOUSE_MONITOR_SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("HOUSE_MONITOR_SMTP_PORT", "587")),
    "sender_email": os.environ.get("HOUSE_MONITOR_SENDER_EMAIL", ""),
    "sender_password": os.environ.get("HOUSE_MONITOR_SENDER_PASSWORD", ""),
    "recipient_email": os.environ.get("HOUSE_MONITOR_RECIPIENT_EMAIL", ""),
}

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
DIBEO_URL = (
    f"https://www.dibeo.at/obj/h-haus-kauf/cv?price.from={EUR_PRICE_FROM}&price.to={EUR_PRICE_TO}"
)

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
IMMOBILIEN_DE_URLS = [
    (
        f"https://www.immobilien.de/Ausland/Suchergebnisse-51834.html"
        f"?search._digest=true&search._filter=ausland&search.land=at"
        f"&search.typ=kaufen&search.preis_von={EUR_PRICE_FROM}&search.preis_bis={EUR_PRICE_TO}"
        f"&search.objektart=rendite"
    ),
    (
        f"https://www.immobilien.de/Ausland/Suchergebnisse-51834.html"
        f"?search._digest=true&search._filter=ausland&search.land=at"
        f"&search.typ=kaufen&search.preis_von={EUR_PRICE_FROM}&search.preis_bis={EUR_PRICE_TO}"
        f"&search.objektart=gastronomie_hotel"
    ),
    (
        f"https://www.immobilien.de/Ausland/Suchergebnisse-51834.html"
        f"?search._digest=true&search._filter=ausland&search.land=at"
        f"&search.typ=kaufen&search.preis_von={EUR_PRICE_FROM}&search.preis_bis={EUR_PRICE_TO}"
        f"&search.objektart=freizeit"
    ),
    (
        f"https://www.immobilien.de/Ausland/Suchergebnisse-51834.html"
        f"?search._digest=true&search._filter=ausland&search.land=at"
        f"&search.typ=kaufen&search.preis_von={EUR_PRICE_FROM}&search.preis_bis={EUR_PRICE_TO}"
        f"&search.objektart=haus"
    ),
]

# used by: GoldgrubeScraper
GOLDGRUBE_BASE_URL = "https://www.goldgrube.at"
GOLDGRUBE_URLS = [
    "https://www.goldgrube.at/immobilien/haeuser-kaufen/1201.html",
    "https://www.goldgrube.at/immobilien/ferienimmobilien-kaufen/1211.html",
    "https://www.goldgrube.at/immobilien/gewerbeimmobilien-kaufen/1209.html",
]
