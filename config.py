import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
STATE_DIR = BASE_DIR / "state"
DATA_DIR = BASE_DIR / "data"
COMPANY_ALIASES_FILE = DATA_DIR / "company_aliases.json"
ENTITY_REGISTRY_FILE = DATA_DIR / "entity_registry.json"
OFFICIAL_REGISTRY_FILE = DATA_DIR / "official_registry.json"
VERIFIED_ENTITY_MEMORY_FILE = DATA_DIR / "verified_entity_memory.jsonl"

INPUT_FILE = INPUT_DIR / "firms.xlsx"
CONTACTS_FILE = OUTPUT_DIR / "contacts.xlsx"
VERIFIED_CONTACTS_FILE = OUTPUT_DIR / "verified_contacts.xlsx"
REVIEW_QUEUE_FILE = OUTPUT_DIR / "review_queue.xlsx"
FAILED_FILE = OUTPUT_DIR / "failed.xlsx"
CANDIDATES_FILE = OUTPUT_DIR / "website_candidates.xlsx"
REPORT_FILE = OUTPUT_DIR / "report.txt"
LOG_FILE = OUTPUT_DIR / "logs.txt"
PROGRESS_FILE = STATE_DIR / "progress.json"
PROGRESS_DB_FILE = STATE_DIR / "progress.sqlite3"
SAVED_API_KEYS_FILE = STATE_DIR / "api_keys.json"
RESOLVER_SETTINGS_FILE = STATE_DIR / "company_resolvers.json"
SEARCH_CACHE_DIR = STATE_DIR / "search_cache"
CRAWL_CACHE_DIR = STATE_DIR / "crawl_cache"
EMAIL_CACHE_DIR = STATE_DIR / "email_cache"
EVIDENCE_FILE = OUTPUT_DIR / "evidence.jsonl"
ENTITY_RELATIONSHIPS_FILE = OUTPUT_DIR / "entity_relationships.jsonl"
TELEMETRY_FILE = OUTPUT_DIR / "telemetry.json"
DISCOVERY_COVERAGE_FILE = OUTPUT_DIR / "discovery_coverage.json"
QUALITY_AUDIT_FILE = OUTPUT_DIR / "quality_audit.json"
REPLAY_SNAPSHOT_FILE = OUTPUT_DIR / "replay_snapshot.json.gz"
REPLAY_SNAPSHOT_INPUT = (
    Path(os.getenv("REPLAY_SNAPSHOT_INPUT", "")).expanduser()
    if os.getenv("REPLAY_SNAPSHOT_INPUT", "").strip()
    else None
)
REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES = int(
    os.getenv("REPLAY_SNAPSHOT_MAX_UNCOMPRESSED_BYTES", str(256 * 1024 * 1024))
)
REPLAY_SNAPSHOT_CHECKPOINT_INTERVAL = max(
    1, int(os.getenv("REPLAY_SNAPSHOT_CHECKPOINT_INTERVAL", "50"))
)

# CLI overrides these values for normal runs.  "off" as the import-time
# default keeps library/unit-test calls isolated from persistent state.
SEARCH_CACHE_MODE = os.getenv("SEARCH_CACHE_MODE", "off").lower()
CRAWL_CACHE_MODE = os.getenv("CRAWL_CACHE_MODE", "off").lower()
SEARCH_CACHE_TTL_DAYS = int(os.getenv("SEARCH_CACHE_TTL_DAYS", "30"))
CRAWL_CACHE_TTL_DAYS = int(os.getenv("CRAWL_CACHE_TTL_DAYS", "7"))
CACHE_SCHEMA_VERSION = 1
# Crawl discovery changed independently from SERP/MX caches.  Keeping a
# separate version refreshes official sites without invalidating paid search
# results that are still reusable.
CRAWL_CACHE_SCHEMA_VERSION = 7

MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "3")))
MIN_DELAY_SEC = 1.0
MAX_DELAY_SEC = 3.0
GLOBAL_REQUESTS_PER_SECOND = float(os.getenv("GLOBAL_REQUESTS_PER_SECOND", "3"))
BRIGHTDATA_REQUESTS_PER_MINUTE = float(
    os.getenv("BRIGHTDATA_REQUESTS_PER_MINUTE", "13")
)
CRAWLER_HTTP_REQUEST_BUDGET = int(os.getenv("CRAWLER_HTTP_REQUEST_BUDGET", "0"))
SEARCH_HTTP_REQUEST_BUDGET = int(os.getenv("SEARCH_HTTP_REQUEST_BUDGET", "0"))
DEFAULT_FREE_SEARCH_QUERY_LIMIT_PER_COMPANY = max(
    1, int(os.getenv("DEFAULT_FREE_SEARCH_QUERY_LIMIT_PER_COMPANY", "10"))
)
BRIGHTDATA_REQUEST_HARD_CAP = int(os.getenv("BRIGHTDATA_REQUEST_BUDGET", "500"))
GOOGLE_PLACES_REQUEST_HARD_CAP = int(os.getenv("GOOGLE_PLACES_REQUEST_BUDGET", "100"))
HUNTER_REQUEST_HARD_CAP = int(os.getenv("HUNTER_REQUEST_BUDGET", "25"))
BRANDFETCH_REQUEST_HARD_CAP = int(os.getenv("BRANDFETCH_REQUEST_BUDGET", "100"))
BRIGHTDATA_REQUEST_RATIO = max(0.0, float(os.getenv("BRIGHTDATA_REQUEST_RATIO", "1.5")))
GOOGLE_PLACES_REQUEST_RATIO = max(0.0, float(os.getenv("GOOGLE_PLACES_REQUEST_RATIO", "0.25")))
HUNTER_REQUEST_RATIO = max(0.0, float(os.getenv("HUNTER_REQUEST_RATIO", "0.10")))
BRANDFETCH_REQUEST_RATIO = max(0.0, float(os.getenv("BRANDFETCH_REQUEST_RATIO", "0.25")))
BRIGHTDATA_REQUEST_BUDGET = BRIGHTDATA_REQUEST_HARD_CAP
LINKEDIN_COMPANY_REQUEST_HARD_CAP = max(
    0, int(os.getenv("LINKEDIN_COMPANY_REQUEST_BUDGET", "500"))
)
LINKEDIN_COMPANY_REQUEST_BUDGET = LINKEDIN_COMPANY_REQUEST_HARD_CAP
ENABLE_LLM_ARBITER = os.getenv("ENABLE_LLM_ARBITER", "1") == "1"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_BASE_URL = os.getenv(
    "GROQ_API_BASE_URL", "https://api.groq.com/openai/v1"
).rstrip("/")
LLM_ARBITER_MODEL = os.getenv(
    "LLM_ARBITER_MODEL", "llama-3.3-70b-versatile"
)
LLM_ARBITER_BUDGET = max(0, int(os.getenv("LLM_ARBITER_BUDGET", "220")))
LLM_ARBITER_TIMEOUT_SEC = max(
    5, int(os.getenv("LLM_ARBITER_TIMEOUT_SEC", "30"))
)
GOOGLE_PLACES_REQUEST_BUDGET = GOOGLE_PLACES_REQUEST_HARD_CAP
HUNTER_REQUEST_BUDGET = HUNTER_REQUEST_HARD_CAP
BRANDFETCH_REQUEST_BUDGET = BRANDFETCH_REQUEST_HARD_CAP

