# B2B Contact Finder

Mevcut ürün kapsamı Türkiye içindeki firmalardır (`TARGET_COUNTRY=TR`); sorgu, telefon ve domain kuralları bu kapsam için optimize edilir.

Firmalardan resmi web sitesi, e-posta ve telefon bulup Excel çıktısı üreten CLI aracı.

## Kurulum

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Brandfetch ve Hunter resolver kurulumu

Firma adindan domain kesfi icin Brandfetch Brand Search ve Hunter Domain Finder
istege bagli olarak kullanilabilir. Bunlar yalnizca aday kesfi yapar; resmi site
kimlik dogrulamasi ve yayin esikleri degismez.

Anahtarlari komut satirina veya `.env` dosyasina yazmak yerine tek seferlik
guvenli kurulum aracini calistirin:

```powershell
python setup_company_resolvers.py
```

Brandfetch icin Developer Dashboard'daki `Client ID`, Hunter icin Dashboard >
API sayfasindaki gizli API key istenir. Giris gorunmez yapilir ve Windows'ta
kullanici hesabina bagli DPAPI ile `state/api_keys.json` icinde sifrelenir.
Etkin/pasif secimleri gizli bilgi icermeyen `state/company_resolvers.json`
dosyasinda tutulur. Kurulumu yeniden calistirarak anahtari degistirebilir veya
bir resolver'i kapatabilirsiniz.

## Kullanım

`input/firms.xlsx` dosyasına tek sütun halinde firma adlarını koyun. İlk satır `company` olabilir.

```powershell
python main.py
```

Golden 30 firma testini başlatmak için her zaman şu komutu kullanın:

```powershell
python run_golden.py
```

Normal golden koşusu arama ve site sayfalarını `state/search_cache` ile
`state/crawl_cache` altında saklar. Aynı kayıtları hiçbir arama veya crawl
isteği yapmadan yeniden puanlamak için:

```powershell
python run_golden.py --rerank-cache
```

Yalnızca belirli firmaları çalıştırmak için:

```powershell
python run_golden.py --companies "AYSAN,KULA,MATRIX"
```

API ve site kayıtlarını bilinçli olarak yenilemek gerektiğinde:

```powershell
python run_golden.py --search-cache refresh --crawl-cache refresh
```

Her başlangıçta Google Places ve Bright Data için `y/n` soruları sorulur.
Kayıtlı API anahtarını kullanmak için `y`, yeni anahtarla değiştirmek için
`n` yazın. Yeni anahtar gizli olarak alınır ve sonraki koşulara kaydedilir.

Farklı bir dosya ile çalıştırmak için:

```powershell
python main.py --input C:\path\to\firms.xlsx
```

## Fuar Katılımcı Sitelerinden Firma Çekme

Desteklenen kaynaklardan Türkiye katılımcılarını çekip `input/firms.xlsx` üretmek için:

```powershell
python scrape_exhibitors.py --source all
```

Tek kaynak çekmek için:

```powershell
python scrape_exhibitors.py --source ifco
python scrape_exhibitors.py --source idos
python scrape_exhibitors.py --source beauty
```

Oluşan Excel kolonları:

- `company`
- `website`
- `source`
- `country`
- `profile_url`
- `sector`
- `description`

`website` doluysa `python main.py` ikinci aşamada bu siteyi doğrudan kullanır; web sitesi araması yapmadan mail ve telefon çıkarmayı dener.

## Bright Data ile Sorunlu Kayıtları Tekrar Deneme

Önce mevcut sonuçlardan tekrar denenecek listeyi üretin:

```powershell
python make_review_input.py
```

Sonra Bright Data SERP API ile sadece bu listeyi ayrı çıktı klasörüne çalıştırın:

```powershell
$env:SEARCH_PROVIDER="brightdata"
$env:BRIGHTDATA_API_KEY="BRIGHT_DATA_API_KEYINIZ"
$env:MAX_SEARCH_QUERIES_PER_COMPANY="8"
python main.py --input output\review_retry_input.xlsx --output-dir output\brightdata_review
```

Bright Data zone adınız farklıysa:

```powershell
$env:BRIGHTDATA_ZONE="serp_api1"
```

Deneme bitince normal ücretsiz DDGS moduna dönmek için:

```powershell
Remove-Item Env:\SEARCH_PROVIDER
Remove-Item Env:\BRIGHTDATA_API_KEY
```

## Çıktılar

- `output/contacts.xlsx`: doğrulanmış ve manuel kontrol gerektiren tüm bulunan sonuçlar
- `output/verified_contacts.xlsx`: yalnızca otomatik kullanıma uygun `OK_HIGH_CONFIDENCE` / `OK_MEDIUM_CONFIDENCE` sonuçları
- `output/review_queue.xlsx`: manuel kontrol gerektiren, belirsiz veya bulunamayan sonuçlar
- `output/failed.xlsx`: bulunamayan ya da eksik kalan kayıtlar
- `output/website_candidates.xlsx`: her firma için ilk 3 website adayı, skor ve seçim gerekçesi
- `output/report.txt`: özet rapor
- `output/logs.txt`: işlem logları
- `output/evidence.jsonl`: sorgu, aday, taranan sayfa ve alan bazlı kaynak kanıtları
- `output/entity_relationships.jsonl`: otomatik güven listesine alınmayan şirket–marka–domain gözlemleri
- `output/telemetry.json`: API, HTTP ve cache kullanım sayaçları
- `state/progress.sqlite3`: firma başına atomik, kesinti sonrası devam checkpoint'i
- `state/progress.json`: aktif SQLite koşusunu gösteren küçük işaret dosyası

Program tamamlanınca checkpoint dosyaları temizlenir. İşlem yarıda kalırsa sonraki aynı koşu input hash'i ve koşu imzasıyla kaldığı yerden devam eder.

API anahtarları Windows DPAPI ile mevcut kullanıcı hesabına bağlı biçimde şifrelenir. Varsayılan koşu bütçeleri Bright Data için 500, Google Places için 100 istektir. Ana komutta değiştirilebilir:

```powershell
python main.py --brightdata-budget 300 --google-places-budget 50
```

## GitHub Paylaşım Notları

Gerçek firma listeleri ve üretilen çıktılar repoya eklenmez. Kendi listenizi `input/firms.xlsx` olarak koyup programı çalıştırın.

Repoya dahil edilmeyen klasörler/dosyalar:

- `.venv/`
- `input/*.xlsx`
- `output/`
- `state/`
- `artifact_work/`

## Discovery and Verification

Website discovery first uses the normal search queries. If no candidate reaches the
acceptance threshold, it runs a small fallback set with the quoted full company name
and then tries conservative `.com.tr`, `.com`, and `.tr` domain candidates. These
candidates still go through the existing page identity, sector context, and contact
checks before they can be accepted.

Publication uses an auditable support/conflict/neutral identity model. Search
snippets, a mailbox on the candidate site, and a phone on that site are discovery
or contact evidence; they do not independently prove ownership. Automatic
publication requires at least two independent identity roots (for example,
intrinsic domain identity plus first-party company identity) and no unresolved
owner, country, transport, or business-context conflict. Explicit first-party
brand/legal-owner statements can resolve an otherwise contradictory structured
organization name.

Candidate crawling is staged. Up to `MAX_CANDIDATE_EVALUATIONS` candidates receive
a light homepage/corporate identity crawl, while only the best
`MAX_FULL_CANDIDATE_EVALUATIONS` candidates receive the full contact, sitemap and
document crawl. Fair-profile hosts are preflighted once and a repeated-5xx circuit
breaker prevents the same failing catalogue host from delaying every company.
Relevant controls are `MAX_IDENTITY_PAGES`, `MAX_FULL_CANDIDATE_EVALUATIONS`, and
`SOURCE_PROFILE_MAX_SERVER_ERRORS`.

`report.txt` and `telemetry.json` separate candidate discovery, identity crawl,
full contact crawl, source availability, abstention, and HTTP/API cost. Golden XLSX
validation can also report stage metrics:

```powershell
python validate_golden_xlsx.py --expected EXPECTED.xlsx --actual contacts.xlsx --candidates website_candidates.xlsx
```

