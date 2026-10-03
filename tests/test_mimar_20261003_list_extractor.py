"""Talimat 23: exhibitor list extraction from fair pages (offline, synthetic pages)."""

from __future__ import annotations

from datetime import datetime
import json

import pytest

from modules import excel, list_extractor, run_launcher


LIST_URL = "https://fuar.example/katilimcilar"

MENU = """
<nav class="main-menu"><ul>
  <li class="menu-item"><a href="/hakkinda">Fuar Hakkında</a></li>
  <li class="menu-item"><a href="/ulasim">Ulaşım</a></li>
  <li class="menu-item"><a href="/konaklama">Konaklama</a></li>
  <li class="menu-item"><a href="/bilet">Bilet</a></li>
  <li class="menu-item"><a href="/basin">Basın</a></li>
  <li class="menu-item"><a href="/iletisim">İletişim</a></li>
</ul></nav>
"""


def _card(index: int) -> str:
    return f"""
    <div class="firm-card">
      <h3 class="firm-card__name">Ornek{index} Makina San. ve Tic. Ltd. Şti.</h3>
      <span>Stand No: A-{index}</span>
      <a href="tel:+902125550{index:03d}">+90 212 555 0{index:03d}</a>
      <a href="https://www.ornek{index}.com.tr">www.ornek{index}.com.tr</a>
      <a href="https://www.facebook.com/ornek{index}">Facebook</a>
    </div>"""


def _list_page(indexes, pagination: str = "") -> str:
    cards = "".join(_card(index) for index in indexes)
    return f"<html><body>{MENU}<div class='firms'>{cards}</div>{pagination}</body></html>"


def test_cards_beat_menu_and_carry_contacts():
    records = list_extractor.parse_records(_list_page(range(1, 7)), LIST_URL)
    assert [record["company"] for record in records][:2] == [
        "Ornek1 Makina San. ve Tic. Ltd. Şti.", "Ornek2 Makina San. ve Tic. Ltd. Şti.",
    ]
    assert len(records) == 6
    first = records[0]
    assert first["website"] == "https://www.ornek1.com.tr"
    assert first["phone"]
    assert first["stand"] == "A-1"


def test_embedded_json_records_prefer_legal_name_and_website():
    cards = "".join(
        f"""<div class="card" onclick='openModal({json.dumps({"name": f"Marka{index}", "firma_adi": f"Ornek{index} Denizcilik A.Ş.", "web": f"https://ornek{index}.com", "stand": f"B-{index}"})})'>
        <img src="/logo{index}.png" alt="Marka{index}"></div>"""
        for index in range(1, 7)
    )
    records = list_extractor.parse_records(f"<html><body>{cards}</body></html>", LIST_URL)
    assert len(records) == 6
    assert records[0]["company"] == "Ornek1 Denizcilik A.Ş."
    assert records[0]["website"] == "https://ornek1.com"
    assert records[0]["stand"] == "B-1"


def test_external_website_skips_fair_site_social_and_files():
    assert list_extractor.external_website("https://www.ornek.com.tr/", LIST_URL) == "https://www.ornek.com.tr/"
    assert list_extractor.external_website("/brand/ornek", LIST_URL) == ""
    assert list_extractor.external_website("https://cdn.fuar.example/x", LIST_URL) == ""
    assert list_extractor.external_website("https://www.instagram.com/ornek", LIST_URL) == ""
    assert list_extractor.external_website("https://ornek.com.tr/katalog.pdf", LIST_URL) == ""


def test_page_urls_follow_numbered_pages_and_keep_filters():
    url = "https://fuar.example/katilimci-listesi?country=TR"
    links = "".join(
        f'<a href="https://fuar.example/katilimci-listesi?country=TR&amp;page={number}">{number}</a>'
        for number in (2, 3, 4)
    ) + '<a href="https://fuar.example/katilimci-listesi?country=DE&amp;page=9">x</a>'
    assert list_extractor.page_urls(f"<html><body>{links}</body></html>", url) == [
        "https://fuar.example/katilimci-listesi?country=TR&page=2",
        "https://fuar.example/katilimci-listesi?country=TR&page=3",
        "https://fuar.example/katilimci-listesi?country=TR&page=4",
    ]


