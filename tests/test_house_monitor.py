"""Regression tests for the house_monitor package.

Run from the project root:
    python3 -m pytest tests/ -v

Dependencies (already required to run house_monitor itself, plus pytest):
    pip install -r requirements.txt

These tests make no real network calls and send no real email — they only
exercise the parsing/filtering/serialization logic in the package, against
hand-built HTML/data fixtures.
"""

import asyncio
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from house_monitor import monitor as _hm_module


@pytest.fixture(scope="session")
def hm():
    return _hm_module


def _new_monitor(hm):
    """A HouseMonitor instance without __init__'s side effects (opening the log file)."""
    monitor = hm.HouseMonitor.__new__(hm.HouseMonitor)
    monitor.seen = {}
    monitor.session = None
    return monitor


# ---------------------------------------------------------------------------
# Encoding fix (the Goldgrube mojibake bug)
# ---------------------------------------------------------------------------


class TestDecodeUtf8OrLatin1:
    def test_utf8_bytes_declared_as_latin1_decode_correctly(self, hm):
        # Exact reproduction of the bug: the server sends UTF-8, but the
        # Content-Type header incorrectly declares iso-8859-1 -> this used
        # to produce mojibake.
        raw = "Zentral und doch im Grünen".encode("utf-8")
        assert hm.decode_utf8_or_latin1(raw, "iso-8859-1") == "Zentral und doch im Grünen"

    def test_utf8_bytes_with_no_declared_charset(self, hm):
        raw = "Köflach".encode("utf-8")
        assert hm.decode_utf8_or_latin1(raw, None) == "Köflach"

    def test_genuine_latin1_bytes_fall_back_correctly(self, hm):
        # If the bytes really are latin-1 (not valid UTF-8), the fallback must kick in.
        raw = "Köflach".encode("latin-1")
        assert hm.decode_utf8_or_latin1(raw, "iso-8859-1") == "Köflach"

    def test_undecodable_bytes_do_not_raise(self, hm):
        raw = b"\xff\xfe\x00broken"
        result = hm.decode_utf8_or_latin1(raw, "utf-8")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Shared German price-parsing helper (parse_de_price) — used by more than 10
# of the 17 scrapers; previously each had its own near-identical, repeated
# regex+replace logic.
# ---------------------------------------------------------------------------


class TestParseDePrice:
    def test_thousands_and_decimal_separators(self, hm):
        assert hm.parse_de_price("139.000,00") == 139000.0

    def test_thousands_without_decimal(self, hm):
        assert hm.parse_de_price("45.000 €") == 45000.0

    def test_currency_symbol_and_nbsp(self, hm):
        assert hm.parse_de_price("€\xa059.999,00") == 59999.0

    def test_extracts_number_from_surrounding_text(self, hm):
        assert hm.parse_de_price("Kaufpreis: 18.000,00 €") == 18000.0

    def test_small_number_no_separator(self, hm):
        assert hm.parse_de_price("3.290") == 3290.0

    def test_empty_string_returns_zero(self, hm):
        assert hm.parse_de_price("") == 0.0

    def test_no_digits_returns_zero(self, hm):
        assert hm.parse_de_price("Preis auf Anfrage") == 0.0

    def test_placeholder_dash_returns_zero(self, hm):
        assert hm.parse_de_price("-") == 0.0


# ---------------------------------------------------------------------------
# Goldgrube price parsing
# ---------------------------------------------------------------------------


class TestGoldgrubePriceParsing:
    @staticmethod
    def _span(text):
        return BeautifulSoup(f"<span>{text}</span>", "html.parser").span

    def test_thousands_and_decimal_separators(self, hm):
        assert hm.GoldgrubeScraper._parse_price(self._span("139.000,00")) == 139000.0

    def test_price_with_currency_symbol_and_nbsp(self, hm):
        assert hm.GoldgrubeScraper._parse_price(self._span("€\xa059.999,00")) == 59999.0

    def test_empty_span_returns_zero(self, hm):
        assert hm.GoldgrubeScraper._parse_price(self._span("")) == 0.0

    def test_none_span_returns_zero(self, hm):
        assert hm.GoldgrubeScraper._parse_price(None) == 0.0

    def test_no_digits_returns_zero(self, hm):
        assert hm.GoldgrubeScraper._parse_price(self._span("Preis auf Anfrage")) == 0.0


