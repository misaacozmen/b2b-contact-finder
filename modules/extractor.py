import html
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

import config


EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(
    r"(?:(?:\+|00)\d{1,3}[\s().-]*)?(?:\(?0?\d{3}\)?[\s().-]*)?\d{3}[\s().-]*\d{2,4}[\s().-]*\d{2,4}"
)
AT_MARKER_RE = re.compile(r"(?i)(?<=\w)\s*(?:\[\s*(?:at|@)\s*\]|\(\s*(?:at|@)\s*\)|\{\s*(?:at|@)\s*\})\s*(?=\w)")
DOT_MARKER_RE = re.compile(r"(?i)(?<=\w)\s*(?:\[\s*(?:dot|nokta|\.)\s*\]|\(\s*(?:dot|nokta|\.)\s*\)|\{\s*(?:dot|nokta|\.)\s*\})\s*(?=\w)")


def _visible_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _normalize_obfuscated_email_text(html_text: str) -> str:
    text = html.unescape(html_text)
    text = AT_MARKER_RE.sub("@", text)
    text = DOT_MARKER_RE.sub(".", text)
    text = text.replace("[at]", "@").replace("(at)", "@").replace("{at}", "@")
    text = text.replace("[@]", "@").replace("(@)", "@").replace("{@}", "@")
    text = text.replace("[dot]", ".").replace("(dot)", ".").replace("{dot}", ".")
    text = text.replace("[nokta]", ".").replace("(nokta)", ".").replace("{nokta}", ".")
    return text


def extract_emails(html_text: str) -> list[str]:
    text = _normalize_obfuscated_email_text(html_text)
    emails = {email.lower().strip(".,;:()[]{}<>") for email in EMAIL_RE.findall(text)}
    filtered = {
        email
        for email in emails
        if not email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))
        and ".." not in email
        and not email.startswith(("example@", "test@"))
    }

    def sort_key(email: str) -> tuple[int, str]:
        prefix = email.split("@", 1)[0].split(".", 1)[0]
        try:
            priority = config.EMAIL_PRIORITY_PREFIXES.index(prefix)
        except ValueError:
            priority = len(config.EMAIL_PRIORITY_PREFIXES)
        return priority, email

    return sorted(filtered, key=sort_key)


def extract_phones(html_text: str) -> list[str]:
    text = _visible_text(html_text)
    phones = []
    seen = set()
    for match in PHONE_RE.findall(text):
        value = re.sub(r"\s+", " ", match).strip(" .,-;:()")
        digits = re.sub(r"\D", "", value)
        if len(digits) < 10 or len(digits) > 15:
            continue
        if digits in seen:
            continue
        seen.add(digits)
        phones.append(value)
    return phones


def extract_contact_page_link(html_text: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html_text, "html.parser")
    keywords = ("contact", "iletisim", "iletişim", "kontakt", "bize ulaş", "bize ulas")
    for link in soup.find_all("a", href=True):
        label = f"{link.get_text(' ', strip=True)} {link.get('href', '')}".lower()
        if any(keyword in label for keyword in keywords):
            href = link.get("href")
            if href and not href.startswith(("mailto:", "tel:", "javascript:")):
                return urljoin(base_url, href)
    return None
