import json
import unittest
from unittest.mock import patch

import requests

import config
from modules import exhibitor_scraper


class ExhibitorIdentityAndPaginationTests(unittest.TestCase):
    _IDOS_CURRENT = """
        <article class="cv2-card">
          <div class="cv2-card-name">Alpha Gıda</div>
          <div class="cv2-country"><span>Türkiye</span></div>
          <div class="cv2-card-sectors">Gıda</div>
          <div class="cv2-stand">Hall/Salon: 5 Stand/Booth: A1</div>
          <a class="stretched-link" href="/portal/catalogue/75/alpha">Profil</a>
        </article>
        <article class="cv2-card">
          <div class="cv2-card-name">Foreign Foods</div>
          <div class="cv2-country"><span>Almanya</span></div>
          <div class="cv2-card-sectors">Gıda</div>
          <div class="cv2-stand">Hall: 6 Stand: B2</div>
          <a class="stretched-link" href="/portal/catalogue/75/foreign">Profil</a>
        </article>
    """
    _IDOS_LEGACY = """
        <div class="catalogue-card">
          <div class="exhibitor-name">Alpha Gıda</div>
          <div class="catalogue-country">Türkiye</div>
          <div class="catalogue-sectors">Gıda</div>
          <div>Salon: 5 Stand: A1</div>
          <a href="/portal/catalogue/75/alpha">Profil</a>
        </div>
    """
    _IDOS_DETAIL = """
        <div class="cv2-detail">
          <div class="cv2-detail-side">
            <h2>Alpha Gıda</h2>
            <div class="cv2-side-actions"><a href="https://alpha.example">Website</a></div>
          </div>
          <div class="cv2-prose">Gıda ürünleri.</div>
          <dl>
            <dt>Telefon</dt><dd><a href="tel:+902125550000">call</a></dd>
            <dt>Adres</dt><dd>İstanbul</dd>
          </dl>
        </div>
        <footer>Organizatör 02129990000</footer>
    """

    def test_texhibition_routes_keep_profile_source_identity(self):
        for route in ("/katilimcilar/", "/exhibitors/"):
            rows = exhibitor_scraper._texhibition_list_rows(
                f'<a href="{route}ornek-tekstil"><div class="item"><div class="title">Örnek Tekstil</div></div></a>',
                f"https://www.texhibitionist.com{route.rstrip('/')}",
            )
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["source_record_id"].startswith("texhibition_2026:"))

    def test_zuchex_pagination_preserves_ids_and_delay(self):
        pages = [
            {"data": {"view": {"exhibitors": {"nodes": [{"_id": "a", "name": "A", "withEvent": {"booth": "1"}}], "pageInfo": {"hasNextPage": True, "endCursor": "cursor-a"}, "totalCount": 2}}}},
            {"data": {"view": {"exhibitors": {"nodes": [{"_id": "b", "name": "B", "withEvent": {"booth": "2"}}], "pageInfo": {"hasNextPage": False, "endCursor": None}, "totalCount": 2}}}},
        ]
        with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
            exhibitor_scraper, "_post_graphql", side_effect=pages
        ), patch.object(exhibitor_scraper.time, "sleep") as sleep, patch.object(
            config, "MAX_ZUCHEX_PAGES", 4
        ), patch.multiple(
            config, ZUCHEX_VIEW_ID="view", ZUCHEX_EVENT_ID="event",
            ZUCHEX_FILTER_ID="filter", ZUCHEX_FILTER_VALUE_ID="value",
        ):
            rows = exhibitor_scraper.scrape_zuchex(delay_sec=0.25)
        self.assertEqual([row["_id"] for row in rows], ["a", "b"])
        sleep.assert_called_once_with(0.25)

    def test_zuchex_cursor_cycle_is_terminal(self):
        page = {"data": {"view": {"exhibitors": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "same"}, "totalCount": 0}}}}
        with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
            exhibitor_scraper, "_post_graphql", side_effect=[page, page]
        ), patch.object(config, "MAX_ZUCHEX_PAGES", 4), patch.multiple(
            config, ZUCHEX_VIEW_ID="view", ZUCHEX_EVENT_ID="event",
            ZUCHEX_FILTER_ID="filter", ZUCHEX_FILTER_VALUE_ID="value",
        ):
            with self.assertRaisesRegex(ValueError, "cursor_cycle"):
                exhibitor_scraper.scrape_zuchex(delay_sec=0)

    def test_zuchex_without_observed_profile_url_never_fetches_detail(self):
        page = {
            "data": {"view": {"exhibitors": {
                "nodes": [{"_id": "a", "name": "Alpha", "withEvent": {"booth": "1"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "totalCount": 1,
            }}}
        }
        with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
            exhibitor_scraper, "_post_graphql", return_value=page
        ) as graphql, patch.object(exhibitor_scraper, "_get") as get, patch.multiple(
            config, ZUCHEX_VIEW_ID="view", ZUCHEX_EVENT_ID="event",
            ZUCHEX_FILTER_ID="filter", ZUCHEX_FILTER_VALUE_ID="value",
        ):
            row = exhibitor_scraper.scrape_zuchex(fetch_details=True, delay_sec=0)[0]
        get.assert_not_called()
        query = graphql.call_args.args[2]["query"]
        self.assertNotIn("profileUrl", query)
        self.assertNotIn("profile_url", query)
        self.assertNotIn("/exhibitor/", row.get("profile_url", ""))
        self.assertEqual(row["source_detail_status"], "UNAVAILABLE_NO_PROFILE_URL")
        self.assertEqual(row["listed_phone_status"], "UNAVAILABLE")
        self.assertEqual(row["listed_address_status"], "UNAVAILABLE")

    def test_idos_current_cards_build_identity_and_filter_foreign_country(self):
        rows = exhibitor_scraper._idos_list_rows(
            self._IDOS_CURRENT,
            "https://crm.idos.events/portal/catalogue/75?keyword=&page=0",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "Alpha Gıda")
        self.assertEqual(rows[0]["profile_url"], "https://crm.idos.events/portal/catalogue/75/alpha")
        self.assertEqual(rows[0]["hall"], "5")
        self.assertEqual(rows[0]["stand"], "A1")
        self.assertTrue(rows[0]["source_record_id"].startswith("idos_f_istanbul:"))

    def test_idos_current_and_legacy_cards_have_same_canonical_fields(self):
        listing_url = "https://crm.idos.events/portal/catalogue/75"
        current = exhibitor_scraper._idos_list_rows(self._IDOS_CURRENT, listing_url)[0]
        legacy = exhibitor_scraper._idos_list_rows(self._IDOS_LEGACY, listing_url)[0]
        fields = ("company", "country", "profile_url", "sector", "hall", "stand", "source_record_id")
        self.assertEqual({field: current[field] for field in fields}, {field: legacy[field] for field in fields})
        for row in (current, legacy):
            self.assertEqual(row["listed_phone_status"], "NOT_REQUESTED")
            self.assertEqual(row["listed_address_status"], "NOT_REQUESTED")

    def test_idos_detail_extracts_labelled_contact_and_ignores_footer(self):
        details = exhibitor_scraper._idos_profile_details(
            self._IDOS_DETAIL,
            "https://crm.idos.events/portal/catalogue/75/alpha",
        )
        self.assertEqual(details["company"], "Alpha Gıda")
        self.assertEqual(details["website"], "https://alpha.example")
        self.assertEqual(details["listed_phone"], "02125550000")
        self.assertEqual(details["listed_address"], "İstanbul")
        self.assertNotIn("02129990000", str(details))

    @patch("modules.exhibitor_scraper.time.sleep")
    @patch("modules.exhibitor_scraper._get")
    def test_idos_identity_mismatch_does_not_merge_contact(self, get_mock, _sleep):
        get_mock.side_effect = [
            self._IDOS_CURRENT,
            self._IDOS_DETAIL.replace("Alpha Gıda", "Different Foods"),
        ]
        row = exhibitor_scraper.scrape_idos(fetch_details=True, delay_sec=0)[0]
        self.assertEqual(row["source_detail_status"], "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH")
        self.assertEqual(row["listed_phone"], "")
        self.assertEqual(row["listed_address"], "")
        self.assertEqual(row["listed_phone_status"], "UNAVAILABLE")

    @patch("modules.exhibitor_scraper.time.sleep")
    @patch("modules.exhibitor_scraper._get")
    def test_idos_fetch_error_is_unavailable(self, get_mock, _sleep):
        get_mock.side_effect = [self._IDOS_CURRENT, requests.RequestException("offline")]
        row = exhibitor_scraper.scrape_idos(fetch_details=True, delay_sec=0)[0]
        self.assertEqual(row["source_detail_status"], "UNAVAILABLE_FETCH_ERROR")
        self.assertEqual(row["listed_phone_status"], "UNAVAILABLE")

    @patch("modules.exhibitor_scraper._get")
    def test_metalexpo_never_fetches_listed_external_website(self, get_mock):
        get_mock.return_value = """
            <a href="https://company.example"><div class="katilimci-text">
              <span class="text">Metal Co</span><span class="text">HALL 3 / 3A-10</span>
            </div></a>
        """
        row = exhibitor_scraper.scrape_metalexpo(fetch_details=True, delay_sec=0)[0]
        self.assertEqual(row["listed_website"], "https://company.example")
        self.assertEqual(row["profile_url"], "")
        self.assertEqual(row["source_detail_status"], "UNAVAILABLE_NO_PROFILE_URL")
        self.assertEqual(row["listed_phone_status"], "UNAVAILABLE")
        self.assertEqual(row["listed_address_status"], "UNAVAILABLE")
        get_mock.assert_called_once()

    @patch("modules.exhibitor_scraper._get")
    def test_metalexpo_without_detail_fetch_is_not_requested(self, get_mock):
        get_mock.return_value = """
            <div class="katilimci-text"><span class="text">Metal Co</span></div>
        """
        row = exhibitor_scraper.scrape_metalexpo(fetch_details=False, delay_sec=0)[0]
        self.assertEqual(row["source_detail_status"], "NOT_REQUESTED")
        self.assertEqual(row["listed_phone_status"], "NOT_REQUESTED")
        self.assertEqual(row["listed_address_status"], "NOT_REQUESTED")

    def test_source_detail_state_helpers_follow_contract(self):
        rows = [
            exhibitor_scraper._metalexpo_list_rows(
                '<a href="https://metal.example"><div class="katilimci-text"><span class="text">Metal Co</span></div></a>'
            )[0],
            exhibitor_scraper._texhibition_list_rows(
                '<a href="/exhibitors/tex"><div class="item"><div class="title">Tex Co</div></div></a>'
                '<div data-total="1"></div>',
                "https://www.texhibitionist.com/en/exhibitors?v=1",
            )[0],
            exhibitor_scraper._idos_list_rows(self._IDOS_CURRENT)[0],
            exhibitor_scraper._brand_catalog_list_rows(
                '<a class="brand-link" href="brand/maktek"><h2 class="brand-name">Maktek Co</h2></a>',
                "https://www.maktekfuari.com",
                source="maktek_avrasya_2026", sector="makine",
            )[0],
            exhibitor_scraper._brand_catalog_list_rows(
                '<a class="brand-link" href="brand/foodist"><h2 class="brand-name">Foodist Co</h2></a>',
                "https://www.foodistexpo.com",
                source="foodist_expo_turkiye", sector="gida",
            )[0],
            {
                "company": "Zuchex Co", "source": "zuchex_2026",
                "source_record_id": "zuchex_2026:z1", "profile_url": "",
            },
            {"company": "IFCO Co", "source": "ifco", "profile_url": ""},
            {"company": "Beauty Co", "source": "beauty_eurasia", "profile_url": ""},
        ]
        required = {
            "listed_phone", "listed_address", "listed_phone_status",
            "listed_address_status", "source_detail_status", "source_evidence",
        }
        for row in rows:
            exhibitor_scraper._set_source_detail_state(
                row, "NOT_REQUESTED", str(row.get("profile_url", ""))
            )
            self.assertTrue(required.issubset(row))
            self.assertEqual(row["source_detail_status"], "NOT_REQUESTED")
            self.assertEqual(row["listed_phone_status"], "NOT_REQUESTED")
            self.assertEqual(row["listed_address_status"], "NOT_REQUESTED")

            exhibitor_scraper._record_profile_observation(
                row,
                {"listed_phone": "+90 212 555 00 00", "listed_address": "İstanbul"},
                "<main>participant</main>",
                "https://catalog.example/profile",
            )
            self.assertEqual(row["source_detail_status"], "COMPLETED")
            self.assertEqual(row["listed_phone_status"], "OBSERVED_PRESENT")
            self.assertEqual(row["listed_address_status"], "OBSERVED_PRESENT")
            claims = __import__("json").loads(row["source_evidence"])
            self.assertEqual({claim["field"] for claim in claims}, {"listed_phone", "listed_address"})
            for claim in claims:
                self.assertEqual(claim["source_record_id"], row["source_record_id"])
                self.assertTrue(claim["url"])
                self.assertTrue(claim["content_sha256"])
                self.assertTrue(claim["observed_at"])

            for status in (
                "UNAVAILABLE_NO_PROFILE_URL",
                "UNAVAILABLE_FETCH_ERROR",
                "UNAVAILABLE_PROFILE_IDENTITY_MISMATCH",
            ):
                exhibitor_scraper._set_source_detail_state(row, status, "https://catalog.example/profile")
                self.assertEqual(row["listed_phone_status"], "UNAVAILABLE")
                self.assertEqual(row["listed_address_status"], "UNAVAILABLE")
                self.assertEqual(row["source_evidence"], "[]")

    def test_all_public_scrapers_emit_source_detail_contract(self):
        marker = "02129990000"
        participant_phone = "+90 212 555 00 00"
        expected_phone = "02125550000"
        participant_address = "İstanbul"

        def run_metalexpo(fetch_details):
            listing = '<a href="https://metal.example"><div class="katilimci-text"><span class="text">Metal Co</span></div></a>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_get", return_value=listing
            ), patch.object(exhibitor_scraper.time, "sleep"):
                return exhibitor_scraper.scrape_metalexpo(fetch_details=fetch_details, delay_sec=0)

        def run_texhibition(fetch_details):
            listing = '<div data-total="1"></div><a href="/exhibitors/tex"><div class="item"><div class="title">Tex Co</div></div></a>'
            detail = f'<main><h1>Tex Co</h1><div class="item"><div class="key">Phone</div><div class="value"><a href="tel:+902125550000">call</a></div></div><div class="item"><div class="key">Address</div><div class="value">{participant_address}</div></div></main><footer>{marker}</footer>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_get", side_effect=[listing, detail]
            ), patch.object(exhibitor_scraper.time, "sleep"):
                return exhibitor_scraper.scrape_texhibition(fetch_details=fetch_details, delay_sec=0)

        def run_zuchex(fetch_details):
            page = {"data": {"view": {"exhibitors": {
                "nodes": [{"_id": "z1", "name": "Zuchex Co", "withEvent": {"booth": "1"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "totalCount": 1,
            }}}}
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_post_graphql", return_value=page
            ), patch.object(exhibitor_scraper, "_get") as get_mock, patch.object(
                exhibitor_scraper.time, "sleep"
            ), patch.multiple(
                config, ZUCHEX_VIEW_ID="view", ZUCHEX_EVENT_ID="event",
                ZUCHEX_FILTER_ID="filter", ZUCHEX_FILTER_VALUE_ID="value",
            ):
                rows = exhibitor_scraper.scrape_zuchex(fetch_details=fetch_details, delay_sec=0)
                get_mock.assert_not_called()
                return rows

        def run_ifco(fetch_details):
            listing = '<a href="/fair/exhibitors/ifco"><img alt="IFCO Co"></a>'
            detail = f'<head><meta name="description" content="IFCO participant profile."></head><main><h1>IFCO Co</h1><dl><dt>Telefon</dt><dd>{participant_phone}</dd><dt>Adres</dt><dd>{participant_address}</dd></dl></main><footer>{marker}</footer>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_get", side_effect=[listing, detail]
            ), patch.object(exhibitor_scraper.time, "sleep"):
                return exhibitor_scraper.scrape_ifco(fetch_details=fetch_details, delay_sec=0)

        def run_idos(fetch_details):
            listing = '<article class="cv2-card"><div class="cv2-card-name">IDOS Co</div><div class="cv2-country"><span>Türkiye</span></div><a class="stretched-link" href="/portal/catalogue/75/idos">Profil</a></article>'
            detail = f'<div class="cv2-detail"><div class="cv2-detail-side"><h2>IDOS Co</h2></div><dl><dt>Telefon</dt><dd>{participant_phone}</dd><dt>Adres</dt><dd>{participant_address}</dd></dl></div><footer>{marker}</footer>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_get", side_effect=[listing, detail]
            ), patch.object(exhibitor_scraper.time, "sleep"):
                return exhibitor_scraper.scrape_idos(fetch_details=fetch_details, delay_sec=0)

        def run_beauty_eurasia(fetch_details):
            listing = ["", "<a href='/tr/company/beauty'>Beauty Co</a>", "Türkiye"]
            detail = f'<main><h1>Beauty Co</h1><b>Telefon</b><a href="tel:+902125550000">call</a><hr><b>Adres</b>{participant_address}<hr></main><footer>{marker}</footer>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_post_json", return_value={"data": [listing], "recordsTotal": 1}
            ), patch.object(exhibitor_scraper, "_get", return_value=detail), patch.object(
                exhibitor_scraper.time, "sleep"
            ):
                return exhibitor_scraper.scrape_beauty_eurasia(fetch_details=fetch_details, delay_sec=0)

        def run_maktek(fetch_details):
            listing = '<a class="brand-link" href="brand/maktek"><h2 class="brand-name">Maktek Co</h2><p class="brand-country">Türkiye</p></a>'
            detail = f'<main><h1>Maktek Co</h1><div class="widget"><h4 class="widget-title">İletişim</h4><div class="schedule-list"><ul><li><i class="fa-phone"></i>{participant_phone}</li><li><i class="fa-location"></i>{participant_address}</li></ul></div></div></main><footer>{marker}</footer>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_get", side_effect=[listing, detail]
            ), patch.object(exhibitor_scraper.time, "sleep"):
                return exhibitor_scraper.scrape_maktek(fetch_details=fetch_details, delay_sec=0)

        def run_foodist(fetch_details):
            listing = '<a class="brand-link" href="brand/foodist"><h2 class="brand-name">Foodist Co</h2><p class="brand-country">Türkiye</p></a>'
            detail = f'<main><h1>Foodist Co</h1><div class="widget"><h4 class="widget-title">İletişim</h4><div class="schedule-list"><ul><li><i class="fa-phone"></i>{participant_phone}</li><li><i class="fa-location"></i>{participant_address}</li></ul></div></div></main><footer>{marker}</footer>'
            with patch.object(exhibitor_scraper, "_session", return_value=object()), patch.object(
                exhibitor_scraper, "_get", side_effect=[listing, detail]
            ), patch.object(exhibitor_scraper.time, "sleep"):
                return exhibitor_scraper.scrape_foodist(fetch_details=fetch_details, delay_sec=0)

        cases = [
            ("scrape_metalexpo", run_metalexpo, True),
            ("scrape_texhibition", run_texhibition, False),
            ("scrape_zuchex", run_zuchex, True),
            ("scrape_ifco", run_ifco, False),
            ("scrape_idos", run_idos, False),
            ("scrape_beauty_eurasia", run_beauty_eurasia, False),
            ("scrape_maktek", run_maktek, False),
            ("scrape_foodist", run_foodist, False),
        ]
        names = {name for name, _runner, _no_profile in cases}
        self.assertEqual(
            names,
            {
                "scrape_metalexpo", "scrape_texhibition", "scrape_zuchex", "scrape_ifco",
                "scrape_idos", "scrape_beauty_eurasia", "scrape_maktek", "scrape_foodist",
            },
            msg="public scraper case coverage",
        )
        required = {
            "source_record_id", "listed_phone", "listed_address", "listed_phone_status",
            "listed_address_status", "source_detail_status", "source_evidence",
        }
        for name, runner, no_profile in cases:
            before = runner(False)
            after = runner(True)
            self.assertEqual(len(before), 1, msg=f"{name}:before row count")
            self.assertEqual(len(after), 1, msg=f"{name}:after row count")
            for phase, rows in (("before", before), ("after", after)):
                for field in required:
                    self.assertIn(field, rows[0], msg=f"{name}:{phase}:{field}")
            self.assertTrue(before[0]["source_record_id"], msg=f"{name}:before source_record_id")
            self.assertTrue(after[0]["source_record_id"], msg=f"{name}:after source_record_id")
            self.assertEqual(
                [row["source_record_id"] for row in before],
                [row["source_record_id"] for row in after],
                msg=f"{name}:source ID stability",
            )
            before_evidence = json.loads(before[0]["source_evidence"])
            after_evidence = json.loads(after[0]["source_evidence"])
            self.assertEqual(before[0]["source_detail_status"], "NOT_REQUESTED", msg=f"{name}:before detail status")
            self.assertEqual(before[0]["listed_phone_status"], "NOT_REQUESTED", msg=f"{name}:before phone status")
            self.assertEqual(before[0]["listed_address_status"], "NOT_REQUESTED", msg=f"{name}:before address status")
            self.assertEqual(before[0]["listed_phone"], "", msg=f"{name}:before phone")
            self.assertEqual(before[0]["listed_address"], "", msg=f"{name}:before address")
            self.assertEqual(before_evidence, [], msg=f"{name}:before evidence")
            self.assertNotIn(marker, str(before), msg=f"{name}:footer leak before")
            self.assertNotIn(marker, str(after), msg=f"{name}:footer leak after")
            self.assertNotIn(marker, str(after_evidence), msg=f"{name}:footer leak evidence")
            if no_profile:
                self.assertEqual(after[0]["source_detail_status"], "UNAVAILABLE_NO_PROFILE_URL", msg=f"{name}:unavailable detail status")
                self.assertEqual(after[0]["listed_phone_status"], "UNAVAILABLE", msg=f"{name}:unavailable phone status")
                self.assertEqual(after[0]["listed_address_status"], "UNAVAILABLE", msg=f"{name}:unavailable address status")
                self.assertEqual(after[0]["listed_phone"], "", msg=f"{name}:unavailable phone")
                self.assertEqual(after[0]["listed_address"], "", msg=f"{name}:unavailable address")
                self.assertEqual(after_evidence, [], msg=f"{name}:unavailable evidence")
            else:
                self.assertEqual(after[0]["source_detail_status"], "COMPLETED", msg=f"{name}:completed detail status")
                self.assertEqual(after[0]["listed_phone_status"], "OBSERVED_PRESENT", msg=f"{name}:completed phone status")
                self.assertEqual(after[0]["listed_address_status"], "OBSERVED_PRESENT", msg=f"{name}:completed address status")
                self.assertEqual(after[0]["listed_phone"], expected_phone, msg=f"{name}:completed phone")
                self.assertEqual(after[0]["listed_address"], participant_address, msg=f"{name}:completed address")
                self.assertEqual(len(after_evidence), 2, msg=f"{name}:claim count")
                self.assertEqual(
                    {claim["field"] for claim in after_evidence},
                    {"listed_phone", "listed_address"},
                    msg=f"{name}:claim fields",
                )
                for claim in after_evidence:
                    self.assertEqual(claim["source_record_id"], after[0]["source_record_id"], msg=f"{name}:claim source ID")
                    self.assertTrue(claim.get("url"), msg=f"{name}:claim URL")
                    self.assertRegex(claim.get("content_sha256", ""), r"^[0-9a-f]{64}$", msg=f"{name}:claim content hash")
                    self.assertTrue(claim.get("observed_at"), msg=f"{name}:claim observed_at")
            if name == "scrape_ifco":
                self.assertEqual(after[0]["description"], "IFCO participant profile.", msg="scrape_ifco:description")


if __name__ == "__main__":
    unittest.main()