The contacts output also includes `email_verification` and
`email_verification_reason`. MX records are checked by default; when no MX is
published, the SMTP-standard A/AAAA implicit-MX fallback is checked. Temporary
DNS failures are retained as `unverified`.

## Optional Google Places and Hunter Enrichment

Google Places is used only when normal web search cannot produce a safe website
candidate. Its returned website still passes the domain, page-identity, and
sector checks. Places phone numbers are evidence only and are never published.

```powershell
$env:GOOGLE_PLACES_API_KEY="GOOGLE_MAPS_API_KEYINIZ"
# Optional: set 0 to disable Places even when an API key is available.
$env:ENABLE_GOOGLE_PLACES="1"
```

Hunter remains optional discovery evidence, but Hunter e-mails are never
published. Contact output is restricted to pages and documents on independently
validated official company domains.

```powershell
$env:HUNTER_API_KEY="HUNTER_API_KEYINIZ"
$env:ENABLE_HUNTER_FALLBACK="1"
$env:HUNTER_MIN_CONFIDENCE="80"
```

`contacts.xlsx` includes field-level source URLs, contact roles and alternatives.

## Human-Verified Aliases and Golden Tests

Basit doğrulanmış eşleşmeler `data/company_aliases.json` içinde tutulabilir. Bir tüzel kişilik, birden fazla marka ve resmî domain ilişkisi için `data/entity_registry.json` kullanılır. Yalnızca `confidence: verified` kayıtları güvenilir aday olur; otomatik gözlemler kendilerini bu dosyaya eklemez.

```json
{
  "LEGAL COMPANY NAME": {
    "aliases": ["Public Brand"],
    "website": "https://www.example.com"
  }
}
```

An alias website is still crawled and validated; it is not blindly exported.

Golden XLSX doğrulaması `present`, `absent` ve `unknown` durumlarını destekler. `unknown` tamamlanmış manuel inceleme sayılır fakat precision/recall hesabına girmez. Dev/Validation/Blind ayrımı ve firma çakışması kontrolü:

```powershell
python validate_benchmark_suite.py
```

Golden 3, 15 yeni firma iceren holdout setidir. Manuel dosya tamamen
doldurulmadan kosu API kullanmadan durur. Dogrulama tamamlandiktan sonra:

```powershell
python run_golden_3.py --brightdata-budget 200 --google-places-budget 25
```

Golden 4, daha önce kullanılmamış WIN EURASIA 2026 fuarından seçilmiş 15
Türkiye katılımcısını içeren kör settir. Mevcut `input/firms.xlsx` ve Golden
1–3 firmalarıyla çakışmaz. Körlük protokolü:

1. Önce `outputs/golden_4_20260715/golden_4_manual_validation_15.xlsx`
   dosyasını pipeline/cache sonuçlarını açmadan bağımsız doldurun.
2. Manuel doğrulama bitmeden kodu Golden 4 firmalarına göre değiştirmeyin.
3. Ardından ücretli koşuyu başlatın; eksik doğrulama varsa script API çağrısından
   önce durur:

```powershell
python run_golden_4.py --brightdata-budget 200 --google-places-budget 25
```

Golden 4 sonuçları görüldükten sonra firma-özel alias/domain düzeltmesi
eklenmemelidir. Genel bir iyileştirme gerekiyorsa Golden 4 skoru değiştirilmeden
sonraki geliştirme setinde ele alınmalıdır.

Mevcut Golden 1 yalnız geliştirme verisidir, Golden 2 validation setidir,
Golden 3 holdout ve Golden 4 blind settir.

For regression measurement, copy `data/golden_contacts_template.csv`, fill in
human-verified expected fields, then compare a run:

```powershell
python validate_golden.py --expected data\golden_contacts.csv --actual output\contacts.xlsx
```

For JavaScript-only websites, optional rendering can be enabled after installing
Playwright and its Chromium runtime:

```powershell
pip install playwright
playwright install chromium
$env:ENABLE_JS_FALLBACK="1"
```

Rendering is used only when a page appears to be an empty JavaScript application
shell or a normal HTTP fetch fails.
