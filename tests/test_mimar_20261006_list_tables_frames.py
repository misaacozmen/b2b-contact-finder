"""Talimat 31: exhibitor tables, linkless cards, framed list pages and brands (offline, synthetic pages)."""

from __future__ import annotations

import json

from modules import excel, list_extractor

LIST_URL = "https://fuar.example/katilimci-listesi/"


def _table_page(count: int = 6) -> str:
    rows = "".join(
        f"<tr><td>ORNEK{index} TEKSTİL SAN. VE TİC. A.Ş.</td><td>MARKA{index}</td><td>1</td><td>B10{index}</td></tr>"
        for index in range(1, count + 1)
    )
    return (
        "<html><body><table>"
        "<tr><th>FİRMA ADI</th><th>MARKA ADI</th><th>HALL</th><th>STAND NO</th></tr>"
        "<tr><td>FİRMA</td><td>MARKA</td><td>HALL NO</td><td>STANT NO</td></tr>"
        f"{rows}</table></body></html>"
    )


def test_table_header_names_the_columns_and_repeated_headers_are_skipped():
    records = list_extractor.parse_records(_table_page(), LIST_URL)
    assert len(records) == 6
    first = records[0]
    assert first["company"] == "ORNEK1 TEKSTİL SAN. VE TİC. A.Ş."
    assert (first["brands"], first["hall"], first["stand"]) == ("MARKA1", "1", "B101")


def test_a_company_called_firma_is_a_row_not_a_header():
    rows = "".join(f"<tr><td>FIRMA HANDLOWA ORNEK{index}</td><td>A{index}</td></tr>" for index in range(1, 7))
    page = f"<html><body><table><tr><th>Firma</th><th>Stand</th></tr>{rows}</table></body></html>"
    records = list_extractor.parse_records(page, LIST_URL)
    assert [record["company"] for record in records][:2] == ["FIRMA HANDLOWA ORNEK1", "FIRMA HANDLOWA ORNEK2"]


def _box(index: int, hall: str = "8") -> str:
    return f"""
    <div class="box"><article class="media"><div class="media-content"><div class="content">
      <p class="is-size-4 has-text-weight-bold">ORNEK{index} GIDA SAN. VE TİC. LTD. ŞTİ.</p>
      <section class="accordions"><article class="accordion">
        <div class="accordion-header"><span>Ürün grubu / Products group</span></div>
        <div class="accordion-body"><div class="accordion-content">-</div></div>
      </article></section>
      <p class="mt-2"><span class="tag">Hall : {hall}</span><span class="tag">Stand No: 8{index:02d}</span>
      <span class="tag is-danger">TÜRKİYE</span></p>
    </div></div></article></div>"""


def test_card_without_heading_or_link_takes_its_first_text_as_the_name():
    page = "<html><body>" + "".join(_box(index) for index in range(1, 7)) + "</body></html>"
    records = list_extractor.parse_records(page, LIST_URL)
    assert len(records) == 6
    assert records[0]["company"] == "ORNEK1 GIDA SAN. VE TİC. LTD. ŞTİ."
    assert (records[0]["hall"], records[0]["stand"]) == ("8", "801")


def test_framed_list_page_is_read_through_its_only_content_frame():
    inner = "https://katilimci.fuar.example/"
    outer = (
        "<html><body>"
        '<iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X"></iframe>'
        f'<iframe src="{inner}"></iframe></body></html>'
    )
    pages = {
        LIST_URL: outer,
        inner: "<html><body>" + "".join(_box(index) for index in range(1, 7))
        + f'<a href="{inner}?page=2">2</a></body></html>',
        f"{inner}?page=2": "<html><body>" + "".join(_box(index) for index in range(7, 13)) + "</body></html>",
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return pages[url]

    result = list_extractor.extract(url=LIST_URL, fetch_html=fetch)
    assert len(result["records"]) == 12 and result["pages"] == 2
    assert not any("googletagmanager" in url for url in fetched)


def test_frame_inside_a_frame_is_not_followed():
    pages = {
        LIST_URL: '<html><body><iframe src="https://a.fuar.example/"></iframe></body></html>',
        "https://a.fuar.example/": '<html><body><iframe src="https://b.fuar.example/"></iframe></body></html>',
    }
    result = list_extractor.extract(url=LIST_URL, fetch_html=lambda url: pages[url])
    assert result["records"] == []


def test_brands_reach_the_run_input(tmp_path):
    page = tmp_path / "tablo.html"
    page.write_text(_table_page(), encoding="utf-8")
    output = tmp_path / "liste.xlsx"
    list_extractor.build_input(LIST_URL, [page], "Ornek Fuarı", output)
    records = excel.read_company_records(output)
    assert records[0]["brands"] == "MARKA1"
    data = [{"firma_adi": f"Ornek{index} Ltd. Şti.", "marka": f"Marka{index}", "web": f"ornek{index}.com.tr"} for index in range(1, 7)]
    assert list_extractor.records_from_texts([json.dumps(data)])[0]["brands"] == "Marka1"
