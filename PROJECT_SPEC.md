# B2B Contact Finder — Proje Spesifikasyonu

Bu dosyayı Claude Code'a verip "bu spesifikasyona göre projeyi inşa et" diyebilirsin.

## Amaç
Fuar katılımcı listesindeki firma isimlerinden otomatik olarak:
- Resmi web sitesi
- E-posta adresi
- Telefon numarası

bulup Excel'e yazan, fuar-bağımsız (Metalexpo, Automechanika, Hannover Messe vb. hepsinde aynı şekilde çalışan), kesintiye dayanıklı (resume edebilen) bir CLI aracı.

---

## Kısıtlar (önemli, sapma)
- **Bütçe: $0.** Ücretli arama API kullanılmayacak. Sadece DDGS (duckduckgo-search / ddgs Python paketi).
- **LinkedIn adımı YOK.** Doğrudan website araması yapılacak.
- DDGS rate-limit'e karşı **agresif olmayan** bir tempo: varsayılan **2-3 paralel worker**, istekler arası **1-3 saniye rastgele gecikme**.
- Sorgu sayısı firma başına sabit 5 değil, **kademeli/early-stop**: ilk sorgu yüksek güvenle sonuç verirse kalan sorgular atlanır.

---

## Klasör Yapısı

```
ContactFinder/
├── input/
│   └── firms.xlsx          ← Kullanıcı sadece bunu değiştirir (tek sütun: company)
├── output/
│   ├── contacts.xlsx       ← Başarılı sonuçlar
│   ├── failed.xlsx         ← Bulunamayanlar (sebep sütunuyla)
│   ├── report.txt          ← Doğruluk/başarı raporu (aşağıda format var)
│   └── logs.txt            ← Detaylı işlem logu (timestamp'li)
├── state/
│   └── progress.json       ← Resume için checkpoint dosyası
├── modules/
│   ├── search.py           ← DDGS sorgu mantığı + kademeli sorgu stratejisi
│   ├── crawler.py          ← Website indirme (requests + headers, timeout, retry)
│   ├── extractor.py        ← HTML'den email/telefon/iletişim sayfası linki çıkarma
│   ├── scorer.py           ← Domain puanlama mantığı
│   ├── excel.py            ← Excel okuma/yazma (openpyxl)
│   ├── phone.py            ← Telefon normalize etme
│   ├── checkpoint.py       ← Progress kaydetme/okuma (resume mantığı)
│   ├── report.py           ← İstatistik hesaplama ve rapor üretme
│   └── utils.py            ← Logging, rate-limit gecikme, ortak yardımcılar
├── config.py                ← Tüm ayarlanabilir parametreler (TEK yerden kontrol)
├── main.py
└── requirements.txt
```

---

## Akış (main.py mantığı)

```
1. config.py'den ayarları oku
2. state/progress.json var mı kontrol et
   → Varsa: son işlenen index'i oku, oradan devam et
   → Yoksa: input/firms.xlsx'i oku, sıfırdan başla
3. Her firma için (ThreadPoolExecutor, max_workers=config.MAX_WORKERS):
   a. search.py: kademeli sorgu ile aday domain'leri bul
   b. scorer.py: adayları puanla, en yüksek puanlıyı seç
      → Hiçbir aday skor eşiğini geçmezse → status="Website not found", score=0
   c. crawler.py: seçilen website + olası /contact, /iletisim, /kontakt sayfalarını indir
   d. extractor.py: HTML'den email ve telefon çıkar (öncelik sırasına göre)
   e. phone.py: bulunan telefonu normalize et
   f. Sonucu satır olarak biriktir
   g. HER FİRMADAN SONRA checkpoint.py ile progress.json güncelle (kesinti güvenliği)
   h. utils.py ile rastgele 1-3 sn bekle (rate-limit koruması)
4. Tüm firmalar bitince:
   a. excel.py: contacts.xlsx ve failed.xlsx yaz
   b. report.py: doğruluk raporunu hesapla, report.txt'ye yaz VE konsola yazdır
5. progress.json'ı temizle/arşivle (tamamlandı işareti)
```