SEARCH_QUERY_TEMPLATES = [
    "{company} resmi sitesi",
    "{company} contact",
    "{company} iletisim",
]
TARGET_COUNTRY = "TR"
TARGET_COUNTRY_QUERY_TERMS = ["Turkiye"]
SEARCH_COUNTRY_QUERY_TEMPLATES = [
    "{company} {country} official website",
]
SEARCH_RESULTS_PER_QUERY = 8
MAX_SEARCH_QUERIES_PER_COMPANY = int(os.getenv("MAX_SEARCH_QUERIES_PER_COMPANY", "0"))
DEFAULT_PAID_SEARCH_QUERY_LIMIT = int(os.getenv("DEFAULT_PAID_SEARCH_QUERY_LIMIT", "10"))
BRIGHTDATA_RETRY_RESERVE_FRACTION = min(
    0.8,
    max(0.0, float(os.getenv("BRIGHTDATA_RETRY_RESERVE_FRACTION", "0.20"))),
)
BRIGHTDATA_CIRCUIT_FAILURE_THRESHOLD = max(
    1, int(os.getenv("BRIGHTDATA_CIRCUIT_FAILURE_THRESHOLD", "3"))
)
BRIGHTDATA_CIRCUIT_COOLDOWN_SEC = max(
    1, int(os.getenv("BRIGHTDATA_CIRCUIT_COOLDOWN_SEC", "300"))
)
BRIGHTDATA_MAX_INFLIGHT_QUERIES = max(
    1, int(os.getenv("BRIGHTDATA_MAX_INFLIGHT_QUERIES", "2"))
)
BRIGHTDATA_EMPTY_BODY_RETRY_SEC = max(
    0.0, float(os.getenv("BRIGHTDATA_EMPTY_BODY_RETRY_SEC", "15"))
)
BRIGHTDATA_MAX_DECODE_RETRIES = max(
    0, int(os.getenv("BRIGHTDATA_MAX_DECODE_RETRIES", "1"))
)
MAX_FALLBACK_SEARCH_QUERIES = int(os.getenv("MAX_FALLBACK_SEARCH_QUERIES", "3"))
MAX_ADAPTIVE_SEARCH_QUERIES = int(os.getenv("MAX_ADAPTIVE_SEARCH_QUERIES", "4"))
# Keep the total paid-query ceiling unchanged: reserve part of the existing
# allowance for evidence-driven expansion instead of spending every request on
# static primary templates before we know whether the candidate set is weak.
PAID_SEARCH_ADAPTIVE_RESERVE = int(os.getenv("PAID_SEARCH_ADAPTIVE_RESERVE", "3"))
DISCOVERY_ACQUISITION_QUERIES_PER_COMPANY = max(
    1, int(os.getenv("DISCOVERY_ACQUISITION_QUERIES_PER_COMPANY", "3"))
)
MAX_AUTONOMOUS_RESOLUTION_ROUNDS = max(
    0, int(os.getenv("MAX_AUTONOMOUS_RESOLUTION_ROUNDS", "2"))
)
MAX_TARGETED_QUERIES_PER_ROUND = max(
    0, int(os.getenv("MAX_TARGETED_QUERIES_PER_ROUND", "2"))
)
MAX_TARGETED_CRAWLS_PER_ROUND = max(
    1, int(os.getenv("MAX_TARGETED_CRAWLS_PER_ROUND", "3"))
)
MAX_SEARCH_BRIDGE_FETCHES = int(os.getenv("MAX_SEARCH_BRIDGE_FETCHES", "2"))
PROFILE_BRIDGE_BLOCKED_DOMAINS = [
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "pinterest.com", "google.com", "google.com.tr",
    "trendyol.com", "hepsiburada.com", "n11.com", "amazon.com.tr",
]
DOMAIN_GUESS_TLDS = [".com.tr", ".com", ".tr"]
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "ddgs").lower()
SEARCH_REPLAY_PROVIDER_FALLBACKS = tuple(
    provider.strip().lower()
    for provider in os.getenv("SEARCH_REPLAY_PROVIDER_FALLBACKS", "brightdata,ddgs").split(",")
    if provider.strip()
)
BRIGHTDATA_API_KEY = os.getenv("BRIGHTDATA_API_KEY", "")
BRIGHTDATA_ZONE = os.getenv("BRIGHTDATA_ZONE", "serp_api1")
BRIGHTDATA_ENDPOINT = "https://api.brightdata.com/request"
BRIGHTDATA_GOOGLE_DOMAIN = "www.google.com.tr"
BRIGHTDATA_GOOGLE_GL = "tr"
BRIGHTDATA_GOOGLE_HL = "tr"
BRIGHTDATA_COUNTRY = "tr"
BRIGHTDATA_TIMEOUT_SEC = int(os.getenv("BRIGHTDATA_TIMEOUT_SEC", "90"))
ENABLE_LINKEDIN_COMPANY_LOOKUP = (
    os.getenv("ENABLE_LINKEDIN_COMPANY_LOOKUP", "1") == "1"
)
LINKEDIN_COMPANY_DATASET_ID = os.getenv(
    "LINKEDIN_COMPANY_DATASET_ID", "gd_l1vikfnt1wgvvqz95w"
)
LINKEDIN_COMPANY_ENDPOINT = os.getenv(
    "LINKEDIN_COMPANY_ENDPOINT",
    "https://api.brightdata.com/datasets/v3/scrape",
)
LINKEDIN_COMPANY_TIMEOUT_SEC = max(
    10, int(os.getenv("LINKEDIN_COMPANY_TIMEOUT_SEC", "90"))
)
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ENABLE_GOOGLE_PLACES = os.getenv("ENABLE_GOOGLE_PLACES", "1") != "0"
GOOGLE_PLACES_TIMEOUT_SEC = int(os.getenv("GOOGLE_PLACES_TIMEOUT_SEC", "20"))
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
ENABLE_HUNTER_FALLBACK = os.getenv("ENABLE_HUNTER_FALLBACK", "0") == "1"
ENABLE_HUNTER_DOMAIN_FINDER = os.getenv("ENABLE_HUNTER_DOMAIN_FINDER", "0") == "1"
HUNTER_TIMEOUT_SEC = int(os.getenv("HUNTER_TIMEOUT_SEC", "20"))
HUNTER_MIN_CONFIDENCE = int(os.getenv("HUNTER_MIN_CONFIDENCE", "80"))
BRANDFETCH_CLIENT_ID = os.getenv("BRANDFETCH_CLIENT_ID", "")
ENABLE_BRANDFETCH_DOMAIN_SEARCH = os.getenv("ENABLE_BRANDFETCH_DOMAIN_SEARCH", "0") == "1"
BRANDFETCH_TIMEOUT_SEC = int(os.getenv("BRANDFETCH_TIMEOUT_SEC", "20"))
COMPANY_RESOLVER_MAX_RESULTS = int(os.getenv("COMPANY_RESOLVER_MAX_RESULTS", "5"))
EARLY_STOP_SCORE_THRESHOLD = 92
MIN_ACCEPT_SCORE = 65
HIGH_CONFIDENCE_SCORE = 90
MEDIUM_CONFIDENCE_SCORE = 75
REVIEW_SCORE = 60
SAFE_OK_MIN_SCORE = 85
PUBLICATION_POLICY_MODE = os.getenv("PUBLICATION_POLICY_MODE", "enforce_downgrade_only")
PUBLICATION_POLICY_MIN_SAFETY_SCORE = int(
    os.getenv("PUBLICATION_POLICY_MIN_SAFETY_SCORE", "75")
)
MAX_CANDIDATE_EVALUATIONS = max(
    1, int(os.getenv("MAX_CANDIDATE_EVALUATIONS", "8"))
)
MAX_CANDIDATE_SCORE_GAP = 24
AMBIGUOUS_CANDIDATE_MARGIN = int(os.getenv("AMBIGUOUS_CANDIDATE_MARGIN", "5"))
SOURCE_PROFILE_MAX_SERVER_ERRORS = int(os.getenv("SOURCE_PROFILE_MAX_SERVER_ERRORS", "2"))
RESULT_RANK_BONUS_MAX = 8
PRE_CRAWL_SCORE_CAP = 92
# Search intent is positive evidence, not a gate: contact/iletisim results keep
# their existing scores when no official-query candidate can be found.
OFFICIAL_WEBSITE_QUERY_BONUS = 6
TARGET_COUNTRY_OFFICIAL_QUERY_BONUS = 10
METADATA_SEARCH_CONTEXT_BONUS = 8

