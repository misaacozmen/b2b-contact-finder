# Project State - B2B Contact Finder

Bu dosya yeni Codex sohbetinde projeyi hızlı anlamak ve kaldığımız yerden devam etmek için yazıldı.

## Amaç

Fuar katılımcı sitelerinden Türkiye odaklı firma listesini çekmek, firma resmi web sitesini bulmak ve bu sitelerden e-posta/telefon çıkarmak.

Ana akış:

```text
fuar katılımcı siteleri
-> input/firms.xlsx
-> python main.py
-> output/contacts.xlsx
```

## Önemli Komutlar

Fuar listelerini çek:

```powershell
python scrape_exhibitors.py --source all --output input\firms.xlsx
```

Bright Data ile arama:

```powershell
$env:SEARCH_PROVIDER="brightdata"
$env:BRIGHTDATA_API_KEY="..."
$env:BRIGHTDATA_ZONE="serp_api1"
$env:MAX_SEARCH_QUERIES_PER_COMPANY="4"
$env:BRIGHTDATA_TIMEOUT_SEC="90"
python main.py --input input\firms.xlsx --output-dir output\brightdata_metadata_test
```

Normal DDGS ile arama:

```powershell
Remove-Item Env:\SEARCH_PROVIDER -ErrorAction SilentlyContinue
python main.py
```

## Kaynaklar ve Sayılar

Son çekilen 3 fuar kaynağı:

```text
IFCO: 99 firma
F İstanbul / IDOS: 436 firma
BeautyEurasia: 59 firma
Toplam: 594 firma
```

Sıra bazlı ayrım:

```text
1-99      IFCO
100-535   F İstanbul / IDOS
536-594   BeautyEurasia
```

Kaynak kolonu olan dosyalarda `source` kullan:

```text
ifco
idos_f_istanbul
beauty_eurasia
```

## Mevcut En İyi Teslim Dosyası

Şu an en iyi birleştirilmiş teslim dosyası:

```text
output\final_merged_contacts.xlsx
```

Özet:

```text
Toplam: 594
Website: 495
E-posta: 414
Telefon: 435
Tam bilgi: 376
```

Bu dosya eski DDGS çıktısını baz aldı ve Bright Data'dan güvenli görülen 28 satırı uyguladı.

Belirsiz kalanlar:

```text
output\final_merged_review_left.xlsx
```

## Bright Data Deneyimi

Bright Data SERP API çalışıyor ama tek başına tüm sonucu ezmek için güvenilir değil.

Gözlemler:

- Daha hızlı çalıştı.
- Daha az yüksek güven verdi.
- Bazı doğru sonuçları kaçırdı.
- Bazı kısa/generic marka adlarında yanlış site bulabildi.
- En iyi kullanım: ikinci görüş veya şüpheli kayıt retry.

Örnek yanlış riskleri:

```text
WHITE STONE -> hotel sitesi
MASTER COOK -> masterchef sitesi
ROYAL HİJYEN -> sertifika sitesi
```

## Arama ve Skorlama Kuralları

Son kullanıcı kararıyla arama sorguları daraltıldı:

```text
{company} resmi sitesi
{company} contact
{company} iletisim
{company} Turkiye official website
```

Firma adı artık tek kelimeye bölünmemeli.

Önemli domain kuralı:

```text
ADEM MAKINA -> ademm akina / ademmakina.com: iyi
ADEM MAKINA -> adem.com: orta
ADEM MAKINA -> ademterzi.com: 0
```

Yani:

- 2 kelimeli isimde iki kelime domain'de varsa yüksek puan.
- Sadece bir kelime varsa ve domain sadece o kelimeyse orta puan.
- Sadece bir kelime varsa ve domain'de alakasız ek kelime varsa 0.

Bu kural `modules/scorer.py` içinde uygulandı.

## Yeni Metadata Sistemi

Son eklenen önemli geliştirme:

`input/firms.xlsx` artık şu kolonları destekliyor:

```text
company
website
source
country
profile_url
sector
description
```

`scrape_exhibitors.py` artık `sector` ve mümkünse `description` üretir.

`main.py` artık bu metadata'yı doğrulamada kullanır:

- Site sayfasında sektör/ürün grubu bağlamı aranır.
- Arama sorgularına metadata'dan sektör ipucu eklenebilir.

Örnek:

```text
kozmetik
ambalaj
makine
giyim
tekstil
```

Hall/stand özellikle istenmedi, eklenmedi.

## Yeni Sohbette Önerilen Sıradaki Adım

