# B2B Fuar İletişim Bulucu

Bir fuarın katılımcı listesinden her firmanın **resmi web sitesini, e-postasını ve telefonunu** bulan ve sonucu Excel olarak veren Windows aracı.

- Fuar sitesindeki katılımcı listesini kendisi çeker ya da hazır bir Excel listesini okur.
- Firmaların sitelerini bulur, sitenin gerçekten o firmaya ait olduğunu doğrular ve iletişim bilgilerini siteden alır.
- **Türkiye** ve **Polonya** fuarları için ayarlıdır. Polonya'da fuar kataloğundaki vergi numarası (NIP) ile siteyi kesin doğrular.
- Masaüstü paneliyle terminal kullanmadan çalışır.
- Ücretsiz çalışır. İsterseniz ücretli kaynaklarla (Bright Data, Google Places, Hunter) eksikleri tamamlar.

Ayrıntılı kullanım: [docs/KULLANIM_KILAVUZU.md](docs/KULLANIM_KILAVUZU.md)

---

## 1. Gereksinimler

- Windows 10 ya da 11 (64 bit)
- İnternet bağlantısı
- Yaklaşık 2 GB boş disk (Python ortamı, paketler ve tarayıcı)

Bilgisayarda Python kurulu olması gerekmez; kurulum, projeye özel bir Python ortamını proje klasörünün içine kurar.

## 2. Kurulum

1. Depoyu indirin:
   ```powershell
   git clone <depo adresi> b2b
   cd b2b
   ```
   Git yoksa GitHub'daki **Code → Download ZIP** ile indirip bir klasöre açın. Klasör yolunda Türkçe karakter olmaması önerilir. Depo özelse GitHub hesabınıza depo sahibi tarafından erişim verilmiş olmalıdır.
