"""Talimat 53: the user's e-mail commands (T12 gönder / sil, S12 kapat) and simple answers that go by themselves."""

from datetime import date, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
import io
import json
import sqlite3

import pytest

import config
from modules import outreach, replies, reply_commands, reply_drafts, sales_db
from tools import eposta_gonder


ACCOUNT = "satis@ornek-gonderen.example"
OWNER = "sahip@ornek-kisisel.example"
AUTH_OK = ("mx.zohomail.eu; dkim=pass; spf=pass (zohomail.eu: domain of ornek-kisisel.example) "
           "smtp.mailfrom=sahip@ornek-kisisel.example; dmarc=pass(p=none dis=none) header.from=ornek-kisisel.example")
FIRMS = [
    ("Alfa Ornek", "info@alfa-ornek.com.tr", "<id-alfa@ornek-gonderen.example>"),
    ("Beta Ornek", "info@beta-ornek.com.tr", "<id-beta@ornek-gonderen.example>"),
    ("Gama Ornek", "info@gama-ornek.com.tr", "<id-gama@ornek-gonderen.example>"),
]
SECTIONS = {
    "imza": "İyi çalışmalar,\nOrnek Gonderen", "ilgileniyor": "Merhaba,\n\n{sss}\n\nHangi fuar?",
    "sss_bicim": "Excel ve CSV.", "sss_sutunlar": "Sütunlar.", "sss_kisi_bilgisi": "Kişi yok.", "sss_teslim": "Aynı gün.",
    "sss_fark": "Fark.", "fiyat_fuarsiz": "Hangi fuar?", "teklif": "{fuar}: {turk_firma} firma, en çok {en_cok_tutar} TL.",
    "teklif_ornek": "Örnek ekte.", "liste_yok": "{fuar} listesi yok.", "ornek_fuarsiz": "Hangi fuar?",
    "soru_adres": "Sitenizden.", "soru_kvkk": "Fuar listesi.", "simdi_degil": "Teşekkürler.", "istemiyor": "Çıkarıldı.",
    "yanlis_kisi": "Sizin ilgilendiğinizi öğrendim.",
}
COLD = "Gönderen: Ornek Gonderen <satis@ornek-gonderen.example>\nKonu: Fuar listeleri\n\nMerhaba {hitap} ekibi,\n\nKısa.\n"
ANSWERS = {
    "Excel": ("ilgileniyor", "yok", "bicim"),
    "ne kadar": ("fiyat", "f1", "yok"),
    "Şimdilik": ("simdi_degil", "yok", "yok"),
}


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    state = tmp_path / "state"
    for name, file_name in (
        ("OUTREACH_LOG_FILE", "eposta_gonderim_kaydi.jsonl"), ("REPLY_LOG_FILE", "yanit_kaydi.jsonl"),
        ("REPLY_STATE_FILE", "durum.json"), ("REPLY_SETTINGS_FILE", "ayarlar.json"),
        ("REPLY_RUN_LOG_FILE", "calisma.log"), ("OPT_OUT_FILE", "ret_listesi.txt"), ("SALES_DB_FILE", "satis.sqlite3"),
        ("SAVED_API_KEYS_FILE", "api_keys.json"),
    ):
        monkeypatch.setattr(config, name, state / file_name)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    state.mkdir()
    (tmp_path / "takip").mkdir()
    with config.OUTREACH_LOG_FILE.open("w", encoding="utf-8") as handle:
        for company, address, message_id in FIRMS:
            handle.write(json.dumps({
                "time": "2026-10-15T09:00:00+00:00", "date": "2026-10-15", "tracker": "Ornek_takip.xlsx",
                "company": company, "email": address, "message_id": message_id, "result": outreach.SENT,
            }) + "\n")
    outreach.new_tracker(tmp_path / "takip" / "Ornek_takip.xlsx",
                         [{"Firma": company, "E-posta": address} for company, address, _id in FIRMS], "TR")
    (tmp_path / "sablon.txt").write_text("".join(f"[{name}]\n{text}\n\n" for name, text in SECTIONS.items()),
                                         encoding="utf-8")
    (tmp_path / "soguk.txt").write_text(COLD, encoding="utf-8")
    sales_db.create(config.SALES_DB_FILE)
    connection = sales_db.connect(config.SALES_DB_FILE)
    sales_db.save_fair(connection, ad="Ornek Bebek Fuarı 2026", diger_adlar="bebek fuarı", turk_firma=85,
                       liste_dosyasi="", iletisim_dosyasi="", durum="hazir")
    connection.close()
    return tmp_path


