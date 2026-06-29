import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import config
from modules import scorer
from modules import checkpoint, crawler, excel, extractor, phone, report, search
from modules.utils import ensure_directories, random_delay, setup_logging


def _empty_result(company: str, status: str, reason: str = "", score: int = 0) -> dict:
    return {
        "company": company,
        "website": "",
        "email": "",
        "phone": "",
        "status": status,
        "confidence": "none",
        "score": score,
        "reason": reason,
    }


def _attach_candidates(row: dict, candidates: list[dict]) -> dict:
    row["selected_website"] = row.get("website", "")
    for idx, candidate in enumerate(candidates[:3], start=1):
        row[f"candidate_{idx}_url"] = candidate.get("url", "")
        row[f"candidate_{idx}_score"] = candidate.get("score", "")
        row[f"candidate_{idx}_reason"] = candidate.get("reason", "")
    return row


def _email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return scorer.normalize_domain(email.split("@", 1)[1])


def _email_is_usable(email: str) -> bool:
    domain = _email_domain(email)
    if not domain:
        return False
    return not any(domain == bad or domain.endswith(f".{bad}") for bad in config.BAD_EMAIL_DOMAINS)


def _select_best_email(company: str, website: str, emails: list[str]) -> str:
    website_domain = scorer.normalize_domain(urlparse(website).netloc or website)
    website_root = scorer.compact_domain_core(website_domain)
    tokens = scorer.distinctive_tokens(company)
    candidates = [email for email in dict.fromkeys(emails) if _email_is_usable(email)]
    if not candidates:
        return ""

    def rank(email: str) -> tuple[int, int, str]:
        local, domain = email.split("@", 1)
        email_domain = scorer.normalize_domain(domain)
        email_root = scorer.compact_domain_core(email_domain)
        email_text = scorer.normalize_text(f"{local} {email_root}")
        score = 0

        if email_root and website_root and (email_root == website_root or email_root in website_root or website_root in email_root):
            score += 80
        if any(token in email_text for token in tokens):
            score += 35
        if email_domain.endswith((".com.tr", ".tr")):
            score += 5

        prefix = local.split(".", 1)[0].split("-", 1)[0].lower()
        try:
            priority = config.EMAIL_PRIORITY_PREFIXES.index(prefix)
        except ValueError:
            priority = len(config.EMAIL_PRIORITY_PREFIXES)
        return score, -priority, email

    ranked = sorted(candidates, key=rank, reverse=True)
    best = ranked[0]
    best_score = rank(best)[0]
    return best if best_score >= 15 else ""


def _page_identity_score(company: str, pages: list[dict]) -> tuple[int, str]:
    tokens = scorer.distinctive_tokens(company)
    if not tokens:
        return 0, "no_distinctive_tokens"
    _HTML_TRUNCATE = 50000
    raw_parts = []
    for page in pages:
        html_content = page.get("html", "")
        if len(html_content) > _HTML_TRUNCATE:
            import logging as _logging
            _logging.getLogger("contact_finder").debug(
                "HTML truncated for page %s: %d → %d chars",
                page.get("url", "?"), len(html_content), _HTML_TRUNCATE,
            )
        raw_parts.append(html_content[:_HTML_TRUNCATE])
    text = scorer.normalize_text(" ".join(raw_parts))
    hits = sum(1 for token in tokens if token in text)
    ratio = hits / len(tokens)
    if ratio >= 0.75:
        return 14, f"page_identity_strong:{hits}/{len(tokens)}"
    if ratio >= 0.5:
        return 8, f"page_identity_medium:{hits}/{len(tokens)}"
    if hits:
        return 3, f"page_identity_weak:{hits}/{len(tokens)}"
    return -20, f"page_identity_missing:0/{len(tokens)}"


def _page_context_score(company: str, pages: list[dict]) -> tuple[int, str]:
    raw_tokens = scorer._raw_company_tokens(company)
    context_tokens = [token for token in raw_tokens if token in config.CONTEXT_VALIDATION_WORDS]
    if not context_tokens:
        return 0, "no_context_tokens"

    text = scorer.normalize_text(" ".join(page.get("html", "")[:50000] for page in pages))
    hits = sum(1 for token in context_tokens if token in text)
    if hits:
        return 8, f"context_match:{hits}/{len(context_tokens)}"
    return -30, f"context_missing:0/{len(context_tokens)}"


