import unittest
from unittest.mock import patch

import config
from modules import exhibitor_scraper


class ExhibitorIdentityAndPaginationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