2. Klasördeki **`KURULUM.cmd`** dosyasına çift tıklayın. Ya da PowerShell'de:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File kurulum.ps1
   ```
3. Kurulum 5–15 dakika sürer:
   - Proje klasöründeki `.runtime` klasörüne conda-forge'dan Python 3.14 ve SQLite (en az 3.51.3) kurar. İndirme micromamba ile yapılır.
   - Python paketlerini (`requirements*.txt`) kurar.
   - Bazı fuar siteleri için Chromium tarayıcısını kurar (Playwright).
   - Paneli dener ve `PANEL_OK` yazar.
   - Masaüstüne **Fuar Koşu Paneli** kısayolunu koyar.

Kurulum tekrar çalıştırılabilir; kurulu olanları atlar. Bilgisayardaki başka Python kurulumlarına dokunmaz.

> **Neden özel Python?** Sistem, koşuyu kaldığı yerden devam ettirebilmek için SQLite veritabanı kullanır ve SQLite 3.51.3 ya da daha yeni bir sürüm ister. Python'un resmi Windows kurulumundaki SQLite daha eskidir. Kurulum bu yüzden conda-forge'daki Python'u kullanır.

## 3. Hızlı başlangıç

1. Masaüstündeki **Fuar Koşu Paneli** kısayolunu açın.
2. Fuar sitesindeki katılımcı listesinin adresini kutuya yapıştırıp **Listeyi çek**'e basın. Hazır bir Excel'iniz varsa **Seç…** ile seçin.
3. Firmaların ülkesini seçin: **Türkiye** ya da **Polonya**.
4. Koşu türü olarak **Ücretsiz** seçip **Koşuyu başlat**'a basın.
5. Bitince **Sonuçları aç** ile `sonuclar.xlsx` açılır. Kullanacağınız liste **İletişim** sayfasıdır.

Süre: firma başına yaklaşık 20–40 saniye; 150 firmalık bir fuar yaklaşık 1–1,5 saat sürer. Koşu sürerken pencereyi kapatmayın.

## 4. Ne kadar sonuç beklenir?

Aşağıdaki oranlar gerçek fuar listeleri üzerinde ölçüldü. Fuara ve sektöre göre değişir.

**Ücretsiz koşu, Türkiye**

| Liste | Web sitesi | E-posta | Telefon |
|---|---|---|---|
| Listede site ve telefon var (162 firma) | %86 | %76 | %97 |
| Listede yalnız firma adı var (5 fuar, 443 firma) | %70–80 bulunur; bulunanların yaklaşık %90'ı doğru | | |

**Ücretsiz koşu, Polonya**

| Ölçüm | Sonuç |
|---|---|
| Teslim dosyası (194 firma, katalogda sitelerin yaklaşık yarısı yazılı) | site %85, e-posta %79, telefon %74 |
| Site bilinmeyen firmalarda kesin doğru site (3 fuar, sistem katalogdaki siteyi görmeden aradı) | %72–80; bulunanların %94–96'sı doğru |

**Ücretli tamamlama** yalnız ücretsiz koşudan sonra eksik kalan firmalar için çalışır:

| Kaynak | Ne ekler | Ölçülen katkı | Maliyet |
|---|---|---|---|
| **Bright Data** (Google araması) | Bulunamayan web siteleri | Türkiye: ücretsizin bulamadığı firmaların %28'ine yeni site; kalibre kuralla kabul edilenlerin %96'sı doğru. Polonya: kesin doğru oranına +5 puan. | Üst sınır 1000 sorgu başına yaklaşık 3 $; başarısız sorgular ücretlendirilmez. Ölçüm: 194 firmalık fuarda yaklaşık 180 ücretli sorgu, yaklaşık 0,55 $. |
| **Google Places** | Eksik telefonlar | 162 firmalık fuarda +1 telefon (listede telefon zaten vardı) | Google Cloud fiyat listesine göre; panel çağrı üst sınırını gösterir |
| **Hunter** | Eksik e-postalar | Aynı fuarda +4 e-posta | Hunter planınızın kredileri |

Öneri: Önce ücretsiz koşun. Listede site yoksa ya da çok site eksikse Bright Data'yı, e-posta eksikleri önemliyse Places + Hunter'ı kullanın.

## 5. Ücretli API'ler

Sistem şu API'lerle uyumludur:

| Sağlayıcı | Kullanılan hizmet | Anahtar nereden alınır |
|---|---|---|
| Bright Data | SERP API (Google arama sonuçları) | brightdata.com → yeni bir **SERP API** zone oluşturun ve adını `serp_api1` koyun → Account settings → API key |
| Google Places | Places API (New), Text Search | Google Cloud Console → proje → **Places API (New)**'i etkinleştirin → Credentials → API key (faturalandırma açık olmalı) |
| Hunter | Domain Search (e-posta) | hunter.io → Dashboard → API → API key |

Bright Data zone'unuzun adı farklıysa `BRIGHTDATA_ZONE` ortam değişkenini ayarlayın:

```powershell
setx BRIGHTDATA_ZONE "zone_adiniz"
```

**Anahtarları kaydetmek** (bir kez):

```powershell
.\.runtime\python3147-sqlite3534\python.exe tools\api_anahtarlari.py
```

- Anahtar görünmeden yazılır.
- `state\api_keys.json` dosyasına, Windows'un kullanıcı hesabına bağlı şifrelemesiyle (DPAPI) kaydedilir. Başka bir kullanıcı ya da başka bir bilgisayar bu dosyayı çözemez.
- Anahtar hiçbir zaman ekrana, günlüğe ya da sonuç dosyalarına yazılmaz.
- Komutu yeniden çalıştırarak anahtarı değiştirebilirsiniz.

Panelde ücretli bir koşu türü seçildiğinde panel, başlamadan önce her sağlayıcı için en çok kaç çağrı yapılacağını gösterir ve onay ister. Sistem bu sınırları aşmaz. Places ve Hunter yalnız anahtarları kayıtlıysa çağrılır. Bright Data'yı seçmeden önce anahtarını kaydedin.

## 6. Güvenlik ve gizlilik

- **API anahtarları depoya girmez.** `state\` klasörü `.gitignore` ile dışarıda tutulur. `tests\test_mimar_20261006_repo_hygiene.py` testi her test koşusunda takip edilen dosyalarda anahtar olmadığını denetler.
- **İş verisi depoya girmez.** Girdi listeleri (`input\*.xlsx`), koşu klasörleri (`runs\`), teslim dosyaları (`teslim\`) ve doğruluk setleri (`data\truth\`) yerelde kalır.
- Sistem firmaların kendi sitelerinde yayımladığı iletişim bilgilerini alır; kişi adı ya da kişisel profil çıkarmaz.
- Şifreli formun arkasındaki listeleri sistem kendisi doldurmaz. Formu siz doldurup sayfayı kaydedersiniz; sistem kaydedilen sayfayı okur (bkz. kılavuz).

## 7. Terminal ile kullanım

Panel açılmazsa ya da toplu çalıştırma için:

```powershell
.\.runtime\python3147-sqlite3534\python.exe main.py --input input\FUAR.xlsx --no-allow-paid --finalize-without-paid --non-interactive
```

- Polonya fuarı için sona `--country PL` ekleyin.
- Fuar listesini terminalden çekmek için:
  ```powershell
  .\.runtime\python3147-sqlite3534\python.exe tools\liste_cek.py --url "https://fuar-sitesi/katilimcilar" --name "FUAR"
  ```
- Ücretli komutlar ve tüm ayrıntılar: [docs/KULLANIM_KILAVUZU.md](docs/KULLANIM_KILAVUZU.md).

## 8. Klasör yapısı

| Yol | İçerik |
|---|---|
| `main.py` | Koşuyu yürüten ana program |
| `config.py` | Ayarlar |
| `modules\` | Arama, tarama, doğrulama, ülke profilleri, çıktı |
| `tools\kosu_paneli.py` | Masaüstü paneli |
| `tools\liste_cek.py` | Fuar sitesinden katılımcı listesi çekici |
| `tools\api_anahtarlari.py` | Ücretli API anahtarlarını kaydetme |
| `tools\eski\` | Eski, tek seferlik betikler (arşiv) |
| `input\` | Girdi listeleri (git'e girmez) |
| `runs\` | Her koşunun klasörü; sonuçlar `runs\<koşu>\output\` altında (git'e girmez) |
| `tests\` | Otomatik testler |
| `docs\` | Kullanım kılavuzu ve eski proje belgeleri |

## 9. Geliştirici notları

Testler internete çıkmadan çalışır:

```powershell
$env:B2B_TEST_OFFLINE = "1"
.\.runtime\python3147-sqlite3534\python.exe -m pytest -q -p no:cacheprovider
Remove-Item Env:\B2B_TEST_OFFLINE
```

- Tam paket 10–15 dakika sürer. Üç uzun test (H01, K09, P11) toplam yaklaşık 30 dakika daha sürer; CI'da ayrı çalışır.
- Bir koşu sürerken aynı klasörde test çalıştırmayın; testler `runs\` ve `state\` klasörlerinde değişiklik görürse hata verir.
- Türkiye profilinin davranışı referans kabul edilir; ülke profilleri `modules\country_profile.py` içindedir.
