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
- **Liste sayfası başka bir sayfayı çerçeve içinde gösteriyorsa** (sayfa kaynağında tek bir `<iframe src="…">` vardır): çerçevenin adresini açıp onu deneyin.
- Yine çekilemezse listeyi kendiniz Excel'e alın ve bölüm 1'deki başlıklarla kaydedin.

Çekilen listede yabancı katılımcılar da olur. Yalnız bir ülkenin firmalarını istiyorsanız listeyi koşudan önce `country` sütununa göre süzün.

## 3. Koşu

### 3.1 Panelden (önerilen)

1. Listeyi çekin ya da hazır Excel'i **Seç…** ile seçin. Dosya `input` klasöründe olmak zorunda değildir. Panel firma sayısını ve hangi sütunların dolu olduğunu gösterir; sorun varsa kırmızı uyarı çıkar.
2. **Firmaların ülkesini** seçin: **Türkiye** ya da **Polonya**. Seçim aramaların dilini, telefon biçimini ve karar kurallarını ayarlar.
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

Polonya fuarında sona `--country PL` ekleyin.

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
- NIP ile doğrulanan sitede e-posta ya da telefon bulunamazsa web sitesi yine teslim edilir (orta güven).
- Ücretli Bright Data koşusunda NIP önce Google'da aranır.
- NIP'i olmayan yabancı katılımcılarda bu kanıt yoktur; onların siteleri daha az bulunur.

## 5. Sonuçlar

Her koşunun dosyaları `runs\<koşu kimliği>\output\` klasöründedir. Panelde **Klasörü aç** bu klasörü açar. Terminalden en son koşunun klasörünü açmak için:

```powershell
explorer "$((Get-ChildItem runs -Directory | Sort-Object CreationTime -Descending | Select-Object -First 1).FullName)\output"
```

Önemli olan iki dosya var.

### `sonuclar.xlsx`

| Sayfa | İçerik |
|---|---|
| **İletişim** | Firma, web sitesi, e-posta, telefon; bütün firmalar girdi sırasıyla. Kullanacağınız liste budur. |
| Özet | Kaç firmada web sitesi, e-posta ve telefon bulunduğu ve oranlar. |
| Detaylar | Aynı firmalar: yayına hazır mı, eksik alanlar, aday web sitesi, her bilginin kaynağı ve güveni, fuar listesindeki bilgiler. |

- **Sarı hücre** güveni düşük bilgidir; kullanmadan önce kontrol edin.
- E-posta firmanın kendi sitesinde yazıyorsa, alan adı siteden farklı olsa da alınır (ör. site `firma.com.tr`, e-posta `info@firma.com`).
- Sitede yalnız gmail, hotmail gibi bir adres varsa o yazılır ve sarı işaretlenir. KEP adresleri yazılmaz.
- **"Aday web sitesi (kontrol edin)"** sütunu (Detaylar): sistem siteyi doğrulayamadığında aramada bulduğu en iyi adayı buraya yazar. Adaylar doğrulanmamıştır; yaklaşık yarısı doğrudur. Kullanmadan önce açıp kontrol edin.

### `rapor.md`

Koşunun özeti: kaç firmada web sitesi, e-posta ve telefon bulunduğu, bilgilerin hangi kaynaktan geldiği, ücretsiz arama motorlarının durumu ve ücretli çağrı sayıları.

Klasördeki diğer dosyalar teknik kayıtlardır.

### Arşivleme

Panelde **Arşive kopyala** sonuç, rapor ve girdi dosyalarını `Belgeler\FUAR_SONUCLARI\<yıl-ay> <fuar adı>` klasörüne kopyalar. Aynı adlı dosya varsa üzerine yazmaz.

## 6. Ücretli API anahtarları

Ayrıntılar için README'nin 5. bölümüne bakın. Kısaca:

```powershell
python tools\api_anahtarlari.py
```

Bright Data, Google Places ve Hunter anahtarları görünmeden yazılır ve `state\api_keys.json` dosyasına Windows kullanıcı hesabına bağlı şifreyle kaydedilir. Bu dosya git'e girmez.

## 7. Sorun giderme

| Belirti | Ne yapmalı |
|---|---|
| `Proje runtime bulunamadı` | Kurulum yapılmamış ya da yarım kalmış. `KURULUM.cmd`'yi yeniden çalıştırın. |
| Panel açılmıyor | `python tools\kosu_paneli.py --self-check` çalıştırın; `PANEL_OK` yazmalı. Yazmıyorsa kurulumu yeniden çalıştırın. |
| `HATA: Listede firma bulunamadı.` | Bölüm 2.3'e bakın. |
| Çok az site bulundu | `rapor.md` içindeki "Ücretsiz arama canary" satırına bakın. Arama motorlarının çoğu yanıt vermiyorsa daha sonra tekrar deneyin ya da Bright Data kullanın. |
| Koşu yarıda kaldı | Aynı komutu aynı girdiyle yeniden çalıştırın; kaldığı yerden devam eder. `rapor.md` içinde durum `KISMI_…` ise o ana kadarki sonuçlar `sonuclar.xlsx` içindedir. |
| Ücretli kaynak çağrılmadı | Anahtar kayıtlı mı bakın: `python tools\api_anahtarlari.py`. Bright Data için zone adının `serp_api1` olduğunu ya da `BRIGHTDATA_ZONE` değişkeninin ayarlandığını kontrol edin. |