class FakeJev:
    def __call__(self, request, timeout):
        state = json.loads(request.data.decode("utf-8"))["state"]
        niyet, fuar, sss = next(value for word, value in ANSWERS.items() if word in state)
        payload = {"answers": {
            "niyet": {"choice": niyet, "confidence": 0.93}, "fiyat_istiyor": {"noul": 0.9 if niyet == "fiyat" else 0.1},
            "ornek_istiyor": {"noul": 0.1}, "fuar": {"choice": fuar, "confidence": 0.9}, "sss": {"choice": sss},
        }}
        return io.BytesIO(json.dumps(payload).encode("utf-8"))


class FakeImap:
    """Zoho folders by UID; drafts can be added, searched by header and moved to the Trash."""

    def __init__(self, folders):
        self.folders = {"INBOX": {}, "Sent": {}, "Drafts": {}, "Trash": {}, **folders}
        self.current = None

    def login(self, user, password):
        pass

    def logout(self):
        pass

    def select(self, folder, readonly=False):
        self.current = folder.strip('"')
        if self.current not in self.folders:
            return "NO", [b"no such folder"]
        return "OK", [str(len(self.folders[self.current])).encode()]

    def response(self, code):
        return code, [b"7"]

    def append(self, mailbox, flags, date_time, message):
        box = self.folders[mailbox.strip('"')]
        box[max(box, default=0) + 1] = message
        return "OK", [b"[APPENDUID 7 1] Success"]

    def uid(self, command, *args):
        box = self.folders[self.current]
        if command == "SEARCH":
            if args[1] == "UID":
                start = int(args[2].split(":")[0])
                found = [uid for uid in box if uid >= start] or ([max(box)] if box else [])
            elif args[1] == "HEADER":
                header, value = args[2], args[3].strip('"')
                found = [uid for uid, raw in box.items()
                         if str(message_from_bytes(raw, policy=policy.default).get(header, "")).strip() == value]
            else:
                found = list(box)
            return "OK", [" ".join(str(uid) for uid in found).encode()]
        uid = int(args[0])
        if command == "FETCH":
            return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), box[uid]), b")"]
        if command == "MOVE":
            trash = self.folders[args[1].strip('"')]
            trash[max(trash, default=0) + 1] = box.pop(uid)
            return "OK", [b""]
        raise AssertionError(command)


class FakeSmtp:
    sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSmtp.sent.append(message)


@pytest.fixture
def smtp():
    FakeSmtp.sent = []
    return FakeSmtp


def _mail(sender: str, body: str, *, subject="Re: Fuar listeleri", message_id="<cevap@alfa-ornek.com.tr>",
          when="Fri, 16 Oct 2026 10:00:00 +0300", auth: str | None = None) -> bytes:
    message = EmailMessage()
    if auth:
        message["Authentication-Results"] = auth
    message["From"], message["To"], message["Subject"] = sender, ACCOUNT, subject
    message["Date"], message["Message-ID"] = when, message_id
    message.set_content(body)
    return message.as_bytes()


def _command(body: str, code: str, *, subject: str | None = None, sender=OWNER, auth=AUTH_OK, number=1) -> bytes:
    return _mail(sender, body, subject=subject if subject is not None else f"B2B komut {code}: {body}",
                 message_id=f"<komut{number}@ornek-kisisel.example>", auth=auth)


def _drafting(files, auto_since=None) -> reply_drafts.Drafting:
    return reply_drafts.Drafting(
        templates=reply_drafts.load_templates(files / "sablon.txt"), sender_name="Ornek Gonderen",
        since=date(2026, 10, 16), db_path=config.SALES_DB_FILE, jev_key="synthetic-jev-key",
        cold_template=outreach.load_template(files / "soguk.txt"), opener=FakeJev(), auto_since=auto_since,
    )