def _profile_page(index: int) -> str:
    return f"""<html><body>
      <header><a href="https://www.sponsor-firma.com">Ana sponsor</a></header>
      <h1>Ornek{index} Ahşap A.Ş.</h1>
      <a href="https://www.ornek{index}.com">https://www.ornek{index}.com</a>
      <p>Telefon: 0232 555 1{index:03d}</p>
      <footer><p>Fuar ofisi: 0212 444 0000</p><a href="https://www.sponsor-firma.com">Sponsor</a></footer>
    </body></html>"""


def test_extract_reads_pages_and_profiles_and_drops_site_wide_links():
    profile_cards = "".join(
        f'<div class="brand-item"><a href="/brand/ornek{index}"><span data-company-name="Ornek{index} Ahşap A.Ş."></span></a></div>'
        for index in range(1, 7)
    )
    second_cards = "".join(
        f'<div class="brand-item"><a href="/brand/ornek{index}"><span data-company-name="Ornek{index} Ahşap A.Ş."></span></a></div>'
        for index in range(7, 13)
    )
    pages = {
        LIST_URL: f'<html><body>{profile_cards}<a href="{LIST_URL}?page=2">2</a></body></html>',
        f"{LIST_URL}?page=2": f"<html><body>{second_cards}</body></html>",
    }
    pages.update({f"https://fuar.example/brand/ornek{index}": _profile_page(index) for index in range(1, 13)})
    fetched = []

    def fetch(url):
        fetched.append(url)
        return pages[url]

    result = list_extractor.extract(url=LIST_URL, fetch_html=fetch)
    records = result["records"]
    assert (len(records), result["pages"], result["profiles"]) == (12, 2, 12)
    assert records[0]["website"] == "https://www.ornek1.com"
    assert records[0]["phone"] and "4440000" not in records[0]["phone"].replace(" ", "")
    assert all("sponsor-firma" not in record["website"] for record in records)


def test_saved_json_file_needs_no_network(tmp_path):
    data = [{"company_name": f"Ornek{index} Kompozit A.Ş.", "website": f"https://ornek{index}.com.tr"} for index in range(1, 7)]
    path = tmp_path / "liste.json"
    path.write_text(json.dumps({"exhibitors": data}), encoding="utf-8")

    def no_fetch(url):
        raise AssertionError("saved files must not fetch")

    result = list_extractor.extract(html_files=[path], fetch_html=no_fetch)
    assert len(result["records"]) == 6
    assert result["records"][0]["website"] == "https://ornek1.com.tr"


def test_build_input_writes_a_run_input_and_refuses_overwrite(tmp_path):
    page = tmp_path / "kayitli.html"
    page.write_text(_list_page(range(1, 7)), encoding="utf-8")
    output = tmp_path / "liste.xlsx"
    counts = list_extractor.build_input(LIST_URL, [page], "Ornek Fuarı", output)
    assert counts == {"firms": 6, "website": 6, "phone": 6, "email": 0, "pages": 1, "profiles": 0}
    records = excel.read_company_records(output)
    assert records[0]["company"] == "Ornek1 Makina San. ve Tic. Ltd. Şti."
    assert records[0]["listed_website"].startswith("https://www.ornek1.com.tr")
    assert records[0]["listed_phone"]
    assert records[0]["source"] == "Ornek Fuarı katılımcı listesi"
    info = run_launcher.inspect_input(output)
    assert info["ok"] and info["company_count"] == 6 and info["has_website"]
    with pytest.raises(FileExistsError):
        list_extractor.build_input(LIST_URL, [page], "Ornek Fuarı", output)


def test_empty_page_reports_no_firms(tmp_path):
    page = tmp_path / "bos.html"
    page.write_text(f"<html><body>{MENU}<form><input name='email'></form></body></html>", encoding="utf-8")
    with pytest.raises(ValueError):
        list_extractor.build_input(LIST_URL, [page], "Ornek Fuarı", tmp_path / "x.xlsx")