# B2B-oriented mailboxes are more actionable than a generic info inbox.
EMAIL_PRIORITY_PREFIXES = ["sales", "export", "office", "info", "marketing"]
MAX_EMAIL_CANDIDATE_VERIFICATIONS = int(os.getenv("MAX_EMAIL_CANDIDATE_VERIFICATIONS", "3"))
BLOCKED_EMAIL_LOCAL_PREFIXES = {
    "privacy", "dataprotection", "data-protection", "dpo", "kvkk",
    "webmaster", "postmaster", "abuse", "noreply", "no-reply",
    "donotreply", "do-not-reply", "global", "globalinfo",
}
CONTACT_PAGE_PATHS = [
    "/contact", "/contact/", "/contacts", "/iletisim", "/kontakt",
    "/contact-us", "/contact-us/", "/about/contact",
    "/pages/contact", "/pages/contact-us", "/bize-ulasin",
    "/iletisim.aspx", "/Iletisim", "/iletisim-bilgileri", "/iletisim.html",
    "/iletisim-1", "/iletisim-2", "/contact-1", "/contact-2",
    "/tr/iletisim", "/tr/iletisim/", "/tr/contact", "/en/contact",
    "/pages/iletisim-bilgileri", "/hakkimizda", "/kurumsal",
]
MAX_CONTACT_PAGES = 6
# A site with many dead legacy routes must not turn the successful-page limit
# into an unbounded sequence of 404 requests.  This cap counts attempted
# contact URLs (homepage/identity/document fetches are tracked separately).
MAX_CONTACT_ATTEMPTS = int(os.getenv("MAX_CONTACT_ATTEMPTS", "10"))
MAX_IDENTITY_PAGES = int(os.getenv("MAX_IDENTITY_PAGES", "4"))
MAX_FULL_CANDIDATE_EVALUATIONS = int(os.getenv("MAX_FULL_CANDIDATE_EVALUATIONS", "3"))
MAX_IDENTITY_EVIDENCE_RECRAWLS = max(
    0, int(os.getenv("MAX_IDENTITY_EVIDENCE_RECRAWLS", "1"))
)
MAX_FIRST_PARTY_ALIAS_CANDIDATES = int(os.getenv("MAX_FIRST_PARTY_ALIAS_CANDIDATES", "3"))
MAX_FIRST_PARTY_CONTACT_ALIAS_CANDIDATES = int(
    os.getenv("MAX_FIRST_PARTY_CONTACT_ALIAS_CANDIDATES", "1")
)
IDENTITY_PAGE_PATHS = [
    "/about", "/about-us", "/hakkimizda", "/kurumsal",
    "/company-information", "/sirket-bilgileri", "/ticari-bilgiler", "/imprint",
    "/kvkk", "/kvkk-aydinlatma-metni", "/kvkk-metni",
    "/aydinlatma-metni", "/kisisel-verilerin-korunmasi",
    "/gizlilik-politikasi",
    "/privacy", "/privacy-policy", "/legal", "/legal-notice",
    "/terms", "/terms-of-use", "/kullanim-kosullari",
    "/mesafeli-satis-sozlesmesi", "/satis-sozlesmesi",
    "/distance-sales-contract",
    "/locations", "/lokasyonlar", "/subeler", "/ofisler",
    "/distributors", "/distributorler", "/bayiler",
]
MAX_SITEMAPS = max(0, int(os.getenv("MAX_SITEMAPS", "4")))
MAX_SITEMAP_URLS = 2500
MAX_DOCUMENT_LINKS = 3
MAX_STATIC_RECOVERY_PAGES = int(os.getenv("MAX_STATIC_RECOVERY_PAGES", "3"))
MAX_HOST_VARIANT_ATTEMPTS = max(
    0, int(os.getenv("MAX_HOST_VARIANT_ATTEMPTS", "1"))
)
REQUEST_TIMEOUT_SEC = max(1, int(os.getenv("REQUEST_TIMEOUT_SEC", "10")))
MAX_HTTP_REDIRECTS = 5
MAX_RETRIES = 2
RETRY_BACKOFF_BASE_SEC = 2.0
MAX_RETRY_AFTER_SEC = int(os.getenv("MAX_RETRY_AFTER_SEC", "30"))