def _check(files, imap, drafting, now):
    return replies.check(account=ACCOUNT, password="synthetic-password", notify_to=OWNER, tracker_dir=files / "takip",
                         imap_factory=lambda: imap, smtp_factory=FakeSmtp, drafting=drafting, now=now)


def _rows():
    connection = sales_db.connect(config.SALES_DB_FILE)
    try:
        return {row["eposta"]: dict(row) for row in connection.execute("SELECT * FROM yanitlar")}
    finally:
        connection.close()


def _today_code(day: date) -> str:
    connection = sales_db.connect(config.SALES_DB_FILE)
    try:
        return sales_db.command_code(connection, day)
    finally:
        connection.close()


def _plain(message) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


# --- reading commands ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject, body, quoted, expected", [
    ("B2B komut ABCD2345: T12 gönder", "T12 gönder", "", [("T", 12, "gonder")]),
    ("Re: Günlük özet", "T3 sil\nS4 KAPAT", "", [("T", 3, "sil"), ("S", 4, "kapat")]),
    ("", "T5 ve T6 gönderin", "", [("T", 5, "gonder"), ("T", 6, "gonder")]),
    ("", "T7 gönder, T8 sil", "", [("T", 7, "gonder"), ("T", 8, "sil")]),
    ("", "GÖNDER", "Kod: [T9]\nKomut kodu: X", [("T", 9, "gonder")]),
    ("", "Bunu gönderme lütfen", "Kod: [T10]", [("T", 10, "sil")]),
    ("", "Teşekkürler, akşam bakarım.", "Kod: [T11]", []),
])
def test_commands_are_read_from_the_new_lines(subject, body, quoted, expected):
    assert reply_commands.parse(subject, body, quoted) == (expected, [])


def test_an_unclear_command_is_reported():
    commands, unclear = reply_commands.parse("", "T1 T2 sil gönder kapat", "")
    assert commands == [] and unclear == ["T1 T2 sil gönder kapat"]
    commands, unclear = reply_commands.parse("", "gönder", "[T1] ... [T2]")
    assert commands == [] and unclear == ["gönder"]
    commands, unclear = reply_commands.parse("", "Gönderme, ben düzeltip sonra göndereceğim. Hallettim sayılmaz.",
                                             "Kod: [T1]")
    assert commands == [] and unclear == ["Gönderme, ben düzeltip sonra göndereceğim. Hallettim sayılmaz."]
    assert reply_commands.parse("", "Dün gönderdim, bugün de göndereceğim.", "Kod: [T1]") == ([], [])


def test_only_the_users_own_verified_mail_is_a_command():
    def message(sender, *auth):
        mail = EmailMessage()
        for value in auth:
            mail["Authentication-Results"] = value
        mail["From"] = sender
        return mail

    assert reply_commands.from_owner(message(f"Sahip <{OWNER}>", AUTH_OK), OWNER)
    assert not reply_commands.from_owner(message("info@alfa-ornek.com.tr", AUTH_OK), OWNER)
    assert not reply_commands.from_owner(message(OWNER, AUTH_OK.replace("dkim=pass", "dkim=fail")), OWNER)
    assert not reply_commands.from_owner(message(OWNER, "mx.baska.example; dkim=pass; dmarc=pass", AUTH_OK), OWNER)
    assert not reply_commands.from_owner(message(OWNER), OWNER)


def test_buttons_write_the_command_with_the_code():
    page = reply_commands.html_version("  • [T12] Alfa — fiyat\n  • [S3] Beta — sipariş", account=ACCOUNT,
                                       command_code="ABCD2345")
    assert page.count("mailto:") == 3
    assert "subject=B2B%20komut%20ABCD2345%3A%20T12%20g%C3%B6nder" in page
    assert "T12 sil</a>" in page and "S3 kapat</a>" in page


