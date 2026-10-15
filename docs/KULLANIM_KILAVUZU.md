# Kullanım Kılavuzu

Bu kılavuz, bir fuarın katılımcı listesinden firma iletişim bilgilerini çıkarmayı adım adım anlatır. Kurulum için proje kökündeki [README.md](../README.md) dosyasına bakın.

Aşağıdaki komutlar proje klasöründe çalıştırılır. Örneklerdeki `python` şu dosyayı kastetmektedir:

```powershell
.\.runtime\python3147-sqlite3534\python.exe
```

---

## 1. Girdi listesi

Her firma bir satırdır; ilk satır başlıktır. Dosya `.xlsx` olmalıdır.

| Sütun başlığı | Zorunlu | İçerik |
|---|---|---|
| `company` | **Evet** | Firma adı |
| `website` | Hayır | Firmanın bilinen web sitesi |
| `listed_website` | Hayır | Fuar listesinde yazan web sitesi |
| `listed_phone` | Hayır | Fuar listesinde yazan telefon |
| `listed_email` | Hayır | Fuar listesinde yazan e-posta |
| `country` | Hayır | Ülke |
| `tax_id` | Hayır | Vergi numarası (Polonya'da NIP) |
| `brands` | Hayır | Firmanın markaları |
| `hall`, `stand` | Hayır | Salon ve stand |

- Başlıklar küçük harfle ve yukarıdaki gibi yazılır.
- Fuar listesinde web sitesi ve telefon varsa mutlaka koyun. Sistem önce kendisi arar, sonra bu bilgilerle karşılaştırır ve boşlukları doldurur; bu sütunlar dolu olduğunda site bulma oranı %82–96 olur.
- Liste çekici (bölüm 2) bu dosyayı kendisi hazırlar.

## 2. Listeyi fuar sitesinden çekmek

### 2.1 Panelden

1. Masaüstündeki **Fuar Koşu Paneli** kısayolunu açın.
2. Katılımcı listesinin adresini kutuya yapıştırın, **Listeyi çek**'e basın ve fuar adını yazın.
3. Liste `input\<Fuar adı>_<tarih>_liste.xlsx` olarak yazılır ve kendiliğinden seçilir.

Sayfalara bölünmüş listelerde ve firma bilgilerinin ayrı firma sayfalarında durduğu sitelerde birkaç dakika sürebilir.

Liste çekici firma kartlarını ve tablo biçimindeki listeleri okur. Liste sayfası firmaları bir çerçeve (iframe) içinde gösteriyorsa çerçeveyi kendisi açar. Katalogda marka bilgisi varsa `brands` sütununa yazar.

### 2.2 Terminalden

```powershell
python tools\liste_cek.py --url "https://fuar-sitesi/katilimcilar" --name "FUAR"
```

Çıktının son satırı `LISTE_OK <dosya> firma=… web=… telefon=… eposta=…` biçimindedir.

### 2.3 Liste çekilemezse

- **Liste bir formun arkasındaysa** ya da sayfa açıldıktan sonra yükleniyorsa:
  1. Sayfayı tarayıcıda açın; gerekiyorsa formu kendiniz doldurun.
  2. Firmalar görününce **Ctrl+S** ile "Web sayfası, tamamı" olarak kaydedin.
  3. Panelde adres kutusunda sayfanın adresi dururken **Kayıtlı sayfa…** ile kaydettiğiniz dosyayı seçin.
- Yine çekilemezse listeyi kendiniz Excel'e alın ve bölüm 1'deki başlıklarla kaydedin.

Çekilen listede yabancı katılımcılar da olur. Yalnız bir ülkenin firmalarını istiyorsanız listeyi koşudan önce `country` sütununa göre süzün.

## 3. Koşu

### 3.1 Panelden (önerilen)

1. Listeyi çekin ya da hazır Excel'i **Seç…** ile seçin. Dosya `input` klasöründe olmak zorunda değildir. Panel firma sayısını ve hangi sütunların dolu olduğunu gösterir; sorun varsa kırmızı uyarı çıkar.
2. **Firmaların ülkesini** seçin: **Türkiye**, **Polonya** ya da **Birleşik Krallık**. Seçim aramaların dilini, telefon biçimini ve karar kurallarını ayarlar.
3. **Koşu türünü** seçin:
   - **Ücretsiz (önerilen)**
   - **Ücretli: Places + Hunter**: eksik telefon ve e-posta için
   - **Ücretli: Bright Data**: sitesi bilinmeyen listeler için
4. Ücretli türde panel her sağlayıcı için çağrı üst sınırını gösterir ve onay ister.
5. **Koşuyu başlat**'a basın. Aşama, biten firma sayısı ve tahmini kalan süre görünür.
6. Bitince sonuç özeti çıkar. **Sonuçları aç**, **Klasörü aç** ve **Arşive kopyala** düğmeleri açılır.

Panel aynı anda ikinci bir koşu başlatmaz. Koşu sürerken bilgisayar uykuya geçmez; pencereyi kapatırsanız koşu durur.

### 3.2 Terminalden

Ücretsiz:

```powershell
python main.py --input input\FUAR.xlsx --no-allow-paid --finalize-without-paid --non-interactive
```

Polonya fuarında sona `--country PL`, İngiltere fuarında `--country GB` ekleyin.

Ücretli, Places + Hunter (bloğun tamamını bir kerede yapıştırın):

```powershell
try {
    $env:ENABLE_LLM_ARBITER = "0"
    $env:ENABLE_LINKEDIN_COMPANY_LOOKUP = "0"
    python main.py --input input\FUAR.xlsx --allow-paid --non-interactive --brightdata-budget 0 --brandfetch-budget 0 --linkedin-company-budget 0 --llm-budget 0
}
finally {
    Remove-Item Env:\ENABLE_LLM_ARBITER, Env:\ENABLE_LINKEDIN_COMPANY_LOOKUP -ErrorAction SilentlyContinue
}
```

Ücretli, Bright Data (`600` yerine firma sayısının 4 katını yazın):

```powershell
try {
    $env:SEARCH_PROVIDER = "brightdata"
    $env:ENABLE_LLM_ARBITER = "0"
    $env:ENABLE_LINKEDIN_COMPANY_LOOKUP = "0"
    python main.py --input input\FUAR.xlsx --allow-paid --non-interactive --brightdata-budget 600 --brandfetch-budget 0 --linkedin-company-budget 0 --llm-budget 0
}
finally {
    Remove-Item Env:\SEARCH_PROVIDER, Env:\ENABLE_LLM_ARBITER, Env:\ENABLE_LINKEDIN_COMPANY_LOOKUP -ErrorAction SilentlyContinue
}
```

- Bütçe firma sayısı × 4 olduğunda sistem firma başına en çok 3 sorgu yapar; kalan pay başarısız sorguların yeniden denenmesine ayrılır.
- Ücretli kaynaklar yalnız ücretsiz koşudan sonra eksik kalan firmalar için çağrılır.

### 3.3 Süre ve kurallar

- Firma başına yaklaşık 20–40 saniye; 150 firma yaklaşık 1–1,5 saat.
- **Aynı anda tek koşu.** İki koşu aynı arama motorlarına gider ve ikisi de yavaşlar; fuarları sırayla çalıştırın.
- **Yarıda kalan koşu:** aynı komutu aynı girdiyle yeniden çalıştırın; sistem kaldığı yerden devam eder.

## 4. Ülke profilleri

### Türkiye

Referans profildir. Aramalar Türkçe yapılır, telefonlar Türkiye biçiminde okunur ve `.com.tr` gibi uzantılar öne çıkar.

### Polonya

- Liste çekici, Varşova (Ptak Expo) kataloglarındaki vergi numarasını (NIP) `tax_id` sütununa kendisi yazar.
- Sistem NIP'i aratır. Firmanın sitesinde aynı NIP'i görürse siteyi kesin kabul eder; başka bir firmanın NIP'ini gösteren siteyi reddeder. Böylece firma adından tahmin edilemeyen marka siteleri de bulunur. Rehber ve firma profili siteleri sayılmaz.
- NIP ana sayfada görünmüyorsa sitenin yasal ve iletişim sayfalarına da bakılır (`regulamin`, `polityka prywatności`, `rodo`, `kontakt` …).
- NIP aramasında yalnız rehber siteleri çıkarsa (bizraport, targeo …), o sayfaların bağlantı verdiği firma siteleri de NIP ile kontrol edilir.
- Çok sayıda numara listeleyen sayfalar ve telefon numarası sorgu siteleri NIP kanıtı sayılmaz.
- NIP ile doğrulanan sitede e-posta ya da telefon bulunamazsa web sitesi yine teslim edilir (orta güven).
- Ücretli Bright Data koşusunda NIP önce Google'da aranır.
- NIP'i olmayan yabancı katılımcılarda bu kanıt yoktur; onların siteleri daha az bulunur.

### Birleşik Krallık

- Aramalar İngilizce yapılır. Telefonlar İngiltere biçiminde okunur (+44, 020 …, 0113 …); `.co.uk` uzantıları öne çıkar.
- İngiltere'nin rehber, şirket sicili kopyası, yorum, iş ilanı ve pazar yeri siteleri ile fuar merkezleri firma sitesi sayılmaz (yell, endole, companycheck, trustpilot, thenec, excel.london …).
- İngiliz kataloglarında vergi numarası olmadığı için NIP benzeri bir kanıt kullanılmaz.
- İngiltere hukuku şirketlere (Ltd, PLC, LLP) izinsiz iş e-postası gönderilmesine izin verir; şahıs firmalarına ve ortaklıklara izin vermez. Bu yüzden teslim dosyasında yalnız şirketler kalır (5. bölüm). Müşteri dosyasında adı şirket türü göstermeyen firmalar ayrıca kontrol edilir.
- Ölçüm henüz yapılmadı; ilk İngiltere koşusunun sonuçları kontrol edilmeden müşteriye gönderilmemelidir.

## 5. Sonuçlar

Her koşunun dosyaları `runs\<koşu kimliği>\output\` klasöründedir. Panelde **Klasörü aç** bu klasörü açar. Terminalden en son koşunun klasörünü açmak için:

```powershell
explorer "$((Get-ChildItem runs -Directory | Sort-Object CreationTime -Descending | Select-Object -First 1).FullName)\output"
```

Önemli olan iki dosya var.

### `sonuclar.xlsx`

| Sayfa | İçerik |
|---|---|
| **İletişim** | Firma, web sitesi, e-posta, telefon; fuar listesinde varsa fuar adı, salon ve stant. Bütün firmalar girdi sırasıyla. Kullanacağınız liste budur. |
| Özet | Kaç firmada web sitesi, e-posta ve telefon bulunduğu ve oranlar. |
| Detaylar | Aynı firmalar: yayına hazır mı, eksik alanlar, aday web sitesi, her bilginin kaynağı ve güveni, fuar listesindeki bilgiler. |

- **Sarı hücre** güveni düşük bilgidir; kullanmadan önce kontrol edin.
- E-posta firmanın kendi sitesinde yazıyorsa, alan adı siteden farklı olsa da alınır (ör. site `firma.com.tr`, e-posta `info@firma.com`).
- Sitede yalnız gmail, hotmail gibi bir adres varsa o yazılır ve sarı işaretlenir. KEP adresleri yazılmaz.
- Yalnız firmanın genel e-posta adresi yazılır (info@, biuro@, satis@ gibi). Kişi adı taşıyan adresler (ad@, ad.soyad@, a.soyad@) ve kişisel veri adresleri (rodo@, kvkk@, privacy@ gibi) yazılmaz; sitede firmanın genel adresi varsa onun yerine o yazılır.
- Polonya ve Birleşik Krallık profillerinde teslim dosyasına yalnız şirketler alınır. Adında şirket türü (sp. z o.o., S.A., sp.j., GmbH, Ltd, Limited, PLC, LLP vb.) olmayan firmalar şahıs firması sayılır; adi ortaklıklar (s.c., "& Co" ile biten adlar) da yazılmaz. Kaç firmanın çıkarıldığı Özet sayfasının açıklamasında görünür. Türkiye profilinde bu kural kapalıdır.
- **"Aday web sitesi (kontrol edin)"** sütunu (Detaylar): sistem siteyi doğrulayamadığında aramada bulduğu en iyi adayı buraya yazar. Adaylar doğrulanmamıştır; yaklaşık yarısı doğrudur. Kullanmadan önce açıp kontrol edin.

### Ret listesi

Listelerde yer almak istemeyen firmalar için panelde **Ret listesi** düğmesine basın. `state\ret_listesi.txt` dosyası Not Defteri'nde açılır; dosya yoksa açıklamasıyla oluşturulur. Her satıra bir tane yazıp kaydedin:

- alan adı: `firma.com.tr`
- e-posta: `biuro@firma.pl`
- bütün bir e-posta alanı: `@firma.pl`
- firma adı: fuar listesindeki gibi, tam haliyle

Bundan sonra üretilen her `sonuclar.xlsx` dosyasında bu firmalar yer almaz; Özet sayfası kaç firmanın çıkarıldığını gösterir. Daha önce üretilmiş dosyalar değişmez.

### `rapor.md`

Koşunun özeti: kaç firmada web sitesi, e-posta ve telefon bulunduğu, bilgilerin hangi kaynaktan geldiği, ücretsiz arama motorlarının durumu ve ücretli çağrı sayıları.

Klasördeki diğer dosyalar teknik kayıtlardır.

### Müşteri dosyası

Müşteriye `sonuclar.xlsx` gönderilmez; içinde teknik sayfalar ve kaynak bilgileri vardır. Müşteri için ayrı bir dosya üretin:

```powershell
python tools\musteri_dosyasi.py --run-dir runs\<koşu kimliği> --out teslim\musteri\FUAR.xlsx
```

- Dosyada tek sayfa firma listesi (Firma, Fuar, Salon, Stant, Web sitesi, E-posta, Telefon) ve kısa bir bilgi sayfası olur. Yanına aynı adla bir `.csv` kopyası yazılır.
- Yalnız güvenilir bilgiler yazılır; sarı (düşük güvenli) bilgiler boş kalır.
- Polonya ve Birleşik Krallık koşularında yalnız şirketler kalır. Öteki koşularda adında kişi adı ve soyadı görünen firmalar (şahıs firmaları) ve adi ortaklıklar çıkarılır. Ret listesindeki firmalar hiç alınmaz.
- Birleşik Krallık koşusunda adı şirket türü göstermeyen firma (ör. yalnız marka adı) iki durumda kalır: firmanın kendi sitesinde yazan şirket numarası Companies House'ta faal bir şirkete aitse ve o şirketin adı firmanın adıyla ya da alan adıyla örtüşüyorsa ya da numaranın yazdığı sayfada geçiyorsa ("Marka, X Ltd'nin markasıdır" gibi); ya da Companies House'ta firmanın adını birebir taşıyan faal bir şirket varsa. Companies House anahtarı kayıtlı değilse (6. bölüm), sitenin aynı sayfasında şirket numarası ve firmanın adını taşıyan "… Ltd" ya da "… Limited" yazısı aranır.
- Bu kontrol için firmaların sitelerine yeniden bağlanılır; 150 firma birkaç dakika sürer. Sonuçlar `state\company_checks.json` dosyasında 30 gün saklanır, örnek ve tam liste aynı sonucu kullanır. Araç çıktısındaki `company_check` satırı kaç firmanın hangi yolla kaldığını (`name`, `register_number`, `register_name`, `site`) ve kaçının çıkarıldığını (`left_out`) gösterir.
- Birkaç fuar tek dosyada: `--run-dir` seçeneğini her koşu için tekrarlayın. Fuar adını değiştirmek için yolun sonuna `="Fuar adı"` ekleyin.
- Başlık dili Polonya koşusunda Lehçe, Birleşik Krallık koşusunda İngilizce, ötekilerde Türkçedir; `--lang tr|pl|en` ile değişir. Polonya ve Birleşik Krallık dosyalarının bilgi sayfasında "yalnız şirketler" satırı da bulunur.
- Ücretsiz örnek için `--sample 20` ekleyin: iletişim bilgisi tam olan ilk 20 firma, e-posta ve telefon kısmen gizli yazılır.

### Arşivleme

Panelde **Arşive kopyala** sonuç, rapor ve girdi dosyalarını `Belgeler\FUAR_SONUCLARI\<yıl-ay> <fuar adı>` klasörüne kopyalar. Aynı adlı dosya varsa üzerine yazmaz.

## 6. Ücretli API anahtarları

Ayrıntılar için README'nin 5. bölümüne bakın. Kısaca:

```powershell
python tools\api_anahtarlari.py
```

Bright Data, Google Places ve Hunter anahtarları görünmeden yazılır ve `state\api_keys.json` dosyasına Windows kullanıcı hesabına bağlı şifreyle kaydedilir. Bu dosya git'e girmez.

**Companies House (ücretsiz):** İngiltere müşteri dosyasındaki şirket kontrolü için kullanılır.

1. developer.company-information.service.gov.uk adresinde **Sign in / Register** ile hesap açın.
2. Bir uygulama oluşturun: ad ve açıklama yazın, ortam olarak **Live** seçin, **Create**'e basın.
3. Uygulamada **Create new key** → anahtar adı yazın, tür olarak **API key** seçin → **Create**.
4. `python tools\api_anahtarlari.py` komutunda Companies House sorusuna `e` yazıp anahtarı yapıştırın.

Companies House 5 dakikada en çok 600 sorguya izin verir; araç sorguları buna göre yavaşlatır.

## 7. Sorun giderme

| Belirti | Ne yapmalı |
|---|---|
| `Proje runtime bulunamadı` | Kurulum yapılmamış ya da yarım kalmış. `KURULUM.cmd`'yi yeniden çalıştırın. |
| Panel açılmıyor | `python tools\kosu_paneli.py --self-check` çalıştırın; `PANEL_OK` yazmalı. Yazmıyorsa kurulumu yeniden çalıştırın. |
| `HATA: Listede firma bulunamadı.` | Bölüm 2.3'e bakın. |
| Çok az site bulundu | `rapor.md` içindeki "Ücretsiz arama canary" satırına bakın. Arama motorlarının çoğu yanıt vermiyorsa daha sonra tekrar deneyin ya da Bright Data kullanın. |
| Koşu yarıda kaldı | Aynı komutu aynı girdiyle yeniden çalıştırın; kaldığı yerden devam eder. `rapor.md` içinde durum `KISMI_…` ise o ana kadarki sonuçlar `sonuclar.xlsx` içindedir. |
| Ücretli kaynak çağrılmadı | Anahtar kayıtlı mı bakın: `python tools\api_anahtarlari.py`. Bright Data için zone adının `serp_api1` olduğunu ya da `BRIGHTDATA_ZONE` değişkeninin ayarlandığını kontrol edin. |

## 8. Soğuk e-posta gönderimi

Takip dosyasındaki firmalara e-postayı tek tek değil, `tools\eposta_gonder.py` ile gönderin. Araç yavaş ve kontrollü gönderir:

- Her firmaya bir kez yazılır. Gönderim tarihi ya da sonucu dolu, Yanıt ya da Not sütunu dolu, ret listesinde olan firmaya yazılmaz; aynı firmanın ikinci adresine de yazılmaz.
- Yalnız genel adreslere yazılır. Kişi adresleri (ad@, ad.soyad@) ve kişisel veri adresleri atlanır.
- Günde en çok 20 e-posta gider; `--limit` ile en çok 40. Sınır bütün takip dosyalarındaki gönderimleri ve deneme e-postalarını birlikte sayar.
- İki e-posta arasında 1,5–4 dakika beklenir. Yalnız hafta içi, alıcının saatiyle 09:00–17:00 arasında gönderilir; ülke `--ulke TR`, `--ulke GB` ya da `--ulke PL` ile seçilir.
- Her gönderim önce `state\eposta_gonderim_kaydi.jsonl` kaydına, sonra takip dosyasına yazılır. Gönderim sürerken takip dosyasını Excel'de açmayın. Açarsanız gönderim durur; gönderilenler bir sonraki çalıştırmada dosyaya işlenir.
- Zoho bir adresi reddederse o firmanın "Gönderim sonucu" sütununa yazılır. Zoho girişi ya da gönderimi durdurursa araç hemen biter; o gün yeniden denemeyin.

**Bir kez: Zoho uygulama şifresi.**

1. accounts.zoho.eu → **Security** → **App Passwords** → **Generate New Password**; ad olarak `fuar-araci` yazın. Bu bölüm görünmüyorsa önce aynı sayfada iki adımlı doğrulamayı açın.
2. `python tools\api_anahtarlari.py` komutunda "Zoho Mail uygulama şifresi" sorusuna `e` yazıp şifreyi yapıştırın.

**Şablon:** düz metin dosyası. İlk satırlar gönderen ve konudur; boş bir satırdan sonra metin gelir. `{hitap}` yerine takip dosyasındaki "Hitap adı" yazılır. Gönderen adresi Zoho'daki kendi adresiniz olmalıdır; araç bu adresle giriş yapar. Şablonda köşeli parantezli bir alan (`[Ad Soyad]` gibi) kalırsa araç göndermez.

```text
Gönderen: Ad Soyad <ad@ozmenworks.com>
Konu: Stand işleriniz için fuar öncesi müşteri listesi

Merhaba {hitap} ekibi,
...
```

**Adımlar:**

```powershell
python tools\eposta_gonder.py yeni-takip --liste teslim\musteri\LISTE.xlsx --takip HEDEF_takip.xlsx --ulke GB
python tools\eposta_gonder.py hazirla --takip HEDEF_takip.xlsx
python tools\eposta_gonder.py onizle --takip HEDEF_takip.xlsx --sablon sablon.txt
python tools\eposta_gonder.py deneme --takip HEDEF_takip.xlsx --sablon sablon.txt --alici ad@ozmenworks.com
python tools\eposta_gonder.py gonder --takip HEDEF_takip.xlsx --sablon sablon.txt --ulke TR
```

1. `yeni-takip` müşteri dosyası aracının yazdığı listeden yeni bir takip dosyası yazar (yalnız e-postası olan firmalar). Eski bir takip dosyası için `hazirla` yeterlidir: eksik "Hitap adı" ve "Gönderim sonucu" sütunlarını ekler ve hitap adlarını doldurur.
2. Hitap adlarını Excel'de kontrol edip düzeltin; "Merhaba Morex ekibi," gibi okunmalı.
3. `onizle` ilk e-postaları `HEDEF_takip_onizleme.txt` dosyasına yazar ve kaç firmanın neden atlandığını gösterir. Hiçbir şey göndermez.
4. `deneme` ilk e-postayı, konusunun başına "[DENEME]" ekleyerek yalnız size gönderir. Gelen kutusunu ve istenmeyen klasörünü kontrol edin.
5. `gonder` firmalara gönderir. Pencereyi kapatmayın; Ctrl+C ile durdurabilirsiniz.

Yanıtları ve geri dönen e-postaları takip dosyasının Yanıt ve Not sütunlarına siz işleyin. "İstemiyorum" diyen firmaları ret listesine ekleyin.
