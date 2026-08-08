import unittest
from unittest.mock import patch

from modules.exhibitor_scraper import (
    _brand_catalog_list_rows,
    _fold,
    _maktek_list_rows,
    _maktek_profile_details,
    scrape_maktek,
)


LIST_HTML = """
<a class="brand-link" href="brand/ornek-makina">
  <div class="brand-info">
    <h2 class="brand-name">ÖRNEK MAKİNA SAN. VE TİC. A.Ş.</h2>
    <p class="brand-country">Türkiye</p>
  </div>
  <div class="brand-location-info">
    <div class="location-item"><span>Salon: 12</span></div>
    <div class="location-item"><span>Stant: 1220 A</span></div>
  </div>
</a>
<a href="?country=T%C3%9CRK%C4%B0YE&page=2">2</a>
"""


PROFILE_HTML = """
<main>
  <h1>ÖRNEK MAKİNA SAN. VE TİC. A.Ş.</h1>
  <div class="schedule-detail-info"><p class="mb-20">CNC üretim sistemleri.</p></div>
  <div class="widget">
    <h4 class="widget-title">Markalar</h4>
    <div class="schedule-list"><ul><li>Örnek CNC</li></ul></div>
  </div>
  <div class="widget">
    <h4 class="widget-title">Temsilcilikler</h4>
    <div class="schedule-info-list"><ul><li><h6>Example GmbH</h6></li></ul></div>
  </div>
  <div class="widget">
    <h4 class="widget-title">Konum Bilgisi</h4>
    <div class="schedule-list"><ul><li>Salon: 12</li><li>Stant: 1220 A</li></ul></div>
  </div>
  <div class="widget">
    <h4 class="widget-title">İletişim</h4>
    <div class="schedule-list"><ul>
      <li><i class="far fa-phone"></i> +90 212 555 12 34</li>
      <li><i class="far fa-location-dot"></i> İstanbul</li>
      <li><i class="far fa-globe"></i><a href="https://ornek.com.tr">Web</a></li>
      <li><i class="far fa-envelope"></i><a href="mailto:info@ornek.com.tr">Mail</a></li>
    </ul></div>
  </div>
</main>
"""


class MaktekScraperTests(unittest.TestCase):
    def test_fold_normalizes_turkish_dotless_i(self):
        self.assertEqual(_fold("Türkı\u0307ye"), "turkiye")

    def test_foodist_brand_catalog_rows_keep_fair_metadata_separate(self):
        rows = _brand_catalog_list_rows(
            LIST_HTML,
            "https://www.foodistexpo.com",
            source="foodist_expo_turkiye",
            sector="gıda ve içecek",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "foodist_expo_turkiye")
        self.assertEqual(rows[0]["sector"], "gıda ve içecek")
        self.assertEqual(
            rows[0]["profile_url"],
            "https://www.foodistexpo.com/brand/ornek-makina",
        )

    def test_list_parser_keeps_profile_and_location(self):
        rows = _maktek_list_rows(LIST_HTML, "https://www.maktekfuari.com")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company"], "ÖRNEK MAKİNA SAN. VE TİC. A.Ş.")
        self.assertEqual(rows[0]["profile_url"], "https://www.maktekfuari.com/brand/ornek-makina")
        self.assertEqual(rows[0]["hall"], "12")
        self.assertEqual(rows[0]["stand"], "1220 A")

    def test_profile_parser_extracts_reference_fields(self):
        details = _maktek_profile_details(PROFILE_HTML)
        self.assertEqual(details["website"], "https://ornek.com.tr")
        self.assertEqual(details["listed_phone"], "+90 212 555 12 34")
        self.assertEqual(details["listed_email"], "info@ornek.com.tr")
        self.assertEqual(details["listed_address"], "İstanbul")
        self.assertEqual(details["brands"], "Örnek CNC")
        self.assertEqual(details["representations"], "Example GmbH")

    @patch("modules.exhibitor_scraper.time.sleep")
    @patch("modules.exhibitor_scraper._get")
    def test_scraper_follows_pages_and_enriches_profiles(self, get_mock, _sleep_mock):
        page_two = LIST_HTML.replace("ornek-makina", "ikinci-makina").replace(
            "ÖRNEK MAKİNA SAN. VE TİC. A.Ş.", "İKİNCİ MAKİNA A.Ş."
        ).replace('href="?country=T%C3%9CRK%C4%B0YE&page=2">2</a>', "")

        def response(_session, url):
            if "brand/" in url:
                return PROFILE_HTML
            if "page=2" in url:
                return page_two
            return LIST_HTML

        get_mock.side_effect = response
        rows = scrape_maktek(fetch_details=True, delay_sec=0)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["website"] == "https://ornek.com.tr" for row in rows))


if __name__ == "__main__":
    unittest.main()