def test_an_older_database_gets_the_new_columns_and_numbers(tmp_path):
    path = tmp_path / "eski.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE yanitlar (anahtar TEXT PRIMARY KEY, eposta TEXT NOT NULL, firma TEXT NOT NULL DEFAULT '', "
        "takip TEXT NOT NULL DEFAULT '', alindi TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', "
        "niyet TEXT NOT NULL DEFAULT '', guven REAL, fuar TEXT NOT NULL DEFAULT '', sonuc TEXT NOT NULL, "
        "neden TEXT NOT NULL DEFAULT '', yanitlandi TEXT NOT NULL DEFAULT '');"
        "INSERT INTO yanitlar (anahtar, eposta, alindi, sonuc) VALUES ('b', 'b@x.example', '2026-10-16T11:00', 'size');"
        "INSERT INTO yanitlar (anahtar, eposta, alindi, sonuc) VALUES ('a', 'a@x.example', '2026-10-16T10:00', 'size');"
    )
    connection.commit()
    connection.close()
    connection = sales_db.connect(path)
    numbers = {row["anahtar"]: row["kod"] for row in connection.execute("SELECT anahtar, kod FROM yanitlar")}
    entry = {"key": "c", "email": "c@x.example", "date": "2026-10-17"}
    assert sales_db.save_reply(connection, entry, niyet="", guven=None, fuar="", sonuc=sales_db.USER, neden="") == 3
    connection.close()
    assert numbers == {"a": 1, "b": 2}


# --- a check with commands ------------------------------------------------------------------------

def test_a_command_sends_the_draft_from_zoho_and_answers_the_user(files, smtp):
    drafting = _drafting(files)
    imap = FakeImap({"INBOX": {1: _mail("Alfa <info@alfa-ornek.com.tr>", "Bebek fuarı listesi ne kadar?")}})
    result = _check(files, imap, drafting, datetime(2026, 10, 16, 10, 30))
    assert result["taslak"] == 1 and len(imap.folders["Drafts"]) == 1
    code = _today_code(date(2026, 10, 16))
    notice = smtp.sent[0]
    assert notice["Subject"] == "Taslak hazır (fiyat): Alfa Ornek"
    assert "Kod: [T1]" in _plain(notice) and f"Komut kodu: {code}" in _plain(notice)
    assert "mailto:" in notice.get_body(preferencelist=("html",)).get_content()

    smtp.sent.clear()
    imap.folders["INBOX"][2] = _command("T1 gönder", code, number=2)
    result = _check(files, imap, drafting, datetime(2026, 10, 16, 11, 0))
    assert result["komut"] == 1
    sent, receipt = smtp.sent
    assert sent["To"] == "info@alfa-ornek.com.tr" and sent["In-Reply-To"] == "<cevap@alfa-ornek.com.tr>"
    assert imap.folders["Drafts"] == {} and len(imap.folders["Trash"]) == 1
    assert receipt["To"] == OWNER and receipt["Subject"] == "Komut sonucu: T1 (Alfa Ornek): gönderildi"
    row = _rows()["info@alfa-ornek.com.tr"]
    assert (row["yanitlandi"], row["gonderim"]) == ("2026-10-16", sales_db.BY_COMMAND)

    smtp.sent.clear()
    imap.folders["INBOX"][3] = _command("T1 gönder", code, number=3)
    _check(files, imap, drafting, datetime(2026, 10, 16, 11, 30))
    assert [message["Subject"] for message in smtp.sent] == [
        "Komut sonucu: T1 (Alfa Ornek): zaten gönderilmiş ya da kapatılmış"]


def test_a_plain_reply_to_the_notice_deletes_the_draft(files, smtp):
    drafting = _drafting(files)
    imap = FakeImap({"INBOX": {1: _mail("Alfa <info@alfa-ornek.com.tr>", "Bebek fuarı listesi ne kadar?")}})
    _check(files, imap, drafting, datetime(2026, 10, 16, 10, 30))
    quoted = "\n".join(f"> {line}" for line in _plain(smtp.sent[0]).split("\n"))
    smtp.sent.clear()
    imap.folders["INBOX"][2] = _command(f"sil\n\n16 Eki 2026 Cum, Yanıt bildirimi şunu yazdı:\n{quoted}", "",
                                        subject="Re: Taslak hazır (fiyat): Alfa Ornek", number=2)
    _check(files, imap, drafting, datetime(2026, 10, 16, 11, 0))
    assert imap.folders["Drafts"] == {} and len(smtp.sent) == 1
    assert smtp.sent[0]["Subject"] == "Komut sonucu: T1 (Alfa Ornek): taslak silindi, yanıt kapatıldı"
    assert _rows()["info@alfa-ornek.com.tr"]["sonuc"] == sales_db.CLOSED


