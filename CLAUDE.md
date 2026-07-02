Projekt: house_monitor (csomag, korábban egyetlen house-monitor.py szkript volt)
Asyncio-alapú Python scraper, ami osztrák ingatlanoldalakat figyel és emailben értesít új/olcsóbb hirdetésekről.
Produkciós helyszín:

Csomag: /opt/house-monitor/house_monitor/ (lásd lent a "Csomagstruktúra" szakaszt)
Belépési pont: python3 -m house_monitor.main (ExecStart a systemd unitban)
Log: tail -f /var/log/house-monitor/service.log
Service: sudo systemctl status/restart house-monitor
DB: /opt/house-monitor/seen-houses.json

Árszűrő: EUR_PRICE_FROM = 3000, EUR_PRICE_TO = 70000
Blacklist (ezekre nem értesít): urlaub, sommerhaus, badehütte, reserviert, stellplatz, garagen, weinkeller
SKIP_NO_PERSIST (kihagyja, de nem menti DB-be, hogy ha megszűnik a "reserviert" állapot, értesítsen): reserviert
19 aktív scraper:
ImmoScout24, Dibeo, FindMyHome, Willhaben, Findheim, Wohnnet, DerStandard, Immodirekt, Raiffeisen-Immobilien, OhneMakler, ImmobIlienNet, Immokralle, Immi, Bazar, ImmobilienDe, Goldgrube, ImmoLive24, DingDong, Sonnberger
ID prefixek (seen-houses.json-ben): is24_, dibeo_, fmh_, wh_, fh_, wn_, ds_, imd_, ri_, om_, inet_, ik_, immi_, bazar_, imde_, gg_, il24_, dd_, sb_
Kereszt-platform duplikáció-szűrés: _already_seen_elsewhere() — azonos ár + hasonló cím esetén nem küld dupla emailt
Lapozási minták scraperenként:

?page=N — Raiffeisen, Immodirekt, Willhaben, Wohnnet, DerStandard, OhneMakler, Findheim, ImmobIlienNet, Immokralle, Immi
&page=N (0-indexelt, state nélküli GET) — DingDong (Drupal Views tábla)
/page/N/ (WordPress szabványos, utolsó után HTTP 404) — Sonnberger
?from=N (25/lap) — (Urbanhome volt, eltávolítva)
?p=N — Goldgrube
&block=N — ImmobilienDe
&entry=N — FindMyHome
REST API JSON — Bazar
__NEXT_DATA__ JSON SSR — Willhaben (fallback: HTML DOM)
/search-results/indexN.html — ImmoLive24 (session-alapú, ld. lent)

Fontos scraper-specifikus tudás:

