# B2B Contact Finder

Firmalardan resmi web sitesi, e-posta ve telefon bulup Excel çıktısı üreten CLI aracı.

## Kurulum

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Kullanım

`input/firms.xlsx` dosyasına tek sütun halinde firma adlarını koyun. İlk satır `company` olabilir.

```powershell
python main.py
```

Farklı bir dosya ile çalıştırmak için:

```powershell
python main.py --input C:\path\to\firms.xlsx
```

## Çıktılar

- `output/contacts.xlsx`: bulunan sonuçlar
- `output/failed.xlsx`: bulunamayan ya da eksik kalan kayıtlar
- `output/website_candidates.xlsx`: her firma için ilk 3 website adayı, skor ve seçim gerekçesi
- `output/report.txt`: özet rapor
- `output/logs.txt`: işlem logları
- `state/progress.json`: kesinti sonrası devam etmek için checkpoint

Program tamamlanınca `state/progress.json` temizlenir. İşlem yarıda kalırsa sonraki `python main.py` aynı input hash'iyle kaldığı yerden devam eder.

## GitHub Paylaşım Notları

Gerçek firma listeleri ve üretilen çıktılar repoya eklenmez. Kendi listenizi `input/firms.xlsx` olarak koyup programı çalıştırın.

Repoya dahil edilmeyen klasörler/dosyalar:

- `.venv/`
- `input/*.xlsx`
- `output/`
- `state/`
- `artifact_work/`