def _email_domain_bonus(website: str, email: str) -> tuple[int, str]:
    if not email:
        return 0, "no_email"
    website_root = scorer.compact_domain_core(website)
    email_root = scorer.compact_domain_core(_email_domain(email))
    if email_root and website_root and (email_root == website_root or email_root in website_root or website_root in email_root):
        return 10, "email_domain_match"
    return -12, "email_domain_mismatch"


def _score_candidate_with_site(company: str, candidate: dict, crawl_result: dict, selected_email: str, normalized_phones: list[str]) -> tuple[int, list[str]]:
    reasons = [candidate.get("reason", "")]
    page_bonus, page_reason = _page_identity_score(company, crawl_result["pages"])
    context_bonus, context_reason = _page_context_score(company, crawl_result["pages"])
    email_bonus, email_reason = _email_domain_bonus(crawl_result["url"], selected_email)
    reasons.extend([page_reason, context_reason, email_reason])
    final_score = max(0, min(100, int(candidate["score"]) + page_bonus + context_bonus + email_bonus))
    has_contact = bool(selected_email or normalized_phones)
    if not has_contact:
        reasons.append("no_tr_contact_or_usable_email")
    if context_bonus < 0:
        reasons.append("context_gate_failed")
    if email_bonus < 0:
        reasons.append("email_gate_failed")
    return final_score, reasons


def _evaluate_candidate(company: str, candidate: dict) -> dict:
    crawl_result = crawler.fetch_site(candidate["url"])
    if not crawl_result["pages"]:
        return {
            "candidate": candidate,
            "crawl_result": crawl_result,
            "email": "",
            "phone": "",
            "final_score": 0,
            "reasons": [crawl_result.get("error", "website_fetch_failed")],
            "has_contact": False,
            "context_failed": False,
            "email_failed": False,
        }

    emails: list[str] = []
    phones: list[str] = []
    for page in crawl_result["pages"]:
        emails.extend(extractor.extract_emails(page["html"]))
        phones.extend(extractor.extract_phones(page["html"]))

    selected_email = _select_best_email(company, crawl_result["url"], emails)
    normalized_phones = [phone.normalize_phone(raw) for raw in phones]
    normalized_phones = [value for value in dict.fromkeys(normalized_phones) if value]
    final_score, reasons = _score_candidate_with_site(company, candidate, crawl_result, selected_email, normalized_phones)
    context_failed = "context_gate_failed" in reasons
    email_failed = "email_gate_failed" in reasons

    return {
        "candidate": candidate,
        "crawl_result": crawl_result,
        "email": selected_email,
        "phone": normalized_phones[0] if normalized_phones else "",
        "final_score": final_score,
        "reasons": reasons,
        "has_contact": bool(selected_email or normalized_phones),
        "context_failed": context_failed,
        "email_failed": email_failed,
    }


def _confidence_status(score: int, has_contact: bool, reasons: list[str]) -> tuple[str, str]:
    if score >= config.HIGH_CONFIDENCE_SCORE and has_contact:
        return "OK_HIGH_CONFIDENCE", "high"
    if score >= config.MEDIUM_CONFIDENCE_SCORE and has_contact:
        return "OK_MEDIUM_CONFIDENCE", "medium"
    if score >= config.REVIEW_SCORE:
        reasons.append("needs_manual_review")
        return "REVIEW_NEEDED", "review"
    return "WEBSITE_NOT_FOUND", "none"