# A host reused by several companies is not automatically a directory (parent
# groups can legitimately host multiple brands).  Reuse becomes a discovery-
# only signal only when the result also has catalogue/profile structure.
SHARED_CANDIDATE_HOST_MIN_COMPANIES = int(
    os.getenv("SHARED_CANDIDATE_HOST_MIN_COMPANIES", "2")
)
ENABLE_JS_FALLBACK = os.getenv("ENABLE_JS_FALLBACK", "1") == "1"
ENABLE_JS_PROFILE_FALLBACK = os.getenv("ENABLE_JS_PROFILE_FALLBACK", "1") == "1"
MAX_BROWSER_RENDER_WORKERS = max(
    1, int(os.getenv("MAX_BROWSER_RENDER_WORKERS", "6"))
)
JS_RENDER_TIMEOUT_SEC = int(os.getenv("JS_RENDER_TIMEOUT_SEC", "20"))
ENABLE_PDF_OCR = os.getenv("ENABLE_PDF_OCR", "1") == "1"
PDF_OCR_MAX_PAGES = int(os.getenv("PDF_OCR_MAX_PAGES", "3"))
PDF_OCR_DPI = int(os.getenv("PDF_OCR_DPI", "150"))
PDF_MIN_TEXT_CHARS = int(os.getenv("PDF_MIN_TEXT_CHARS", "40"))