1. Önce bu dosyayı oku.
2. Sonra güncel metadata'lı input üret:

```powershell
python scrape_exhibitors.py --source all --output input\firms.xlsx
```

3. Önce küçük test yap:

```powershell
python main.py --input input\firms.xlsx --output-dir output\metadata_small_test
```

Gerekirse önce Excel'den 50-100 satırlık test input'u oluştur.

4. Kalite iyiyse Bright Data ile tüm listeyi koştur:

```powershell
$env:SEARCH_PROVIDER="brightdata"
$env:MAX_SEARCH_QUERIES_PER_COMPANY="4"
$env:BRIGHTDATA_TIMEOUT_SEC="90"
python main.py --input input\firms.xlsx --output-dir output\brightdata_metadata_full
```

5. Eski final ile yeni sonucu karşılaştır:

```powershell
python compare_retry.py --old output\final_merged_contacts.xlsx --new output\brightdata_metadata_full\contacts.xlsx --output output\final_vs_metadata_full.xlsx
python filter_comparison.py --input output\final_vs_metadata_full.xlsx --output output\final_vs_metadata_decisions.xlsx
```

## Dikkat Edilecekler

- API key'i koda yazma; PowerShell env ile ver.
- `output/contacts.xlsx` eski ana çıktı, üstüne yazmamak için `--output-dir` kullan.
- `state/progress.json` kalırsa koşu eski input hash'ine göre resume etmeye çalışabilir. Büyük yeni koşu öncesi gerekirse sil:

```powershell
if (Test-Path state\progress.json) { Remove-Item state\progress.json }
```

- IFCO'da ülke filtresi yok; IFCO'dan gelen 99 firma kesin Türkiye demek değildir.
- IDOS ve BeautyEurasia Türkiye filtresiyle çekildi.
- Telefon normalize tarafında `PHONE_ALLOWED_COUNTRIES = ["TR"]` var ama yine de bazı şüpheli alan kodları audit edilmeli.

## Kullanılabilecek Yardımcı Scriptler

```text
scrape_exhibitors.py             fuar listesinden input üretir
make_review_input.py             sorunlu/suspicious kayıtlardan retry input üretir
audit_suspicious.py              yüksek skorlu ama şüpheli satırları ayıklar
compare_retry.py                 eski-yeni sonuçları yan yana koyar
filter_comparison.py             karar gerektiren farkları süzer
suggest_decisions.py             old/new/review önerisi verir
apply_suggested_decisions.py     önerilen kararları final dosyaya uygular
merge_results.py                 eski retry mantığı için güvenli merge yapar
test_brightdata.py               Bright Data tek sorgu testi yapar
```

## 2026-07-09 Metadata Input ve Kucuk Test Sonucu

Guncel metadata'li input yeniden uretildi:

```text
input\firms.xlsx
Toplam benzersiz firma: 597
IFCO: 104
F Istanbul / IDOS: 434
BeautyEurasia: 59
Website bulunan: 39
sector dolu: 597
description dolu: 59
```

Kucuk ve karma test input'u olusturuldu:

```text
input\firms_metadata_small_test.xlsx
30 firma = IFCO 10 + IDOS 10 + BeautyEurasia 10
```

DDGS ile kucuk test kosuldu:

```powershell
python main.py --input input\firms_metadata_small_test.xlsx --output-dir output\metadata_small_test
```

Rapor:

```text
Toplam firma: 30
Website bulundu: 12 (40.0%)
E-posta bulundu: 10 (33.3%)
Telefon bulundu: 10 (33.3%)
Tam bilgi: 8 (26.7%)
OK_HIGH_CONFIDENCE: 3
REVIEW_NEEDED: 9
WEBSITE_NOT_FOUND: 18
Ortalama skor: 32.4
```

Gozlem:

- Metadata context gate calisiyor.
- Bazi hazir website'ler bile `metadata_context_missing` nedeniyle review'a dustu.
- Bu iyi bir guvenlik freni ama full kosu oncesi `metadata_context_missing` cezasinin cok sert olup olmadigi kontrol edilebilir.
- `state/progress.json` test sonunda temizlendi.

## 2026-07-09 Metadata Gate Ayari

`metadata_context_missing` aramayla bulunan adaylar icin sert kalmali. Denemede fazla genis yumusatma `VOLUMEX -> volumex-energy.com` gibi sektor disi ama marka/domain/email eslesen yanlis adayi OK'a cikarabildi.

Uygulanan daha guvenli kural:

```text
Sadece input'tan hazir gelen website icin,
page_identity_strong + email_domain_match varsa
metadata_context_missing hard gate ve -20 ceza yumusatilabilir.
```

Kucuk hazir-website testi:

```text
input\firms_metadata_known_website_test.xlsx
output\metadata_known_website_soft_test

AKSAN KOZMETIK -> OK_MEDIUM_CONFIDENCE, score 85
ARET FIRCA -> OK_HIGH_CONFIDENCE, score 97
34 ETIKET -> REVIEW_NEEDED, zayif page identity ve email yok
4 MEDIA -> WEBSITE_NOT_FOUND, zayif page identity ve contact yok
4K KIMYA / olioverde -> REVIEW_NEEDED, page identity missing
```

## 2026-07-11 Metadata Context Iyilestirmesi

Bright Data 30 satirlik testi oncesi metadata kullanimi iyilestirildi:

```text
- Serbest sektor tokenlari yerine kanonik context etiketleri eklendi.
- IDOS ingilizce urun gruplari gida / ambalaj / makine etiketlerine donusuyor.
- BeautyEurasia personal care, dermocosmetic ve hygiene sektorleri kozmetik / temizlik etiketlerine donusuyor.
- Sayfa dogrulamasi Turkce ve Ingilizce alias'larla kelime siniri kullanarak yapiliyor.
- BEAUTYEURASIA.COM icindeki "beauty" gibi alt-dizi yanlis eslesmeleri engellendi.
- Softening karari reason metni yerine candidate.query == input_website ile veriliyor.
- Beauty detayindan sektor okunamazsa genel sektor uydurmak yerine bos birakiliyor.
```

Yeni input yeniden uretildi:

```text
input\firms.xlsx: 595 firma
IFCO: 104
IDOS: 432
BeautyEurasia: 59
description dolu: 59
```

Guncel kucuk test input'u yeniden yazildi:

```text
input\firms_metadata_small_test.xlsx
30 firma = kaynak basina 10
metadata query coverage: 30/30
hazir website: 6
```

Kontrol:

```text
python -m unittest discover -s tests -v
5 test OK
py_compile OK
state/progress.json yok
```

## 2026-07-11 Karisik Hatali Cikti Bright Data Testi

Bright Data retry testi icin yeni input hazirlandi:

```text
input\firms_metadata_error_mixed_test.xlsx
Toplam: 30
IFCO: 10
IDOS: 10
BeautyEurasia: 10
```

Secim tipi:

```text
14 review/failed
9 yuksek skorlu ama supheli domain sonucu
7 onceki DDGS/Bright Data celiskisi
```

Tum 30 satir yeni metadata context sorgusu aliyor. Ek olarak `laboratuvar` context'i eklendi; ARAZLAB / Laboratory Services bu sayede `laboratuvar` sorgusunu aliyor.

Calistirma icin:

```powershell
$env:SEARCH_PROVIDER="brightdata"
$env:BRIGHTDATA_API_KEY="..."
$env:BRIGHTDATA_ZONE="serp_api1"
$env:MAX_SEARCH_QUERIES_PER_COMPANY="4"
$env:BRIGHTDATA_TIMEOUT_SEC="90"
python main.py --input input\firms_metadata_error_mixed_test.xlsx --output-dir output\brightdata_metadata_error_mixed_test
```

## 2026-07-11 Discovery Pipeline Iyilestirmesi

30 satirlik karisik hata Bright Data testinde ana darboğaz website discovery oldu:

```text
13 satir No candidate passed score threshold
1 satir Bright Data non-JSON response nedeniyle SEARCH_FAILED
Website bulunan 13 satirin 9'unda tam iletisim bilgisi bulundu
```

Uygulanan iyilestirmeler:

```text
- Ana sorgu yetersizse quoted full company name ile 3 fallback sorgu.
- Hala aday yoksa kontrollu .com.tr / .com / .tr domain tahminleri.
- Bright Data non-JSON response icin decode retry.
- Website aday Excel'inde candidate query bilgisi.
- Contact, iletisim, kurumsal ve about linklerinden en fazla 6 ic sayfa tarama.
- HTTPS basarisizsa HTTP fallback.
- Opsiyonel Playwright JavaScript shell rendering fallback (ENABLE_JS_FALLBACK=1).
- dnspython ile MX email dogrulamasi; outputs email_verification alanlari.
```

Kontrol:

```text
12 unittest OK
py_compile OK
input\known_website_pipeline_test.xlsx ile e2e test: 2/2 website, email, MX ve telefon
```