**Kesinti senaryosu:** Program ctrl+C, elektrik kesintisi vb. ile kapanırsa, bir sonraki `python main.py` çalıştırmasında progress.json'daki son tamamlanan index'ten devam eder. Zaten işlenmiş firmalar tekrar sorgulanmaz.

---

## Modül Detayları

### `config.py`
Tüm "ayar" niteliğindeki değerler burada, başka hiçbir dosyada hardcoded sayı/string olmamalı:
```python
MAX_WORKERS = 3                  # paralel worker sayısı (DDGS güvenliği için düşük tutulmalı)
MIN_DELAY_SEC = 1.0
MAX_DELAY_SEC = 3.0
SEARCH_QUERY_TEMPLATES = [
    "{company} resmi sitesi",
    "{company} official website",
    "{company} contact",
    "{company} iletişim",
]
EARLY_STOP_SCORE_THRESHOLD = 80  # bu skoru geçen ilk aday bulununca diğer sorguları atla
MIN_ACCEPT_SCORE = 50            # bu skorun altındaki adaylar "bulunamadı" sayılır
EMAIL_PRIORITY_PREFIXES = ["info", "sales", "export", "marketing", "office"]
CONTACT_PAGE_PATHS = ["/contact", "/iletisim", "/kontakt", "/contact-us", "/about/contact"]
REQUEST_TIMEOUT_SEC = 10
MAX_RETRIES = 2
EXCLUDED_DOMAINS = ["linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
                    "youtube.com", "wikipedia.org", "yellowpages.com", "kompass.com",
                    "europages.com", "indeed.com", "glassdoor.com"]
```