def test_commands_without_a_code_or_from_others_are_not_run(files, smtp):
    drafting = _drafting(files)
    imap = FakeImap({"INBOX": {1: _mail("Alfa <info@alfa-ornek.com.tr>", "Bebek fuarı listesi ne kadar?")}})
    _check(files, imap, drafting, datetime(2026, 10, 16, 10, 30))
    smtp.sent.clear()
    imap.folders["INBOX"][2] = _command("T1 gönder", "", subject="T1 gönder", number=2)
    imap.folders["INBOX"][3] = _command("T1 gönder", _today_code(date(2026, 10, 16)), sender="biri@baska.example",
                                        number=3)
    imap.folders["INBOX"][4] = _command("T1 gönder", "ESKIKOD9", number=4)
    _check(files, imap, drafting, datetime(2026, 10, 16, 11, 0))
    assert len(imap.folders["Drafts"]) == 1
    assert [message["Subject"] for message in smtp.sent] == [
        "Komut sonucu: Komut uygulanmadı: e-postada son günlerin komut kodu yok. Bildirimdeki düğmelere basın "
        "ya da bildirimi yanıtlayın."] * 2
    assert _rows()["info@alfa-ornek.com.tr"]["yanitlandi"] == ""


# --- answers that go by themselves -----------------------------------------------------------------

def test_simple_first_answers_go_by_themselves_inside_office_hours(files, smtp, monkeypatch):
    drafting = _drafting(files, auto_since=date(2026, 10, 16))
    imap = FakeImap({"INBOX": {
        1: _mail("Alfa <info@alfa-ornek.com.tr>", "Listeler Excel mi geliyor?"),
        2: _mail("Beta <info@beta-ornek.com.tr>", "Bebek fuarı listesi ne kadar?", message_id="<b@beta.example>"),
        3: _mail("Gama <info@gama-ornek.com.tr>", "Şimdilik gerek yok.", message_id="<g@gama.example>"),
    }})
    _check(files, imap, drafting, datetime(2026, 10, 16, 10, 0))
    rows = _rows()
    assert (rows["info@alfa-ornek.com.tr"]["otomatik"], rows["info@beta-ornek.com.tr"]["otomatik"],
            rows["info@gama-ornek.com.tr"]["otomatik"]) == (1, 0, 1)
    subjects = {message["Subject"] for message in smtp.sent}
    assert "Kendiliğinden gidecek (ilgileniyor): Alfa Ornek" in subjects
    assert "Taslak hazır (fiyat): Beta Ornek" in subjects
    assert len(imap.folders["Drafts"]) == 3

    smtp.sent.clear()
    _check(files, imap, drafting, datetime(2026, 10, 16, 10, 20))
    assert smtp.sent == []  # too soon after the draft

    monkeypatch.setattr(config, "AUTO_REPLY_DAILY_LIMIT", 1)
    result = _check(files, imap, drafting, datetime(2026, 10, 16, 10, 40))
    assert result["kendiliğinden giden"] == 1 and [message["To"] for message in smtp.sent] == ["info@alfa-ornek.com.tr"]
    assert _rows()["info@alfa-ornek.com.tr"]["gonderim"] == sales_db.BY_ITSELF

    smtp.sent.clear()
    monkeypatch.setattr(config, "AUTO_REPLY_DAILY_LIMIT", 20)
    assert _check(files, imap, drafting, datetime(2026, 10, 16, 20, 0))["kendiliğinden giden"] == 0  # after hours
    assert _check(files, imap, drafting, datetime(2026, 10, 17, 10, 0))["kendiliğinden giden"] == 0  # Saturday
    result = _check(files, imap, drafting, datetime(2026, 10, 19, 9, 30))
    assert result["kendiliğinden giden"] == 1
    assert [message["To"] for message in smtp.sent if message["To"] != OWNER] == ["info@gama-ornek.com.tr"]
    assert len(imap.folders["Drafts"]) == 1  # Beta's quote waits for the user