# ---------------------------------------------------------------------------
# Cross-platform duplicate detection
# ---------------------------------------------------------------------------


class TestTitleSimilarity:
    def test_identical_titles_match(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Haus in Graz", "Haus in Graz")

    def test_case_and_whitespace_insensitive(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("  Haus IN Graz  ", "haus in graz")

    def test_substring_match_counts_as_similar(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Haus in Graz", "Schönes Haus in Graz mit Garten")

    def test_html_entities_are_unescaped_before_compare(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Haus &amp; Garten", "Haus & Garten")

    def test_apostrophe_variants_are_normalized(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Bauer's Haus", "Bauer’s Haus")

    def test_unrelated_titles_do_not_match(self, hm):
        monitor = _new_monitor(hm)
        assert not monitor._titles_similar("Haus in Graz", "Wohnung in Wien")


class TestAlreadySeenElsewhere:
    @staticmethod
    def _listing(hm, id_, title, price, source="test"):
        now = "2026-01-01T00:00:00"
        return hm.Listing(
            id=id_,
            title=title,
            price=price,
            url="http://x",
            source=source,
            first_seen=now,
            last_seen=now,
        )

    def test_matches_against_persistent_db(self, hm):
        monitor = _new_monitor(hm)
        existing = self._listing(hm, "a_1", "Haus in Graz", 50000.0)
        monitor.seen = {"a_1": existing}
        candidate = self._listing(hm, "b_1", "Haus in Graz", 50000.0)
        assert monitor._already_seen_elsewhere(candidate)

    def test_different_price_does_not_match(self, hm):
        monitor = _new_monitor(hm)
        existing = self._listing(hm, "a_1", "Haus in Graz", 50000.0)
        monitor.seen = {"a_1": existing}
        candidate = self._listing(hm, "b_1", "Haus in Graz", 51000.0)
        assert not monitor._already_seen_elsewhere(candidate)

    def test_matches_against_same_run_batch(self, hm):
        monitor = _new_monitor(hm)
        other = self._listing(hm, "b_1", "Haus in Graz", 50000.0)
        candidate = self._listing(hm, "c_1", "Haus in Graz", 50000.0)
        assert monitor._already_seen_elsewhere(candidate, also_check=[other])

    def test_goldgrube_mojibake_title_would_not_have_matched(self, hm):
        # This test documents WHY the encoding fix matters: with a mojibake
        # title, duplicate detection fails to recognize it's the same listing.
        monitor = _new_monitor(hm)
        existing = self._listing(
            hm, "a_1", "Zentral und doch im GrÃ¼nen", 60000.0, source="goldgrube.at"
        )
        monitor.seen = {"a_1": existing}
        candidate = self._listing(
            hm, "b_1", "Zentral und doch im Grünen", 60000.0, source="willhaben.at"
        )
        assert not monitor._already_seen_elsewhere(candidate)


# ---------------------------------------------------------------------------
# Email body formatting
# ---------------------------------------------------------------------------


class TestBuildEmailBody:
    @staticmethod
    def _listing(hm, **kwargs):
        defaults = dict(
            id="x",
            title="Haus",
            price=10000.0,
            url="http://x",
            source="test",
            first_seen="now",
            last_seen="now",
        )
        defaults.update(kwargs)
        return hm.Listing(**defaults)

    def test_new_listings_sorted_by_price_ascending(self, hm):
        monitor = _new_monitor(hm)
        cheap = self._listing(hm, id="a", title="Cheap house", price=10000.0)
        expensive = self._listing(hm, id="b", title="Expensive house", price=60000.0)
        body = monitor._build_email_body([expensive, cheap])
        assert body.index("Cheap house") < body.index("Expensive house")

    def test_new_listings_appear_before_price_changes(self, hm):
        monitor = _new_monitor(hm)
        changed = self._listing(
            hm, id="a", title="Changed", price=20000.0, price_changed=True, old_price=25000.0
        )
        new = self._listing(hm, id="b", title="New listing", price=90000.0)
        body = monitor._build_email_body([changed, new])
        assert body.index("New listing") < body.index("Changed")

    def test_price_change_shows_old_and_new_price(self, hm):
        monitor = _new_monitor(hm)
        changed = self._listing(
            hm, id="a", title="Changed", price=20000.0, price_changed=True, old_price=25000.0
        )
        body = monitor._build_email_body([changed])
        assert "25 000" in body and "20 000" in body

    def test_price_uses_space_as_thousands_separator(self, hm):
        monitor = _new_monitor(hm)
        listing = self._listing(hm, id="a", title="House", price=1234000.0)
        body = monitor._build_email_body([listing])
        assert "1 234 000" in body
        assert "1234000" not in body

    def test_no_emoji_in_body(self, hm):
        monitor = _new_monitor(hm)
        new_listing = self._listing(hm, id="a", title="New house", price=10000.0)
        changed = self._listing(
            hm, id="b", title="Changed house", price=20000.0, price_changed=True, old_price=25000.0
        )
        body = monitor._build_email_body([new_listing, changed])
        assert "🆕" not in body and "📉" not in body

    def test_source_header_uses_console_display_name(self, hm):
        monitor = _new_monitor(hm)
        listing = self._listing(hm, id="a", title="House", price=10000.0, source="sonnberger.co.at")
        body = monitor._build_email_body([listing])
        assert "Sonnberger (1 db)" in body
        assert "sonnberger.co.at (1 db)" not in body


# ---------------------------------------------------------------------------
# JSON DB round-trip (Listing <-> dict serialization)
# ---------------------------------------------------------------------------


class TestDbRoundTrip:
    def test_save_and_load_preserve_listing_data(self, hm, tmp_path):
        listing = hm.Listing(
            id="is24_1",
            title="Test house",
            price=45000.0,
            url="http://example.com",
            source="ImmoScout24",
            first_seen="2026-01-01T00:00:00",
            last_seen="2026-01-01T00:00:00",
        )
        monitor = _new_monitor(hm)
        monitor.seen = {listing.id: listing}

        data_file = tmp_path / "roundtrip.json"
        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(data_file)
        try:
            asyncio.run(monitor._save_db())
            reloaded = _new_monitor(hm)
            asyncio.run(reloaded._load_db())
        finally:
            hm.DATA_FILE = original_data_file

        assert reloaded.seen["is24_1"].title == "Test house"
        assert reloaded.seen["is24_1"].price == 45000.0

    def test_price_changed_and_old_price_not_persisted(self, hm, tmp_path):
        listing = hm.Listing(
            id="is24_1",
            title="Test house",
            price=45000.0,
            url="http://example.com",
            source="ImmoScout24",
            first_seen="2026-01-01T00:00:00",
            last_seen="2026-01-01T00:00:00",
            price_changed=True,
            old_price=50000.0,
        )
        monitor = _new_monitor(hm)
        monitor.seen = {listing.id: listing}

        data_file = tmp_path / "no_transient_fields.json"
        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(data_file)
        try:
            asyncio.run(monitor._save_db())
            saved = json.loads(data_file.read_text(encoding="utf-8"))
        finally:
            hm.DATA_FILE = original_data_file

        assert "price_changed" not in saved["is24_1"]
        assert "old_price" not in saved["is24_1"]


class TestLoadDbResilience:
    """Regression test for _load_db: a single corrupt entry must never wipe
    out the whole DB — otherwise every already-seen listing would look "new"
    on the next run, sending a flood of duplicate emails."""

    def test_skips_corrupt_entry_keeps_valid_ones(self, hm, tmp_path):
        data_file = tmp_path / "partially_corrupt.json"
        data_file.write_text(
            json.dumps(
                {
                    "good_1": {
                        "id": "good_1",
                        "title": "Good house 1",
                        "price": 10000.0,
                        "url": "http://x",
                        "source": "s",
                        "first_seen": "t",
                        "last_seen": "t",
                    },
                    "corrupt_1": {
                        "id": "corrupt_1",
                        "title": "Corrupt",
                        "price": 20000.0,
                        "url": "http://x",
                        "source": "s",
                        "first_seen": "t",
                        "last_seen": "t",
                        "unexpected_field": "this does not exist on Listing",
                    },
                    "good_2": {
                        "id": "good_2",
                        "title": "Good house 2",
                        "price": 30000.0,
                        "url": "http://x",
                        "source": "s",
                        "first_seen": "t",
                        "last_seen": "t",
                    },
                }
            ),
            encoding="utf-8",
        )

        monitor = _new_monitor(hm)
        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(data_file)
        try:
            asyncio.run(monitor._load_db())
        finally:
            hm.DATA_FILE = original_data_file

        assert set(monitor.seen.keys()) == {"good_1", "good_2"}
        assert monitor.seen["good_1"].price == 10000.0
        assert monitor.seen["good_2"].price == 30000.0

    def test_missing_required_field_is_skipped_not_fatal(self, hm, tmp_path):
        data_file = tmp_path / "missing_field.json"
        data_file.write_text(
            json.dumps(
                {
                    "good_1": {
                        "id": "good_1",
                        "title": "Good house",
                        "price": 10000.0,
                        "url": "http://x",
                        "source": "s",
                        "first_seen": "t",
                        "last_seen": "t",
                    },
                    "corrupt_1": {
                        "id": "corrupt_1",
                        "title": "Incomplete",
                        # 'url', 'source', 'first_seen', 'last_seen' are missing
                    },
                }
            ),
            encoding="utf-8",
        )

        monitor = _new_monitor(hm)
        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(data_file)
        try:
            asyncio.run(monitor._load_db())
        finally:
            hm.DATA_FILE = original_data_file

        assert set(monitor.seen.keys()) == {"good_1"}


class TestSaveDbAtomic:
    """Regression test for _save_db: the save writes to a temp file first,
    then an atomic os.replace() moves it to the final name, so an
    interrupted/failed write can never corrupt or empty the existing
    database file."""

    @staticmethod
    def _monitor_with_one_listing(hm):
        monitor = _new_monitor(hm)
        monitor.seen = {
            "x": hm.Listing(
                id="x",
                title="T",
                price=1.0,
                url="http://x",
                source="s",
                first_seen="t",
                last_seen="t",
            )
        }
        return monitor

    def test_no_leftover_tmp_file_after_successful_save(self, hm, tmp_path):
        data_file = tmp_path / "atomic.json"
        monitor = self._monitor_with_one_listing(hm)

        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(data_file)
        try:
            asyncio.run(monitor._save_db())
        finally:
            hm.DATA_FILE = original_data_file

        assert data_file.exists()
        assert not Path(f"{data_file}.tmp").exists()

    def test_original_file_preserved_if_replace_fails(self, hm, tmp_path, monkeypatch):
        data_file = tmp_path / "atomic.json"
        data_file.write_text('{"old": "data"}', encoding="utf-8")
        monitor = self._monitor_with_one_listing(hm)

        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(data_file)

        def _boom(*args, **kwargs):
            raise OSError("simulated error during save")

        monkeypatch.setattr(hm.os, "replace", _boom)
        try:
            asyncio.run(monitor._save_db())
        finally:
            hm.DATA_FILE = original_data_file

        # The existing file's contents stay intact — not truncated or emptied.
        assert data_file.read_text(encoding="utf-8") == '{"old": "data"}'


# ---------------------------------------------------------------------------
# Static card parsers (testable without network access)
# ---------------------------------------------------------------------------


class TestImmobilienNetCardParsing:
    HTML = """
    <li class="_98L38">
      <a class="_2BVPu" href="/immobilie/haus-graz-abc123/">
        <h2 class="_3r8AR">Sch&ouml;nes Haus in Graz</h2>
        <h4 class="D1pOB">45.000 €</h4>
      </a>
    </li>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.ImmobIlienNetScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "inet_haus-graz-abc123"
        assert title == "Schönes Haus in Graz"
        assert url == "https://www.immobilien.net/immobilie/haus-graz-abc123/"
        assert price == 45000.0


class TestImmokralleCardParsing:
    HTML = """
    <li class="immo" data-id="99887">
      <a class="anzeigen_link" href="https://www.immokralle.com/x/99887">x</a>
      <h2>Haus am Land</h2>
      <div class="price">33.500 €</div>
    </li>
    """

    def test_extracts_uid_title_url_price(self, hm):
        scraper = hm.ImmokralleScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        uid, title, url, price = cards[0]
        assert uid == "99887"
        assert title == "Haus am Land"
        assert price == 33500.0


class TestImmoLive24CardParsing:
    HTML = """
    <div class="item">
      <table class="sTable">
        <tr><td class="custom_title">
          <h2><a alt="Haus, 2135, Kirchstetten" title="Haus, 2135, Kirchstetten"
                 href="https://at.immolive24.com/immobilien/haeuser/haeuser-kauf/haus-2135-kirchstetten-854297.html?highlight">
            <strong>Haus, 2135, Kirchstetten</strong>
          </a></h2>
        </td></tr>
        <tr class="listing_bg">
          <td class="fields" valign="top">
            <div>Kleines Einfamilienhaus mit Garten</div>
          </td>
        </tr>
        <tr class="listing_bg"><td>
          <span class="miete icon" title="Miete">Kaufpreis: <br /><span class="big">€ 64.000,00</span></span>
        </td></tr>
      </table>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.ImmoLive24Scraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "il24_854297"
        assert title == "Kleines Einfamilienhaus mit Garten"
        assert (
            url
            == "https://at.immolive24.com/immobilien/haeuser/haeuser-kauf/haus-2135-kirchstetten-854297.html"
        )
        assert price == 64000.0

    def test_falls_back_to_address_when_no_teaser(self, hm):
        html = """
        <div class="item">
          <h2><a href="https://at.immolive24.com/x/haus-123.html">Haus, 1234, Ort</a></h2>
          <span class="miete">Kaufpreis: € 50.000,00</span>
        </div>
        """
        scraper = hm.ImmoLive24Scraper(session=None)
        cards = scraper._parse_cards(html)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert title == "Haus, 1234, Ort"


class TestDingDongCardParsing:
    HTML = """
    <table class="views-table cols-5 footable">
      <thead><tr><th>x</th></tr></thead>
      <tbody>
        <tr class="odd views-row-first">
          <td class="views-field views-field-field-bilder"><a href="/immobilien/mobilheim"><img /></a></td>
          <td class="views-field views-field-title">
            <h2><a href="/immobilien/mobilheim">Mobilheim</a></h2>Description text here...
          </td>
          <td class="views-field views-field-field-nutzflaeche">55 m²</td>
          <td class="views-field views-field-field-raeume">3,0</td>
          <td class="views-field views-field-field-preis active">17.000,00 €</td>
        </tr>
      </tbody>
    </table>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.DingDongScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "dd_mobilheim"
        assert title == "Mobilheim"
        assert url == "https://www.ding-dong.at/immobilien/mobilheim"
        assert price == 17000.0


class TestSonnbergerCardParsing:
    HTML = """
    <div class="item-listing-wrap hz-item-gallery-js card">
      <span class="hz-show-lightbox-js" data-listid="43433" data-toggle="tooltip"></span>
      <div class="item-body">
        <h2 class="item-title">
          <a href="https://sonnberger.co.at/wp/immobilien/waldnah-haus/">WALDNAH – Haus mit großem Grund</a>
        </h2>
        <ul class="item-price-wrap hide-on-list"><li class="item-price">€ 278.000</li></ul>
      </div>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.SonnbergerScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "sb_43433"
        assert title == "WALDNAH – Haus mit großem Grund"
        assert url == "https://sonnberger.co.at/wp/immobilien/waldnah-haus/"
        assert price == 278000.0

    def test_reserved_status_has_no_digits_parses_to_zero(self, hm):
        html = """
        <div class="item-listing-wrap">
          <span data-listid="40923"></span>
          <h2 class="item-title"><a href="https://sonnberger.co.at/wp/immobilien/summer-breeze/">SUMMER BREEZE</a></h2>
          <ul class="item-price-wrap hide-on-list"><li class="item-price">RESERVIERT</li></ul>
        </div>
        """
        scraper = hm.SonnbergerScraper(session=None)
        cards = scraper._parse_cards(html)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert price == 0.0


class TestImmoScout24CardParsing:
    HTML = """
    <ol data-testid="results-items">
      <li>
        <a href="/expose/12345678">
          <h2>Nice house</h2>
          <ul class="PriceKeyFacts">
            <li>65.000 €</li>
          </ul>
        </a>
      </li>
    </ol>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.ImmoScout24Scraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "is24_12345678"
        assert title == "Nice house"
        assert url == "https://www.immobilienscout24.at/expose/12345678"
        assert price == 65000


class TestDibeoCardParsing:
    HTML = """
    <a href="https://www.dibeo.at/expose/998877">
      <h2>Charming Cottage</h2>
      <span>€ 55.000</span>
    </a>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.DibeoScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "dibeo_998877"
        assert title == "Charming Cottage"
        assert url == "https://www.dibeo.at/expose/998877"
        assert price == 55000.0


class TestFindMyHomeCardParsing:
    HTML = """
    <div class="col-xs-12 col-sm-9">
      <h3 class="obj_list"><a href="/5549779">Sonniges Haus mit Garten</a></h3>
      <div class="col-xs-4">Kauf: 30.000,- €</div>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.FindMyHomeScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "fmh_5549779"
        assert title == "Sonniges Haus mit Garten"
        assert url == "https://www.findmyhome.at/5549779"
        assert price == 30000


class TestWohnnetCardParsing:
    HTML = """
    <a data-id="778899" data-title="Haus am See" href="/immobilien/haus-778899">
      <i class="fas fa-map-marker-alt"></i> Kärnten
      <div class="col text-right text-nowrap">
        <b style="font-size: x-large">120.000 €</b>
      </div>
    </a>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.WohnnetScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "wn_778899"
        assert title == "Haus am See"
        assert url == "https://www.wohnnet.at/immobilien/haus-778899"
        assert price == 120000.0

    def test_german_location_is_excluded(self, hm):
        html = """
        <a data-id="1" data-title="Haus" href="/x">
          <i class="fas fa-map-marker-alt"></i> Deutschland
          <div class="col text-right text-nowrap"><b style="font-size: x-large">1 €</b></div>
        </a>
        """
        scraper = hm.WohnnetScraper(session=None)
        cards = scraper._parse_cards(html)
        assert cards == []


class TestDerStandardCardParsing:
    HTML = """
    <li class="sc-listing-card">
      <a class="sc-listing-card-content-background-link" href="/detail/12345678"></a>
      <div class="sc-listing-card-title">Haus in Villach</div>
      <span class="ResultItemPrice-module-scss-module__abc">€ 189.000</span>
    </li>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.DerStandardScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "ds_12345678"
        assert title == "Haus in Villach"
        assert url == "https://immobilien.derstandard.at/detail/12345678"
        assert price == 189000.0


class TestRaiffeisenCardParsing:
    HTML = """
    <div class="bg-white flex flex-col relative group">
      <a href="/en/properties/buy/0001009858" title="Haus am Land"></a>
      <h4>Haus am Land</h4>
      <dl>
        <dt>Purchase price</dt>
        <dd>250.000,00 €</dd>
      </dl>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.RaiffeisenScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "ri_0001009858"
        assert title == "Haus am Land"
        assert url == "https://www.raiffeisen-immobilien.at/en/properties/buy/0001009858"
        assert price == 250000.0


class TestImmiCardParsing:
    HTML = """
    <section class="teasers">
      <article class="teaser" id="immo_7-5542219">
        <h2><a href="/immobilien/haus-am-land"><span>Haus am Land</span></a></h2>
        <div class="description"><h3>Gemütliches Haus</h3></div>
        <div class="infos"><div><strong>€ 60.000</strong></div></div>
      </article>
    </section>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.ImmiScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "immi_immo_7-5542219"
        assert title == "Gemütliches Haus"
        assert url == "https://immi.at/immobilien/haus-am-land"
        assert price == 60000.0


class TestBazarItemParsing:
    def test_extracts_id_title_url_price(self, hm):
        data = {
            "content": [
                {
                    "id": 5551,
                    "common": {"title": "Nice house", "price": {"price": 45000}},
                    "path": "/immobilien/haus-5551",
                }
            ]
        }
        scraper = hm.BazarScraper(session=None)
        items = scraper._parse_items(data)
        assert len(items) == 1
        listing_id, title, url, price = items[0]
        assert listing_id == "bazar_5551"
        assert title == "Nice house"
        assert url == "https://www.bazar.at/immobilien/haus-5551"
        assert price == 45000.0

    def test_dibeo_url_normalized_to_expose_format(self, hm):
        data = {
            "content": [
                {
                    "id": 999,
                    "common": {"title": "X", "price": {"price": 30000}},
                    "path": "https://www.dibeo.at/expose/12345678?utm=1",
                }
            ]
        }
        scraper = hm.BazarScraper(session=None)
        items = scraper._parse_items(data)
        assert items[0][2] == "https://www.dibeo.at/expose/12345678"


class TestGoldgrubeCardParsing:
    HTML = """
    <article id="778899" class="twelvecol-xs-nm">
      <a class="detaillink" href="/immobilie/haus-778899">Details</a>
      <h3 class="twelvecol-xs">Gemütliches Landhaus</h3>
      <span class="price">€ 139.000,00</span>
    </article>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.GoldgrubeScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "gg_778899"
        assert title == "Gemütliches Landhaus"
        assert url == "https://www.goldgrube.at/immobilie/haus-778899"
        assert price == 139000.0


class TestOhneMaklerCardParsing:
    HTML = """
    <div id="bookmark_334455">
      <a href="/immobilie/334455/">Details</a>
      <h4>Haus mit Garten</h4>
      <span class="font-semibold text-primary-500">89.000 €</span>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.OhneMaklerScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "om_334455"
        assert title == "Haus mit Garten"
        assert url == "https://www.ohne-makler.at/immobilie/334455/"
        assert price == 89000.0


class TestImmodirektCardParsing:
    HTML = """
    <section class="_98L38">
      <a href="/immobilie/8010-graz/haus-mit-garten-abcdef0123456789abcdef01/">
        <h2 class="_2jNcY">Haus mit Garten</h2>
      </a>
      <div class="_1-CSS">
        <span class="_1xxDl">Kaufpreis</span>
        <span class="_2Pe1d">185.000,00</span>
      </div>
    </section>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.ImmodirektScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "imd_abcdef0123456789abcdef01"
        assert title == "Haus mit Garten"
        assert (
            url
            == "https://www.immodirekt.at/immobilie/8010-graz/haus-mit-garten-abcdef0123456789abcdef01/"
        )
        assert price == 185000.0


class TestImmobilienDeCardParsing:
    HTML = """
    <a class="lr-card" href="/ausland/9667674">
      <div class="lr-card__title">Ferienhaus in Kärnten</div>
      <div class="lr-card__price-amount">208.000 €</div>
    </a>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.ImmobilienDeScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "imde_9667674"
        assert title == "Ferienhaus in Kärnten"
        assert url == "https://www.immobilien.de/ausland/9667674"
        assert price == 208000.0


class TestFindheimCardParsing:
    HTML = """
    <div class="group overflow-hidden border rounded-3xl">
      <a href="/de/immobilie/haus-mit-garten-abcd1234">
        <h3>Haus mit Garten</h3>
        <p class="font-semibold text-lg">€ 59.999</p>
      </a>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.FindheimScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "fh_abcd1234"
        assert title == "Haus mit Garten"
        assert url == "https://findheim.at/de/immobilie/haus-mit-garten-abcd1234"
        assert price == 59999.0

    def test_card_missing_overflow_hidden_class_is_ignored(self, hm):
        html = """
        <div class="group border rounded-md">
          <a href="/de/immobilie/x"><h3>X</h3><p class="font-semibold">€ 1</p></a>
        </div>
        """
        scraper = hm.FindheimScraper(session=None)
        assert scraper._parse_cards(html) == []


class TestWillhabenJsonAdvertParsing:
    def test_extracts_id_title_url_price(self, hm):
        adverts = [
            {
                "id": 123456789,
                "description": "Charmantes Haus",
                "advertStatus": {"statusId": "active"},
                "attributes": {
                    "attribute": [
                        {
                            "name": "URL_SLUG",
                            "values": ["/iad/immobilien/d/haus-kaufen/wien/haus-123456789/"],
                        },
                        {"name": "PRICE", "values": ["45000"]},
                    ]
                },
            }
        ]
        scraper = hm.WillhabenScraper(session=None)
        results = scraper._parse_json_adverts(adverts)
        assert len(results) == 1
        listing_id, title, url, price = results[0]
        assert listing_id == "wh_123456789"
        assert title == "Charmantes Haus"
        assert url == "https://www.willhaben.at/iad/immobilien/d/haus-kaufen/wien/haus-123456789/"
        assert price == 45000.0

    def test_per_square_meter_price_is_excluded(self, hm):
        adverts = [
            {
                "id": 1,
                "description": "Grundstück",
                "attributes": {
                    "attribute": [
                        {"name": "URL_SLUG", "values": ["/iad/x"]},
                        {"name": "PRICE", "values": ["6700"]},
                        {"name": "LIVING_AREA", "values": ["15"]},
                    ]
                },
            }
        ]
        scraper = hm.WillhabenScraper(session=None)
        assert scraper._parse_json_adverts(adverts) == []

    def test_foreign_listing_is_excluded(self, hm):
        adverts = [
            {
                "id": 2,
                "description": "Haus im Ausland",
                "attributes": {
                    "attribute": [
                        {"name": "URL_SLUG", "values": ["/iad/immobilien/andere-laender/haus-2/"]},
                        {"name": "PRICE", "values": ["50000"]},
                    ]
                },
            }
        ]
        scraper = hm.WillhabenScraper(session=None)
        assert scraper._parse_json_adverts(adverts) == []


class TestWillhabenHtmlFallbackParsing:
    HTML = """
    <div id="123456789">
      <a data-testid="search-result-entry-header-123456789" href="/iad/object/123456789">
        <h2>Haus in Wien <svg></svg></h2>
      </a>
    </div>
    <span data-testid="search-result-entry-price-123456789">65.000 €</span>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.WillhabenScraper(session=None)
        cards = scraper._parse_html_fallback_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "wh_123456789"
        assert title == "Haus in Wien"
        assert url == "https://www.willhaben.at/iad/object/123456789"
        assert price == 65000.0


# ---------------------------------------------------------------------------
# Configuration / smoke tests
# ---------------------------------------------------------------------------


def test_price_range_is_sane(hm):
    assert 0 < hm.EUR_PRICE_FROM < hm.EUR_PRICE_TO


def test_all_scrapers_are_constructible(hm):
    """Regression smoke test: every scraper class must be constructible with
    a session. If someone breaks an __init__, this catches it."""
    scraper_classes = [
        hm.ImmoScout24Scraper,
        hm.DibeoScraper,
        hm.FindMyHomeScraper,
        hm.WillhabenScraper,
        hm.FindheimScraper,
        hm.WohnnetScraper,
        hm.DerStandardScraper,
        hm.ImmodirektScraper,
        hm.RaiffeisenScraper,
        hm.OhneMaklerScraper,
        hm.ImmobIlienNetScraper,
        hm.ImmokralleScraper,
        hm.ImmiScraper,
        hm.BazarScraper,
        hm.ImmobilienDeScraper,
        hm.GoldgrubeScraper,
        hm.ImmoLive24Scraper,
        hm.DingDongScraper,
        hm.SonnbergerScraper,
    ]
    for cls in scraper_classes:
        assert cls(session=None) is not None