## 2026-07-11 Discovery Pipeline Bright Data Sonucu

`output\brightdata_discovery_pipeline_test` ile 30 sorunlu firma testi:

```text
Website: 18/30 (60.0%)
E-posta: 13/30 (43.3%)
Telefon: 17/30 (56.7%)
Tam bilgi: 12/30 (40.0%)
OK_HIGH_CONFIDENCE: 6
OK_MEDIUM_CONFIDENCE: 1
REVIEW_NEEDED: 11
Sure: 21:13
```

Onceki metadata testine gore website 13 -> 18 ve tam bilgi 9 -> 12 artti.

Sonraki duzeltme uygulandi:

```text
- Domain guess adaylari artik DNS A/AAAA cozumlenmeden eklenmiyor.
- HTTP fetch hic basarisizsa JavaScript renderer calismiyor.
- JavaScript renderer sadece HTTP ile gelen bos SPA shell'lerinde calisiyor.
```

Bu, var olmayan guessed domainlerin `WEBSITE_FETCH_FAILED` sonucunu ve her biri icin gereksiz Playwright beklemesini engeller. Ornek olarak gannadonnalyonplus.com.tr, 2fdonukgida.com.tr, akdaglardriedfood.com.tr ve anyonggroupkozmetiksanveticltdsirketi.com.tr DNS'te cozumlenmiyor.

## 2026-07-11 Discovery ve Contact Enrichment Iyilestirmeleri

Benzer B2B finder/crawler yaklasimlari incelenerek su guvenli iyilestirmeler uygulandi:

```text
- Domain kimligi icin hukuki, sektor ve is tanimi kelimelerinden ayri marka tokenlari.
- Ayni domain birden fazla farkli sorguda gorulurse en fazla +8 sinirli consensus bonusu.
- mailto: ve tel: linklerinden URL decode ederek dogrudan iletisim cikarimi.
- Schema.org JSON-LD ve microdata telephone/faxNumber cikarimi.
- /hakkimizda ve /kurumsal kontrollu contact-page fallback yollarina eklendi.
- MX yoksa RFC 5321 implicit MX kuraliyla A/AAAA kontrolu; null MX yine gecersiz.
```

Eklenmeyenler:

```text
- Tahmini kisi e-postasi uretimi: yanlis pozitif ve teslimat riski.
- SMTP mailbox probing: yavaslik, engellenme ve hukuki/operasyonel risk.
- Genis sosyal ag taramasi: kapsam ve veri kaynagi riski.
```

Kontrol:

```text
19 unittest OK
py_compile OK
git diff --check OK
Harici 30 firma testi calistirilmadi.
```

## 2026-07-11 Enrichment Test Audit ve Risk Duzeltmesi

Ilk enrichment kosusu sayisal olarak 26 website, 21 email, 24 telefon ve 20 tam iletisim verdi. Satir bazli audit artisin tamamiyla guvenilir olmadigini gosterdi:

```text
- AQUA ANA: 92 puanli aquaana.com.tr yerine 57 puanli dreamworldaqua.com.tr secildi (yanlis).
- HILAY: 92 puanli hilay.com.tr yerine 72 puanli hilaytasarim.com secildi (yanlis secim).
- AR KAGIT: arsonkagit.com / ars10.com domain uyusmazligi ve zayif marka kaniti.
- ABRAJ GLOBAL GIDA: globalgida.net harici kaynakta dogrulandi ancak yeni token kuralinda kaybedildi.
```

Uygulanan korumalar:

```text
- Domain identity skoru 0 ise rank bonusu adayi artik listeye sokamaz.
- Consensus bonusu clean-single-token ve short-name risk tavanlarini asamaz.
- En iyi pre-crawl adayindan 18 puandan fazla gerideki aday secim icin crawl edilmez.
- kariyer.net ve isinolsun.com excluded domain listesine eklendi.
```

Kontrol:

```text
21 unittest OK
py_compile OK
git diff --check OK
Yeni harici kosu henuz yapilmadi.
```

## 2026-07-12 Kisa Marka Alt-Dizi Koruması

Quality gate test auditinde `AR KAGIT -> yasar.com.tr` yanlis pozitif bulundu. Neden, iki harfli `ar` markasinin `yasar` domaininin icinde alt dizi olarak sayilmasiydi.

```text
- 3 veya daha kisa domain tokeni artik yalniz domain core ile tam eslesirse hit sayilir.
- 2F DONUK GIDA -> 2f.com.tr gibi tam kisa-domain eslesmesi korunur.
- AR KAGIT -> yasar.com.tr ve AR KAGIT -> arsonkagit.com reddedilir.
```

