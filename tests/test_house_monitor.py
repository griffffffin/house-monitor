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
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from bs4 import BeautifulSoup

from house_monitor import health as _health
from house_monitor import monitor as _hm_module
from house_monitor import scrapers as _scrapers_module
from house_monitor.scrapers import propylo as _propylo
from house_monitor.fetch import HTTPStatusError, fetch_bytes, fetch_text


@pytest.fixture(scope="session")
def hm():
    return _hm_module


def _new_monitor(hm):
    """A HouseMonitor instance without __init__'s side effects (opening the log file)."""
    monitor = hm.HouseMonitor.__new__(hm.HouseMonitor)
    monitor.seen = {}
    monitor.session = None
    monitor.day_counts = {}
    monitor.today_errors = {}
    monitor.last_email_ok = True
    monitor.day_unmatched = {}
    monitor.day_late = []
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
        # Real case: one portal appends "Provisionsfrei" to the same title.
        monitor = _new_monitor(hm)
        assert monitor._titles_similar(
            "Mobilheim nähe Sieghartskirchen",
            "Mobilheim nähe Sieghartskirchen Provisionsfrei",
        )

    def test_short_generic_title_is_not_a_substring_match(self, hm):
        # Regression (2026-09-24): an unrelated April listing titled just
        # "Mobilheim" at the same price swallowed a real price drop
        # on all three portals, because "mobilheim" is a substring of it.
        monitor = _new_monitor(hm)
        assert not monitor._titles_similar(
            "Mobilheim", "GEMÜTLICHES MOBILHEIM - IDEAL FÜR ZWEI PERSONEN"
        )
        assert not monitor._titles_similar("Haus", "Bauernhaus in Sonnenlage mit Grundstück")

    def test_short_titles_still_match_exactly(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Mobilheim", "mobilheim")

    def test_emphasis_punctuation_is_ignored(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Kein Hauptwohnsitz", "Kein Hauptwohnsitz!")
        assert monitor._titles_similar("Wohn- oder Freizeitdomizil", "WOHN- ODER FREIZEITDOMIZIL?")

    def test_substring_must_end_on_a_word_boundary(self, hm):
        monitor = _new_monitor(hm)
        assert not monitor._titles_similar("Tiefgaragenplatz Nr. 4", "Tiefgaragenplatz Nr. 42")

    def test_html_entities_are_unescaped_before_compare(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Haus &amp; Garten", "Haus & Garten")

    def test_apostrophe_variants_are_normalized(self, hm):
        monitor = _new_monitor(hm)
        assert monitor._titles_similar("Bauer's Haus", "Bauer’s Haus")

    def test_double_quote_variants_are_normalized(self, hm):
        # Same listing re-syndicated with vs without quotes around a word must
        # still count as similar (real case: Immodirekt vs Immokralle/IS24).
        monitor = _new_monitor(hm)
        assert monitor._titles_similar(
            'Premium Tiny House "Igluhut" - sofort verfügbar',
            "Premium Tiny House Igluhut - sofort verfügbar",
        )

    def test_unrelated_titles_do_not_match(self, hm):
        monitor = _new_monitor(hm)
        assert not monitor._titles_similar("Haus in Graz", "Wohnung in Wien")


class TestAlreadySeenElsewhere:
    @staticmethod
    def _listing(hm, id_, title, price, source="test", url="http://x", days_ago=0):
        # Timestamps are relative to "now": DB entries older than
        # DUPLICATE_LOOKBACK_DAYS are deliberately ignored by the dedup check.
        ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
        return hm.Listing(
            id=id_,
            title=title,
            price=price,
            url=url,
            source=source,
            first_seen=ts,
            last_seen=ts,
        )

    def test_stale_db_entry_is_not_a_live_duplicate(self, hm):
        # An identical title at the same price, but last seen months ago,
        # can't be a copy of a listing that shows up today.
        monitor = _new_monitor(hm)
        stale = self._listing(
            hm, "a_1", "Haus in Graz", 50000.0, days_ago=hm.DUPLICATE_LOOKBACK_DAYS + 30
        )
        monitor.seen = {"a_1": stale}
        candidate = self._listing(hm, "b_1", "Haus in Graz", 50000.0)
        assert not monitor._already_seen_elsewhere(candidate)

    def test_recently_seen_db_entry_is_still_a_duplicate(self, hm):
        monitor = _new_monitor(hm)
        recent = self._listing(hm, "a_1", "Haus in Graz", 50000.0, days_ago=3)
        monitor.seen = {"a_1": recent}
        candidate = self._listing(hm, "b_1", "Haus in Graz", 50000.0)
        assert monitor._already_seen_elsewhere(candidate)

    def test_unparseable_last_seen_is_kept(self, hm):
        monitor = _new_monitor(hm)
        existing = self._listing(hm, "a_1", "Haus in Graz", 50000.0)
        existing.last_seen = "not-a-timestamp"
        monitor.seen = {"a_1": existing}
        candidate = self._listing(hm, "b_1", "Haus in Graz", 50000.0)
        assert monitor._already_seen_elsewhere(candidate)

    def test_matches_against_persistent_db(self, hm):
        monitor = _new_monitor(hm)
        existing = self._listing(hm, "a_1", "Haus in Graz", 50000.0)
        monitor.seen = {"a_1": existing}
        candidate = self._listing(hm, "b_1", "Haus in Graz", 50000.0)
        assert monitor._already_seen_elsewhere(candidate)

    def test_generic_short_title_at_same_price_does_not_hide_listing(self, hm):
        # The 2026-09-24 case end-to-end: a stale, unrelated
        # "Mobilheim" entry at the same price must not count as a duplicate.
        monitor = _new_monitor(hm)
        monitor.seen = {"wh_1": self._listing(hm, "wh_1", "Mobilheim", 18000.0)}
        candidate = self._listing(
            hm, "heger_1", "GEMÜTLICHES MOBILHEIM - IDEAL FÜR ZWEI PERSONEN", 18000.0
        )
        assert not monitor._already_seen_elsewhere(candidate)

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

    def test_cross_platform_quote_variant_is_deduped(self, hm):
        # Regression: the Koblach "Igluhut" tiny house came in from both
        # Immodirekt and Immokralle (an IS24 expose) with the only title
        # difference being the quotes around "Igluhut"; at the same price it
        # must be recognized as a duplicate.
        monitor = _new_monitor(hm)
        existing = self._listing(
            hm,
            "imd_1",
            'Premium Tiny House "Igluhut" - sofort verfügbar',
            39900.0,
            source="immodirekt.at",
        )
        monitor.seen = {"imd_1": existing}
        candidate = self._listing(
            hm,
            "ik_1",
            "Premium Tiny House Igluhut - sofort verfügbar",
            39900.0,
            source="immokralle.com",
        )
        assert monitor._already_seen_elsewhere(candidate)

    def test_shared_url_object_id_dedupes_across_platforms(self, hm):
        # Robust layer: Immodirekt and Immokralle (an IS24 expose) carry the
        # SAME 24-hex object id in the URL. Even with divergent titles AND a
        # different price, it must be recognized as the same object.
        monitor = _new_monitor(hm)
        existing = self._listing(
            hm,
            "imd_1",
            "Premium Tiny House Igluhut - sofort verfügbar",
            39900.0,
            source="immodirekt.at",
            url=(
                "https://www.immodirekt.at/immobilie/6842-koblach/"
                "premium-tiny-house-igluhut-sofort-verfuegbar-"
                "6a9848d484bfba0be7803920/"
            ),
        )
        monitor.seen = {"imd_1": existing}
        candidate = self._listing(
            hm,
            "ik_1",
            "Ganz anderer Titel des Portals",  # title diverges
            41000.0,  # price diverges too
            source="immokralle.com",
            url=(
                "https://www.immobilienscout24.at/expose/"
                "6a9848d484bfba0be7803920?utm_source=alleskralle.com"
            ),
        )
        assert monitor._already_seen_elsewhere(candidate)

    def test_object_key_none_does_not_false_match(self, hm):
        # Two listings without a 24-hex id in the URL must not be deduped by
        # the object-id layer just because both keys are None.
        monitor = _new_monitor(hm)
        existing = self._listing(hm, "wh_1", "Haus A", 30000.0, url="https://willhaben.at/x-111/")
        monitor.seen = {"wh_1": existing}
        candidate = self._listing(
            hm, "dd_1", "Haus B", 30000.0, url="https://dingdong.at/immobilien/y-222"
        )
        assert not monitor._already_seen_elsewhere(candidate)

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

    def test_listing_shows_console_display_name_inline(self, hm):
        monitor = _new_monitor(hm)
        listing = self._listing(hm, id="a", title="House", price=10000.0, source="sonnberger.co.at")
        body = monitor._build_email_body([listing])
        assert "House (Sonnberger)" in body
        assert "sonnberger.co.at" not in body


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
        # ... and the failure is counted as an error of the day.
        assert "simulated error during save" in monitor.today_errors["db_save"]


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


class TestPropyloCardParsing:
    # Mirrors at.propylo.com: the whole card is a single <a> whose href ends in
    # /verkaufsimmobilie/<id>; the title is an inner <h2> and the price a
    # div.price ("15.000 €", German thousands-dot format). The href is already
    # absolute.
    HTML = """
    <div class="itemList">
      <a href="https://at.propylo.com/verkaufsimmobilie/57124186" title="Einfamilienhaus Abriss">
        <picture><img class="itemImg" src="x.webp"/></picture>
        <div class="itemInfos">
          <h2>Einfamilienhaus Abriss , 3000 m2 Grundstück</h2>
          <div class="price">15.000 €</div>
        </div>
      </a>
      <a href="https://at.propylo.com/verkaufsimmobilie/57152810" title="Nettes Haus">
        <div class="itemInfos">
          <h2>Nettes Haus</h2>
          <div class="price">1.250.000 €</div>
        </div>
      </a>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.PropyloScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 2
        listing_id, title, url, price = cards[0]
        assert listing_id == "pro_57124186"
        assert title == "Einfamilienhaus Abriss , 3000 m2 Grundstück"
        assert url == "https://at.propylo.com/verkaufsimmobilie/57124186"
        assert price == 15000.0
        # thousands-dot parsed correctly, not truncated to 1.25
        assert cards[1][3] == 1250000.0

    def test_falls_back_to_anchor_title_when_no_h2(self, hm):
        html = """
        <a href="https://at.propylo.com/verkaufsimmobilie/999" title="Titel aus Attribut">
          <div class="price">40.000 €</div>
        </a>
        """
        scraper = hm.PropyloScraper(session=None)
        cards = scraper._parse_cards(html)
        assert len(cards) == 1
        assert cards[0][1] == "Titel aus Attribut"
        assert cards[0][3] == 40000.0

    def test_ignores_non_listing_anchors(self, hm):
        # Nav links / detail-page anchors that aren't /verkaufsimmobilie/<id>
        # must be skipped entirely.
        html = """
        <a href="https://at.propylo.com?search">Suche</a>
        <a href="https://at.propylo.com/immobilie/123">alt detail form</a>
        <a href="https://at.propylo.com/immobilien/wien">Wien</a>
        """
        scraper = hm.PropyloScraper(session=None)
        assert scraper._parse_cards(html) == []


class TestLystioApiParsing:
    """Lystio's search API (POST https://api.lystio.at/tenement/search)."""

    DATA = {
        "res": [
            {
                "id": 299151,
                "title": "Ferienhaus am Sulmsee",
                "rentDisplay": [28000, 28000, False],
                # The per-m² figure must never be read as the price.
                "rentPerDisplay": [933.33, 933.33, True],
                "pathSegments": ["kaufen", "haus", "steiermark", "leibnitz"],
            },
            {
                "id": 197128,
                "title": "",
                "projectTitle": "Projekt in 1100 Wien",
                "rentDisplay": [4500, 69000, False],  # a project card: min is used
                "pathSegments": ["kaufen", "gewerbe", "wien", "favoriten"],
            },
            {"id": 5, "title": "Preis auf Anfrage", "rentDisplay": [None, None, False]},
        ],
        "paging": {"pageCount": 1},
    }

    def test_extracts_id_title_url_price(self, hm):
        cards = hm.LystioScraper(session=None)._parse_items(self.DATA)
        assert cards == [
            (
                "lys_299151",
                "Ferienhaus am Sulmsee",
                "https://lystio.at/kaufen/haus/steiermark/leibnitz/299151",
                28000.0,
            ),
            (
                "lys_197128",
                "Projekt in 1100 Wien",
                "https://lystio.at/kaufen/gewerbe/wien/favoriten/197128",
                4500.0,
            ),
            ("lys_5", "Preis auf Anfrage", "https://lystio.at", 0.0),
        ]

    def test_pages_until_page_count(self, hm):
        def _page(n):
            item = {"id": n, "title": f"Haus {n}", "rentDisplay": [30000, 30000, False]}
            return json.dumps(
                {"res": [item], "paging": {"pageCount": 2, "page": n, "cardCount": 2}}
            )

        class _ApiSession:
            def __init__(self):
                self.pages = []

            def post(self, url, json=None, **kwargs):
                self.pages.append(json["paging"]["page"])
                return _FakeResponse(body=_page(json["paging"]["page"]))

        session = _ApiSession()
        scraper = hm.LystioScraper(session=session)
        listings = asyncio.run(scraper.fetch_listings())
        assert session.pages == [1, 2]
        assert [listing.id for listing in listings] == ["lys_1", "lys_2"]
        assert scraper.coverage == (2, 2)


class TestHegerRealCardParsing:
    # Mirrors the real hegerreal.at (Justimmo) list markup: a div.realty-wrapper
    # per listing, title in h3 > a (href="/objekt/<id>?from=..."), and a
    # short-info <li> list where one li holds the area and another the price,
    # each split into a .list-item-desc label + .list-item-value.
    HTML = """
    <div class="realty-wrapper w-100">
      <div class="text-cell">
        <h3 class="mt-0 mb-1">
          <a href="/objekt/17035357?from=899260" title="Immobilie im Detail">EINFAMILIENHAUS - ECKGRUNDSTÜCK</a>
        </h3>
        <ul class="short-info">
          <li class="info-rooms"><span class="list-item-desc">Zimmer</span><span class="list-item-value">4</span></li>
          <li class="info-area"><span class="list-item-desc">Fläche</span><span class="list-item-value">ca. 120,00 m<sup>2</sup></span></li>
          <li class="info-price"><span class="list-item-desc">Kaufpreis</span><span class="list-item-value">349.000,00&nbsp;€</span></li>
        </ul>
      </div>
    </div>
    """

    def test_extracts_id_title_url_price(self, hm):
        scraper = hm.HegerRealScraper(session=None)
        cards = scraper._parse_cards(self.HTML)
        assert len(cards) == 1
        listing_id, title, url, price = cards[0]
        assert listing_id == "heger_17035357"
        assert title == "EINFAMILIENHAUS - ECKGRUNDSTÜCK"
        assert url == "/objekt/17035357?from=899260"
        # Must read the Kaufpreis li, not the area li that also carries digits.
        assert price == 349000.0

    def test_rental_without_kaufpreis_parses_to_zero(self, hm):
        # Rentals show a "Miete" row instead of "Kaufpreis" -> no price -> 0.0,
        # which the caller's price==0 filter drops.
        html = """
        <div class="realty-wrapper">
          <h3><a href="/objekt/17208961?from=899260">2 ZIMMER - LOGGIA</a></h3>
          <ul class="short-info">
            <li><span class="list-item-desc">Miete</span><span class="list-item-value">799,00&nbsp;€</span></li>
          </ul>
        </div>
        """
        scraper = hm.HegerRealScraper(session=None)
        cards = scraper._parse_cards(html)
        assert len(cards) == 1
        assert cards[0][3] == 0.0

    def test_empty_page_yields_no_cards(self, hm):
        scraper = hm.HegerRealScraper(session=None)
        assert scraper._parse_cards("<div class='container'></div>") == []


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

    # The markup since the ~2026-08 redesign (trimmed from a live page): the
    # price facts are <li class="PriceKeyFact-…"> with no "PriceKeyFacts" <ul>.
    # The old selector read no price from any card, so every listing was
    # dropped as price=0 for weeks.
    HTML_2026 = """
    <ol data-testid="results-items">
      <li class="ListingCard-listing-card-X9D">
        <a href="/expose/69f0d0744631ea0b033b5119"><h2>Mobilheim am See</h2>
          <ul class="FlexBox-flexbox-X4q">
            <li class="PriceKeyFact-price-key-fact-UBF FlexBox-flexbox-X4q">
              <span class="Text-font-weight--bold-MjP">10.000 €</span></li>
          </ul></a>
      </li>
      <li class="AdSlot-ad-slot-1Qx"></li>
      <li class="ListingCard-listing-card-X9D">
        <a href="/expose/6ac8c8730fef2ebeab6c7030"><h2>Einfamilienhaus</h2>
          <ul class="FlexBox-flexbox-X4q">
            <li class="PriceKeyFact-price-key-fact-UBF"><span>ab 201,19 €/m²</span></li>
            <li class="PriceKeyFact-price-key-fact-UBF"><span>ab 16.900 €</span></li>
          </ul></a>
      </li>
      <li class="ListingCard-listing-card-X9D">
        <a href="/expose/6a7c61bc51295b3defb5c154"><h2>Besichtigung am Mittwoch</h2>
          <ul><li class="PriceKeyFact-price-key-fact-UBF">
            <span>29.000 €</span> <span>statt 35.000 €</span> <span>-17%</span></li></ul></a>
      </li>
    </ol>
    """

    def test_current_markup_prices_are_read(self, hm):
        cards = hm.ImmoScout24Scraper(session=None)._parse_cards(self.HTML_2026)
        # The ad slot (no link) is skipped; the €/m² fact never wins over the
        # total price; a discounted price reads the current one, not the old.
        assert [(c[0], c[3]) for c in cards] == [
            ("is24_69f0d0744631ea0b033b5119", 10000.0),
            ("is24_6ac8c8730fef2ebeab6c7030", 16900.0),
            ("is24_6a7c61bc51295b3defb5c154", 29000.0),
        ]


class TestDibeoApiParsing:
    """Dibeo's JSON API (GET /api/realEstate/list, Spring-style pages)."""

    DATA = {
        "content": [
            {
                "id": 2290001,
                "title": "Kleines Bauernhaus",
                "minPrice": 70000.0,
                "maxPrice": 70000.0,
            },
            {"id": 2290000, "title": "Grund", "minPrice": 95.0, "pricePerSqMeter": True},
            {"id": 2280000, "title": "Preis auf Anfrage", "minPrice": None},
        ],
        "last": True,
    }

    def test_extracts_id_title_url_price(self, hm):
        cards = hm.DibeoScraper(session=None)._parse_items(self.DATA)
        # A per-m² figure is skipped; a missing price parses as 0.0 (dropped
        # later by the price filter).
        assert cards == [
            ("dibeo_2290001", "Kleines Bauernhaus", "https://www.dibeo.at/expose/2290001", 70000.0),
            ("dibeo_2280000", "Preis auf Anfrage", "https://www.dibeo.at/expose/2280000", 0.0),
        ]

    def test_pages_until_the_last_flag(self, hm):
        def _page(number, last):
            item = {"id": 100 + number, "title": f"Haus {number}", "minPrice": 30000.0}
            return json.dumps({"content": [item], "last": last, "totalElements": 2})

        class _PagedSession:
            def __init__(self):
                self.pages = []

            def get(self, url, params=None, **kwargs):
                self.pages.append(params["page"])
                return _FakeResponse(body=_page(int(params["page"]), params["page"] == "1"))

        session = _PagedSession()
        scraper = hm.DibeoScraper(session=session)
        listings = asyncio.run(scraper.fetch_listings())
        assert session.pages == ["0", "1"]
        assert [listing.id for listing in listings] == ["dibeo_100", "dibeo_101"]
        assert scraper.coverage == (2, 2)  # (received, the site's own total)


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


class TestImmobilienDeApiParsing:
    """The site's public REST API (POST /api/rest/estates/search)."""

    DATA = {
        "items": [
            {
                "legacyId": 10053073,
                "title": "Charmantes Einfamilienhaus",
                "purchasePrice": "16900.00",
                "country": "at",
            },
            {"legacyId": 10053366, "title": "Landhaus", "purchasePrice": None, "country": "at"},
            {
                "legacyId": 9604844,
                "title": "Apartment",
                "purchasePrice": "63000.00",
                "country": "es",
            },
        ],
        "nextCursor": None,
    }

    def test_extracts_id_title_url_price(self, hm):
        cards = hm.ImmobilienDeScraper(session=None)._parse_items(self.DATA)
        # Price on request -> 0.0 (dropped later by the price filter); the
        # non-Austrian item is skipped.
        assert cards == [
            (
                "imde_10053073",
                "Charmantes Einfamilienhaus",
                "https://www.immobilien.de/expose/10053073",
                16900.0,
            ),
            ("imde_10053366", "Landhaus", "https://www.immobilien.de/expose/10053366", 0.0),
        ]

    def test_follows_the_cursor_and_echoes_the_csrf_cookie(self, hm):
        class _Cookie:
            key, value = "csrf-token", "tok123"

        def _page(legacy_id, title, price, next_cursor):
            item = {"legacyId": legacy_id, "title": title, "purchasePrice": price, "country": "at"}
            return json.dumps({"items": [item], "nextCursor": next_cursor, "total": 2})

        pages = {None: _page(1, "A", "20000", "c2"), "c2": _page(2, "B", "30000", None)}

        class _ApiSession:
            cookie_jar = [_Cookie()]

            def __init__(self):
                self.bodies, self.headers = [], []

            def get(self, url, **kwargs):
                return _FakeResponse(body="{}")

            def post(self, url, json=None, headers=None, **kwargs):
                self.bodies.append(dict(json))
                self.headers.append(headers)
                return _FakeResponse(body=pages[json.get("cursor")])

        session = _ApiSession()
        scraper = hm.ImmobilienDeScraper(session=session)
        listings = asyncio.run(scraper.fetch_listings())
        assert [listing.id for listing in listings] == ["imde_1", "imde_2"]
        assert [b.get("cursor") for b in session.bodies] == [None, "c2"]
        assert all(h == {"x-csrf-token": "tok123"} for h in session.headers)
        assert scraper.incomplete is False
        assert session.bodies[0]["count"] is True  # asks the API for its total
        assert scraper.coverage == (2, 2)


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

    @staticmethod
    def _advert(price, display, area):
        return {
            "id": 1,
            "description": "Wochenendhaus",
            "attributes": {
                "attribute": [
                    {"name": "URL_SLUG", "values": ["/iad/x"]},
                    {"name": "PRICE", "values": [price]},
                    {"name": "PRICE_FOR_DISPLAY", "values": [display]},
                    {"name": "ESTATE_SIZE", "values": [area]},
                    {"name": "PRICE/SQUARE_METER", "values": ["950"]},
                ]
            },
        }

    def test_total_price_of_an_ad_with_an_area_is_kept(self, hm):
        # Regression: the old "price × area > EUR_PRICE_TO => per-m² price"
        # guess dropped this real 19 000 € / 20 m² ad (and ~149 others a day).
        scraper = hm.WillhabenScraper(session=None)
        results = scraper._parse_json_adverts([self._advert("19000", "€ 19.000", "20")])
        assert [r[3] for r in results] == [19000.0]

    def test_explicit_per_square_meter_display_price_is_excluded(self, hm):
        scraper = hm.WillhabenScraper(session=None)
        assert scraper._parse_json_adverts([self._advert("6700", "€ 6.700/m²", "15")]) == []

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


def test_blacklist_catches_both_spellings_of_presshaus(hm):
    # str.lower() keeps "ß", so "Preßhaus" needs its own entry next to "Presshaus".
    for title in ("Traditionelles Presshaus mit Keller", "Uriges Preßhaus in der Kellergasse"):
        assert any(word.lower() in title.lower() for word in hm.BLACKLIST), title


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
        hm.HegerRealScraper,
        hm.PropyloScraper,
        hm.LystioScraper,
    ]
    for cls in scraper_classes:
        assert cls(session=None) is not None


# ---------------------------------------------------------------------------
# fetch_text: retry on dropped keep-alive connections (the recurring
# "[Errno 32] Broken pipe" on Findheim/Raiffeisen/Willhaben page 2)
# ---------------------------------------------------------------------------


class _FakeResponse:
    # Deliberately a wrong charset label: fetch_bytes must hand it through
    # untouched so decode_utf8_or_latin1 can distrust it (the Goldgrube case).
    charset = "iso-8859-1"

    def __init__(self, status=200, body="ok"):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return self._body

    async def read(self):
        return self._body.encode("utf-8")

    async def json(self, content_type=None):
        return json.loads(self._body or "{}")


class _RaisingContext:
    """Mimics aiohttp's request context manager blowing up on __aenter__."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _FlakySession:
    """session.get() fails with a connection error the first `failures` times."""

    def __init__(self, failures, status=200, body="ok"):
        self.failures = failures
        self.status = status
        self.body = body
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            return _RaisingContext(aiohttp.ClientOSError(32, "Broken pipe"))
        return _FakeResponse(status=self.status, body=self.body)


class TestFetchText:
    def test_retries_once_on_broken_pipe_then_succeeds(self):
        session = _FlakySession(failures=1)
        status, body = asyncio.run(fetch_text(session, "http://x", backoff=0))
        assert (status, body) == (200, "ok")
        assert session.calls == 2

    def test_gives_up_after_all_attempts_and_reraises(self):
        session = _FlakySession(failures=99)
        with pytest.raises(aiohttp.ClientConnectionError):
            asyncio.run(fetch_text(session, "http://x", attempts=3, backoff=0))
        assert session.calls == 3

    def test_non_200_is_returned_not_retried(self):
        # HTTP error statuses are the callers' business (their own
        # status-handling/break logic) - fetch_text must not retry them.
        session = _FlakySession(failures=0, status=404, body="not found")
        status, _ = asyncio.run(fetch_text(session, "http://x", backoff=0))
        assert status == 404
        assert session.calls == 1

    def test_fetch_bytes_retries_and_returns_raw_body_and_charset(self):
        # The Goldgrube path: raw bytes + the (possibly wrong) declared
        # charset come back untouched, so decode_utf8_or_latin1 stays in
        # charge of decoding — with the same retry as fetch_text.
        session = _FlakySession(failures=1, body="Grünen")
        status, raw, charset = asyncio.run(fetch_bytes(session, "http://x", backoff=0))
        assert status == 200
        assert raw == "Grünen".encode("utf-8")
        assert charset == "iso-8859-1"
        assert session.calls == 2


# ---------------------------------------------------------------------------
# _scrape_and_notify: collecting failed sources for the same-day retry pass
# ---------------------------------------------------------------------------


class _StubNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class _OkScraper:
    incomplete = False

    def __init__(self, listings):
        self._listings = listings

    async def fetch_listings(self):
        return self._listings


class _IncompleteScraper(_OkScraper):
    """Returned partial results, but flagged itself incomplete (page error)."""

    incomplete = True


class _CrashingScraper:
    async def fetch_listings(self):
        raise RuntimeError("boom")


class TestScrapeAndNotifyRetryCollection:
    @staticmethod
    def _listing(hm, id_, title, price, source="findheim.at"):
        return hm.Listing(
            id=id_,
            title=title,
            price=price,
            url=f"http://example.test/{id_}",
            source=source,
            first_seen="2026-07-13T16:00:00",
            last_seen="2026-07-13T16:00:00",
        )

    def test_failed_sources_returned_and_partial_results_still_notified(self, hm, tmp_path):
        monitor = _new_monitor(hm)
        monitor.notifier = _StubNotifier()

        ok = _OkScraper([self._listing(hm, "fh_ok1", "Haus in Graz", 30000.0)])
        partial = _IncompleteScraper(
            [self._listing(hm, "wh_p1", "Keller in Wien", 15000.0, source="willhaben.at")]
        )
        crashed = _CrashingScraper()

        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(tmp_path / "seen.json")
        try:
            failed = asyncio.run(monitor._scrape_and_notify([ok, partial, crashed]))
        finally:
            hm.DATA_FILE = original_data_file

        # The crashed AND the partial scraper are queued for retry; the healthy
        # one is not.
        assert failed == [partial, crashed]
        # The incomplete scraper's partial results are NOT thrown away: they go
        # out in the main email immediately.
        assert len(monitor.notifier.sent) == 1
        body = monitor.notifier.sent[0][1]
        assert "Haus in Graz" in body
        assert "Keller in Wien" in body

    def test_healthy_run_returns_no_failures_and_sends_nothing(self, hm, tmp_path):
        monitor = _new_monitor(hm)
        monitor.notifier = _StubNotifier()

        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(tmp_path / "seen.json")
        try:
            failed = asyncio.run(monitor._scrape_and_notify([_OkScraper([])]))
        finally:
            hm.DATA_FILE = original_data_file

        assert failed == []
        assert monitor.notifier.sent == []


# ---------------------------------------------------------------------------
# Every scraper flags itself incomplete on a fetch error, so the same-day
# retry pass covers all sources — not just the few that used to hit broken
# pipes (sources that timed out at 16:00 were otherwise skipped until the
# next day's run).
# ---------------------------------------------------------------------------


class _TimeoutSession:
    """Every request times out — the 16:00 concurrent-peak failure mode."""

    def get(self, url, **kwargs):
        return _RaisingContext(asyncio.TimeoutError())

    post = get


class _EmptyPageSession:
    """Every request succeeds with an empty page: a clean run with 0 results."""

    def get(self, url, **kwargs):
        return _FakeResponse(body="")

    post = get


class _FakeUrlopenResponse:
    """Immokralle fetches through urllib, not the aiohttp session."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def _urlopen_timeout(*args, **kwargs):
    raise TimeoutError("timed out")


@pytest.mark.parametrize("name", _scrapers_module.__all__)
class TestEveryScraperFlagsIncomplete:
    def test_fetch_error_sets_incomplete(self, hm, name, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_timeout)
        scraper = getattr(hm, name)(session=_TimeoutSession())
        asyncio.run(scraper.fetch_listings())
        assert scraper.incomplete is True

    def test_flag_resets_on_a_clean_run(self, hm, name, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeUrlopenResponse())
        scraper = getattr(hm, name)(session=_EmptyPageSession())
        scraper.incomplete = True  # left over from an earlier failed run
        assert asyncio.run(scraper.fetch_listings()) == []
        assert scraper.incomplete is False


class _StatusSession:
    """Every request is answered with the given (non-200) HTTP status."""

    def __init__(self, status):
        self.status = status

    def get(self, url, **kwargs):
        return _FakeResponse(status=self.status, body="")

    post = get


def _urlopen_http_503(url, *args, **kwargs):
    raise urllib.error.HTTPError(getattr(url, "full_url", url), 503, "unavailable", None, None)


@pytest.mark.parametrize("name", _scrapers_module.__all__)
class TestEveryScraperProbe:
    """probe() feeds the health check: it must parse with the scraper's own
    code and let every failure through (an error must never look like an
    empty page)."""

    def test_empty_page_parses_to_no_cards(self, hm, name, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeUrlopenResponse())
        scraper = getattr(hm, name)(session=_EmptyPageSession())
        assert asyncio.run(scraper.probe()) == []

    def test_timeout_propagates(self, hm, name, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_timeout)
        scraper = getattr(hm, name)(session=_TimeoutSession())
        with pytest.raises(TimeoutError):
            asyncio.run(scraper.probe())

    def test_non_200_propagates(self, hm, name, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", _urlopen_http_503)
        scraper = getattr(hm, name)(session=_StatusSession(503))
        with pytest.raises((HTTPStatusError, urllib.error.HTTPError)):
            asyncio.run(scraper.probe())


# ---------------------------------------------------------------------------
# Daily source health check (house_monitor/health.py)
# ---------------------------------------------------------------------------


class _ProbeScraper:
    """A stand-in source: probe() returns `cards`, or raises `error` for the
    first `failures` calls."""

    def __init__(self, cards=(), error=None, failures=0):
        self.cards = list(cards)
        self.error = error
        self.failures = failures
        self.probes = 0

    async def probe(self):
        self.probes += 1
        if self.error is not None and self.probes <= self.failures:
            raise self.error
        return self.cards


_PRICED = [("x_1", "Haus", "http://example.test/1", 250000.0)]
_UNPRICED = [("x_1", "Haus", "http://example.test/1", 0.0)]


@pytest.fixture
def no_probe_delay(monkeypatch):
    monkeypatch.setattr(_health, "HEALTH_PROBE_RETRY_DELAY", 0)


class TestHealthDiagnose:
    def test_source_with_results_is_healthy_without_probing(self):
        scraper = _ProbeScraper()
        assert asyncio.run(_health.diagnose(scraper, 5, fetch_failed=False)) is None
        assert scraper.probes == 0

    def test_failed_fetch_with_zero_results_is_broken_without_probing(self):
        scraper = _ProbeScraper(cards=_PRICED)
        reason = asyncio.run(_health.diagnose(scraper, 0, fetch_failed=True))
        assert "hibával" in reason
        assert scraper.probes == 0

    def test_nothing_in_price_range_but_site_works_is_healthy(self):
        # The OhneMakler/Immobilien.de case: 0 in 3000-70000, but the
        # unfiltered page has priced listings.
        assert asyncio.run(_health.diagnose(_ProbeScraper(cards=_PRICED), 0, False)) is None

    def test_no_cards_on_the_unfiltered_page_is_broken(self):
        reason = asyncio.run(_health.diagnose(_ProbeScraper(cards=[]), 0, False))
        assert "sem talált" in reason

    def test_cards_without_any_price_is_broken(self):
        # The ImmoScout24 case: cards are found, but no price can be read.
        reason = asyncio.run(_health.diagnose(_ProbeScraper(cards=_UNPRICED * 3), 0, False))
        assert "3 hirdetés" in reason and "árat" in reason

    def test_probe_error_is_retried_then_reported_with_its_type(self, no_probe_delay):
        scraper = _ProbeScraper(error=asyncio.TimeoutError(), failures=99)
        reason = asyncio.run(_health.diagnose(scraper, 0, False))
        assert scraper.probes == _health.HEALTH_PROBE_ATTEMPTS
        # str(TimeoutError()) is '' - the type name must still show up.
        assert "TimeoutError" in reason

    def test_one_off_probe_error_recovers_on_retry(self, no_probe_delay):
        scraper = _ProbeScraper(cards=_PRICED, error=ConnectionResetError(), failures=1)
        assert asyncio.run(_health.diagnose(scraper, 0, False)) is None
        assert scraper.probes == 2


def test_format_error_keeps_the_type_of_an_empty_message():
    assert _health.format_error(asyncio.TimeoutError()) == "TimeoutError"
    assert _health.format_error(ValueError("bad")) == "ValueError: bad"


class TestStreaksAndAlertText:
    def test_update_streaks_counts_consecutive_days(self):
        previous = {
            "source:A": {"days": 1, "last": "2026-10-09", "reason": "old"},
            "source:B": {"days": 3, "last": "2026-10-09", "reason": "gone today"},
            "source:C": {"days": 5, "last": "2026-10-07", "reason": "a day was skipped"},
        }
        findings = {"source:A": "r", "source:C": "again", "internal:db_save": "x"}
        streaks = _health.update_streaks(previous, findings, "2026-10-10")
        assert {key: s["days"] for key, s in streaks.items()} == {
            "source:A": 2,  # yesterday too -> continues
            "source:C": 1,  # not yesterday -> starts over
            "internal:db_save": 1,
        }  # B wasn't found today -> dropped
        assert streaks["source:A"] == {"days": 2, "last": "2026-10-10", "reason": "r"}

    def test_due_alerts_need_two_days_and_get_readable_labels(self):
        streaks = {
            "source:ImmoScout24": {"days": 2, "last": "d", "reason": "r1"},
            "source:Propylo": {"days": 1, "last": "d", "reason": "r2"},
            "internal:email_send": {"days": 3, "last": "d", "reason": "r3"},
        }
        assert _health.due_alerts(streaks) == [
            {"label": "Email-küldés", "days": 3, "reason": "r3"},
            {"label": "ImmoScout24", "days": 2, "reason": "r1"},
        ]

    def test_missed_days(self):
        assert _health.missed_days("2026-10-07", "2026-10-10") == ["2026-10-08", "2026-10-09"]
        assert _health.missed_days("2026-10-09", "2026-10-10") == []
        assert _health.missed_days(None, "2026-10-10") == []

    def test_subject_names_the_errors(self):
        assert _health.email_subject(5, 0) == "Ingatlanok: 5 db"
        assert _health.email_subject(5, 2) == "Ingatlanok: 5 db + 2 hiba"
        assert _health.email_subject(0, 1) == "Ingatlanok: 1 hiba"

    def test_alert_section(self):
        assert _health.build_alert_section([]) == ""
        text = _health.build_alert_section(
            [
                {"label": "ImmoScout24", "days": 2, "reason": "nincs ár"},
                {"label": "Kimaradt futás", "days": None, "reason": "nem futott"},
            ]
        )
        assert "HIBÁK" in text
        assert "- ImmoScout24 (2. napja): nincs ár\n" in text
        assert "- Kimaradt futás: nem futott\n" in text


def _yesterday():
    return (datetime.now().date() - timedelta(days=1)).isoformat()


def _today():
    return datetime.now().date().isoformat()


@pytest.fixture
def state_file(hm, tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    monkeypatch.setattr(hm, "HEALTH_STATE_FILE", str(path))
    monkeypatch.setattr(hm, "DATA_FILE", str(tmp_path / "seen.json"))
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class _ResultNotifier(_StubNotifier):
    def __init__(self, result=True):
        super().__init__()
        self.result = result

    async def send(self, subject, body):
        self.sent.append((subject, body))
        return self.result


class TestEndOfDayCheck:
    def test_a_source_broken_on_two_days_becomes_a_pending_alert(self, hm, state_file):
        monitor = _new_monitor(hm)
        broken = _ProbeScraper(cards=[])  # 0 today, and the unfiltered page is empty
        monitor.day_counts = {"_ProbeScraper": 0}

        asyncio.run(monitor._end_of_day_check([broken], []))  # day 1
        assert _read(state_file)["pending_alerts"] == []

        state = _read(state_file)
        state["last_check_date"] = _yesterday()
        state["streaks"]["source:_ProbeScraper"]["last"] = _yesterday()
        state_file.write_text(json.dumps(state), encoding="utf-8")
        asyncio.run(monitor._end_of_day_check([broken], []))  # day 2
        alerts = _read(state_file)["pending_alerts"]
        assert [(a["label"], a["days"]) for a in alerts] == [("_ProbeScraper", 2)]

    def test_a_second_check_on_the_same_day_counts_nothing(self, hm, state_file):
        monitor = _new_monitor(hm)
        monitor.day_counts = {"_ProbeScraper": 0}
        asyncio.run(monitor._end_of_day_check([_ProbeScraper(cards=[])], []))
        asyncio.run(monitor._end_of_day_check([_ProbeScraper(cards=[])], []))
        assert _read(state_file)["streaks"]["source:_ProbeScraper"]["days"] == 1

    def test_own_errors_of_two_days_become_a_pending_alert(self, hm, state_file):
        state_file.write_text(
            json.dumps(
                {
                    "last_check_date": _yesterday(),
                    "streaks": {
                        "internal:email_send": {"days": 1, "last": _yesterday(), "reason": "x"}
                    },
                }
            ),
            encoding="utf-8",
        )
        monitor = _new_monitor(hm)
        monitor.today_errors = {"email_send": "nem sikerült elküldeni"}
        asyncio.run(monitor._end_of_day_check([], [], check_sources=False))
        assert _read(state_file)["pending_alerts"] == [
            {"label": "Email-küldés", "days": 2, "reason": "nem sikerült elküldeni"}
        ]
        assert monitor.today_errors == {}  # counted, so cleared

    def test_a_crash_inside_the_check_never_propagates(self, hm, tmp_path, monkeypatch):
        # An unwritable state path makes save_state raise.
        monkeypatch.setattr(hm, "HEALTH_STATE_FILE", str(tmp_path / "missing-dir" / "h.json"))
        monitor = _new_monitor(hm)
        asyncio.run(monitor._end_of_day_check([_ProbeScraper(cards=[])], []))  # must not raise
        assert "health_check" in monitor.today_errors


class TestErrorsInTheDailyEmail:
    ALERT = {"label": "ImmoScout24", "days": 2, "reason": "nincs ár"}

    def test_errors_alone_still_send_an_email_flagged_hiba(self, hm, state_file):
        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier()
        asyncio.run(monitor._scrape_and_notify([_OkScraper([])], alerts=[self.ALERT]))
        subject, body = monitor.notifier.sent[0]
        assert subject == "Ingatlanok: 1 hiba"
        assert "ImmoScout24 (2. napja): nincs ár" in body

    def test_listings_and_errors_share_one_email(self, hm, state_file):
        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier()
        listing = TestScrapeAndNotifyRetryCollection._listing(hm, "fh_1", "Haus", 30000.0)
        asyncio.run(monitor._scrape_and_notify([_OkScraper([listing])], alerts=[self.ALERT]))
        subject, body = monitor.notifier.sent[0]
        assert subject == "Ingatlanok: 1 db + 1 hiba"
        assert body.index("Haus") < body.index("HIBÁK")

    def test_a_failed_send_is_an_error_of_the_day(self, hm, state_file):
        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier(result=False)
        listing = TestScrapeAndNotifyRetryCollection._listing(hm, "fh_1", "Haus", 30000.0)
        asyncio.run(monitor._scrape_and_notify([_OkScraper([listing])]))
        assert monitor.last_email_ok is False
        assert "email_send" in monitor.today_errors
        assert "fh_1" not in monitor.seen  # not persisted: re-sent on the next run

    def test_daily_run_reports_pending_alerts_once_sent(self, hm, state_file):
        state_file.write_text(json.dumps({"pending_alerts": [self.ALERT]}), encoding="utf-8")
        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier()
        asyncio.run(monitor._daily_run([_OkScraper([])]))
        assert monitor.notifier.sent[0][0] == "Ingatlanok: 1 hiba"
        state = _read(state_file)
        assert state["pending_alerts"] == []
        assert state["last_run_date"] == _today()

    def test_pending_alerts_survive_a_failed_send(self, hm, state_file):
        state_file.write_text(json.dumps({"pending_alerts": [self.ALERT]}), encoding="utf-8")
        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier(result=False)
        asyncio.run(monitor._daily_run([_OkScraper([])]))
        assert _read(state_file)["pending_alerts"] == [self.ALERT]

    def test_two_or_more_missed_days_are_reported(self, hm, state_file):
        three_days_ago = (datetime.now().date() - timedelta(days=3)).isoformat()
        state_file.write_text(json.dumps({"last_run_date": three_days_ago}), encoding="utf-8")
        monitor = _new_monitor(hm)
        alerts = monitor._start_of_day()
        assert [a["label"] for a in alerts] == ["Kimaradt futás"]
        assert "2 napig nem futott" in alerts[0]["reason"]

    def test_a_single_missed_day_is_not_reported(self, hm, state_file):
        two_days_ago = (datetime.now().date() - timedelta(days=2)).isoformat()
        state_file.write_text(json.dumps({"last_run_date": two_days_ago}), encoding="utf-8")
        assert _new_monitor(hm)._start_of_day() == []


class TestPartialLossRules:
    def test_drop_needs_history_and_a_real_baseline(self):
        assert _health.drop_reason([20] * 6, 0) is None  # too little history
        assert _health.drop_reason([3] * 10, 0) is None  # median below 5
        assert _health.drop_reason([20] * 10, 9) is None  # 9 >= 0.4 * 20
        assert "ma csak 7 találat" in _health.drop_reason([20] * 10, 7)

    def test_history_keeps_a_window(self):
        assert _health.push_history(list(range(14)), 99) == list(range(1, 14)) + [99]

    def test_coverage(self):
        assert _health.coverage_reason(54, 54) is None
        assert _health.coverage_reason(44, 54) is None  # 81%: within tolerance
        assert "54 találat" in _health.coverage_reason(29, 54)
        assert _health.coverage_reason(0, 0) is None


class _NamedScraper(_OkScraper):
    """A source with a fixed day count and optional coverage."""

    def __init__(self, coverage=None):
        super().__init__([])
        self.coverage = coverage

    async def probe(self):
        return _PRICED


class TestPartialLossFindings:
    def test_drop_coverage_unmatched_and_late(self, hm):
        monitor = _new_monitor(hm)
        dropping, short, late = _NamedScraper(), _NamedScraper(coverage=(29, 54)), object()
        monitor.day_counts = {"_NamedScraper": 3}
        monitor.day_unmatched = {"Willhaben": 2}
        monitor.day_late = [late]
        state = {"count_history": {"_NamedScraper": [20] * 10}}
        findings = {}
        monitor._source_findings_without_probe([dropping, short], state, findings)
        assert "ma csak 3 találat" in findings["drop:_NamedScraper"]
        assert "54 találat" in findings["coverage:_NamedScraper"]
        assert "2 olyan" in findings["missing:Willhaben"]
        assert "újrapróbálásból" in findings["late:object"]
        assert state["count_history"]["_NamedScraper"][-1] == 3

    def test_one_unmatched_house_a_day_is_tolerated(self, hm):
        monitor = _new_monitor(hm)
        monitor.day_unmatched = {"Willhaben": 1}
        findings = {}
        monitor._source_findings_without_probe([], {}, findings)
        assert findings == {}

    def test_a_broken_source_is_not_also_dropping(self, hm, state_file):
        state_file.write_text(
            json.dumps({"count_history": {"_ProbeScraper": [20] * 10}}), encoding="utf-8"
        )
        monitor = _new_monitor(hm)
        monitor.day_counts = {"_ProbeScraper": 0}
        asyncio.run(monitor._end_of_day_check([_ProbeScraper(cards=[])], []))
        assert set(_read(state_file)["streaks"]) == {"source:_ProbeScraper"}


class _FlakyOnceScraper(_OkScraper):
    """Fails at 16:00, succeeds on the retry."""

    def __init__(self):
        super().__init__([])
        self.calls = 0

    async def fetch_listings(self):
        self.calls += 1
        self.incomplete = self.calls == 1
        return []


def test_memory_snapshot_never_raises(hm):
    snapshot = hm.HouseMonitor._memory_snapshot()
    assert snapshot.startswith("VmRSS=") or snapshot.startswith("unavailable")


class TestLateSourcesAndCrossCheckTotals:
    def test_a_source_rescued_by_the_retry_is_late(self, hm, state_file, monkeypatch):
        monkeypatch.setattr(hm, "INCOMPLETE_RETRY_DELAYS", (0,))
        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier()
        flaky, fine = _FlakyOnceScraper(), _OkScraper([])
        assert asyncio.run(monitor._daily_run([flaky, fine])) == []
        assert monitor.day_late == [flaky]

    def test_unmatched_houses_add_up_over_the_day(self, hm, state_file):
        class _Screening(_ScreeningScraper):
            async def screen_listings(self, listings, stored_url, is_known):
                self.unmatched_origins = {"Willhaben": 1}
                return listings, [], []

        monitor = _new_monitor(hm)
        monitor.notifier = _ResultNotifier()
        scraper = _Screening([])
        asyncio.run(monitor._scrape_and_notify([scraper]))
        asyncio.run(monitor._scrape_and_notify([scraper]))  # e.g. the retry pass
        assert monitor.day_unmatched == {"Willhaben": 2}


# ---------------------------------------------------------------------------
# Propylo: resolving aggregator cards to the original ads
# ---------------------------------------------------------------------------


_PRO = "https://at.propylo.com/verkaufsimmobilie/"
_WH_HOUSE = (
    "https://www.willhaben.at/iad/immobilien/d/haus-kaufen/burgenland/x/mobilheim-am-see-{}/"
)
_WH_FLAT = "https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/x/wohnung-{}/"


class TestPropyloOriginHelpers:
    @pytest.mark.parametrize(
        "url, key",
        [
            (_WH_HOUSE.format(1086675364), "wh_1086675364"),
            (_WH_HOUSE.format(1086675364) + "?utm_source=propylo", "wh_1086675364"),
            ("https://www.willhaben.at/iad/object?adId=1086675364", "wh_1086675364"),
            ("https://www.dibeo.at/expose/2230765", "dibeo_2230765"),
            (
                "https://www.wohnnet.at/immobilien/ferienhaus-7201-neudoerfl-kauf-297153200",
                "wn_297153200",
            ),
            (
                "https://www.immobilienscout24.at/expose/6ac7c2ae4aecd28523b42c32",
                "6ac7c2ae4aecd28523b42c32",
            ),
            ("https://www.immowelt.at/expose/eb307f82-b2c3-4078-8714-d92f7d66ce74", None),
            ("", None),
        ],
    )
    def test_origin_key(self, url, key):
        assert _propylo.origin_key(url) == key

    @pytest.mark.parametrize(
        "url, title, house",
        [
            (_WH_HOUSE.format(1), "Mobilheim", True),
            (_WH_FLAT.format(1), "Haus am See", False),  # willhaben's category wins
            ("https://www.urbanhome.at/suchen/8296709-3-zimmer-wohnung", "Schöne Lage", False),
            ("https://www.immowelt.at/expose/x", "Gemütliche Gartenwohnung", False),
            ("https://www.immowelt.at/expose/x", "Haus mit Einliegerwohnung", True),
            ("https://www.immowelt.at/expose/x", "Mobilheim am See", True),
            ("https://www.dibeo.at/expose/1", "Kompakte Garçonnière zum kleinen Preis", False),
            ("https://www.immobilienscout24.at/expose/x", "Sicherer Komfort: KFZ-Parkplatz", False),
            ("https://www.immowelt.at/expose/x", "BUNGALOW AM SEE - UFERPARZELLE", True),
            ("https://www.immowelt.at/expose/x", "Helles Büro im Zentrum", True),  # wanted
        ],
    )
    def test_is_house(self, url, title, house):
        assert _propylo.is_house(url, title) is house


class _RedirectResponse:
    def __init__(self, status, location=""):
        self.status = status
        self.headers = {"Location": location} if location else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RedirectSession:
    """GET answers from per-URL scripts of (status, Location); the last
    answer repeats. A URL without a script must never be requested."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        assert kwargs.get("allow_redirects") is False
        self.calls.append(url)
        script = self.routes[url]
        answer = script.pop(0) if len(script) > 1 else script[0]
        return _RedirectResponse(*answer)


def _pro_listing(hm, n, title="Haus", price=30000.0):
    return hm.Listing(
        id=f"pro_{n}",
        title=title,
        price=price,
        url=f"{_PRO}{n}",
        source="at.propylo.com",
        first_seen="2026-10-09T16:00:00",
        last_seen="2026-10-09T16:00:00",
    )


@pytest.fixture
def no_resolve_delay(monkeypatch):
    monkeypatch.setattr(_propylo, "PROPYLO_RESOLVE_DELAY", 0)
    monkeypatch.setattr(_propylo, "PROPYLO_RESOLVE_BACKOFF", 0)


class TestPropyloScreenListings:
    def test_sorts_cards_by_their_original_ad(self, hm, no_resolve_delay):
        session = _RedirectSession(
            {
                f"{_PRO}1": [(302, _WH_HOUSE.format(1086675111))],  # copy of a known ad
                f"{_PRO}2": [(302, _WH_FLAT.format(1086675222))],  # an apartment
                f"{_PRO}3": [(301, "https://www.immowelt.at/expose/abc")],  # new to us
                f"{_PRO}4": [(429, "")],  # rate-limited on every attempt
                f"{_PRO}5": [(429, ""), (302, "https://www.dibeo.at/expose/555")],  # 2nd try
                f"{_PRO}8": [(200, "")],  # Propylo serves the ad itself
                f"{_PRO}9": [(302, _WH_HOUSE.format(1086675999))],  # missed by our scraper
            }
        )
        scraper = hm.PropyloScraper(session)
        cards = [_pro_listing(hm, n) for n in (1, 2, 3, 4, 5, 8, 9)]
        known = {"wh_1086675111"}
        normal, silent, deferred = asyncio.run(
            scraper.screen_listings(cards, lambda lid: None, known.__contains__)
        )
        assert [c.id for c in normal] == ["pro_3", "pro_5", "pro_8", "pro_9"]
        assert [c.id for c in silent] == ["pro_1", "pro_2"]
        assert [c.id for c in deferred] == ["pro_4"]
        # A willhaben house our Willhaben scraper never returned is counted.
        assert scraper.unmatched_origins == {"Willhaben": 1}
        # Emails link the original ad; Propylo's own page stays as it is.
        assert normal[0].url == "https://www.immowelt.at/expose/abc"
        assert normal[1].url == "https://www.dibeo.at/expose/555"
        assert normal[2].url == f"{_PRO}8"
        assert session.calls.count(f"{_PRO}4") == _propylo.PROPYLO_RESOLVE_ATTEMPTS
        # Only Propylo's own card URLs are requested, never the original ads.
        assert all(url.startswith(_PRO) for url in session.calls)

    def test_cards_already_in_the_db_are_not_resolved_again(self, hm, no_resolve_delay):
        session = _RedirectSession({})  # any request would raise KeyError
        scraper = hm.PropyloScraper(session)
        stored = {"pro_6": _WH_HOUSE.format(1086675666), "pro_7": f"{_PRO}7"}
        cards = [_pro_listing(hm, 6), _pro_listing(hm, 7)]
        normal, silent, deferred = asyncio.run(
            scraper.screen_listings(cards, stored.get, {"wh_1086675666"}.__contains__)
        )
        # pro_6's original is a scraped willhaben ad: that source reports it
        # (and its price changes), so the copy stays silent.
        assert [c.id for c in silent] == ["pro_6"]
        assert silent[0].url == _WH_HOUSE.format(1086675666)
        assert [c.id for c in normal] == ["pro_7"] and deferred == []
        assert session.calls == []


class _ScreeningScraper(_OkScraper):
    """A stand-in aggregator: screen_listings() splits by title."""

    async def screen_listings(self, listings, stored_url, is_known):
        def pick(word):
            return [listing for listing in listings if word in listing.title]

        return pick("new"), pick("silent"), pick("deferred")


class TestScrapeAndNotifyScreening:
    def test_silent_are_stored_unmailed_and_deferred_are_retried(self, hm, tmp_path, monkeypatch):
        monkeypatch.setattr(hm, "DATA_FILE", str(tmp_path / "seen.json"))
        monitor = _new_monitor(hm)
        monitor.notifier = _StubNotifier()
        scraper = _ScreeningScraper(
            [
                _pro_listing(hm, 1, "a new house"),
                _pro_listing(hm, 2, "a silent copy"),
                _pro_listing(hm, 3, "a deferred card"),
                # Blacklisted: never screened, stored silently by the main loop.
                _pro_listing(hm, 4, "a deferred Sommerhaus"),
            ]
        )
        failed = asyncio.run(monitor._scrape_and_notify([scraper]))
        body = monitor.notifier.sent[0][1]
        assert "a new house" in body and "silent" not in body and "deferred" not in body
        # The deferred card isn't stored; the blacklisted one is.
        assert set(monitor.seen) == {"pro_1", "pro_2", "pro_4"}
        assert failed == [scraper]  # ... and the source is retried the same day

    def test_origin_lookup_ignores_the_aggregators_own_entries(self, hm):
        monitor = _new_monitor(hm)
        hex_id = "6ac7c2ae4aecd28523b42c32"
        own = _pro_listing(hm, 9)
        own.url = f"https://www.immobilienscout24.at/expose/{hex_id}"
        monitor.seen = {own.id: own}
        assert monitor._origin_lookup([], {"at.propylo.com"})(hex_id) is False
        twin = _pro_listing(hm, 10)
        twin.id, twin.source = f"imd_{hex_id}", "immodirekt.at"
        twin.url = f"https://www.immodirekt.at/immobilie/x-{hex_id}/"
        monitor.seen[twin.id] = twin
        assert monitor._origin_lookup([], {"at.propylo.com"})(hex_id) is True


class TestScrapeAndNotifyDbUpdate:
    _listing = staticmethod(TestScrapeAndNotifyRetryCollection._listing)

    def test_hidden_price_drop_keeps_original_first_seen(self, hm, tmp_path):
        # A price drop hidden as a cross-platform duplicate used to be stored
        # with the scraper's fresh first_seen, erasing when we first saw it.
        monitor = _new_monitor(hm)
        monitor.notifier = _StubNotifier()
        recent = datetime.now().isoformat()
        original = self._listing(hm, "heger_1", "Mobilheim mit Garten am Badesee", 25000.0)
        original.first_seen = "2026-07-23T16:00:00"
        original.last_seen = recent
        twin = self._listing(
            hm, "wh_1", "Mobilheim mit Garten am Badesee", 18000.0, source="willhaben.at"
        )
        twin.last_seen = recent
        monitor.seen = {"heger_1": original, "wh_1": twin}
        dropped = self._listing(hm, "heger_1", "Mobilheim mit Garten am Badesee", 18000.0)
        dropped.first_seen = recent

        original_data_file = hm.DATA_FILE
        hm.DATA_FILE = str(tmp_path / "seen.json")
        try:
            asyncio.run(monitor._scrape_and_notify([_OkScraper([dropped])]))
        finally:
            hm.DATA_FILE = original_data_file

        assert monitor.notifier.sent == []  # hidden as a duplicate of wh_1
        assert monitor.seen["heger_1"].price == 18000.0
        assert monitor.seen["heger_1"].first_seen == "2026-07-23T16:00:00"