Goldgrube: raw bytes dekódolása a decode_utf8_or_latin1() helper függvénnyel (house_monitor/models.py) — mindig UTF-8-at próbál előbb, mert a szerver néha tévesen iso-8859-1-et deklarál olyan válaszra, aminek a bájtjai valójában UTF-8-ak (a latin-1 dekódolás ezt simán, hiba nélkül elfogadná, és mojibake-et eredményezne, pl. "GrÃ¼nen" "Grünen" helyett)
Ár-parszolás: 2026-07-02 óta mind a 18 szöveg-alapú scraper (minden scraper, kivéve Bazar) a közös parse_de_price(text) helperen (house_monitor/models.py) keresztül olvassa ki a németes formátumú árat (pont=ezres elválasztó, vessző=tizedes) — korábban ~11 helyen szó szerint ismétlődött ugyanez a regex+replace logika, most egy helyen van tesztelve. Bazar egyedüli kivétel: közvetlenül a JSON API számértékét castolja (nincs szöveges ár-string, nem is kell neki a helper). ImmoScout24 és Dibeo megtartja a saját kártya-/karakterlánc-szűkítő logikáját (ImmoScout24: több ár-jellegű `<li>` közül kell kiválasztani a nem `/m²`-eset; Dibeo: a kártya teljes szövegében kell €-jel közelében keresni a számot, mert egy szűkítés nélküli parse_de_price az első számsorozatot venné, ami tévesen a címben szereplő házszám/irányítószám is lehetne) — de a végső szám-normalizálást (pont/vessző konverzió, float-tá alakítás) már ők is a helperre bízzák, csak a "melyik szövegrészt nézzük" döntés maradt egyedi. A korábban dokumentált 4 "történelmi maradvány" (Findheim, FindMyHome, Immi, Wohnnet) migrálva lett — mindegyiket egyenként leteszteltem edge case-ekre migráció előtt (pl. FindMyHome "30.000,-" trailing-dash formátuma), a 68 teszt ezután is zöld maradt.
Willhaben: __NEXT_DATA__ JSON-ből olvas, m²-ár szűrés (ha ár × terület > EUR_PRICE_TO, kizárja)
Findheim: szerver oldali árszűrő megbízhatatlan → kliens oldali biztonsági szűrő a fő loopban. 2026-07-01: a site Tailwind-redesignt kapott, a korábbi kártya-szelektor (div.group + "rounded-md") és ár-konténer (div.z-[1]/right-0/bottom-0) megszűnt működni, emiatt HÓNAPOKIG csendben 0 találatot adott (holott valós, ártartományba eső hirdetések voltak). Javítva: kártya = div.group + "overflow-hidden" + "border" a class-listában; ár = a cím melletti <p class="font-semibold ...">€ X</p>. Tanulság: egy scraper "0 hirdetés" eredménye NEM automatikusan azt jelenti, hogy nincs kínálat — ellenőrizni kell élesben (kártyaszám szűrő nélkül, majd ár-parszolás), mert egy site-redesign ugyanígy csendben eltörheti a szelektorokat.
Immokralle: urllib-et használ aiohttp helyett (a [ ] karakterek URL-enkódolása miatt)
Urbanhome.at: eltávolítva — ASP.NET session-alapú, automatikusan nem scrапelható
Kurier (immo.kurier.at): eltávolítva 2026-07-01-én — a site ingatlan-rovata megszűnt, minden /suche keresés HTTP 302-vel a dibeo.at/obj/miete/v (BÉRLÉS, nem eladás) oldalra irányít át (utm_campaign=immokurier_facade), függetlenül a régió/típus paraméterektől. A 27 URL-es (9 tartomány × 3 típus) scraper strukturálisan halott volt, sosem adhatott érdemi találatot, csak feleslegesen lassította a futást.
ImmoLive24: Flynax-alapú portál, Category_ID=228 = Häuser/Kauf. Az 1. oldalt POST-tal kell lekérni (post_form_key=immobilien_quick, f[kaufpreis][from]/[to]), ez állítja be a szűrést a PHPSESSID session state-ben; a további oldalak sima GET-tel jönnek (/immobilien/search-results/indexN.html), a közös aiohttp.ClientSession cookie-jarja tartja a session-t. Utolsó oldal után 0 kártya, HTTP 200 (nincs explicit "nincs több oldal" jelzés). A cím (h2 > a, "Haus, PLZ, Ort") kevésbé informatív, mint a kártyán belüli leíró szöveg (td.fields első div-je) — ezt használjuk title-ként, a címre csak fallback.
DingDong: Drupal Views tábla (table.views-table), partnere az immokralle.com-nak (a footerben hivatkozik rá), de a keresztplatform-duplikáció-szűrés (_already_seen_elsewhere) kezeli az esetleges átfedést. Nincs numerikus hirdetés-ID a linkben, csak egyedi slug ("/immobilien/<slug>") — ez a slug az azonosító (dd_<slug>). Lapozás state nélküli: &page=N (0-indexelt), minden oldal a teljes szűrő query stringgel jön, nincs szükség session/cookie-ra (ellentétben az ImmoLive24-gyel).
Sonnberger: WordPress + Houzez téma, nincs szerver oldali árszűrő paraméter (csak ?sortby=a_price árnövekvő rendezés) — teljesen kliens oldali szűrés, mint Findheimnél. "Reserviert"/"Verkauft"/"auf Anfrage" állapotú hirdetéseknél a lista nézetben az ár helyén ez a szöveg jelenik meg számjegy nélkül, így a parse_de_price természetesen 0.0-t ad rájuk, amit a meglévő ár==0 kliens szűrő már úgyis kizár — nincs szükség külön reserved/sold kezelésre. ID: kártyán belüli [data-listid] attribútum (sb_<id>). Lapozás: /page/N/?sortby=a_price, utolsó oldal után HTTP 404 jön (nem 0 kártya).