def test_no_answer_goes_by_itself_before_the_start_or_after_a_delete(files, smtp):
    drafting = _drafting(files, auto_since=date(2026, 10, 17))
    imap = FakeImap({"INBOX": {1: _mail("Alfa <info@alfa-ornek.com.tr>", "Listeler Excel mi geliyor?")}})
    _check(files, imap, drafting, datetime(2026, 10, 16, 10, 0))
    assert _rows()["info@alfa-ornek.com.tr"]["otomatik"] == 0  # came before the start date

    drafting.auto_since = date(2026, 10, 16)
    imap.folders["INBOX"][2] = _mail("Beta <info@beta-ornek.com.tr>", "Şimdilik gerek yok.", message_id="<b@beta.example>")
    _check(files, imap, drafting, datetime(2026, 10, 16, 10, 5))
    assert _rows()["info@beta-ornek.com.tr"]["otomatik"] == 1
    imap.folders["INBOX"][3] = _command("T2 sil", _today_code(date(2026, 10, 16)), number=3)
    smtp.sent.clear()
    _check(files, imap, drafting, datetime(2026, 10, 16, 11, 0))
    assert [message["Subject"] for message in smtp.sent] == ["Komut sonucu: T2 (Beta Ornek): taslak silindi, yanıt kapatıldı"]
    assert len(imap.folders["Drafts"]) == 1


def test_the_summary_shows_codes_buttons_and_what_went_by_itself(files, smtp):
    drafting = _drafting(files, auto_since=date(2026, 10, 16))
    imap = FakeImap({"INBOX": {
        1: _mail("Alfa <info@alfa-ornek.com.tr>", "Listeler Excel mi geliyor?"),
        2: _mail("Beta <info@beta-ornek.com.tr>", "Bebek fuarı listesi ne kadar?", message_id="<b@beta.example>"),
    }})
    _check(files, imap, drafting, datetime(2026, 10, 16, 7, 0))
    _check(files, imap, drafting, datetime(2026, 10, 16, 9, 30))
    smtp.sent.clear()
    _check(files, imap, drafting, datetime(2026, 10, 17, 8, 5))
    summary = smtp.sent[-1]
    text = _plain(summary)
    assert "  • [T2] Beta Ornek — fiyat — teklif" in text
    assert "- Kendiliğinden giden yanıt: 1\n  • Alfa Ornek — ilgileniyor" in text
    assert "- Komutunuzla giden yanıt: 0" in text
    assert f"Komut kodu: {_today_code(date(2026, 10, 17))}" in text
    assert "T2%20g%C3%B6nder" in summary.get_body(preferencelist=("html",)).get_content()


def test_the_self_test_sends_a_draft_to_the_user_and_moves_it_to_the_trash(smtp):
    imap = FakeImap({})
    assert reply_commands.self_test(account=ACCOUNT, password="synthetic-password", owner=OWNER,
                                    imap_factory=lambda: imap, smtp_factory=FakeSmtp) == ""
    assert [message["To"] for message in smtp.sent] == [OWNER]
    assert smtp.sent[0]["Subject"] == "[DENEME] B2B komut denemesi"
    assert imap.folders["Drafts"] == {} and len(imap.folders["Trash"]) == 1


def test_the_tool_sets_the_start_day(files, capsys):
    assert eposta_gonder.main(["otomatik-ayar", "--baslangic", "2026-10-31"]) == 1
    assert "Önce taslak-ayar" in capsys.readouterr().out
    assert eposta_gonder.main(["yanit-ayar", "--hesap", ACCOUNT, "--bildirim", OWNER, "--klasor", str(files / "takip")]) == 0
    assert eposta_gonder.main(["taslak-ayar", "--yanit-sablon", str(files / "sablon.txt"),
                               "--soguk-sablon", str(files / "soguk.txt")]) == 0
    assert eposta_gonder.main(["otomatik-ayar", "--baslangic", "2026-10-31"]) == 0
    assert replies.load_settings()["otomatik_baslangic"] == "2026-10-31"
    assert eposta_gonder._drafting(replies.load_settings()).auto_since == date(2026, 10, 31)
    assert eposta_gonder.main(["otomatik-ayar", "--kapat"]) == 0
    assert "otomatik_baslangic" not in replies.load_settings()
    assert eposta_gonder.main(["ozet"]) == 0
    assert "KOMUTLAR" in capsys.readouterr().out
