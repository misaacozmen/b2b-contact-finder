from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
STATE_DIR = BASE_DIR / "state"

INPUT_FILE = INPUT_DIR / "firms.xlsx"
CONTACTS_FILE = OUTPUT_DIR / "contacts.xlsx"
FAILED_FILE = OUTPUT_DIR / "failed.xlsx"
CANDIDATES_FILE = OUTPUT_DIR / "website_candidates.xlsx"
REPORT_FILE = OUTPUT_DIR / "report.txt"
LOG_FILE = OUTPUT_DIR / "logs.txt"
PROGRESS_FILE = STATE_DIR / "progress.json"

MAX_WORKERS = 3
MIN_DELAY_SEC = 1.0
MAX_DELAY_SEC = 3.0

SEARCH_QUERY_TEMPLATES = [
    "{company} resmi sitesi",
    "{company} official website",
    "{company} contact",
    "{company} iletisim",
]
TARGET_COUNTRY = "TR"
TARGET_COUNTRY_QUERY_TERMS = ["Turkiye", "Turkey"]
SEARCH_COUNTRY_QUERY_TEMPLATES = [
    "{company} {country} resmi sitesi",
    "{company} {country} official website",
]
SEARCH_RESULTS_PER_QUERY = 8
EARLY_STOP_SCORE_THRESHOLD = 92
MIN_ACCEPT_SCORE = 65
HIGH_CONFIDENCE_SCORE = 90
MEDIUM_CONFIDENCE_SCORE = 75
REVIEW_SCORE = 60
MAX_CANDIDATE_EVALUATIONS = 3
RESULT_RANK_BONUS_MAX = 8
PRE_CRAWL_SCORE_CAP = 92

EMAIL_PRIORITY_PREFIXES = ["info", "sales", "export", "marketing", "office"]
CONTACT_PAGE_PATHS = ["/contact", "/iletisim", "/kontakt", "/contact-us", "/about/contact"]
REQUEST_TIMEOUT_SEC = 10
MAX_RETRIES = 2
RETRY_BACKOFF_BASE_SEC = 2.0

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
    "wikipedia.org",
    "yellowpages.com",
    "kompass.com",
    "europages.com",
    "indeed.com",
    "glassdoor.com",
    "idef.com.tr",
    "demircelik.com.tr",
    "metalmedya.com",
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
    "club",
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

CONTEXT_VALIDATION_WORDS = [
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
]

FOREIGN_COUNTRY_TLDS = [
    "ae", "at", "au", "be", "bg", "br", "ca", "ch", "cn", "cz", "de", "dk", "es",
    "fi", "fr", "gr", "hu", "ie", "il", "in", "ir", "it", "jp", "kr", "nl", "no",
    "pl", "pt", "qa", "ro", "ru", "sa", "se", "sg", "sk", "uk", "us",
]

MIN_DISTINCTIVE_DOMAIN_HIT_RATIO = 0.5
MIN_DISTINCTIVE_DOMAIN_HITS = 1
SHORT_COMPANY_MIN_SCORE = 80
BAD_EMAIL_DOMAINS = ["example.com", "mail.com", "sentry.io"]

COUNTRY_TLD_BONUSES = {
    ".com.tr": 8,
    ".tr": 5,
}