PHONE_DEFAULT_COUNTRY = "TR"
PHONE_OUTPUT_FORMAT = "national"
PHONE_ALLOWED_COUNTRIES = ["TR"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

EXCLUDED_DOMAINS = [
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "pinterest.com",
    "yelp.com",
    "wikipedia.org",
    "eksisozluk.com",
    "trendyol.com",
    "hepsiburada.com",
    "n11.com",
    "amazon.com.tr",
    "yemeksepeti.com",
    "tripadvisor.com",
    "tripadvisor.com.tr",
    "sikayetvar.com",
    "yandex.com",
    "yandex.com.tr",
    "maps.google.com",
    "google.com",
    "google.com.tr",
    "apps.apple.com",
    "play.google.com",
    "webfactory.com.tr",
    "wa.me",
    "t.me",
    "era.az",
    "icaevents.com.tr",
    "bbc.com",
    "turkishexporter.com.tr",
    "turkishexporter.net",
    "turkish-manufacturers.com",
    "ito.org.tr",
    "gulfood.com",
    "gso.org.tr",
    "find.com.tr",
    "mukellef.info",
    "europages.com.tr",
    "yellowpages.com",
    "kompass.com",
    "europages.com",
    "indeed.com",
    "glassdoor.com",
    "kariyer.net",
    "isinolsun.com",
    "idef.com.tr",
    "ifco.com.tr",
    "beautyeurasia.com",
    "crm.idos.events",
    "masterchef.com.tr",
    "fashionunited.com.tr",
    "sistemglobal.com.tr",
    "royalcert.com",
    "demircelik.com.tr",
    "metalmedya.com",
    # Hosted catalogues/flipbooks are documents linked by an exhibitor profile,
    # not the exhibitor's own official website.
    "heyzine.com",
    "issuu.com",
    "fliphtml5.com",
    "anyflip.com",
    "publuu.com",
    "flipsnack.com",
    "scribd.com",
    "docs.google.com",
    "drive.google.com",
    # Multi-company marketplaces/directories can reproduce a firm's full legal
    # name, phone and even a platform mailbox. They are discovery sources, never
    # the firm's first-party official website.
    "all.biz",
    "tradekey.com",
    "tradeindia.com",
    "tradeatlas.com",
    "exportersindia.com",
    "go4worldbusiness.com",
    "made-in-china.com",
    "ec21.com",
    "exporthub.com",
    "company-list.org",
    "alibaba.com",
    "alibaba.com.tr",
    "alibabagroup.com",
    "ecocert.com",
    "emis.com",
    "tendata.com",
    "rocketreach.co",
]

# Services that publish a mirrored company page under a host shaped like
# ``<company-domain>.<mirror-service>``.  These pages may repeat first-party
# contact details, but the host is owned by the mirror and is never the
# company's official website.
MIRROR_DIRECTORY_DOMAINS = [
    "siteindices.com",
]

AMBIGUOUS_BRAND_WORDS = [
    "fashion",
    "white",
    "stone",
    "master",
    "cook",
    "royal",
    "global",
    "look",
    "keep",
    "euro",
    "la",
]

GENERIC_DOMAIN_KEYWORDS = [
    "alibaba",
    "amazon",
    "marketplace",
    "directory",
    "firma",
    "firmalar",
    "metalmarket",
    "trade",
    "b2b",
    "exporters",
    "portal",
    "haber",
    "news",
    "medya",
    "fair",
    "expo",
    "ihracat",
    "katalog",
    "rehber",
    "sanayirehber",
    "ticaretrehber",
    "sirketler",
    "kobirehber",
    "ihracatci",
    "sektor",
]

NON_COMPANY_DOMAIN_KEYWORDS = [
    "spor",
    "sport",
    "kulubu",
    "belediye",
    "municipality",
    "universite",
    "university",
    "edu",
    "project",
    "github",
    "gitlab",
    "sourceforge",
    "docs",
    "wiki",
    "blogspot",
    "wordpress",
]

LEGAL_COMPANY_WORDS = [
    "ltd",
    "sti",
    "sti",
    "san",
    "sanayi",
    "tic",
    "ticaret",
    "anonim",
    "limited",
    "as",
    "as",
    "co",
    "inc",
    "gmbh",
]

SECTOR_GENERIC_WORDS = [
    "demir",
    "celik",
    "celik",
    "metal",
    "kimya",
    "kimyasal",
    "boya",
    "profil",
    "sac",
    "hadde",
    "haddecilik",
    "endustri",
    "endustri",
    "makine",
]

BUSINESS_GENERIC_WORDS = [
    "endustriyel",
    "mineraller",
    "kimyasal",
    "maddeler",
    "tekstil",
    "hizmetleri",
    "servis",
    "satis",
    "ambalaj",
    "barkod",
    "dijital",
    "baski",
    "sistemleri",
    "elektronik",
    "dahili",
    "ve",
]

# Words that describe the corporate structure or line of business rather than
# the brand itself. These are ignored only while matching a company name to a
# domain; page identity and context validation continue to use their existing
# token sets.
DOMAIN_IDENTITY_GENERIC_WORDS = [
    "group",
    "grup",
    "global",
    "kozmetik",
    "gida",
    "food",
    "dried",
    "kagit",
    "seluloz",
    "lojistik",
    "ic",
    "dis",
    "sirket",
    "sirketi",
    "urun",
    "urunleri",
]

CONTEXT_VALIDATION_WORDS = [
    "kozmetik",
    "cosmetic",
    "cosmetics",
    "gida",
    "food",
    "temizlik",
    "cleaning",
    "kimya",
    "boya",
    "tekstil",
    "ambalaj",
    "barkod",
    "baski",
    "elektronik",
    "mineraller",
    "plastik",
    "plastic",
    "otomotiv",
    "automotive",
]

METADATA_CONTEXTS = {
    "kozmetik": {
        "query_term": "kozmetik",
        "aliases": [
            "kozmetik", "cosmetic", "cosmetics", "beauty", "dermokozmetik",
            "dermocosmetic", "personal care", "kisisel bakim", "skin care",
            "cilt bakim", "makeup", "makyaj",
        ],
    },
    "temizlik": {
        "query_term": "temizlik",
        "aliases": [
            "temizlik", "cleaning", "hijyen", "hygiene", "detergent", "disinfectant",
            "wet wipe", "wet wipes", "diaper",
        ],
    },
    "gida": {
        "query_term": "gida",
        "aliases": [
            "gida", "food", "beverage", "drink", "chocolate", "biscuit", "cake",
            "bakery", "confectionery", "candy", "halva", "turkish delight", "sauce",
            "olive", "olives", "olive oil", "pickle", "pickles", "pepper paste",
            "pulp", "puree", "tomato paste", "hazelnut", "pistachio", "kadayif",
            "frozen food", "meat", "mineral water", "carbonated", "fruit", "juice",
            "snack", "fmcg", "fish", "seafood", "balik", "su urunleri",
        ],
    },
    "ambalaj": {
        "query_term": "ambalaj",
        "aliases": [
            "ambalaj", "packaging", "label", "shrink sleeve", "carton", "box",
            "etiket", "matbaa", "printing",
        ],
    },
    "tekstil": {
        "query_term": "tekstil",
        "aliases": [
            "tekstil", "giyim", "moda", "fashion", "clothing", "apparel", "garment",
            "ready to wear", "hazir giyim", "triko",
        ],
    },
    "ev_mutfak": {
        "query_term": "ev mutfak esyalari",
        "aliases": [
            "ev ve mutfak esyalari", "ev mutfak", "zuccaciye", "home and kitchen",
            "homeware", "housewares", "kitchenware", "cookware", "tableware",
            "dinnerware", "glassware", "porcelain", "ceramic", "cutlery",
            "flatware", "kitchen appliances", "small domestic appliances",
            "sofra", "mutfak", "porselen", "seramik", "cam esya", "catal bicak",
            "tencere", "tava", "pisirme gerecleri",
        ],
    },
    "makine": {
        "query_term": "makine",
        "aliases": ["makine", "machine", "machinery", "equipment", "manufacturing line"],
    },
    "kimya": {
        "query_term": "kimya",
        "aliases": ["kimya", "chemical", "chemicals"],
    },
    "plastik": {
        "query_term": "plastik",
        "aliases": ["plastik", "plastic"],
    },
    "baski": {
        "query_term": "baski",
        "aliases": ["baski", "printing", "print"],
    },
    "elektronik": {
        "query_term": "elektronik",
        "aliases": ["elektronik", "electronic", "electronics"],
    },
    "laboratuvar": {
        "query_term": "laboratuvar",
        "aliases": ["laboratuvar", "laboratory", "laboratory services"],
    },
    "otomotiv": {
        "query_term": "otomotiv",
        "aliases": [
            "otomotiv", "automotive", "automotive aftermarket", "aftermarket",
            "auto parts", "vehicle parts", "yedek parca", "spare parts",
            "driveshaft", "drive shaft", "suspension", "suspansiyon",
            "egzoz", "exhaust", "friction", "gasket", "connecting rod",
            "motor oil", "lubricant",
        ],
    },
}

FOREIGN_COUNTRY_TLDS = [
    "ae", "at", "au", "be", "bg", "br", "ca", "ch", "cn", "cz", "de", "dk", "es",
    "fi", "fr", "gr", "hu", "ie", "il", "in", "ir", "it", "jp", "kr", "nl", "no",
    "pl", "pt", "qa", "ro", "ru", "sa", "se", "sg", "sk", "uk", "us",
]

MIN_DISTINCTIVE_DOMAIN_HIT_RATIO = 0.5
MIN_DISTINCTIVE_DOMAIN_HITS = 1
SHORT_COMPANY_MIN_SCORE = 80
BAD_EMAIL_DOMAINS = ["example.com", "mail.com", "sentry.io"]
# Public mailbox providers can be valid contact addresses, but their domains
# must never be crawled as possible corporate sites merely because a company
# publishes one of those addresses.
NON_CORPORATE_EMAIL_DOMAINS = [
    "gmail.com", "googlemail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "yahoo.com", "yandex.com", "icloud.com", "proton.me",
    "protonmail.com", "mail.com",
]
VERIFY_EMAIL_MX = os.getenv("VERIFY_EMAIL_MX", "1") != "0"
EMAIL_DNS_TIMEOUT_SEC = float(os.getenv("EMAIL_DNS_TIMEOUT_SEC", "4"))

COUNTRY_TLD_BONUSES = {
    ".com.tr": 8,
    ".tr": 5,
}
