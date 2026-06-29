from datetime import timedelta
from statistics import mean


OK_STATUSES = {"OK_HIGH_CONFIDENCE", "OK_MEDIUM_CONFIDENCE"}


def failed_rows(rows: list[dict]) -> list[dict]:
    failed: list[dict] = []
    for row in rows:
        if row.get("status") in OK_STATUSES:
            continue
        failed.append(
            {
                "company": row.get("company", ""),
                "status": row.get("status", ""),
                "reason": row.get("reason", ""),
            }
        )
    return failed


def _pct(count: int, total: int) -> str:
    return f"{(count / total * 100):.1f}%" if total else "0.0%"


def build_report(rows: list[dict], elapsed_seconds: float) -> str:
    total = len(rows)
    website_count = sum(1 for row in rows if row.get("website"))
    email_count = sum(1 for row in rows if row.get("email"))
    phone_count = sum(1 for row in rows if row.get("phone"))
    complete_count = sum(1 for row in rows if row.get("website") and row.get("email") and row.get("phone"))
    high_confidence_count = sum(1 for row in rows if row.get("status") == "OK_HIGH_CONFIDENCE")
    medium_confidence_count = sum(1 for row in rows if row.get("status") == "OK_MEDIUM_CONFIDENCE")
    review_count = sum(1 for row in rows if row.get("status") == "REVIEW_NEEDED")
    scores = [int(row.get("score") or 0) for row in rows]
    average_score = mean(scores) if scores else 0
    elapsed = str(timedelta(seconds=int(elapsed_seconds)))

    return "\n".join(
        [
            "================================",
            "B2B Contact Finder - Sonuc Raporu",
            "================================",
            f"Toplam firma: {total}",
            f"Website bulundu: {website_count} ({_pct(website_count, total)})",
            f"E-posta bulundu: {email_count} ({_pct(email_count, total)})",
            f"Telefon bulundu: {phone_count} ({_pct(phone_count, total)})",
            f"Tam iletisim bilgisi bulunan firma (website+email+phone): {complete_count} ({_pct(complete_count, total)})",
            f"Yuksek guvenli OK: {high_confidence_count} ({_pct(high_confidence_count, total)})",
            f"Orta guvenli OK: {medium_confidence_count} ({_pct(medium_confidence_count, total)})",
            f"Manuel kontrol gereken: {review_count} ({_pct(review_count, total)})",
            "--------------------------------",
            f"Ortalama skor: {average_score:.1f}",
            f"Islem suresi: {elapsed}",
            "================================",
        ]
    )
