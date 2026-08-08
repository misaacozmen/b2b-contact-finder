import html
import json
import re
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

import config
from modules import evidence_ledger, scorer


EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(
    r"(?:(?:\+|00)\d{1,3}[\s().-]*)?(?:\(?0?\d{3}\)?[\s().-]*)?\d{3}[\s().-]*\d{2,4}[\s().-]*\d{2,4}"
)
# Turkish sites sometimes split a subscriber number irregularly (for example
# "0 216 669 0 555"). Capture a country/trunk prefix followed by exactly ten
# digits regardless of separator grouping, then let phonenumbers validate it.
TR_PHONE_FLEX_RE = re.compile(r"(?<!\d)(?:\+?90|0)(?:[\s().-]*\d){10}(?!\d)")
TR_SERVICE_PHONE_RE = re.compile(r"(?<!\d)(?:0[\s().-]*)?444(?:[\s().-]*\d){4}(?!\d)")
AT_MARKER_RE = re.compile(r"(?i)(?<=\w)\s*(?:\[\s*(?:at|@)\s*\]|\(\s*(?:at|@)\s*\)|\{\s*(?:at|@)\s*\})\s*(?=\w)")
DOT_MARKER_RE = re.compile(r"(?i)(?<=\w)\s*(?:\[\s*(?:dot|nokta|\.)\s*\]|\(\s*(?:dot|nokta|\.)\s*\)|\{\s*(?:dot|nokta|\.)\s*\})\s*(?=\w)")


def _json_ld_documents(soup: BeautifulSoup) -> list:
    documents = []
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text() or ""
        try:
            documents.append(json.loads(raw.strip().strip(";")))
        except (json.JSONDecodeError, TypeError):
            continue
    return documents


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _decode_cfemail(value: str) -> str:
    try:
        raw = bytes.fromhex(value)
        if len(raw) < 2:
            return ""
        key = raw[0]
        return bytes(byte ^ key for byte in raw[1:]).decode("utf-8", errors="ignore")
    except (ValueError, TypeError):
        return ""


def _contact_label(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).casefold()
    labels = (
        (("fax", "faks"), "fax"),
        (("whatsapp", "whats app", "wa.me"), "whatsapp"),
        (("headquarters", "genel merkez", "merkez ofis", "merkez"), "headquarters"),
        (("specialist", "uzman"), "specialist"),
        (("owner", "sahibi", "yetkili"), "owner"),
        (("marketing", "pazarlama"), "marketing"),
        (("sales", "satis", "satış"), "sales"),
        (("export", "ihracat"), "export"),
        (("istanbul",), "istanbul"),
        (("izmir",), "izmir"),
        (("ankara",), "ankara"),
        (("factory", "fabrika"), "factory"),
        (("branch", "sube", "şube"), "branch"),
    )
    for markers, label in labels:
        if any(marker in normalized for marker in markers):
            return label
    return "general"


def _node_value(node) -> str:
    if node.get("content"):
        return str(node["content"]).strip()
    if node.get("href"):
        return str(node["href"]).strip()
    return node.get_text(" ", strip=True)


def _schema_property_names(node) -> list[str]:
    raw = " ".join((str(node.get("itemprop", "")), str(node.get("property", ""))))
    result = []
    for value in raw.split():
        result.append(re.split(r"[:/#]", value)[-1])
    return result


