"""Send the cold e-mails of a tracking workbook slowly and under control (Talimat 45).

Steps:
    python tools\\eposta_gonder.py yeni-takip --liste teslim\\musteri\\LISTE.xlsx --takip HEDEF_takip.xlsx --ulke GB
    python tools\\eposta_gonder.py hazirla --takip HEDEF_takip.xlsx
    python tools\\eposta_gonder.py onizle --takip HEDEF_takip.xlsx --sablon sablon_TR.txt
    python tools\\eposta_gonder.py deneme --takip HEDEF_takip.xlsx --sablon sablon_TR.txt --alici kendi@adresiniz
    python tools\\eposta_gonder.py gonder --takip HEDEF_takip.xlsx --sablon sablon_TR.txt --ulke TR

Replies (Talimat 50), set up once and then checked every half hour:
    python tools\\eposta_gonder.py yanit-ayar --hesap ad@firmaniz.com --bildirim kendi@adresiniz --klasor TAKIP_KLASORU --sessiz 15:20-17:15
    python tools\\eposta_gonder.py yanitlar
    python tools\\eposta_gonder.py yanit-zamanla

Reply drafts and the morning summary (Talimat 51), set up once:
    python tools\\eposta_gonder.py taslak-ayar --yanit-sablon yanit_sablonlari_TR.txt --soguk-sablon sablon_TR.txt --ornek ORNEK.xlsx
    python tools\\eposta_gonder.py fuar-ekle --ad "Fuar 2026" --turk 120 --iletisim teslim\\musteri\\FUAR.xlsx
    python tools\\eposta_gonder.py fuarlar
    python tools\\eposta_gonder.py ozet

E-mail commands and answers that go by themselves (Talimat 53):
    python tools\\eposta_gonder.py otomatik-ayar --baslangic 2026-10-31
    python tools\\eposta_gonder.py komut-dene

"gonder" writes the cold e-mails.  The reply check writes to a firm only on the
user's command (T12 gönder) or, from the otomatik-ayar day, with a simple first
answer.  The Zoho app password is saved once with tools\\api_anahtarlari.py and
never printed.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import (  # noqa: E402
    country_profile, intent, opt_out, outreach, replies, reply_commands, reply_drafts, sales_db,
)


def _tracker(path: Path, *, write: bool) -> outreach.Tracker:
    if not path.is_file():
        raise outreach.OutreachError(f"Takip dosyası bulunamadı: {path}")
    if write and not outreach.writable(path):
        raise outreach.OutreachError("Takip dosyası Excel'de açık. Kapatıp yeniden çalıştırın.")
    return outreach.Tracker(path)


def _status(tracker: outreach.Tracker, template: dict | None, country: str, limit: int) -> tuple[list[dict], str]:
    log = outreach.load_log()
    outreach.sync_from_log(tracker, log)
    ready, reasons = outreach.next_firms(tracker, opt_out_entries=opt_out.load(config.OPT_OUT_FILE), log=log)
    today = datetime.now().date()
    lines = [
        f"Gönderilebilecek firma: {len(ready)}",
        *(f"  atlanan ({reason}): {count}" for reason, count in sorted(reasons.items())),
        f"Bugün gönderilen: {outreach.sent_on(log, today)}; bugün kalan hak: {outreach.daily_room(log, limit, today)}",
        f"Şu an mesai saati ({country}): {'evet' if outreach.office_hours(datetime.now(timezone.utc), country) else 'hayır'}",
    ]
    if template:
        lines.insert(0, f"Gönderen: {template['sender_name']} <{template['sender_address']}>; konu: {template['subject']}")
    return ready, "\n".join(lines)


def _drafting(settings: dict) -> reply_drafts.Drafting | None:
    """Draft settings saved by taslak-ayar; None while drafts are not set up."""
    if not settings.get("yanit_sablon"):
        return None
    cold = outreach.load_template(Path(settings["soguk_sablon"])) if settings.get("soguk_sablon") else None
    return reply_drafts.Drafting(
        templates=reply_drafts.load_templates(Path(settings["yanit_sablon"])),
        sender_name=(cold or {}).get("sender_name", ""), since=date.fromisoformat(settings["taslak_baslangic"]),
        db_path=config.SALES_DB_FILE, jev_key=intent.api_key(), cold_template=cold,
        fallback_sample=Path(settings["ornek"]) if settings.get("ornek") else None,
        auto_since=date.fromisoformat(settings["otomatik_baslangic"]) if settings.get("otomatik_baslangic") else None,
    )


def _check_replies() -> int:
    """One reply check; it usually runs from the Windows task, so it also writes the run log."""
    now = datetime.now()
    settings = replies.load_settings()
    if not settings.get("hesap") or not settings.get("klasor"):
        raise outreach.OutreachError("Önce yanit-ayar komutunu çalıştırın.")
    if replies.quiet_now(settings.get("sessiz", ""), now):
        print("Sessiz saat: kontrol yapılmadı.")
        return 0
    password = outreach.smtp_password()
    if not password:
        raise outreach.OutreachError("Zoho uygulama şifresi kayıtlı değil: tools\\api_anahtarlari.py")
    start = settings.get("baslangic")
    try:
        result = replies.check(
            account=settings["hesap"], password=password, notify_to=settings.get("bildirim", ""),
            tracker_dir=Path(settings["klasor"]), notify_since=date.fromisoformat(start) if start else None,
            drafting=_drafting(settings), now=now,
        )
    except (replies.ReplyError, reply_drafts.ReplyDraftError) as error:
        replies.write_run_log(now, f"HATA: {error}")
        raise outreach.OutreachError(str(error)) from None
    except Exception as error:  # noqa: BLE001 - the window-less task has no other place to report
        replies.write_run_log(now, f"HATA: {type(error).__name__}: {error}")
        raise
    summary = "; ".join(f"{name} {count}" for name, count in result.items() if count) or "yeni ileti yok"
    replies.write_run_log(now, summary)
    print(f"Yanıt kontrolü: {summary}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    countries = tuple(country_profile.PROFILES)

    new = commands.add_parser("yeni-takip", help="müşteri dosyasından yeni takip dosyası")
    new.add_argument("--liste", required=True, type=Path)
    new.add_argument("--takip", required=True, type=Path)
    new.add_argument("--ulke", choices=countries, default="TR")

    prepare = commands.add_parser("hazirla", help="eksik sütunları ve hitap adlarını doldur")
    prepare.add_argument("--takip", required=True, type=Path)
    prepare.add_argument("--ulke", choices=countries, default="TR")

    for name, help_text in (("onizle", "gönderilecek e-postaları dosyaya yaz"), ("deneme", "ilk e-postayı kendinize gönderin"),
                            ("gonder", "firmalara gönder")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--takip", required=True, type=Path)
        command.add_argument("--sablon", required=True, type=Path)
        command.add_argument("--ulke", choices=countries, default="TR")
        command.add_argument("--limit", type=int, default=config.OUTREACH_DAILY_LIMIT,
                             help=f"günlük en çok e-posta (üst sınır {config.OUTREACH_MAX_DAILY_LIMIT})")
        if name == "onizle":
            command.add_argument("--adet", type=int, default=5)
        if name == "deneme":
            command.add_argument("--alici", required=True)

    reply_setup = commands.add_parser("yanit-ayar", help="yanıt kontrolünün ayarları (bir kez)")
    reply_setup.add_argument("--hesap", required=True, help="e-postaların gönderildiği Zoho adresi")
    reply_setup.add_argument("--bildirim", required=True, help="yanıt bildirimlerinin gideceği kendi adresiniz")
    reply_setup.add_argument("--klasor", required=True, type=Path, help="takip dosyalarının klasörü")
    reply_setup.add_argument("--sessiz", default="", help="hafta içi kontrol yapılmayacak saatler, örnek 15:20-17:15")
    commands.add_parser("yanitlar", help="gelen kutusunu oku, yanıtları işle ve bildir")
    reply_task = commands.add_parser("yanit-zamanla", help="yanıt kontrolünü Windows'ta yarım saatte bir çalıştır")
    reply_task.add_argument("--kaldir", action="store_true", help="zamanlanmış görevi kaldır")
    draft_setup = commands.add_parser("taslak-ayar", help="yanıt taslakları ve sabah özeti (bir kez)")
    draft_setup.add_argument("--yanit-sablon", required=True, type=Path, help="yanıt şablonları dosyası")
    draft_setup.add_argument("--soguk-sablon", required=True, type=Path, help="soğuk e-posta şablonu (gönderen adı için)")
    draft_setup.add_argument("--ornek", type=Path, help="fuar adı verilmeden örnek isteyene eklenecek hazır örnek dosyası")
    fair = commands.add_parser("fuar-ekle", help="teklif verilebilecek fuarı ekle ya da güncelle")
    fair.add_argument("--ad", required=True)
    fair.add_argument("--diger", default="", help="fuarın diğer adları, noktalı virgülle")
    fair.add_argument("--turk", type=int, default=0, help="fuardaki Türk katılımcı sayısı")
    fair.add_argument("--liste", type=Path, help="katılımcı listesi (girdi dosyası)")
    fair.add_argument("--iletisim", type=Path, help="iletişim bilgili müşteri dosyası (örnek buradan seçilir)")
    fair.add_argument("--durum", choices=sales_db.FAIR_STATES, default="hazir", help="hazir: liste var; yok: yayımlanmadı")
    commands.add_parser("fuarlar", help="kayıtlı fuarları göster")
    commands.add_parser("ozet", help="sabah özetinin bugünkü halini ekrana yaz (göndermez)")
    auto = commands.add_parser("otomatik-ayar", help="basit yanıtların kendiliğinden gideceği başlangıç günü")
    auto_choice = auto.add_mutually_exclusive_group(required=True)
    auto_choice.add_argument("--baslangic", type=date.fromisoformat, help="YYYY-AA-GG; o gün ve sonrası gelen yanıtlar")
    auto_choice.add_argument("--kapat", action="store_true", help="kendiliğinden gönderimi kapat")
    commands.add_parser("komut-dene", help="kendi adresinize bir deneme taslağını komut yoluyla gönder")
    args = parser.parse_args(argv)

    try:
        if args.command == "komut-dene":
            settings = replies.load_settings()
            if not settings.get("hesap") or not settings.get("bildirim"):
                raise outreach.OutreachError("Önce yanit-ayar komutunu çalıştırın.")
            password = outreach.smtp_password()
            if not password:
                raise outreach.OutreachError("Zoho uygulama şifresi kayıtlı değil: tools\\api_anahtarlari.py")
            problem = reply_commands.self_test(account=settings["hesap"], password=password, owner=settings["bildirim"])
            if problem:
                raise outreach.OutreachError(f"Komut denemesi olmadı: {problem}")
            print(f"Komut denemesi tamam: deneme taslağı gönderildi ({settings['bildirim']}) ve Çöp kutusuna taşındı.")
            return 0
        if args.command == "otomatik-ayar":
            settings = replies.load_settings()
            if not settings.get("yanit_sablon"):
                raise outreach.OutreachError("Önce taslak-ayar komutunu çalıştırın.")
            if args.kapat:
                settings.pop("otomatik_baslangic", None)
                replies.save_settings(settings)
                print("Kendiliğinden gönderim kapalı; bütün yanıtlar taslak olarak kalır.")
                return 0
            replies.save_settings({**settings, "otomatik_baslangic": args.baslangic.isoformat()})
            print(f"Kendiliğinden gönderim {args.baslangic:%d.%m.%Y} ve sonrası gelen yanıtlar için açık: "
                  f"{', '.join(config.AUTO_REPLY_TEMPLATES)}; yalnız firmaya ilk yanıtımızsa, mesai saatinde, "
                  f"günde en çok {config.AUTO_REPLY_DAILY_LIMIT}.")
            return 0
        if args.command == "taslak-ayar":
            settings = replies.load_settings()
            if not settings.get("hesap"):
                raise outreach.OutreachError("Önce yanit-ayar komutunu çalıştırın.")
            reply_drafts.load_templates(args.yanit_sablon)
            outreach.load_template(args.soguk_sablon)
            if args.ornek and not args.ornek.is_file():
                raise outreach.OutreachError(f"Örnek dosyası bulunamadı: {args.ornek}")
            sales_db.create(config.SALES_DB_FILE)
            replies.save_settings({
                **settings, "yanit_sablon": str(args.yanit_sablon.resolve()),
                "soguk_sablon": str(args.soguk_sablon.resolve()),
                "ornek": str(args.ornek.resolve()) if args.ornek else "", "taslak_baslangic": date.today().isoformat(),
            })
            key = "kayıtlı" if intent.api_key() else "kayıtlı değil (tools\\api_anahtarlari.py)"
            print(f"Taslak ayarları kaydedildi; veritabanı: {config.SALES_DB_FILE}. Jev anahtarı {key}. "
                  "Bugünden önceki yanıtlara taslak yazılmaz.")
            return 0
        if args.command in {"fuar-ekle", "fuarlar", "ozet"}:
            if not config.SALES_DB_FILE.is_file():
                raise outreach.OutreachError("Önce taslak-ayar komutunu çalıştırın.")
            connection = sales_db.connect(config.SALES_DB_FILE)
            try:
                if args.command == "fuar-ekle":
                    for path in (args.liste, args.iletisim):
                        if path and not path.is_file():
                            raise outreach.OutreachError(f"Dosya bulunamadı: {path}")
                    sales_db.save_fair(
                        connection, ad=args.ad.strip(), diger_adlar=args.diger.strip(), turk_firma=args.turk,
                        liste_dosyasi=str(args.liste.resolve()) if args.liste else "",
                        iletisim_dosyasi=str(args.iletisim.resolve()) if args.iletisim else "", durum=args.durum,
                    )
                    print(f"Fuar kaydedildi: {args.ad.strip()}")
                    return 0
                if args.command == "fuarlar":
                    for row in sales_db.fairs(connection):
                        price = reply_drafts.quote(row)
                        print(f"{row['id']}. {row['ad']} | durum {row['durum']} | Türk firma {row['turk_firma']} | "
                              f"en çok {reply_drafts.money(price['en_cok'])} TL | diğer adlar: {row['diger_adlar'] or '-'}")
                    return 0
                settings = replies.load_settings()
                log = outreach.load_log()
                message = replies.summary_message(
                    connection, today=date.today(), log=log, tracker_dir=Path(settings["klasor"]),
                    account=settings["hesap"], to=settings.get("bildirim", ""),
                )
                print(message.get_body(preferencelist=("plain",)).get_content())
                return 0
            finally:
                connection.close()
        if args.command == "yanit-ayar":
            if not args.klasor.is_dir():
                raise outreach.OutreachError(f"Klasör bulunamadı: {args.klasor}")
            replies.save_settings({
                "hesap": args.hesap.strip().casefold(), "bildirim": args.bildirim.strip(),
                "klasor": str(args.klasor.resolve()), "sessiz": args.sessiz.strip(),
                "baslangic": date.today().isoformat(),
            })
            print(f"Ayarlar kaydedildi: {config.REPLY_SETTINGS_FILE}. Bugünden önceki yanıtlar için bildirim gitmez.")
            return 0
        if args.command == "yanitlar":
            return _check_replies()
        if args.command == "yanit-zamanla":
            code, output = replies.schedule_task(remove=args.kaldir)
            print(output)
            if code:
                raise outreach.OutreachError("Windows görevi oluşturulamadı ya da kaldırılamadı.")
            print("Görev kaldırıldı." if args.kaldir else
                  f"Görev kuruldu: her gün 08:00-22:00 arası {config.REPLY_TASK_EVERY_MIN} dakikada bir. "
                  f"Sonuçlar: {config.REPLY_RUN_LOG_FILE}")
            return 0
        if args.command == "yeni-takip":
            count = outreach.new_tracker(args.takip, outreach.read_customer_list(args.liste), args.ulke)
            print(f"Takip dosyası yazıldı: {args.takip} ({count} firma). Hitap adlarını Excel'de kontrol edin.")
            return 0
        tracker = _tracker(args.takip, write=args.command in {"hazirla", "gonder"})
        if args.command == "hazirla":
            filled = outreach.fill_salutations(tracker, args.ulke)
            synced = outreach.sync_from_log(tracker, outreach.load_log())
            tracker.save()
            print(f"Hitap adı doldurulan: {filled}; kayıttan işlenen gönderim: {synced}. Hitap adlarını Excel'de kontrol edin.")
            return 0
        template = outreach.load_template(args.sablon)
        ready, status = _status(tracker, template, args.ulke, args.limit)
        print(status)
        if args.command == "onizle":
            out = args.takip.with_name(f"{args.takip.stem}_onizleme.txt")
            out.write_text(outreach.preview(template, ready[:args.adet]), encoding="utf-8")
            print(f"Önizleme: {out}")
            return 0
        password = outreach.smtp_password()
        if not password:
            raise outreach.OutreachError("Zoho uygulama şifresi kayıtlı değil: tools\\api_anahtarlari.py")
        if not ready:
            raise outreach.OutreachError("Gönderilebilecek firma yok.")
        if args.command == "deneme":
            outreach.send_test(template, ready[0], to=args.alici, password=password)
            print(f"Deneme e-postası gönderildi: {args.alici}. Gelen kutusunu ve istenmeyen klasörünü kontrol edin.")
            return 0
        print("Gönderim başladı. Durdurmak için Ctrl+C; gönderilenler kaydedilir.")
        try:
            result = outreach.send_batch(
                tracker, template, country=args.ulke, limit=args.limit, password=password,
                opt_out_entries=opt_out.load(config.OPT_OUT_FILE),
            )
        except KeyboardInterrupt:
            print("Durduruldu. Gönderilenler takip dosyasına ve kayda işlendi.")
            return 0
        print(f"Bitti: gönderilen {result['sent']}, reddedilen adres {result['refused']}"
              + (f"; durma nedeni: {result['stopped']}" if result["stopped"] else ""))
        return 0
    except outreach.OutreachError as error:
        print(f"HATA: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