Kontrol:

```text
22 unittest OK
py_compile OK
git diff --check OK
```

## 2026-07-12 Compact Brand Regression Fix

Kisa token korumasi `DR. SEYDA ATABAY -> dratabay.com` sonucunu da filtreledi. Dar istisna eklendi:

```text
- Domain, 3 karakter veya daha kisa bir unvan/token + en az 5 karakterlik uzun marka tokeninin tam birlesimi ise kabul edilir.
- Ornek: dr + atabay -> dratabay.com.
- AR -> yasar.com.tr gibi rastgele alt-dizi eslesmeleri kabul edilmez.
```

Kontrol:

```text
23 unittest OK
py_compile OK
git diff --check OK
```

## 2026-07-13 Multi-Source Enrichment ve Golden Test Mimarisi

Uygulanan yeni opsiyonel katmanlar:

```text
- Google Places API (New): normal arama guvenli aday bulamazsa website/telefon icin ikinci kaynak.
- Places adayi yine domain, page identity ve metadata kontrollerinden gecmeden kabul edilmez.
- Hunter Domain Search: yalniz crawl edilen site e-posta vermediyse ve ENABLE_HUNTER_FALLBACK=1 ise calisir.
- Hunter sonucu minimum confidence ve mevcut MX dogrulamasindan gecmelidir.
- contacts.xlsx artik website_source, email_source ve phone_source alanlarini yazar.
- data/company_aliases.json insan onayli firma/marka/domain eslesmeleri icin eklendi; otomatik doldurulmaz.
- validate_golden.py ve data/golden_contacts_template.csv ile insan-dogrulanmis regresyon testi eklendi.
```

Google Places ayari:

```powershell
$env:GOOGLE_PLACES_API_KEY="..."
$env:ENABLE_GOOGLE_PLACES="1"
```

Hunter ayari (kredi tuketir, varsayilan kapali):

```powershell
$env:HUNTER_API_KEY="..."
$env:ENABLE_HUNTER_FALLBACK="1"
$env:HUNTER_MIN_CONFIDENCE="80"
```

Kontrol:

```text
27 unittest OK
py_compile OK
git diff --check OK
```

## 2026-07-13 Google Places Telefon Enrichment

Ilk temiz Places pilotunda Places yalniz TRENÇ GIYIM icin website adayi uretti ve telefon website'den zaten bulundu. Places'in telefon faydasini olcmek icin ek korumali enrichment eklendi:

```text
- Crawl edilmis ve telefon bulunamayan website icin Google Places sorgulanir.
- Places telefonunun kabul edilmesi icin Places website domaini secilmis website ile tam normalize eslesmelidir.
- Kabul edilen telefon phone_source=google_places_domain_match ve reason=google_places_phone_domain_match ile audit edilir.
```

Kontrol:

```text
28 unittest OK
py_compile OK
git diff --check OK
```

## 2026-07-13 Yeni Golden Manual DoÄŸrulama Seti

Ã–nceki problemli 30 firma (`input/firms_metadata_error_mixed_test.xlsx`) ile hiÃ§ Ã§akÄ±ÅŸmayan yeni 30 firma seti oluÅŸturuldu:

```text
outputs/golden_manual_validation_20260713/golden_manual_validation_30.xlsx
```

Ã‡alÄ±ÅŸma kitabÄ± Ã¼Ã§ sayfa iÃ§erir:

```text
- Pipeline Input: Ã§alÄ±ÅŸtÄ±rÄ±lacak, deÄŸiÅŸtirilmemesi gereken 30 firmalÄ±k girdi.
- Manual Report: resmi website/e-posta/telefon iÃ§in insan doÄŸrulama tablosu; yes/no/not_found seÃ§imleri.
- Instructions: kabul kurallarÄ± ve manuel kontrol adÄ±mlarÄ±.
```

DaÄŸÄ±lÄ±m: 10 IFCO + 10 IDOS + 10 Beauty Eurasia. Golden kayÄ±tlarda yalnÄ±zca resmi domain ve resmi sitede gÃ¶rÃ¼nen iletiÅŸim bilgisi kabul edilir; bulunamayan alanlar boÅŸ kalÄ±r. Bu rapor, sonraki sohbetin `validate_golden.py` ile gerÃ§ek doÄŸruluk/yanlÄ±ÅŸ pozitif analizi yapmasÄ± iÃ§in temel kaynaktÄ±r.