def extract_organization_evidence(
    html_text: str, source_url: str = "", retrieval_method: str = "http",
) -> dict:
    """Extract auditable first-party identity fields from structured and legal HTML."""
    soup = BeautifulSoup(html_text, "html.parser")
    result = {
        "names": [], "urls": [], "same_as": [], "addresses": [],
        "identifiers": [], "ownership_statements": [],
        "legal_names": [], "brand_names": [], "related_organizations": [],
        "phones": [], "relationships": [], "claims": [],
    }

    def plausible_organization_name(value: object) -> bool:
        normalized = scorer.normalize_text(str(value or ""))
        if not normalized or "@" in normalized or "http" in normalized:
            return False
        address_markers = {
            "mah", "mahallesi", "mh", "cad", "caddesi", "cd", "sok",
            "sokak", "sk", "bulvari", "blv", "no", "kat", "daire",
        }
        words = set(normalized.split())
        return not (words & address_markers and any(char.isdigit() for char in normalized))

    def add(field: str, value: object, method: str, target: str | None = None, relation: str = "") -> None:
        text = str(value or "").strip()
        if not text:
            return
        if (target or field) in {"names", "legal_names"} and not plausible_organization_name(text):
            return
        result[target or field].append(text)
        result["claims"].append(evidence_ledger.build_claim(
            field, text, source_url, method,
            html_text=html_text, relation=relation,
            retrieval_method=retrieval_method,
        ))

    organization_types = {"organization", "localbusiness", "corporation", "store", "professionalservice"}
    for selector in ('meta[property="og:site_name"]', 'meta[name="application-name"]'):
        for node in soup.select(selector):
            if node.get("content"):
                add("name", node["content"], "html_meta", "names")
    for node in soup.select('link[rel="canonical"][href]'):
        add("url", node["href"], "html_canonical", "urls")
    for document in _json_ld_documents(soup):
        for node in _walk_json(document):
            raw_types = node.get("@type", [])
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            if not any(str(item).casefold() in organization_types for item in types):
                continue
            for key in ("name", "legalName", "alternateName"):
                values = node.get(key, [])
                values = values if isinstance(values, list) else [values]
                for value in values:
                    if not value:
                        continue
                    add("legal_name" if key == "legalName" else "name", value, "json_ld", "names")
                    if key == "legalName":
                        add("legal_name", value, "json_ld", "legal_names")
            for key, target in (("url", "urls"), ("sameAs", "same_as")):
                values = node.get(key, [])
                values = values if isinstance(values, list) else [values]
                for value in values:
                    if value:
                        add("url" if key == "url" else "same_as", value, "json_ld", target)
            address = node.get("address")
            if address:
                rendered_address = address if isinstance(address, str) else json.dumps(address, ensure_ascii=False)
                add("address", rendered_address, "json_ld", "addresses")
            for key in ("taxID", "vatID", "leiCode", "iso6523Code", "globalLocationNumber"):
                if node.get(key):
                    add("identifier", f"{key}:{node[key]}", "json_ld", "identifiers")
            identifier = node.get("identifier")
            identifiers = identifier if isinstance(identifier, list) else [identifier]
            for item in identifiers:
                if isinstance(item, dict):
                    name = item.get("propertyID") or item.get("name") or "identifier"
                    value = item.get("value") or item.get("@value")
                    if value:
                        add("identifier", f"{name}:{value}", "json_ld", "identifiers")
                elif item:
                    add("identifier", f"identifier:{item}", "json_ld", "identifiers")
            for key in ("telephone", "phone"):
                values = node.get(key, [])
                values = values if isinstance(values, list) else [values]
                for value in values:
                    if value:
                        add("telephone", value, "json_ld", "phones")
            contact_points = node.get("contactPoint", [])
            contact_points = contact_points if isinstance(contact_points, list) else [contact_points]
            for contact_point in contact_points:
                if isinstance(contact_point, dict) and contact_point.get("telephone"):
                    add("telephone", contact_point["telephone"], "json_ld", "phones")
            for key, target in (
                ("brand", "brand_names"),
                ("parentOrganization", "related_organizations"),
                ("subOrganization", "related_organizations"),
                ("branchOf", "related_organizations"),
                ("department", "related_organizations"),
                ("memberOf", "related_organizations"),
            ):
                values = node.get(key, [])
                values = values if isinstance(values, list) else [values]
                for value in values:
                    if isinstance(value, dict):
                        related_name = str(value.get("legalName") or value.get("name") or "").strip()
                        related_url = str(value.get("url") or "").strip()
                    else:
                        related_name, related_url = str(value).strip(), ""
                    if related_name:
                        add("organization_name", related_name, "json_ld", target, key)
                        result["ownership_statements"].append(f"{key}: {related_name}")
                    if related_url:
                        add("relationship_url", related_url, "json_ld", "same_as", key)
                    if related_name or related_url:
                        result["relationships"].append({
                            "kind": key, "name": related_name, "url": related_url,
                        })

    # JSON-LD is common, but many older company sites expose the same fields
    # through Microdata or RDFa. Restrict generic name properties to explicit
    # organization scopes so product markup cannot become company identity.
    structured_roots = soup.select(
        '[itemscope][itemtype*="schema.org/Organization"],'
        '[itemscope][itemtype*="schema.org/LocalBusiness"],'
        '[itemscope][itemtype*="schema.org/Corporation"],'
        '[typeof*="Organization"], [typeof*="LocalBusiness"], [typeof*="Corporation"]'
    )
    scalar_properties = {
        "name": ("name", "names"),
        "legalName": ("legal_name", "legal_names"),
        "alternateName": ("name", "names"),
        "url": ("url", "urls"),
        "sameAs": ("same_as", "same_as"),
        "address": ("address", "addresses"),
        "telephone": ("telephone", "phones"),
        "taxID": ("identifier", "identifiers"),
        "vatID": ("identifier", "identifiers"),
        "leiCode": ("identifier", "identifiers"),
        "iso6523Code": ("identifier", "identifiers"),
        "globalLocationNumber": ("identifier", "identifiers"),
        "identifier": ("identifier", "identifiers"),
    }
    relation_properties = {
        "brand", "parentOrganization", "subOrganization", "branchOf", "department", "memberOf",
    }
    for root in structured_roots:
        method = "microdata" if root.has_attr("itemscope") else "rdfa"
        for node in [root, *root.select("[itemprop], [property]")]:
            value = _node_value(node)
            for prop in _schema_property_names(node):
                if prop in scalar_properties and value:
                    field, target = scalar_properties[prop]
                    rendered = f"{prop}:{value}" if field == "identifier" else value
                    add(field, rendered, method, target)
                elif prop in relation_properties and value:
                    related_url = str(node.get("href") or node.get("resource") or "").strip()
                    related_name = "" if related_url and value == related_url else value
                    result["relationships"].append({
                        "kind": prop, "name": related_name, "url": related_url,
                    })
                    if related_name:
                        add("organization_name", related_name, method, "related_organizations", prop)
                    if related_url:
                        add("relationship_url", related_url, method, "same_as", prop)

    # Labeled identifiers in a company's own legal/privacy pages are often
    # more useful than markup. Labels are mandatory to avoid treating random
    # page numbers as legal identifiers.
    visible_lines = soup.get_text("\n", strip=True)
    visible_compact = re.sub(r"\s+", " ", visible_lines)
    identifier_specs = (
        ("mersis", re.compile(r"(?i)\bMERS[İI]S(?:\s*(?:NO|NUMARASI|NUMBER))?\s*[:#-]?\s*([0-9][0-9 .-]{14,24}[0-9])"), 16),
        ("vkn", re.compile(r"(?i)\b(?:VKN|VERG[İI]\s*(?:K[İI]ML[İI]K\s*)?(?:NO|NUMARASI))\s*[:#-]?\s*([0-9][0-9 .-]{8,16}[0-9])"), 10),
    )
    for kind, pattern, required_digits in identifier_specs:
        for match in pattern.finditer(visible_compact):
            digits = re.sub(r"\D", "", match.group(1))
            if len(digits) == required_digits:
                add("identifier", f"{kind}:{digits}", "visible_labeled", "identifiers")

    legal_name_pattern = re.compile(
        r"(?i)\b(?:(?:T[İI]CAR[İI]|RESM[İI]|[ŞS][İI]RKET|F[İI]RMA)\s+(?:Ü|U)NVAN(?:I|İ)?|"
        r"VER[İI]\s+SORUMLUSUNUN\s+(?:Ü|U)NVAN(?:I|İ)?(?:/İSMİ)?)\s*[:#-]?\s*"
        r"([A-ZÇĞİÖŞÜ0-9&.,'()/-][A-ZÇĞİÖŞÜ0-9&.,'() /-]{2,180}?"
        r"(?:ANON[İI]M\s+[ŞS][İI]RKET[İI]?|L[İI]M[İI]TED\s+[ŞS][İI]RKET[İI]?|"
        r"LTD\.?\s*[ŞS]T[İI]\.?|A\.?\s*[ŞS]\.?))(?=\s|$|[,;])"
    )
    for match in legal_name_pattern.finditer(visible_compact):
        legal_name = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
        if 5 <= len(legal_name) <= 200:
            add("legal_name", legal_name, "visible_labeled", "names")
            add("legal_name", legal_name, "visible_labeled", "legal_names")

    trade_pattern = re.compile(
        r"(?i)\bT[İI]CARET\s*S[İI]C[İI]L(?:\s*(?:NO|NUMARASI))?\s*[:#-]?\s*([A-Z0-9][A-Z0-9/-]{2,23})"
    )
    for match in trade_pattern.finditer(visible_compact):
        registry_value = match.group(1)
        if any(char.isdigit() for char in registry_value):
            add("identifier", f"trade_registry:{registry_value}", "visible_labeled", "identifiers")
    kep_pattern = re.compile(
        r"\b(?:KEP|KAYITLI\s+ELEKTRON[İI]K\s+POSTA)\s*(?:ADRES[İI])?\s*[:#-]?\s*"
        r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
        re.IGNORECASE,
    )
    for match in kep_pattern.finditer(visible_compact):
        kep = match.group(1).casefold()
        result["identifiers"].append(f"kep:{kep}")
        result["claims"].append(evidence_ledger.build_claim(
            "kep", kep, source_url, "visible_labeled", html_text=html_text,
        ))
    for node in soup.find_all("address"):
        address = node.get_text(" ", strip=True)
        if 10 <= len(address) <= 500:
            add("address", address, "html_address", "addresses")

    visible = soup.get_text(" ", strip=True)
    normalized_visible = re.sub(r"\s+", " ", visible)
    ownership_markers = re.compile(
        r"(?i)\b(?:markas[ıi](?:dır|dir)?|ait\s+bir\s+marka|bünyesinde|bunyesinde|"
        r"ticari\s+(?:ü|u)nvan[ıi]|resmi\s+(?:ü|u)nvan[ıi]|bir\s+markad[ıi]r|"
        r"a\s+brand\s+of|brand\s+of|"
        r"owned\s+by|belongs\s+to|operated\s+by|trading\s+name\s+of|part\s+of)\b"
    )
    normalized_for_markers = scorer.normalize_text(normalized_visible)
    normalized_markers = re.compile(
        r"\b(?:markasi(?:dir)?|ait\s+bir\s+marka|bunyesinde|"
        r"ticari\s+unvani|resmi\s+unvani|bir\s+markadir|"
        r"a\s+brand\s+of|brand\s+of|owned\s+by|belongs\s+to|"
        r"operated\s+by|trading\s+name\s+of|part\s+of)\b"
    )
    marker_matches = list(ownership_markers.finditer(normalized_visible))
    marker_matches.extend(normalized_markers.finditer(normalized_for_markers))
    for match in marker_matches:
        source = normalized_for_markers if match.re is normalized_markers else normalized_visible
        start = max(0, match.start() - 220)
        end = min(len(source), match.end() + 220)
        result["ownership_statements"].append(source[start:end].strip())
    deduplicated = {}
    for key, values in result.items():
        if key == "relationships":
            seen_relationships = set()
            deduplicated[key] = []
            for value in values:
                marker = (value.get("kind", ""), value.get("name", ""), value.get("url", ""))
                if marker not in seen_relationships:
                    seen_relationships.add(marker)
                    deduplicated[key].append(value)
        elif key == "claims":
            deduplicated[key] = evidence_ledger.deduplicate(values)
        else:
            deduplicated[key] = list(dict.fromkeys(values))
    return deduplicated