### `search.py`
- `find_candidate_domains(company_name: str) -> list[dict]`
- Sorgu şablonlarını sırayla dener (config'deki `SEARCH_QUERY_TEMPLATES`)
- Her sorgudan dönen ilk N sonucu toplar, `EXCLUDED_DOMAINS` listesindekileri eler
- Her aday için scorer.py'yi çağırır; bir aday `EARLY_STOP_SCORE_THRESHOLD`'u geçerse kalan sorguları atlar (gereksiz istek yapmamak için — hem hız hem ban riski açısından kritik)
- DDGS exception'larını (rate-limit, timeout) yakalar, `utils.py`'deki retry/backoff mantığını kullanır

### `scorer.py`
Domain puanlama mantığı, örnek kurallar (config'e taşınabilir sabitler):
- Firma adının kelimeleri domain'de geçiyor mu (fuzzy match, örn. `difflib.SequenceMatcher` veya `rapidfuzz`) → en yüksek ağırlık
- `.com.tr`, ülke kodu uzantıları (firma Türkiye merkezliyse) → bonus puan
- `EXCLUDED_DOMAINS` listesi → otomatik 0 / elenir
- Domain çok generic / pazar yeri görünümlü ise (örn. "metalmarket", "alibaba", "europages") → düşük puan
- Sonuç: 0-100 arası skor

### `crawler.py`
- `fetch_site(url: str) -> dict` — ana sayfa + `CONTACT_PAGE_PATHS`'teki olası alt sayfaları dener
- `requests` + gerçekçi `User-Agent` header'ı, `REQUEST_TIMEOUT_SEC`, `MAX_RETRIES`
- SSL hatalarını, 404'leri, yönlendirmeleri nazikçe handle eder
- Başarısız olursa structured bir hata sebebi döner (rapor için: "timeout", "404", "connection_error" vb.)

### `extractor.py`
- `extract_emails(html: str) -> list[str]` — regex ile email çıkarır, `EMAIL_PRIORITY_PREFIXES` sırasına göre sıralar, resim/sprite içindeki sahte mailleri filtreler (örn. `.png`, `.jpg` uzantılı olanlar)
- `extract_phones(html: str) -> list[str]` — telefon pattern'leri (TR ve genel uluslararası formatlar)
- `extract_contact_page_link(html: str, base_url: str) -> str | None` — ana sayfada iletişim/contact linki varsa onu döner (crawler bir sonraki adımda onu da tarar)

### `phone.py`
- `normalize_phone(raw: str, default_country="TR") -> str`
- `phonenumbers` kütüphanesi (Google'ın libphonenumber Python portu) kullanılması önerilir — elle regex yazmaktan çok daha güvenilir
- Örnek dönüşüm: `+90 212 555 55 55` / `0090 212 5555555` / `0212 555 55 55` → hepsi `02125555555` formatına normalize edilir (E.164 formatı `+902125555555` da bir seçenek, hangisini istediğine config'den karar verebilirsin)

### `checkpoint.py`
```python
# state/progress.json formatı:
{
  "input_file_hash": "...",       # firms.xlsx değişirse eski progress'i geçersiz kıl
  "last_completed_index": 423,
  "results_so_far": [...],        # o ana kadarki tüm satırlar (output'u en sonda tek seferde yazmak için)
  "timestamp": "2026-06-24T..."
}
```
- `input_file_hash` önemli: kullanıcı firms.xlsx'i değiştirip yeniden çalıştırırsa, eski progress'in yanlışlıkla kullanılmaması için dosya hash'i kontrol edilir.

### `report.py`
Program sonunda hem `output/report.txt`'ye yazılacak hem konsola basılacak format:
```
================================
B2B Contact Finder — Sonuç Raporu
================================
Toplam firma: 500
Website bulundu: 482 (96.4%)
E-posta bulundu: 441 (88.2%)
Telefon bulundu: 459 (91.8%)
Tam iletişim bilgisi bulunan firma (website+email+phone): 428 (85.6%)
--------------------------------
Ortalama skor: 87.3
İşlem süresi: 14 dk 22 sn
================================
```
Bu fonksiyon ayrıca `output/failed.xlsx`'e yazılacak satırları da (company, status, sebep) ayrı tutar.

### `excel.py`
- `read_companies(path) -> list[str]`
- `write_contacts(path, rows)` → kolonlar: `company | website | email | phone | status | score`
- `write_failed(path, rows)` → kolonlar: `company | status | reason`
- `openpyxl` kullan (pandas da olur ama openpyxl format kontrolü için daha esnek)

### `utils.py`
- Logging setup (hem dosyaya hem konsola, timestamp'li)
- `random_delay(min_sec, max_sec)` 
- `retry_with_backoff(func, max_retries)` decorator/wrapper — DDGS rate-limit hatası alındığında exponential backoff ile bekleyip tekrar dener

---

## requirements.txt (öneri)
```
ddgs
requests
openpyxl
phonenumbers
rapidfuzz
beautifulsoup4
tqdm
```
(`tqdm` ilerleme çubuğu için — kullanıcı deneyimi açısından eklemeye değer, sen hızı görmek istiyorsun.)

---

## Test Stratejisi (Claude Code'a önerim)
1. Önce 5-10 firmalık küçük bir test Excel'i ile (gerçek Metalexpo firmalarından bir alt küme) uçtan uca dene.
2. Ctrl+C ile işlem ortasında durdurup resume'un çalıştığını doğrula.
3. Sonra elindeki ~170 Metalexpo firmasının kalanıyla gerçek koşum yap.
4. Rapor çıktısına göre `MIN_ACCEPT_SCORE` ve `EARLY_STOP_SCORE_THRESHOLD` değerlerini ayarla — bunlar config'de olduğu için kod değişmeden iyileştirme yapılabilir.

---

## Gelecek İyileştirme Notları (şimdilik kapsam dışı, sonradan eklenebilir)
- Ücretli arama API'sine (Serper.dev vb.) geçiş istenirse, `search.py` içindeki tek bir fonksiyonu değiştirmek yeterli olacak şekilde tasarlanmalı (interface sabit kalmalı).
- LinkedIn adımı ileride eklenmek istenirse, akışa search.py ile scorer.py arasına ayrı bir modül olarak eklenebilir; şu an kapsam dışı.