Email: Gmail SMTP — EMAIL_CONFIG mostantól env változókból olvas (HOUSE_MONITOR_SMTP_SERVER/PORT/SENDER_EMAIL/SENDER_PASSWORD/RECIPIENT_EMAIL), nincs hardcode-olt hitelesítő adat a kódban. A tényleges értékek egy megosztott, `/opt/secrets.env` fájlban vannak (ezt a house-monitor és a product-monitor projekt is használja ugyanahhoz a Gmail fiókhoz — systemd `EnvironmentFile=/opt/secrets.env` tölti be produkcióban). A `.get()` fallback (üres string) miatt hiányzó env változó esetén sem dobódik kivétel import/teszt-collection időben.
Email body dizájn: _build_email_body() forrás szerint csoportosít (a testvér-projekt, product-monitor.py mintájára) — dekorált "==== forrás (N db) ====" fejléc forrásonként (14 "="-jel), "-" * 148 elválasztó a blokkok előtt/között/után, számozott lista, forrás-neve szerint ABC sorrendben; egy-egy forráson belül az új hirdetések (ár szerint növekvő) az árváltozások előtt jönnek. Nincs emoji a szövegben. Az ár ezres elválasztója szóköz (_fmt_count helper, ugyanaz mint a konzol-összegzőknél). A forrás-fejléc a Listing.source nyers értéke helyett a SOURCE_DISPLAY_NAMES szótáron át a konzolon már használt rövid nevet mutatja (pl. "sonnberger.co.at" -> "Sonnberger"). Subject: "Ingatlanok: {N} db".
Ütemezés: 2026-07-02 óta élesítve — _seconds_until_1600() + asyncio.sleep() a while True loop elején, a folyamat maga sosem lép ki (nincs break). A systemd unit `Restart=always` + `RestartSec=60` + `StartLimitIntervalSec=0` (nincs restart-limit) gondoskodik róla, hogy egy váratlan crash után is újrainduljon; mivel _seconds_until_1600() minden induláskor újraszámol, ha a hiba már aznap 16:00 után történt, a következő próbálkozás automatikusan másnapra csúszik.
Lock: /tmp/house-monitor.lock — megakadályozza a párhuzamos futást.
HTTP timeout: a session-szintű alapértelmezett 30s (aiohttp.ClientTimeout a ClientSession létrehozásakor a run()-ban) — korábban csak 4 scraper állított be explicit timeout-ot, a többi a 300s-os aiohttp default-ra hagyatkozott

**Scraper-futtatás párhuzamosítása:**
- A 19 scraper `asyncio.gather(*(s.fetch_listings() for s in scrapers), return_exceptions=True)` segítségével párhuzamosan fut a run()-ban (korábban egymás után, szigorúan szekvenciálisan futottak, ami feleslegesen lassította a teljes futást, holott a scraperek független domaineket hívnak és nincs köztük megosztott állapot a közös aiohttp.ClientSession-ön kívül)
- `return_exceptions=True` miatt egy kivételt dobó scraper nem dönti be a teljes futást — csak az az egy forrás marad ki (logolva), a többi zavartalanul lefut
- Azoknál a scrapereknél, amelyek több független base URL-t is bejárnak (OhneMakler: 2, Immokralle: 2, ImmobilienDe: 4 kategória-URL), a fetch_listings() szétbontva `_fetch_one_url(base_url)` segédmetódusra, amit szintén `asyncio.gather`-rel hívunk meg URL-enként párhuzamosan; a végeredményt utólag, egyetlen közös `seen_ids` szettel fésüljük össze, hogy a base URL-ek közötti esetleges átfedés (duplikált ID) ne kerüljön kétszer a végeredménybe

**Logolás — NOTICE szint (kézi futtatás konzoljának tömörítése):**
- `NOTICE = 25` egyedi log-szint (INFO=20 és WARNING=30 között), a `log_notice()` helper használja
- Kézi futtatásnál (nem systemd) a konzol handler csak NOTICE+ szintet ír ki (mérföldkövek: futás indul/vége, DB betöltve, összesített találatszám, scraperenkénti végösszeg, email eredménye), a részletes oldalankénti/hirdetésenkénti INFO logok csak a fájlba (house-monitor.log) mennek
- Konzol formátum kézi futtatásnál: `%H:%M üzenet` (pl. `19:12 Searching...`) — nincs dátum, másodperc, szint-név; a fájl/systemd log formátuma változatlan (teljes dátum+idő+szint, hibakereséshez)
- A "Logging initialized..." sor mostantól plain `logging.info()`-val megy (nem `log_notice()`-szal), ezért a konzolon nem jelenik meg, csak a fájlban
- Scraperenkénti végösszeg-sorok tömör formátumban: `{Forrás}: {N} listings` vagy `{Forrás}: {N} ads` (a forrásnév a domain-utótag nélkül, pl. "FindMyHome" nem "FindMyHome.at" — kivéve ahol a TLD megkülönböztető szerepű: Immobilien.net vs Immobilien.de)
- Systemd módban (INVOCATION_ID env var) ez nem változott: egyetlen stdout stream van, minden INFO részlet benne marad — ez a service.log-ba kerül, amit a CLAUDE.md hibakereséshez dokumentál, ezért ott a teljes részletesség megmaradt