def process_company(index: int, company: str, logger) -> tuple[int, dict]:
    logger.info("Processing %s: %s", index + 1, company)
    try:
        candidates = search.find_candidate_domains(company)
    except Exception as exc:
        logger.exception("Search failed for %s", company)
        random_delay()
        return index, _attach_candidates(_empty_result(company, "SEARCH_FAILED", str(exc)), [])

    best = candidates[0] if candidates else None
    if not best or best["score"] < config.MIN_ACCEPT_SCORE:
        random_delay()
        row = _empty_result(company, "WEBSITE_NOT_FOUND", "No candidate passed score threshold")
        return index, _attach_candidates(row, candidates)

    evaluations = [_evaluate_candidate(company, candidate) for candidate in candidates[: config.MAX_CANDIDATE_EVALUATIONS]]
    successful = [item for item in evaluations if item["crawl_result"]["pages"]]
    if not successful:
        random_delay()
        row = _empty_result(company, "WEBSITE_FETCH_FAILED", evaluations[0]["reasons"][0], best["score"])
        return index, _attach_candidates(row, candidates)

    best_eval = max(
        successful,
        key=lambda item: (
            0 if item["context_failed"] else 1,
            0 if item["email_failed"] else 1,
            1 if item["has_contact"] else 0,
            item["final_score"],
        ),
    )
    crawl_result = best_eval["crawl_result"]
    selected_email = best_eval["email"]
    selected_phone = best_eval["phone"]
    final_score = best_eval["final_score"]
    reasons = best_eval["reasons"]
    has_contact = best_eval["has_contact"]

    if best_eval["context_failed"]:
        if has_contact:
            status, confidence = "REVIEW_NEEDED", "review"
        else:
            status, confidence = "WEBSITE_NOT_FOUND", "none"
    elif best_eval["email_failed"]:
        status, confidence = "REVIEW_NEEDED", "review"
    else:
        status, confidence = _confidence_status(final_score, has_contact, reasons)
    random_delay()
    row = {
        "company": company,
        "website": crawl_result["url"],
        "email": selected_email,
        "phone": selected_phone,
        "status": status,
        "confidence": confidence,
        "score": final_score,
        "reason": "; ".join(reason for reason in reasons if reason),
    }
    if status == "WEBSITE_NOT_FOUND":
        row["website"] = ""
        row["email"] = ""
        row["phone"] = ""
    return index, _attach_candidates(row, candidates)


def _write_outputs(rows: list[dict], elapsed_seconds: float) -> str:
    for row in rows:
        row.pop("__index", None)
    excel.write_contacts(config.CONTACTS_FILE, rows)
    excel.write_failed(config.FAILED_FILE, report.failed_rows(rows))
    excel.write_website_candidates(config.CANDIDATES_FILE, rows)
    report_text = report.build_report(rows, elapsed_seconds)
    config.REPORT_FILE.write_text(report_text, encoding="utf-8")
    return report_text


def run(input_file: Path) -> str:
    ensure_directories()
    logger = setup_logging()
    start_time = time.monotonic()
    companies = excel.read_companies(input_file)
    if not companies:
        raise RuntimeError(f"No companies found in {input_file}")

    progress = checkpoint.load_progress(input_file)
    results_by_index: dict[int, dict] = {}
    start_index = 0
    if progress:
        results_so_far = progress.get("results_so_far", [])
        for offset, row in enumerate(results_so_far):
            idx = int(row.get("__index", offset))
            results_by_index[idx] = row
        start_index = int(progress.get("last_completed_index", -1)) + 1
        logger.info("Resuming from index %s", start_index)

    pending = [(idx, company) for idx, company in enumerate(companies) if idx not in results_by_index and idx >= start_index]
    try:
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as executor:
            futures = {
                executor.submit(process_company, idx, company, logger): idx
                for idx, company in pending
            }
            for future in as_completed(futures):
                idx, row = future.result()
                row["__index"] = idx
                results_by_index[idx] = row
                completed_indexes = sorted(results_by_index)
                contiguous_last = -1
                for completed_idx in completed_indexes:
                    if completed_idx == contiguous_last + 1:
                        contiguous_last = completed_idx
                    else:
                        break
                ordered_rows = [results_by_index[i] for i in sorted(results_by_index)]
                checkpoint.save_progress(input_file, contiguous_last, ordered_rows)
                logger.info("Completed %s/%s: %s", len(results_by_index), len(companies), row["company"])
    except KeyboardInterrupt:
        logger.warning("Interrupted. Progress checkpoint was saved.")
        raise

    rows = [results_by_index[i] for i in range(len(companies))]
    report_text = _write_outputs(rows, time.monotonic() - start_time)
    checkpoint.clear_progress()
    return report_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="B2B Contact Finder")
    parser.add_argument("--input", type=Path, default=config.INPUT_FILE, help="Path to firms.xlsx")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(run(args.input))