def _visible_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _normalize_obfuscated_email_text(html_text: str) -> str:
    text = html.unescape(html_text)
    # Some cached/minified HTML contains a literal JSON non-breaking-space
    # escape immediately before an address.  Without removing the escape,
    # the email regex treats ``u00a0`` as part of the local name.
    text = re.sub(
        r"(?i)(?:\\u00a0|\\xa0|\\[nrt]|u00a0)(?=[a-z0-9._%+-]+@)",
        " ", text,
    )
    # JSON/HTML delimiter escapes can otherwise become part of the local name
    # (for example ``u003einfo@example.com``).  Decode only when the token is
    # immediately followed by an address, preserving legitimate local names.
    text = re.sub(
        r"(?i)(?:\\u003[ce]|\\x3[ce]|u003[ce])(?=[a-z0-9._%+-]+@)",
        " ", text,
    )
    text = AT_MARKER_RE.sub("@", text)
    text = DOT_MARKER_RE.sub(".", text)
    text = text.replace("[at]", "@").replace("(at)", "@").replace("{at}", "@")
    text = text.replace("[@]", "@").replace("(@)", "@").replace("{@}", "@")
    text = text.replace("[dot]", ".").replace("(dot)", ".").replace("{dot}", ".")
    text = text.replace("[nokta]", ".").replace("(nokta)", ".").replace("{nokta}", ".")
    return text