**DB megbízhatóság (seen-houses.json):**
- _load_db(): soronként tölti be a bejegyzéseket, egy sérült/inkompatibilis bejegyzés csak kihagyásra kerül (logolva), nem dobja el az egész DB-t — korábban egyetlen rossz bejegyzés az egészet elszállította volna, ami tömeges duplikált email-spamhez vezetett volna (minden már látott ház "újnak" tűnt volna)
- _save_db(): atomi írás — előbb `seen-houses.json.tmp`-be ír, utána `os.replace()`-dzsel nevezi át a végleges névre, hogy félbeszakadt írás (áramkimaradás, kill) ne korrumpálhassa/üríthesse ki a meglévő adatbázist

**Csomagstruktúra (2026-07-01-i modularizálás óta):**
```
house_monitor/
├── __init__.py          — minimális, nincs re-export (lásd lent, miért)
├── models.py            — Listing dataclass, decode_utf8_or_latin1, parse_de_price
├── config.py            — összes konstans (BLACKLIST, SKIP_NO_PERSIST, EUR_PRICE_*, EMAIL_CONFIG, scraperenkénti URL-ek)
├── logging_setup.py      — NOTICE, log_notice, _fmt_count
├── email_notifier.py     — EmailNotifier
├── scrapers/
│   ├── __init__.py        — mind a 19 scraper class re-exportja + __all__
│   └── <egy fájl / scraper>  (pl. sonnberger.py, immolive24.py, ...)
├── monitor.py            — HouseMonitor, SCRAPER_SUMMARY_LABELS, SOURCE_DISPLAY_NAMES
└── main.py               — a régi `if __name__ == "__main__":` lock+run blokk, main() függvényként
```
- A tesztek (`tests/test_house_monitor.py`) `hm` fixture-je a `house_monitor.monitor` modult aliasolja közvetlenül (NEM a csomag `__init__.py`-ját!) — ez azért kritikus, mert néhány teszt közvetlenül módosítja a `hm.DATA_FILE`-t és monkeypatch-eli a `hm.os.replace`-t, majd a valódi `_save_db`/`_load_db` metódusokat hívja, amik ugyanabból a modul-namespace-ből olvassák ezeket. Ha `hm` a csomag `__init__.py`-ja lenne egy re-exporttal, a monkeypatch csak egy másolatot módosítana, a tényleges kód nem venné észre — ezért is marad a `house_monitor/__init__.py` szándékosan üres (csak `__version__`), nincs benne re-export.
- `tests/conftest.py` (3 sor) állítja be a `sys.path`-ot, hogy `from house_monitor import monitor` működjön telepítés (`pip install -e .`) nélkül is — a Pi-n sincs internet/pip, a csomagot egyszerűen fájlként másoljuk át (rsync/sshfs), ugyanúgy, mint korábban az egyetlen .py fájlt.

**Fejlesztési minta:**
- A scraper-lista és a BLACKLIST folyamatosan bővül — új forrásokat és szűrőszavakat rendszeresen adunk hozzá
- Új scraper hozzáadásakor mindig: `config.py`-ba a konstansok → `scrapers/<uj_fajl>.py` az osztály → `scrapers/__init__.py`-ba az import+`__all__` → `monitor.py`-ban a `scrapers` lista és `SCRAPER_SUMMARY_LABELS`/`SOURCE_DISPLAY_NAMES` → esetleges kliens oldali árszűrő
- Encoding, lapozás, duplikáció-szűrés minden új scrapernél külön figyelmet igényel

**Szerver:**
- Raspberry Pi (ARM Linux, Ubuntu server alapú)
- Python 3.11+, systemd service
- Korlátozott RAM → kerülni kell a nagy memóriahasználatot (pl. ne töltsük be az összes oldalt egyszerre)

