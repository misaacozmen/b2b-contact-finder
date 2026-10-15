"""Send the cold e-mails of a tracking workbook slowly and under control (Talimat 45).

Steps:
    python tools\\eposta_gonder.py yeni-takip --liste teslim\\musteri\\LISTE.xlsx --takip HEDEF_takip.xlsx --ulke GB
    python tools\\eposta_gonder.py hazirla --takip HEDEF_takip.xlsx
    python tools\\eposta_gonder.py onizle --takip HEDEF_takip.xlsx --sablon sablon_TR.txt
    python tools\\eposta_gonder.py deneme --takip HEDEF_takip.xlsx --sablon sablon_TR.txt --alici kendi@adresiniz
    python tools\\eposta_gonder.py gonder --takip HEDEF_takip.xlsx --sablon sablon_TR.txt --ulke TR

Only "gonder" writes to firms.  The Zoho app password is saved once with
tools\\api_anahtarlari.py and never printed.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from modules import country_profile, opt_out, outreach  # noqa: E402


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
    args = parser.parse_args(argv)

    try:
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