def _is_placeholder_email(value: str) -> bool:
    local, domain = value.rsplit("@", 1)
    registrable_label = domain.split(".", 1)[0]
    placeholder_labels = {
        "mysite", "mywebsite", "yourdomain", "yourwebsite", "yourcompany",
    }
    return local in {"info", "contact", "email", "name"} and registrable_label in placeholder_labels


def extract_emails(html_text: str) -> list[str]:
    # Search visible page text plus explicit contact fields.  Scanning raw
    # scripts publishes telemetry/Sentry and site-template placeholder emails.
    text = _normalize_obfuscated_email_text(_visible_text(html_text))
    soup = BeautifulSoup(html_text, "html.parser")
    mailto_values = [
        unquote(link.get("href", "")[7:]).split("?", 1)[0]
        for link in soup.select('a[href^="mailto:"]')
    ]
    cfemail_values = [
        _decode_cfemail(node.get("data-cfemail", ""))
        for node in soup.select("[data-cfemail]")
    ]
    structured_values = []
    for document in _json_ld_documents(soup):
        for node in _walk_json(document):
            if node.get("email"):
                values = node["email"] if isinstance(node["email"], list) else [node["email"]]
                structured_values.extend(str(value).removeprefix("mailto:") for value in values)
    email_source = _normalize_obfuscated_email_text(
        " ".join([text, *mailto_values, *cfemail_values, *structured_values])
    )
    emails = {
        email.lower().strip(".,;:()[]{}<>")
        for email in EMAIL_RE.findall(email_source)
    }
    typo_suffixes = {
        ".comm": ".com", ".comt": ".com", ".con": ".com", ".cmo": ".com",
    }
    typo_variants = set()
    for email in emails:
        local, domain = email.rsplit("@", 1)
        leading_digits = re.match(r"^\d{5,}(.+)$", local)
        if leading_digits and f"{leading_digits.group(1)}@{domain}" in emails:
            typo_variants.add(email)
        for bad_suffix, good_suffix in typo_suffixes.items():
            if domain.endswith(bad_suffix):
                corrected = f"{local}@{domain[:-len(bad_suffix)]}{good_suffix}"
                if corrected in emails:
                    typo_variants.add(email)
                break
    filtered = {
        email
        for email in emails
        if not email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"))
        and ".." not in email
        and not email.startswith(("example@", "test@"))
        and not _is_placeholder_email(email)
        and email not in typo_variants
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
    soup = BeautifulSoup(html_text, "html.parser")
    tel_values = [
        unquote(link.get("href", "")[4:]).split("?", 1)[0]
        for link in soup.select('a[href^="tel:"]')
    ]
    whatsapp_values = []
    for link in soup.find_all("a", href=True):
        parsed = urlparse(unquote(link.get("href", "")))
        host = parsed.netloc.casefold().removeprefix("www.")
        if host not in {"wa.me", "api.whatsapp.com", "web.whatsapp.com"}:
            continue
        whatsapp_values.append(parsed.path.strip("/"))
        whatsapp_values.extend(
            part.split("=", 1)[1]
            for part in parsed.query.split("&")
            if part.casefold().startswith(("phone=", "send=")) and "=" in part
        )
    structured_values = [
        node.get("content") or node.get_text(" ", strip=True)
        for node in soup.select('[itemprop="telephone"], meta[property="telephone"]')
    ]

    def collect_phones(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"telephone", "faxnumber"}:
                    values = item if isinstance(item, list) else [item]
                    structured_values.extend(str(entry) for entry in values if entry)
                else:
                    collect_phones(item)
        elif isinstance(value, list):
            for item in value:
                collect_phones(item)

    for document in _json_ld_documents(soup):
        collect_phones(document)

    text = " ".join([_visible_text(html_text), *tel_values, *whatsapp_values, *structured_values])
    phones = []
    seen = set()
    matches = [
        *PHONE_RE.findall(text), *TR_PHONE_FLEX_RE.findall(text),
        *TR_SERVICE_PHONE_RE.findall(text),
    ]
    for match in matches:
        value = re.sub(r"\s+", " ", match).strip(" .,-;:()")
        digits = re.sub(r"\D", "", value)
        service_number = bool(re.fullmatch(r"0?444\d{4}", digits))
        if (len(digits) < 10 and not service_number) or len(digits) > 15:
            continue
        if digits in seen:
            continue
        seen.add(digits)
        phones.append(value)
    return phones


def extract_contact_records(
    html_text: str, source_url: str, retrieval_method: str = "http",
) -> dict:
    """Return all contacts with their page and best available role label."""
    soup = BeautifulSoup(html_text, "html.parser")
    email_labels: dict[str, str] = {}
    phone_labels: list[tuple[str, str]] = []
    for link in soup.find_all("a", href=True):
        href = unquote(link.get("href", ""))
        context = " ".join((link.get_text(" ", strip=True), link.parent.get_text(" ", strip=True)[:250] if link.parent else ""))
        if href.casefold().startswith("mailto:"):
            email = href[7:].split("?", 1)[0].casefold()
            email_labels[email] = _contact_label(context)
        elif href.casefold().startswith("tel:"):
            phone_labels.append((href[4:].split("?", 1)[0], _contact_label(context)))
        else:
            parsed = urlparse(href)
            host = parsed.netloc.casefold().removeprefix("www.")
            if host in {"wa.me", "api.whatsapp.com", "web.whatsapp.com"}:
                digits = re.sub(r"\D", "", f"{parsed.path} {parsed.query}")
                if digits:
                    phone_labels.append((digits, "whatsapp"))
    visible_text = _visible_text(html_text)
    for match in PHONE_RE.finditer(visible_text):
        start, _ = match.span()
        context = visible_text[max(0, start - 80):start]
        phone_labels.append((match.group(0), _contact_label(context)))
    for match in [
        *TR_PHONE_FLEX_RE.finditer(visible_text),
        *TR_SERVICE_PHONE_RE.finditer(visible_text),
    ]:
        start, _ = match.span()
        context = visible_text[max(0, start - 80):start]
        phone_labels.append((match.group(0), _contact_label(context)))

    structured_email_labels: dict[str, str] = {}
    structured_phone_labels: list[tuple[str, str]] = []
    for document in _json_ld_documents(soup):
        for node in _walk_json(document):
            contact_type = str(node.get("contactType", ""))
            label = _contact_label(contact_type)
            if node.get("email"):
                values = node["email"] if isinstance(node["email"], list) else [node["email"]]
                for value in values:
                    structured_email_labels[str(value).removeprefix("mailto:").casefold()] = label
            if node.get("telephone"):
                values = node["telephone"] if isinstance(node["telephone"], list) else [node["telephone"]]
                structured_phone_labels.extend((str(value), label) for value in values)
            if node.get("faxNumber"):
                values = node["faxNumber"] if isinstance(node["faxNumber"], list) else [node["faxNumber"]]
                structured_phone_labels.extend((str(value), "fax") for value in values)

    emails = [
        {"value": value, "label": email_labels.get(value, structured_email_labels.get(value, "general")), "source_url": source_url, "retrieval_method": retrieval_method}
        for value in extract_emails(html_text)
    ]
    phones = []
    for value in extract_phones(html_text):
        digits = re.sub(r"\D", "", value)
        label = "general"
        for labelled_value, candidate_label in [*phone_labels, *structured_phone_labels]:
            labelled_digits = re.sub(r"\D", "", labelled_value)
            if digits and labelled_digits and (digits.endswith(labelled_digits) or labelled_digits.endswith(digits)):
                label = candidate_label
                break
        phones.append({"value": value, "label": label, "source_url": source_url, "retrieval_method": retrieval_method})
    return {
        "emails": emails,
        "phones": phones,
        "identity": extract_organization_evidence(html_text, source_url, retrieval_method),
    }


def extract_contact_page_links(
    html_text: str, base_url: str, limit: int, allow_official_subdomains: bool = False
) -> list[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    base_domain = urlparse(base_url).netloc.lower()
    contact_keywords = ("contact", "iletisim", "iletişim", "kontakt", "bize ulaş", "bize ulas")
    company_keywords = ("hakkımızda", "hakkimizda", "kurumsal", "about", "company", "corporate")
    candidates: list[tuple[int, str]] = []
    seen = set()
    for link in soup.find_all("a", href=True):
        label = f"{link.get_text(' ', strip=True)} {link.get('href', '')}".lower()
        score = 0
        if any(keyword in label for keyword in contact_keywords):
            score += 100
        if "whatsapp" in label or "whats-app" in label:
            score += 100
        if any(keyword in label for keyword in company_keywords):
            score += 40
        if not score:
            continue
        href = link.get("href")
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href)
        target_domain = urlparse(url).netloc.lower()
        same_site = target_domain == base_domain or (
            allow_official_subdomains and scorer.same_registrable_domain(target_domain, base_domain)
        )
        if not same_site or url in seen:
            continue
        seen.add(url)
        candidates.append((score, url))
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return [url for _, url in candidates[:limit]]


def extract_contact_page_link(html_text: str, base_url: str) -> str | None:
    links = extract_contact_page_links(html_text, base_url, limit=1)
    return links[0] if links else None