**Tesztek:**
- tests/test_house_monitor.py — pytest, nincs hálózati hívás/e-mail küldés, csak parszoló/szűrő/szerializáló logika
- Futtatás: `python3 -m pytest tests/ -v` a projekt gyökeréből (a `tests/conftest.py` állítja be a sys.path-ot, hogy a `house_monitor` csomag importálható legyen)
- Függőségek: `pip install -r requirements.txt` (aiohttp, aiofiles, beautifulsoup4, lxml, pytest)
- Lefedi: decode_utf8_or_latin1 (Goldgrube encoding), parse_de_price (közös ár-parszoló helper), Goldgrube árparszolás, _titles_similar / _already_seen_elsewhere (duplikáció-szűrés), _build_email_body, seen-houses.json save/load round-trip, _load_db hibatűrése (egy sérült bejegyzés nem nullázza le a DB-t), _save_db atomicitása (nincs elveszett/csonkolt adat félbeszakadt írásnál), mind a 19 scraper dedikált kártya/JSON-parszoló tesztje, minden scraper példányosíthatósága — összesen 68 teszt
- Új scraper/logika hozzáadásakor érdemes hozzá tesztet is írni ide, hogy a jövőbeli módosítások ne törjenek el csendben semmit
- 2026-07-02: mind a 19 scraper kártya-parszoló logikája kiemelve egy önálló, hálózat nélkül tesztelhető `_parse_cards(html_text)` (JSON-alapú scrapereknél `_parse_items`/`_parse_json_adverts`) metódusba — korábban csak 5 scraper (ImmobIlienNet, Immokralle, ImmoLive24, DingDong, Sonnberger) rendelkezett ilyennel, a többinél a kártya-feldolgozás közvetlenül a lapozó `fetch_listings()`/`_fetch_one_url()` loopba volt ágyazva. A `_parse_cards` csak a nyers kinyerést végzi (id/title/url/price tuple-ök listája); a dedup (`seen_ids`) és az ár-tartomány-szűrés a hívó oldalon (fetch_listings) marad — ez a mintázat volt a precedens az 5 már meglévő scrapernél is.
- FONTOS KORLÁT: a tesztek kézzel épített statikus HTML/JSON fixture-ökön futnak, nincs bennük valódi hálózati hívás — ez azt jelenti, hogy egy élő site-redesignt (mint a Findheim 2026-07-01-i esete, lásd fent) a tesztkészlet ÖNMAGÁBAN nem vesz észre, csak egy már ismert és egyszer kijavított regressziót előz meg. Éles ellenőrzés (kártyaszám/ár-parszolás manuális futtatással) szükséges, ha egy forrás gyanúsan 0 találatot ad.
- `scripts/live_smoke_check.py` (2026-07-02): a fenti korlátra válaszul készült éles ellenőrző szkript — `python3 -m scripts.live_smoke_check`, mind a 19 scrapert éles oldalak ellen futtatja, ár/blacklist-szűrés és email/DB-írás nélkül, forrásonként kiírja a találatszámot (a 0 találatú/hibázó forrásokat kiemeli). Szándékosan NEM része a pytest-nek (hálózatfüggő, lassú, nem determinisztikus) — kézi/időszakos diagnosztikai eszköz. A session-nek a valódi `monitor.py`-éval EGYEZŐ böngésző-fejléceket (User-Agent, Accept, Accept-Language) kell küldenie — enélkül több site (ImmoScout24, Immodirekt, Immobilien.net — mind ugyanaz az ImmoScout24 AT portál) HTTP 401-gyel blokkol, ami hamis "törött" jelzést adna.
- 2026-07-02: az első futtatás 6 forrást jelzett gyanúsnak; kivizsgálva mind a 6 valós hiba nélkülinek bizonyult. A 3 HTTP 401 (ImmoScout24, Immodirekt, Immobilien.net) a szkript saját hibája volt (hiányzó fejlécek, lásd fent), nem site-probléma. A másik 3 (OhneMakler, Immobilien.de, ImmoLive24) HTTP 200-at adott, 0 kártyával — manuális curl-lal, emelt árplafonnal (100k/150k/300k) igazolva, hogy a szelektorok/keresési paraméterek helyesen működnek, csak ÉPP nincs egyik oldalon sem 3000-70000 € közötti ház kínálva (OhneMakler: explicit üres GeoJSON; Immobilien.de: "keine Treffer gefunden" szöveg a lapon; ImmoLive24: a POST-os ár-szűrés helyesen skálázódik, csak 70k alatt tényleg 0-t ad). Tanulság: a "0 találat" ÖNMAGÁBAN tényleg nem elég egy hiba megállapításához — mindig emelt árplafonnal/tágabb szűrővel kell megerősíteni, mielőtt egy scrapert "törtnek" minősítünk.
- Dev tooling (formázás/lint/típusellenőrzés, NEM szükséges a scraper futtatásához): `requirements-dev.txt` (black, ruff, mypy, types-beautifulsoup4), konfiguráció a `pyproject.toml`-ban. A `[tool.mypy.overrides]` szakasz szándékosan kikapcsol néhány hibakódot (`union-attr`, `index`, `operator`, `call-arg`, `arg-type`, `assignment`) a `house_monitor.scrapers.*` modulokra — ez a BeautifulSoup `Tag.get()`/`.find()` pontatlan típusaiból (pl. `str | list[str] | None`) fakadó zaj, nem valódi hiba; a `house_monitor/` gyökér modulok (monitor.py, email_notifier.py, stb.) teljes szigorral futnak.