def test_list_output_path_goes_to_input_folder():
    path = run_launcher.list_output_path("Ornek Fuarı 2026", now=datetime(2026, 10, 3, 9, 5))
    assert path == run_launcher.PROJECT_ROOT / "input" / "Ornek_Fuarı_2026_20261003_0905_liste.xlsx"


def test_cards_with_only_stand_and_location_are_exhibitors_and_dialogs_are_ignored():
    cards = "".join(
        f"""<article class="card" data-company-id="{index}">
          <h3 data-card-name>Ornek{index} Kompozit San. ve Tic. A.Ş.</h3>
          <span data-card-hall><strong>Hol / Stant:</strong> <span>9 / A-{index}</span></span>
          <span data-card-location><span data-card-location-name>İSTANBUL, Türkiye</span></span>
          <button type="button">Detaylar</button>
        </article>"""
        for index in range(1, 7)
    )
    dialog = '<dialog><article class="card"><h3>Hol / Stant:</h3><span>Temsil eden firma</span></article></dialog>'
    records = list_extractor.parse_records(f"<html><body>{MENU}{cards}{dialog}</body></html>", LIST_URL)
    assert len(records) == 6
    assert records[0]["company"] == "Ornek1 Kompozit San. ve Tic. A.Ş."
    assert records[0]["stand"] == "9 / A-1"
    assert records[0]["country"] == "Türkiye"
    assert records[0]["website"] == ""


def _catalog_entry(index: int) -> dict:
    return {
        "exhibitorId": str(index),
        "companyInfo": {
            "name": f"Przyklad{index} Sp. z o.o.",
            "displayName": f"Marka{index}",
            "contactPhone": f"+48 22 555 00 0{index}",
            "contactEmail": f"biuro@przyklad{index}.pl",
            "website": f"przyklad{index}.pl",
        },
        "stand": {"hallName": "Hala B", "standNumber": f"B{index}"},
        "people": [{"full_name": f"Osoba {index}", "email": f"osoba{index}@przyklad{index}.pl"}],
        "products": [{"name": f"Produkt {index}-{number}"} for number in range(3)],
    }


def test_catalog_json_uses_company_block_and_parent_stand_not_people_or_products():
    text = json.dumps([_catalog_entry(index) for index in range(1, 7)])
    records = list_extractor.records_from_texts([text])
    assert len(records) == 6
    first = records[0]
    assert first["company"] == "Przyklad1 Sp. z o.o."
    assert first["website"] == "przyklad1.pl"
    assert first["phone"] == "+48 22 555 00 01"
    assert first["email"] == "biuro@przyklad1.pl"
    assert (first["hall"], first["stand"]) == ("Hala B", "B1")


def test_page_without_cards_reads_its_catalog_data_file_on_the_same_site():
    data = json.dumps([_catalog_entry(index) for index in range(1, 7)])
    page = (
        "<html><body><div id='app'>Katalog</div><script>window.CONFIG = "
        r'{"dataUrl":"https:\/\/fuar.example\/uploads\/katalog.json"};'
        'var other = "https://baska.example/veri.json";</script></body></html>'
    )
    pages = {LIST_URL: page, "https://fuar.example/uploads/katalog.json": data}
    fetched = []

    def fetch(url):
        fetched.append(url)
        return pages[url]

    result = list_extractor.extract(url=LIST_URL, fetch_html=fetch)
    assert len(result["records"]) == 6
    assert "https://baska.example/veri.json" not in fetched


def test_website_values_without_scheme_are_kept_and_junk_is_dropped():
    assert list_extractor._website_value("przyklad.pl") == "przyklad.pl"
    assert list_extractor._website_value("https://www.przyklad.com.pl/kontakt") == "https://www.przyklad.com.pl/kontakt"
    assert list_extractor._website_value("brak") == ""
    assert list_extractor._website_value("-") == ""


def test_fold_maps_polish_l():
    assert list_extractor.fold("SPÓŁKA Złota") == "spolka zlota"
