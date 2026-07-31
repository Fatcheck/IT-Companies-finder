"""
Business Finder with WhatsApp & Key Person Email
---------------------------------------------------
Given a business niche/type and a location (city, region, or country),
this script:
1. Finds companies in that niche using OpenStreetMap's Overpass API (free, no key needed).
2. Visits each company's website and extracts:
   - Decision-maker names/titles from team/about pages (CEO, Founder, etc.)
   - Contact email addresses (contact/careers pages)
   - Phone numbers
   - WhatsApp availability (checks for wa.me links, WhatsApp mentions, and API verification)
3. Infers the email pattern from found company emails and generates the
   decision-maker's email address.
4. Only saves companies that have a confirmed WhatsApp number.
5. Saves results to a CSV file with clickable WhatsApp links and contact info.

USAGE:
    python business_finder.py "fitness gym" "Denver, Colorado"
    python business_finder.py "dentist" "London, UK"
    python business_finder.py "real estate" "Dubai, UAE"
    python business_finder.py --limit 20 "restaurant" "Paris, France"

EXAMPLES BY NICHE:
    # Fitness & Health
    python business_finder.py "fitness gym" "Berlin, Germany"
    python business_finder.py "yoga studio" "Amsterdam, Netherlands"
    python business_finder.py "dentist" "Casablanca, Morocco"

    # Services
    python business_finder.py "real estate" "Dubai, UAE"
    python business_finder.py "marketing agency" "New York, USA"
    python business_finder.py "car rental" "Madrid, Spain"

    # Food & Hospitality
    python business_finder.py "restaurant" "Paris, France"
    python business_finder.py "cafe" "Sharjah, UAE"
    python business_finder.py "hotel" "Marrakech, Morocco"

    # Tech (original use-case still works)
    python business_finder.py "IT" "Berlin, Germany"
    python business_finder.py "software" "Austin, Texas"

NOTES / ETIQUETTE:
- Coverage depends on how well OpenStreetMap is mapped in that region — this will NOT find
  every business, especially in North America. Treat it as a lead generator, not a complete list.
- This only reads publicly listed emails on public web pages.
- Decision-maker detection: the script scans team/about pages for names near leadership
  titles (CEO, Founder, Managing Director, etc.). Generated emails are best-guess
  inferences based on the company's email pattern — always double-check before sending.
- WhatsApp detection: looks for WhatsApp mentions, wa.me links, and api.whatsapp.com/send
  links on the company website. It also attempts a wa.me API check as a fallback.
- Prefer personalized over mass-blast messaging — better response rate, and more respectful,
  especially for EU companies where GDPR applies to personal data.
- Some sites block scrapers or disallow it in robots.txt; the script just skips those.
"""

# ─── Imports ─────────────────────────────────────────────────────────────────────
import os
import re
import sys
import csv
import time
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin
from email.utils import parseaddr

import requests

# ─── Configuration ───────────────────────────────────────────────────────────────
# How long to wait between website requests (seconds).
# For high limits (500+), lower this to ~0.3 but be aware sites may rate-limit you.
SCRAPE_DELAY = 0.35

# Maximum number of pages to check per website
# Can be overridden via the MAX_PAGES_PER_SITE environment variable.
MAX_PAGES_PER_SITE = int(os.environ.get("MAX_PAGES_PER_SITE", "6"))

# Maximum concurrent workers for parallel website scraping.
# Default 30 — aggressive parallelization. Lower if you get rate-limited.
# Can be overridden via the MAX_WORKERS environment variable.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "30"))

# Maximum companies to process (0 = unlimited). Helps avoid timeouts on large cities.
# Can also be set via --limit CLI argument.
MAX_COMPANIES = 0

# ─── Terminal-safe symbols (Windows cp1252 compat) ──────────────────────────────
_CHECK = "[OK]"
_CROSS = "[X]"
_WARN = "[!]"
_ARROW = "->"
_LINE = "-" * 60
_INFO = "[i]"

# ─── Constants ───────────────────────────────────────────────────────────────────

# Email regex
EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*')

# Image extensions to filter out
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".tiff")

# Known non-company email domains to exclude
SPAM_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "email.com",
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "aol.com", "ymail.com", "protonmail.com", "proton.me",
}

# Preferred email prefixes (these get listed first)
PREFERRED_PREFIXES = (
    "info", "careers", "jobs", "hr", "recruitment", "hiring", "apply",
    "contact", "hello", "team", "support", "work", "talent", "join",
    "cv", "resume", "career", "office", "admin", "mail", "business",
    "enquiry", "enquiries", "inquiry", "inquiries", "recruit",
)

# Generic/non-personal email local-parts (used for pattern inference)
GENERIC_EMAIL_LOCALS = {
    'info', 'contact', 'hello', 'mail', 'admin', 'support', 'office',
    'team', 'careers', 'jobs', 'hr', 'sales', 'enquiry', 'enquiries',
    'inquiry', 'inquiries', 'recruit', 'recruitment', 'noreply',
    'no-reply', 'newsletter', 'marketing', 'billing', 'press',
    'media', 'partner', 'partners', 'service', 'services',
    'customerservice', 'customersupport', 'feedback', 'commercial',
}

# Descriptive User-Agent with contact email (required by Overpass API).
# ⚠️  If you get 406 errors on GitHub Actions, replace with YOUR real email.
_CONTACT_EMAIL = "faysal.bohmo@gmail.com"  # ← Your real email
if "@example.com" in _CONTACT_EMAIL:
    _WARN_EMAIL = True
else:
    _WARN_EMAIL = False
_DESCRIPTION = f"BusinessFinder/1.0 (contact={_CONTACT_EMAIL}; business-search project)"
HEADERS = {
    "User-Agent": _DESCRIPTION,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Overpass API requires a descriptive User-Agent with a way to contact the operator.
# The _DESCRIPTION string (with contact email) satisfies this requirement.
# A Chrome UA is kept as a fallback if the descriptive one is rejected.
_OVERPASS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Primary Overpass headers: descriptive UA with contact email (per Overpass policy)
OVERPASS_HEADERS = {
    "User-Agent": _DESCRIPTION,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Fallback Overpass headers: Chrome UA (used if primary gets a 406)
_OVERPASS_FALLBACK_HEADERS = {
    "User-Agent": _OVERPASS_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# ─── HERE Geocoding & Search API Configuration ──────────────────────────────────
# Get a free API key at: https://developer.here.com (Freemium plan = 30k req/mo)
# Set it as the HERE_API_KEY environment variable.
HERE_API_KEY = os.environ.get("HERE_API_KEY", "")
HERE_DISCOVER_URL = "https://discover.search.hereapi.com/v1/discover"

# Known country codes for phone number handling
# Format: {code: {"name": ..., "min_digits": remaining digits needed after country code}}
COUNTRY_CODES = {
    "1": {"name": "US/Canada", "min_digits": 10},
    "49": {"name": "Germany", "min_digits": 10},
    "212": {"name": "Morocco", "min_digits": 9},
    "213": {"name": "Algeria", "min_digits": 9},
    "966": {"name": "Saudi Arabia", "min_digits": 9},
    "971": {"name": "UAE", "min_digits": 9},
    "974": {"name": "Qatar", "min_digits": 8},
    "965": {"name": "Kuwait", "min_digits": 8},
    "968": {"name": "Oman", "min_digits": 8},
    "973": {"name": "Bahrain", "min_digits": 8},
}

# Country hints for guessing country code from a location string
COUNTRY_HINTS = {
    # US
    "united states": "+1", "usa": "+1", "u.s.a.": "+1", "america": "+1",
    "alabama": "+1", "alaska": "+1", "arizona": "+1", "arkansas": "+1",
    "california": "+1", "colorado": "+1", "connecticut": "+1", "delaware": "+1",
    "florida": "+1", "georgia": "+1", "hawaii": "+1", "idaho": "+1",
    "illinois": "+1", "indiana": "+1", "iowa": "+1", "kansas": "+1",
    "kentucky": "+1", "louisiana": "+1", "maine": "+1", "maryland": "+1",
    "massachusetts": "+1", "michigan": "+1", "minnesota": "+1",
    "mississippi": "+1", "missouri": "+1", "montana": "+1", "nebraska": "+1",
    "nevada": "+1", "new hampshire": "+1", "new jersey": "+1",
    "new mexico": "+1", "new york": "+1", "north carolina": "+1",
    "north dakota": "+1", "ohio": "+1", "oklahoma": "+1", "oregon": "+1",
    "pennsylvania": "+1", "rhode island": "+1", "south carolina": "+1",
    "south dakota": "+1", "tennessee": "+1", "texas": "+1", "utah": "+1",
    "vermont": "+1", "virginia": "+1", "washington": "+1",
    "west virginia": "+1", "wisconsin": "+1", "wyoming": "+1",
    # Canada provinces
    "ontario": "+1", "quebec": "+1", "british columbia": "+1", "alberta": "+1",
    "manitoba": "+1", "saskatchewan": "+1", "nova scotia": "+1",
    "new brunswick": "+1", "newfoundland": "+1", "prince edward island": "+1",
    "canada": "+1",
    # GCC
    "united arab emirates": "+971", "uae": "+971", "dubai": "+971",
    "abu dhabi": "+971", "sharjah": "+971", "ajman": "+971",
    "saudi arabia": "+966", "saudi": "+966", "riyadh": "+966", "jeddah": "+966",
    "qatar": "+974", "doha": "+974",
    "kuwait": "+965", "kuwait city": "+965",
    "oman": "+968", "muscat": "+968",
    "bahrain": "+973", "manama": "+973",
    # Europe
    "germany": "+49", "deutschland": "+49", "de": "+49",
    "united kingdom": "+44", "uk": "+44", "england": "+44", "london": "+44",
    "france": "+33", "paris": "+33",
    "spain": "+34", "madrid": "+34", "barcelona": "+34",
    "italy": "+39", "rome": "+39", "milan": "+39",
    "netherlands": "+31", "holland": "+31", "amsterdam": "+31",
    # Africa
    "morocco": "+212", "maroc": "+212", "casablanca": "+212", "rabat": "+212",
    "marrakech": "+212", "tangier": "+212", "fes": "+212", "agadir": "+212",
}

# ─── Business niche keyword extraction ──────────────────────────────────────────

def extract_niche_keywords(niche: str) -> list:
    """Extract meaningful keywords from a niche string.
    
    Handles multi-word niches like 'fitness gym', 'real estate agent', etc.
    Returns a list of individual keywords PLUS the full niche phrase.
    """
    # Split by common delimiters
    raw = re.split(r'[,/&]+', niche)
    keywords = []
    seen = set()
    for part in raw:
        part = part.strip().lower()
        if not part:
            continue
        # Add the whole phrase
        if part not in seen:
            seen.add(part)
            keywords.append(part)
        # Add individual words
        for word in part.split():
            word = word.strip()
            if word and len(word) > 1 and word not in seen:
                seen.add(word)
                keywords.append(word)
    return keywords

# ─── Helpers ─────────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """Normalize a URL — add scheme, handle protocol-relative URLs."""
    url = url.strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(( "http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def is_preferred_email(email: str) -> bool:
    local_part = email.split("@")[0].lower()
    return any(local_part.startswith(p) for p in PREFERRED_PREFIXES)


def is_spam_or_irrelevant(email: str) -> bool:
    lower = email.lower()
    if lower.endswith(IMAGE_EXTENSIONS):
        return True
    if re.match(r'.+@\d+\.\d+\.\d+\.\d+$', email):
        return True
    domain = lower.split("@")[-1]
    if domain in SPAM_DOMAINS:
        return True
    if any(t in domain for t in ("tracking", "analytics", "marketing", "newsletter", "mailchimp")):
        return True
    local = email.split("@")[0]
    if len(local) > 40:
        return True
    return False


def sort_emails_by_relevance(emails: set) -> list:
    preferred = sorted(e for e in emails if is_preferred_email(e))
    other = sorted(e for e in emails if not is_preferred_email(e))
    return preferred + other


def is_valid_email(email: str) -> bool:
    name, addr = parseaddr(email)
    if not addr:
        return False
    if " " in addr.strip():
        return False
    if addr.count("@") != 1:
        return False
    local, domain = addr.split("@")
    if len(local) < 1 or not domain or "." not in domain:
        return False
    return True


def get_country_code_from_location(location: str) -> str | None:
    """Guess the likely country code from a location string."""
    loc_lower = location.lower()
    for keyword, cc in COUNTRY_HINTS.items():
        if keyword in loc_lower:
            return cc
    return None


# ─── Phone Number Extraction & Normalization ──────────────────────────────────────

# Phone number pattern (international + national formats)
PHONE_RE = re.compile(
    r'(?:(?:\+|00)[1-9][0-9]{0,2}[\s\-/]*(?:\(0\))?[\s\-/]*|0)'
    r'[\s\-/]*\d{2,5}[\s\-/]*\d{2,4}[\s\-/]*\d{2,4}(?:[\s\-/]*\d{2,6})?'
)

# WhatsApp indicators on a page
WHATSAPP_RE = re.compile(r'whatsapp', re.IGNORECASE)
# Catch both wa.me/PHONE and api.whatsapp.com/send?phone=PHONE links
WA_ME_RE = re.compile(
    r'(?:wa\.me[\s\-/]*|api\.whatsapp\.com/send\?phone[=\-]?)(\d{7,15})',
    re.IGNORECASE,
)


def normalize_phone(raw: str) -> str | None:
    """
    Normalize a phone number to international format (+XXX...).
    Handles numbers from multiple countries.
    """
    cleaned = re.sub(r'[\s\-/()\.\,]', '', raw)

    if cleaned.startswith('+'):
        # Already in international format — extract and validate country code
        for cc_len in [3, 2, 1]:
            cc = cleaned[1:1+cc_len]
            if cc in COUNTRY_CODES:
                return '+' + cc + cleaned[1+cc_len:]
        return cleaned
    elif cleaned.startswith('00'):
        # International format with 00 prefix — convert to +
        for cc_len in [3, 2, 1]:
            cc = cleaned[2:2+cc_len]
            if cc in COUNTRY_CODES:
                return '+' + cc + cleaned[2+cc_len:]
        return '+' + cleaned[2:]
    elif cleaned.startswith('0'):
        # National format — can't determine country without context
        return cleaned
    return None


def is_valid_phone(phone: str) -> bool:
    """Validate a normalized phone number against known country codes."""
    if not phone or not phone.startswith('+'):
        return False
    digits = re.sub(r'\D', '', phone)
    if len(digits) < 8 or len(digits) > 16:
        return False
    # Check if country code is known and has enough remaining digits
    for cc_len in [3, 2, 1]:
        cc = digits[1:1+cc_len]
        if cc in COUNTRY_CODES:
            info = COUNTRY_CODES[cc]
            remaining = len(digits) - 1 - cc_len
            return remaining >= info["min_digits"]
    return False


def extract_wa_me_number(text: str) -> str | None:
    """Extract phone number from a wa.me link found on the page."""
    match = WA_ME_RE.search(text)
    if match:
        num = match.group(1)
        if len(num) >= 8 and len(num) <= 15:
            # Check if number starts with a known country code
            for cc_len in [3, 2, 1]:
                cc = num[:cc_len]
                if cc in COUNTRY_CODES:
                    return '+' + num
    return None


# ─── Site Scraper (Emails + Phones + WhatsApp) ───────────────────────────────────

def scrape_site(base_url: str) -> dict:
    """
    Scrape a website for emails, phone numbers, WhatsApp indicators,
    and decision-maker names/titles.

    Returns:
    {
      "emails": set of email strings,
      "phones": list of normalized phone strings (international format),
      "has_whatsapp_mention": bool (whether the site mentions WhatsApp),
      "wa_me_numbers": list of phones extracted from wa.me links on the site,
      "site_whatsapp": bool (overall: has WhatsApp mention OR valid wa.me links),
      "people": list of dicts with {name, title, score} from most senior first,
    }
    """
    result = {
        "emails": set(),
        "phones": [],
        "has_whatsapp_mention": False,
        "wa_me_numbers": [],
        "site_whatsapp": False,
        "people": [],
    }

    base_url = normalize_url(base_url)
    if not base_url:
        return result

    # Homepage and contact pages first (core data), then people pages for names
    paths = [
        "", "/contact", "/contact-us", "/contactez-nous",
        "/team", "/about", "/about-us", "/a-propos", "/notre-equipe",
        "/qui-sommes-nous", "/leadership", "/management",
        "/founders", "/board", "/executive", "/company",
        "/uber-uns", "/ueber-uns", "/unternehmen",
        "/careers", "/jobs", "/impressum", "/imprint",
    ]
    # Deduplicate while preserving order
    seen_paths = set()
    unique_paths = []
    for p in paths:
        if p not in seen_paths:
            seen_paths.add(p)
            unique_paths.append(p)

    urls = [urljoin(base_url + "/", p.lstrip("/")) for p in unique_paths]
    urls = urls[:MAX_PAGES_PER_SITE + 1]

    seen_phones = set()
    all_html_chunks = []

    for page_url in urls:
        try:
            resp = requests.get(
                page_url,
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code != 200:
                continue

            text = resp.text
            all_html_chunks.append(text)

            # ── Emails ──
            raw_emails = EMAIL_RE.findall(text)
            for em in raw_emails:
                if is_valid_email(em) and not is_spam_or_irrelevant(em):
                    result["emails"].add(em.lower())

            # ── WhatsApp mentions ──
            if WHATSAPP_RE.search(text):
                result["has_whatsapp_mention"] = True

            # ── wa.me links ──
            wa_num = extract_wa_me_number(text)
            if wa_num and wa_num not in seen_phones:
                seen_phones.add(wa_num)
                result["wa_me_numbers"].append(wa_num)

            # ── Phone numbers ──
            raw_phones = PHONE_RE.findall(text)
            for p in raw_phones:
                normalized = normalize_phone(p)
                if normalized and normalized not in seen_phones:
                    if normalized.startswith('+') and not is_valid_phone(normalized):
                        continue
                    seen_phones.add(normalized)
                    result["phones"].append(normalized)

        except requests.RequestException:
            continue

        time.sleep(SCRAPE_DELAY)

    # ── People detection (once, using all collected HTML) ──
    all_html = '\n'.join(all_html_chunks)
    result["people"] = extract_people_from_html(all_html)

    # Determine overall WhatsApp availability
    result["site_whatsapp"] = (
        result["has_whatsapp_mention"] or len(result["wa_me_numbers"]) > 0
    )

    return result


# ─── Decision-Maker Detection (Names & Titles) ────────────────────────────────

# Leadership / decision-maker titles, ranked by seniority
TITLE_PATTERNS = [
    # (rank, pattern) — lower number = more senior
    (10, r'ceo|chief\s*executive\s*officer|geschäftsführer|geschaeftsfuehrer|président|présidente|directeur\s*général|directrice\s*générale'),
    (20, r'founder|co-founder|cofounder|gründer|mitgründer|fondateur|fondatrice|cofondateur|cofondatrice'),
    (30, r'managing\s*director|geschäftsleitung|standortleiter'),
    (40, r'owner|inhaber|eigentümer|president|präsident'),
    (45, r'gérant|gérante'),
    (50, r'partner|teilhaber|gesellschafter|associé|associée'),
    (60, r'cto|chief\s*technology\s*officer|technikvorstand|cfo|chief\s*financial\s*officer'),
    (70, r'director|leitung|abteilungsleiter|bereichsleiter|directeur|directrice|chef\s*de|responsable\s*de'),
    (80, r'head\s*of|vp\s*of|vice\s*president|vize'),
    (90, r'manager|teamlead|team\s*lead|senior|responsable'),
]

# Combined regex for all titles
_TITLE_COMBINED = '|'.join(f'(?:{p})' for _, p in TITLE_PATTERNS)
TITLE_RE = re.compile(_TITLE_COMBINED, re.IGNORECASE)

# Name pattern: two consecutive capitalized words (German, English, French, Spanish chars)
NAME_RE = re.compile(
    r'([A-ZÄÖÜÀÂÆÇÈÉÊËÌÍÎÏÑÒÓÔŒÙÚÛÜŸ][a-zäöüßàâæçèéêëìíîïñòóôœùúûüÿ]+(?:-[A-ZÄÖÜÀÂÆÇÈÉÊËÌÍÎÏÑÒÓÔŒÙÚÛÜŸ][a-zäöüßàâæçèéêëìíîïñòóôœùúûüÿ]+)?)\s+'
    r'([A-ZÄÖÜÀÂÆÇÈÉÊËÌÍÎÏÑÒÓÔŒÙÚÛÜŸ][a-zäöüßàâæçèéêëìíîïñòóôœùúûüÿ]+(?:-[A-ZÄÖÜÀÂÆÇÈÉÊËÌÍÎÏÑÒÓÔŒÙÚÛÜŸ][a-zäöüßàâæçèéêëìíîïñòóôœùúûüÿ]+)?)'
)

# Hard-coded known non-names to filter out
NON_NAMES = {
    "about", "contact", "impressum", "imprint", "privacy", "legal", "search",
    "careers", "jobs", "apply", "team", "leadership", "management",
    "services", "products", "solutions", "portfolio", "projects",
    "english", "german", "french", "spanish", "deutsch", "englisch",
    "email", "phone", "address", "location", "opening", "hours",
    "social", "media", "facebook", "twitter", "linkedin", "instagram",
    "download", "subscribe", "newsletter", "sign", "login", "register",
    "home", "menu", "navigation", "footer", "header", "sidebar",
    "powered", "copyright", "all", "rights", "reserved",
    # French common words
    "accueil", "contactez", "mentions", "legales", "conditions", "confidentialité",
    "politique", "charte", "données", "personnelles", "cookies",
    # Arabic transliteration common words
    "mohammed", "ahmed", "ali", "hassan", "hussein", "ibrahim",
}


def extract_people_from_html(html_text: str) -> list:
    """
    Find decision-maker names and titles in scraped HTML.
    Uses multiple strategies and returns ranked results.
    """
    # Remove script/style blocks
    cleaned = re.sub(
        r'<script[^>]*>.*?</script>', '', html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        r'<style[^>]*>.*?</style>', '', cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Get stripped text (strip all HTML tags)
    text = re.sub(r'<[^>]+>', ' ', cleaned)
    text = re.sub(r'\s+', ' ', text).strip()

    found = []
    seen_names = set()

    # ── Strategy 1: Name before title ("John Doe, CEO" / "John Doe - CEO") ──
    for match in TITLE_RE.finditer(text):
        title_raw = match.group(0)
        # Look backwards for a name (up to 50 chars)
        start = max(0, match.start() - 60)
        before = text[start:match.start()]
        name_matches = list(NAME_RE.finditer(before))
        if name_matches:
            nm = name_matches[-1]  # closest before title
            name = nm.group(0)
            name_lower = name.lower()
            if name_lower not in NON_NAMES and name_lower not in seen_names:
                seen_names.add(name_lower)
                rank = _get_title_rank(title_raw)
                found.append({
                    "name": name.strip(),
                    "title": title_raw.strip(),
                    "score": rank,
                })

    # ── Strategy 2: Title before name ("CEO John Doe") ──
    for match in TITLE_RE.finditer(text):
        title_raw = match.group(0)
        after = text[match.end():match.end() + 60]
        nm = NAME_RE.search(after)
        if nm:
            name = nm.group(0)
            name_lower = name.lower()
            if name_lower not in NON_NAMES and name_lower not in seen_names:
                seen_names.add(name_lower)
                rank = _get_title_rank(title_raw)
                found.append({
                    "name": name.strip(),
                    "title": title_raw.strip(),
                    "score": rank,
                })

    # ── Strategy 3: HTML structure (heading + text / card pattern) ──
    heading_pattern = re.compile(
        r'<h[2-6][^>]*>([^<]{2,50})</h[2-6]>'
        r'[^<]*(?:<p[^>]*>[^<]{0,150}</p>|<div[^>]*>[^<]{0,150}</div>)',
        re.IGNORECASE | re.DOTALL,
    )
    for hm in heading_pattern.finditer(cleaned):
        heading_text = hm.group(1).strip()
        nm = NAME_RE.match(heading_text)
        if nm:
            name = nm.group(0)
            name_lower = name.lower()
            if name_lower not in NON_NAMES and name_lower not in seen_names:
                context = hm.group(0)[len(heading_text):]
                title_match = TITLE_RE.search(context)
                title = title_match.group(0) if title_match else "Team Member"
                rank = _get_title_rank(title)
                seen_names.add(name_lower)
                found.append({
                    "name": name.strip(),
                    "title": title.strip(),
                    "score": rank,
                })

    # Sort by score (most senior first), then by name
    found.sort(key=lambda p: (p["score"], p["name"]))
    return found


def _get_title_rank(title: str) -> int:
    """Get the seniority rank for a title (lower = more senior)."""
    title_lower = title.lower().strip()
    for rank, pattern in TITLE_PATTERNS:
        if re.search(pattern, title_lower):
            return rank
    return 999


# ─── Google Founder Search ────────────────────────────────────────────────────────

_GOOGLE_SEARCH_LOCK = threading.Lock()
_GOOGLE_SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}


def search_google_founder(company_name: str) -> tuple | None:
    """
    Search Google for '{company_name} founder' and extract the founder's name.

    Returns (name, title) or None if not found.
    Uses a global lock to be polite (max 1 Google search per second across all workers).
    This is a best-effort search — Google may block or return no useful results.
    """
    query = f"{company_name} founder"

    with _GOOGLE_SEARCH_LOCK:
        time.sleep(1.0)  # Polite delay
        try:
            resp = requests.get(
                "https://www.google.com/search",
                params={"q": query, "hl": "en", "num": 5},
                headers=_GOOGLE_SEARCH_HEADERS,
                timeout=10,
            )
        except requests.RequestException:
            return None

    if resp.status_code != 200:
        return None

    people = extract_people_from_html(resp.text)

    if people:
        top = people[0]
        return top["name"], top["title"]

    return None


# ─── HERE Geocoding & Search API Search ──────────────────────────────────────────

def here_search(niche: str, location: str, limit: int = 100) -> list:
    """
    Search HERE Geocoding & Search API for businesses matching the niche in a location.

    Uses the HERE_API_KEY environment variable for authentication.
    Unlike Yelp, HERE returns business websites directly in the response,
    so no page scraping is needed.

    Free tier: 30,000 transactions/month at developer.here.com

    Returns a list of dicts: {name, website, source} where source is "HERE".
    Returns empty list if HERE_API_KEY is not set.
    """
    if not HERE_API_KEY:
        print(f"  {_INFO} HERE_API_KEY not set — skipping HERE search.")
        print(f"       Get a free key at https://developer.here.com (30k req/mo free)")
        return []

    print(f"\n{_ARROW} Searching HERE for '{niche}' businesses...")

    all_businesses = []
    seen_ids = set()
    page_token = None
    pages = 0
    max_pages = 5  # Up to 5 pages × 100 = 500 results max

    while pages < max_pages:
        params = {
            "apiKey": HERE_API_KEY,
            "q": niche,
            "in": location,
            "limit": min(limit, 100),
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get(
                HERE_DISCOVER_URL,
                params=params,
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"  {_WARN} HERE API request failed: {e}")
            break

        if resp.status_code != 200:
            if resp.status_code == 401:
                print(f"  {_WARN} HERE API: Invalid API key. Check HERE_API_KEY.")
            elif resp.status_code == 429:
                print(f"  {_WARN} HERE API rate limit reached. Try again later.")
            else:
                print(f"  {_WARN} HERE API returned status {resp.status_code}")
            break

        try:
            data = resp.json()
        except ValueError:
            break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            item_id = item.get("id", "")
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            name = item.get("title", "")
            if not name:
                continue

            # Extract website from contacts array
            website = None
            contacts = item.get("contacts", [])
            for contact in contacts:
                www_list = contact.get("www", [])
                for www in www_list:
                    url = www.get("value", "")
                    if url and url.startswith("http"):
                        website = normalize_url(url)
                        break
                if website:
                    break

            if not website:
                continue

            # Extract phone
            phone = ""
            for contact in contacts:
                phone_list = contact.get("phone", [])
                for p in phone_list:
                    val = p.get("value", "")
                    if val:
                        phone = val
                        break
                if phone:
                    break

            all_businesses.append({
                "name": name,
                "website": website,
                "phone": phone,
                "source": "HERE",
            })

        pages += 1
        print(f"       Got businesses from HERE (page {pages}, total unique so far: {len(all_businesses)})...")

        # Check for next page token
        page_token = data.get("next", None)
        if not page_token or len(items) < min(limit, 100):
            break

        # Polite delay
        time.sleep(0.5)

    if all_businesses:
        print(f"  {_INFO} HERE found {len(all_businesses)} businesses with websites.")
    else:
        print(f"       No HERE businesses found with websites.")

    return all_businesses


# ─── Email Pattern Inference ────────────────────────────────────────────────────

def infer_email_pattern(known_emails: set, domain: str) -> str | None:
    """
    Infer the email pattern used by a company from known emails.
    Returns a pattern string like '{first}.{last}' or None if can't infer.
    """
    local_parts = []
    for em in known_emails:
        parts = em.split('@')
        if len(parts) == 2 and parts[1].lower() == domain.lower():
            local_parts.append(parts[0])

    if not local_parts:
        return None

    # Check for patterns
    for local in local_parts:
        # Skip generic/anonymous emails
        if local.lower() in GENERIC_EMAIL_LOCALS:
            continue

        # Pattern: firstname.lastname
        if '.' in local and not local.startswith('.') and not local.endswith('.'):
            parts = local.split('.')
            if len(parts) == 2 and all(p.isalpha() for p in parts):
                return '{first}.{last}'

        # Pattern: firstname
        if local.isalpha() and len(local) > 2:
            return '{first}'

        # Pattern: flastname (first initial + last name)
        if len(local) > 3 and local[0].isalpha() and local[1:].isalpha():
            return '{f}{last}'

        # Pattern: firstname_lastname
        if '_' in local:
            parts = local.split('_')
            if len(parts) == 2 and all(p.isalpha() for p in parts):
                return '{first}_{last}'

    return '{first}.{last}'  # safe default


# ─── Email Generation ───────────────────────────────────────────────────────────

def generate_contact_email(
    name: str,
    domain: str,
    pattern: str = '{first}.{last}',
) -> str:
    """Generate an email address for a person using a given pattern."""
    name_clean = name.strip()
    parts = name_clean.split()
    if len(parts) < 2:
        return f"{parts[0].lower()}@{domain}" if parts else ''

    first = parts[0].lower()
    last = parts[-1].lower()
    f_initial = first[0]
    l_initial = last[0]

    replacements = {
        '{first}': first,
        '{last}': last,
        '{f}': f_initial,
        '{l}': l_initial,
        '{first}.{last}': f"{first}.{last}",
        '{first}_{last}': f"{first}_{last}",
        '{f}{last}': f"{f_initial}{last}",
        '{first}{last}': f"{first}{last}",
        '{first}.{l}': f"{first}.{l_initial}",
        '{last}.{first}': f"{last}.{first}",
    }

    email_local = replacements.get(pattern, f"{first}.{last}")
    return f"{email_local}@{domain}"


# ─── WhatsApp Verification ───────────────────────────────────────────────────────

def check_whatsapp_via_api(phone: str) -> bool:
    """
    Try to verify if a phone number has WhatsApp by visiting wa.me.

    Note: This is approximate — wa.me may be blocked behind Cloudflare.
    The function falls back gracefully: if the check fails, it returns
    the site-level WhatsApp indicator result.
    """
    url = f"https://wa.me/{phone.lstrip('+')}"
    try:
        resp = requests.get(
            url,
            headers=HEADERS,
            timeout=10,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            body = resp.text.lower()
            if 'whatsapp' in body and ('send' in body or 'chat' in body or 'continue' in body):
                return True
        return False
    except requests.RequestException:
        return False


def get_whatsapp_phones(site_data: dict, location: str = "") -> list:
    """
    Get the list of phone numbers that are confirmed to have WhatsApp.

    Priority order:
    1. Numbers from wa.me links on the site (most reliable indicator)
    2. Numbers from site if the site mentions WhatsApp
    3. Numbers verified via wa.me API check

    For national format numbers (starting with 0), tries to guess
    the country code from the location string.
    """
    confirmed = []
    seen = set()

    # 1. wa.me links on the site — these are explicitly WhatsApp numbers
    for num in site_data["wa_me_numbers"]:
        if num not in seen:
            seen.add(num)
            confirmed.append((num, True, "wa.me link on site"))

    # 2. If site mentions WhatsApp, try to verify found phone numbers
    if site_data["site_whatsapp"]:
        country_code = get_country_code_from_location(location) if location else None
        for num in site_data["phones"]:
            if num not in seen:
                seen.add(num)
                if num.startswith('0') and country_code:
                    international = country_code + num[1:]
                    if is_valid_phone(international):
                        confirmed.append((international, True, "site mentions WhatsApp"))
                        continue
                confirmed.append((num, True, "site mentions WhatsApp"))

    # 3. For remaining phones (site has no WhatsApp indicators), try wa.me API
    if not site_data["site_whatsapp"]:
        country_code = get_country_code_from_location(location) if location else None
        for num in site_data["phones"]:
            if num not in seen:
                if num.startswith('0') and country_code:
                    international = country_code + num[1:]
                    if is_valid_phone(international):
                        has_wa = check_whatsapp_via_api(international)
                        if has_wa:
                            confirmed.append((international, True, "wa.me API check"))
                        continue
                has_wa = check_whatsapp_via_api(num)
                seen.add(num)
                if has_wa:
                    confirmed.append((num, True, "wa.me API check"))

    return [(num, source) for num, _, source in confirmed]


# ─── Geocoding ───────────────────────────────────────────────────────────────────
# Uses Nominatim (free, OpenStreetMap-based) with a descriptive User-Agent.

GEOCODING_CACHE: dict = {}  # location -> bbox


def geocode_nominatim(location: str) -> dict | None:
    """Try Nominatim geocoding. Returns dict with bbox, osm_type, display_name or None."""
    time.sleep(1.0)

    params = {
        "q": location,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0,
    }
    headers = {
        "User-Agent": _DESCRIPTION,
        "Referer": "https://github.com/",
    }

    try:
        r = requests.get(
            NOMINATIM_URL,
            params=params,
            headers=headers,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  {_WARN} Nominatim connection failed: {e}")
        return None

    if r.status_code != 200:
        print(f"  {_WARN} Nominatim returned status {r.status_code}")
        if r.status_code == 403:
            print(f"        Access denied. See: https://operations.osmfoundation.org/policies/nominatim/")
            print(f"        This may be due to IP rate-limiting. Try again later.")
        return None

    try:
        data = r.json()
    except ValueError:
        print(f"  {_WARN} Nominatim response wasn't JSON")
        return None

    if not data:
        return None

    result_data = data[0]
    bbox = result_data["boundingbox"]
    display_name = result_data.get("display_name", location)
    lat, lon = result_data["lat"], result_data["lon"]
    osm_type = result_data.get("type", "")
    print(f"  Found: {display_name}")
    print(f"         Lat: {lat}, Lon: {lon}")
    print(f"         Type: {osm_type}")
    return {"bbox": bbox, "osm_type": osm_type, "display_name": display_name}


def geocode_manual() -> list | None:
    """Prompt user to enter coordinates manually."""
    print()
    print(f"  {_INFO} Enter bounding box coordinates manually.")
    print(f"       (You can find these on openstreetmap.org:")
    print(f"        Export -> manually select a bounding box)")
    print()
    try:
        south = input("  South latitude: ").strip()
        north = input("  North latitude: ").strip()
        west = input("  West longitude: ").strip()
        east = input("  East longitude: ").strip()
        if south and north and west and east:
            return [south, north, west, east]
    except (EOFError, KeyboardInterrupt):
        pass
    return None


def geocode(location: str) -> list:
    """Geocode a location. Tries Nominatim with multiple fallback strategies.

    When a POI (Point of Interest) is returned instead of a region, retries
    with progressively broader location strings:
    1. Original location
    2. City + Country (from display_name)
    3. Just the country (from display_name)
    4. Last comma-separated part of original location

    Returns bbox as [south, north, west, east].
    """

    # Check cache
    if location in GEOCODING_CACHE:
        return GEOCODING_CACHE[location]

    print(f"  {_ARROW} Geocoding with Nominatim (OpenStreetMap)...")

    result = geocode_nominatim(location)
    if result is not None:
        bbox = result["bbox"]
        osm_type = result["osm_type"]

        south, north, west, east = [float(x) for x in bbox]
        lat_span = north - south
        lon_span = east - west

        is_poi = (
            osm_type in ("university", "hotel", "restaurant", "museum", "school",
                         "hospital", "church", "stadium", "theatre", "attraction",
                         "yes", "building", "cafe", "pub", "shop", "office") or
            (lat_span < 0.02 and lon_span < 0.02)
        )

        if is_poi:
            print()
            print(f"  {_WARN} That looks like a specific POI (Point of Interest), not a region.")
            print(f"        Type: {osm_type}, area: ~{lat_span * 111:.0f} km x {lon_span * 111:.0f} km")
            print()

            # Try progressively broader location strings
            display_parts = result["display_name"].split(", ")
            candidates = []

            # Strategy 1: City, Country (last 2 parts of display_name)
            if len(display_parts) >= 2:
                candidates.append(", ".join(display_parts[-2:]))
            # Strategy 2: Just the country (last part of display_name)
            if len(display_parts) >= 1:
                candidates.append(display_parts[-1])
            # Strategy 3: Last part of original location string
            loc_parts = location.rsplit(",", 1)
            if len(loc_parts) > 1:
                candidates.append(loc_parts[-1].strip())

            for broader in candidates:
                print(f"  {_ARROW} Retrying with: {broader}")
                broader_result = geocode_nominatim(broader)
                if broader_result is not None:
                    new_bbox = broader_result["bbox"]
                    sn, nn, wn, en = [float(x) for x in new_bbox]
                    new_lat_span = nn - sn
                    new_lon_span = en - wn

                    # Check if the broader result is still a POI
                    still_poi = (
                        broader_result["osm_type"] in (
                            "university", "hotel", "restaurant", "museum", "school",
                            "hospital", "church", "stadium", "theatre", "attraction",
                            "yes", "building", "cafe", "pub", "shop", "office"
                        ) or
                        (new_lat_span < 0.02 and new_lon_span < 0.02)
                    )

                    if not still_poi:
                        result = broader_result
                        bbox = new_bbox
                        print(f"       Got region: {broader_result['display_name']}")
                        print()
                        break
                    else:
                        print(f"       Still a POI ({broader_result['osm_type']}), trying broader...")
                        print()
                else:
                    print(f"       Geocoding failed for this candidate.")
                    print()

        GEOCODING_CACHE[location] = bbox
        return bbox

    # Nominatim failed — try manual (only if running interactively)
    if sys.stdin.isatty():
        print(f"  {_WARN} Automatic geocoding failed.")
        bbox = geocode_manual()
        if bbox:
            GEOCODING_CACHE[location] = bbox
            return bbox

    raise RuntimeError(
        f"Could not geocode location '{location}'. "
        f"Try again later, or use a different location string."
    )


# ─── Overpass Query Builder (Dynamic — works for ANY niche) ──────────────────────

def build_overpass_queries(bbox: list, niche: str) -> tuple:
    """
    Build dynamic Overpass queries for a generic business niche.

    Uses a multi-phase approach:
    Phase 1: Name-based matching + tag-value matching across all business types
    Phase 2: Broad catch-all for any element with a website

    The niche string is split into keywords, and the query matches:
    - Businesses whose NAME contains any niche keyword
    - Businesses whose TAG VALUES match niche keywords (e.g., leisure=fitness_centre for "fitness")
    - Businesses with relevant industry tags
    """
    south, north, west, east = bbox

    # Extract keywords from the niche string
    keywords = extract_niche_keywords(niche)
    if not keywords:
        raise ValueError(f"No valid keywords extracted from niche: '{niche}'")

    # Build regex patterns
    # Escape special regex characters in keywords
    escaped_keywords = [re.escape(k) for k in keywords]
    name_pattern = "|".join(escaped_keywords)

    def _run_overpass(query_body: str) -> list:
        """Execute an Overpass query and return elements.

        Uses a retry strategy:
        1. Try each endpoint with the descriptive UA (has contact email)
        2. On 406, retry the same endpoint with Chrome UA as fallback
        3. Move to next endpoint if all attempts fail
        """
        full_query = f"""
        [out:json][timeout:300];
        (
          {query_body}
        );
        out center;
        """
        last_error = None
        for i, endpoint in enumerate(OVERPASS_ENDPOINTS):
            if i > 0:
                print(f"    Retrying with fallback server...")

            # Try with descriptive UA first
            for attempt, headers in [(1, OVERPASS_HEADERS), (2, _OVERPASS_FALLBACK_HEADERS)]:
                if attempt == 2:
                    print(f"         Trying Chrome UA fallback...")
                try:
                    r = requests.post(
                        endpoint,
                        data={"data": full_query},
                        headers=headers,
                        timeout=180,
                    )
                    r.raise_for_status()
                    return r.json().get("elements", [])
                except requests.RequestException as e:
                    status = getattr(e, 'response', None) and e.response.status_code
                    if status == 406 and attempt == 1:
                        # 406 with descriptive UA — try Chrome UA on same endpoint
                        continue
                    if status == 406 and attempt == 2:
                        # Both UAs got 406 on this endpoint — move to next
                        print(f"  {_WARN} Endpoint {endpoint} refused both User-Agents (406)")
                        last_error = e
                        break  # break inner loop, continue to next endpoint
                    if status:
                        print(f"  {_WARN} Endpoint {endpoint} failed: {e}")
                        print(f"        Status: {status}")
                    else:
                        print(f"  {_WARN} Endpoint {endpoint} failed: {e}")
                    last_error = e
                    break  # break inner loop, continue to next endpoint

        raise RuntimeError(f"All {len(OVERPASS_ENDPOINTS)} Overpass endpoints failed: {last_error}")

    # ── Phase 1: Name-based + tag-specific queries ──
    print(f"  {_ARROW} Searching for '{niche}' businesses by name and tags...")
    name_based_query = f"""
      // All office types with name matching the niche
      node["office"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["office"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      relation["office"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // All shop types with name matching the niche
      node["shop"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["shop"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // All amenity types with name matching the niche
      node["amenity"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["amenity"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // All leisure types with name matching the niche
      node["leisure"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["leisure"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // All sport-related with name matching
      node["sport"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["sport"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // Tourism businesses with name matching
      node["tourism"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["tourism"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // Healthcare providers with name matching
      node["healthcare"]["name"~"{name_pattern}",i]({south},{west},{north},{east});
      way["healthcare"]["name"~"{name_pattern}",i]({south},{west},{north},{east});

      // Office+industry matching the niche
      node["office"]["industry"~"{name_pattern}",i]({south},{west},{north},{east});
      way["office"]["industry"~"{name_pattern}",i]({south},{west},{north},{east});

      // Direct tag-value matching (e.g., leisure=fitness_centre for "fitness")
      // This catches entities where the tag value itself matches the niche
      node[~"^(amenity|shop|office|leisure|sport|healthcare|tourism|catering)$"~"{name_pattern}",i]({south},{west},{north},{east});
      way[~"^(amenity|shop|office|leisure|sport|healthcare|tourism|catering)$"~"{name_pattern}",i]({south},{west},{north},{east});

      // Any company offices (common in less-detailed regions)
      node["office"="company"]({south},{west},{north},{east});
      way["office"="company"]({south},{west},{north},{east});
      relation["office"="company"]({south},{west},{north},{east});
    """

    elements = _run_overpass(name_based_query)
    print(f"\n{_INFO} Found {len(elements)} candidates from name/tag-based queries.")

    # ── Phase 2: Broad catch-all for global coverage ──
    print(f"     Running broader catch-all query for global coverage...")
    catchall_query = f"""
      // Any element with a website and name matching the niche
      node["name"~"{name_pattern}",i]["website"~"."]({south},{west},{north},{east});
      way["name"~"{name_pattern}",i]["website"~"."]({south},{west},{north},{east});
      relation["name"~"{name_pattern}",i]["website"~"."]({south},{west},{north},{east});

      // Any office with a website (even without name matching — catches generic offices)
      node["office"]["website"~"."]({south},{west},{north},{east});
      way["office"]["website"~"."]({south},{west},{north},{east});
      relation["office"]["website"~"."]({south},{west},{north},{east});

      // Any company office with a website
      node["office"="company"]["website"~"."]({south},{west},{north},{east});
      way["office"="company"]["website"~"."]({south},{west},{north},{east});
      relation["office"="company"]["website"~"."]({south},{west},{north},{east});
    """
    extra_elements = _run_overpass(catchall_query)

    # Deduplicate by element id, keep elements from name-based query first
    seen_ids = {(el.get("type", ""), el.get("id")) for el in elements}
    for el in extra_elements:
        key = (el.get("type", ""), el.get("id"))
        if key not in seen_ids:
            seen_ids.add(key)
            elements.append(el)

    print(f"{_INFO} Total candidates after catch-all: {len(elements)}")

    return elements


# ─── CSV Writer (incremental) ────────────────────────────────────────────────────

# Clean, Google Sheets-friendly column headers
CSV_FIELDS = [
    "Company Name",
    "Website",
    "Source",
    "WhatsApp Link",
    "Phone Number",
    "WhatsApp Source",
    "Contact Person",
    "Contact Title",
    "Contact Email",
    "Founder Source",
    "All Emails Found",
]


def _clean_csv_val(value: str) -> str:
    """Clean a single CSV value — strip whitespace and never return None."""
    if not value:
        return ""
    return value.strip()


def _dedup_results(results: list) -> list:
    """Remove duplicate companies by (lowercase name, lowercase website) pair."""
    seen = set()
    deduped = []
    for row in results:
        name = row.get("Company Name", "").lower().strip()
        website = row.get("Website", "").lower().strip()
        key = (name, website)
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped


def save_partial_results(filename: str, results: list):
    """Write/overwrite the CSV with current results (incremental save).

    - Writes UTF-8 BOM for proper Google Sheets import (utf-8-sig adds the BOM).
    - Deduplicates rows by (Company Name, Website) pair.
    - Ensures no None values slip into the output.
    """
    results = _dedup_results(results)

    try:
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(results)
    except IOError as e:
        print(f"  {_WARN} Could not save intermediate results: {e}")


# ─── Signal Handler ──────────────────────────────────────────────────────────────

def signal_handler(sig, frame):
    print(f"\n\n{_WARN} Interrupted by user. Saving results so far...")
    raise KeyboardInterrupt()


# ─── Main ────────────────────────────────────────────────────────────────────────

def main():
    # Parse CLI args
    args = sys.argv[1:]
    if not args or "-h" in args or "--help" in args:
        print("Business Finder — Find companies in ANY niche with emails & WhatsApp")
        print()
        print("Usage:")
        print(f"  python {os.path.basename(__file__)} <niche> <location>")
        print(f"  python {os.path.basename(__file__)} --limit 30 <niche> <location>")
        print()
        print("Examples:")
        print(f'  python {os.path.basename(__file__)} "fitness gym" "Denver, Colorado"')
        print(f'  python {os.path.basename(__file__)} "dentist" "London, UK"')
        print(f'  python {os.path.basename(__file__)} "real estate" "Dubai, UAE"')
        print(f'  python {os.path.basename(__file__)} --limit 20 "restaurant" "Paris, France"')
        print(f'  python {os.path.basename(__file__)} "IT" "Berlin, Germany"')
        print()
        print("Options:")
        print("  --limit N   Max companies to process (default: unlimited)")
        print("  -h, --help  Show this help")
        sys.exit(0)

    global MAX_COMPANIES
    i = 0
    niche = None
    location_parts = []

    while i < len(args):
        if args[i] == "--limit":
            if i + 1 >= len(args):
                print(f"{_CROSS} --limit requires a number argument")
                sys.exit(1)
            try:
                MAX_COMPANIES = int(args[i + 1])
            except ValueError:
                print(f"{_CROSS} --limit requires a number, got '{args[i + 1]}'")
                sys.exit(1)
            if MAX_COMPANIES < 1:
                print(f"{_CROSS} --limit must be a positive number")
                sys.exit(1)
            i += 2
        elif niche is None:
            # First non-flag arg is the NICHE
            niche = args[i]
            i += 1
        else:
            # Remaining args are the LOCATION
            location_parts.append(args[i])
            i += 1

    if not niche:
        print(f"{_CROSS} Please provide a business niche (e.g., \"fitness gym\", \"restaurant\", \"IT\")")
        print(f"  Usage: python {os.path.basename(__file__)} <niche> <location>")
        sys.exit(1)

    location = " ".join(location_parts)
    if not location:
        print(f"{_CROSS} Please provide a location (e.g., \"Denver, Colorado\", \"London, UK\")")
        print(f"  Usage: python {os.path.basename(__file__)} <niche> <location>")
        sys.exit(1)

    # Set up signal handler for graceful Ctrl+C
    signal.signal(signal.SIGINT, signal_handler)

    # Warn if contact email is still the placeholder
    if _WARN_EMAIL:
        print(f"  {_WARN} Replace '_CONTACT_EMAIL' with your real email to avoid Overpass API 406 errors.")
        print(f"      Edit business_finder.py, find '_CONTACT_EMAIL' at the top, and")
        print(f"      change 'user@example.com' to your actual email address.")
        print()

    print(_LINE)
    print(f"  Business Finder — Search for '{niche}' businesses")
    print(f"  Location: {location}")
    if MAX_COMPANIES > 0:
        print(f"  Max companies: {MAX_COMPANIES}")
    print(f"{_LINE}\n")

    # Step 1: Geocode
    print(f"{_ARROW} Geocoding location...")
    try:
        bbox = geocode(location)
    except (RuntimeError, ValueError) as e:
        print(f"{_CROSS} {e}")
        sys.exit(1)

    # Step 2: Query Overpass (dynamic — works for any niche)
    print(f"\n{_ARROW} Querying OpenStreetMap for '{niche}' businesses (up to 180 seconds)...")
    try:
        elements = build_overpass_queries(bbox, niche)
    except (RuntimeError, ValueError) as e:
        print(f"{_CROSS} {e}")
        sys.exit(1)

    target_count = MAX_COMPANIES
    print(f"\n{_INFO} Found {len(elements)} candidate businesses on OpenStreetMap.")
    if target_count > 0:
        print(f"       Targeting {target_count} successful results (must have BOTH emails & WhatsApp).")
    print()

    # Step 3: Search HERE API (if API key is available — 30k free req/mo)
    here_results = here_search(niche, location, limit=min(100, target_count or 100))

    # Step 4: Scrape websites for emails, phones, and WhatsApp
    results = []
    skipped_no_name = 0
    skipped_no_website = 0
    skipped_no_whatsapp = 0
    skipped_no_email = 0
    errors = 0
    here_count = 0

    # Build CSV filename: {niche}_{location}.csv
    clean_location = re.sub(r'[\\/*?:"<>|, ]', '_', location).strip('_')
    clean_niche = re.sub(r'[\\/*?:"<>|, ]', '_', niche).strip('_')
    csv_filename = f"businesses_{clean_niche}_{clean_location}.csv"

    # Pre-filter: cheap checks (no name, no website) done sequentially first
    valid_companies = []

    # Add OSM results
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        website = tags.get("website") or tags.get("contact:website") or ""
        if not name:
            skipped_no_name += 1
            continue
        if not website:
            skipped_no_website += 1
            continue
        valid_companies.append((name, normalize_url(website), "OSM"))

    # Add HERE results (dedup by name match with OSM)
    here_names = set()
    for biz in here_results:
        biz_name = biz["name"]
        biz_website = biz["website"]
        # Dedup against OSM results by checking if name already exists
        name_lower = biz_name.lower().strip()
        if name_lower in here_names:
            continue
        here_names.add(name_lower)
        # Check if a similar name exists in OSM results
        already_have = any(
            name_lower == n.lower().strip() or
            name_lower in n.lower() or n.lower() in name_lower
            for n, _, _ in valid_companies
        )
        if not already_have:
            valid_companies.append((biz_name, biz_website, "HERE"))
            here_count += 1

    if here_count > 0:
        print(f"\n{_INFO} Added {here_count} unique {'business' if here_count == 1 else 'businesses'} from HERE.")

    total_valid = len(valid_companies)
    if total_valid == 0:
        print(f"\n{_WARN} No companies with names and websites found.")
        save_partial_results(csv_filename, [])
        print(f"{_CROSS} Nothing to process.")
        skipped_no_whatsapp = 0
        skipped_no_email = 0
        errors = 0
    else:
        total_valid = len(valid_companies)
        print(f"\n{_INFO} Processing {total_valid} companies across {MAX_WORKERS} parallel workers...\n")

        _target_reached = threading.Event()

        def _worker(c_name, c_website, c_data_source="OSM"):
            """Worker: scrape one company website. Returns (row|None, info_dict)."""
            if target_count > 0 and _target_reached.is_set():
                return None, {"skip": "cancelled"}

            c_domain = urlparse(c_website).netloc
            try:
                c_data = scrape_site(c_website)
                c_emails = c_data["emails"]
                c_sorted = sort_emails_by_relevance(c_emails) if c_emails else []
                c_wa = get_whatsapp_phones(c_data, location)

                if not c_sorted:
                    return None, {"skip": "no_email", "name": c_name, "domain": c_domain}
                if not c_wa:
                    return None, {"skip": "no_whatsapp", "name": c_name, "domain": c_domain}

                c_people = c_data.get("people", [])
                c_person = c_title = c_email = ""
                founder_source = ""
                if c_people:
                    top = c_people[0]
                    c_person = top["name"]
                    c_title = top["title"]
                    founder_source = "website"
                    c_pattern = infer_email_pattern(c_emails, c_domain)
                    c_email = generate_contact_email(c_person, c_domain,
                                                     c_pattern or '{first}.{last}')
                elif c_emails:
                    for em in c_sorted:
                        if em.split('@')[0].lower() not in GENERIC_EMAIL_LOCALS:
                            c_email = em
                            break

                # Try Google search for the founder
                google_result = search_google_founder(c_name)
                if google_result:
                    g_name, g_title = google_result
                    g_rank = _get_title_rank(g_title)
                    current_rank = _get_title_rank(c_title) if c_person else 999
                    if g_rank < current_rank:
                        c_person = g_name
                        c_title = g_title
                        founder_source = "Google search"
                        c_pattern = infer_email_pattern(c_emails, c_domain)
                        c_email = generate_contact_email(
                            c_person, c_domain,
                            c_pattern or '{first}.{last}',
                        )
                    elif not founder_source:
                        founder_source = "website"
                elif not founder_source:
                    founder_source = "none"

                c_phone = c_wa[0][0]
                c_source = c_wa[0][1]
                c_wa_link = f"https://wa.me/{re.sub(r'\D', '', c_phone)}"

                row = {
                    "Company Name": _clean_csv_val(c_name),
                    "Website": _clean_csv_val(c_website),
                    "Source": _clean_csv_val(c_data_source),
                    "WhatsApp Link": _clean_csv_val(c_wa_link),
                    "Phone Number": _clean_csv_val(c_phone),
                    "WhatsApp Source": _clean_csv_val(c_source),
                    "Contact Person": _clean_csv_val(c_person),
                    "Contact Title": _clean_csv_val(c_title),
                    "Contact Email": _clean_csv_val(c_email),
                    "Founder Source": _clean_csv_val(founder_source),
                    "All Emails Found": "; ".join(c_sorted) if c_sorted else "",
                }
                info = {
                    "name": c_name, "domain": c_domain,
                    "n_emails": len(c_sorted),
                    "contact_person": c_person,
                    "contact_title": c_title,
                    "contact_email": c_email,
                    "phone": c_phone,
                    "founder_source": founder_source,
                }
                return row, info

            except Exception as e:
                return None, {"skip": f"error: {e}", "name": c_name, "domain": c_domain}

        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_map = {executor.submit(_worker, n, w, s): (n, w, s)
                              for n, w, s in valid_companies}

                for future in as_completed(future_map):
                    if target_count > 0 and len(results) >= target_count:
                        _target_reached.set()
                        for f in future_map:
                            f.cancel()
                        pending = sum(1 for f in future_map if not f.done())
                        print(f"\n  {_INFO} Reached target of {target_count} successful results."
                              f" {pending} unprocessed candidates remaining.\n")
                        break

                    row, info = future.result()

                    if row is not None:
                        results.append(row)
                        idx = len(results)
                        progress = f"{idx}/{target_count}" if target_count > 0 else str(idx)
                        print(f"  [{progress}] {info['name']}")
                        print(f"         {_ARROW} {info['domain']}")
                        print(f"         {info['n_emails']} email(s) | WhatsApp {_CHECK}"
                              f" | Result {progress}")
                        if info['contact_person']:
                            print(f"         {_ARROW} Person: {info['contact_person']}"
                                  f" ({info['contact_title']})")
                            print(f"         {_ARROW} Email: {info['contact_email']}")
                        print(f"         {_ARROW} Phone: {info['phone']}")

                        if idx % 5 == 0:
                            save_partial_results(csv_filename, results)
                    else:
                        skip = info.get("skip", "unknown")
                        if skip == "no_email":
                            skipped_no_email += 1
                        elif skip == "no_whatsapp":
                            skipped_no_whatsapp += 1
                        elif skip.startswith("error"):
                            errors += 1

                        if skip != "cancelled":
                            print(f"  [-] {info['name']}")
                            print(f"         {_ARROW} {info['domain']}")
                            print(f"         {_WARN} Skipped — {skip.replace('_', ' ')}")

        except KeyboardInterrupt:
            _target_reached.set()

        # Final save
        save_partial_results(csv_filename, results)
        print()

    # Step 4: Summary
    print()
    print(_LINE)
    print(f"  {_CHECK} DONE - Results saved to: {csv_filename}")
    print(_LINE)

    final_results = _dedup_results(results)
    total = len(final_results)
    with_emails = sum(1 for r in final_results if r["All Emails Found"])
    total_emails = sum(len(r["All Emails Found"].split("; ")) for r in final_results if r["All Emails Found"])
    with_whatsapp = total

    print(f"  Total companies with WhatsApp:  {total}")
    print(f"  Companies with emails + WhatsApp: {with_emails}")
    print(f"  Total emails collected:         {total_emails}")
    if skipped_no_name:
        print(f"  Skipped (no name):              {skipped_no_name}")
    if skipped_no_website:
        print(f"  No website listed:              {skipped_no_website}")
    if skipped_no_email:
        print(f"  Skipped (no emails found):      {skipped_no_email}")
    print(f"  Skipped (no WhatsApp found):    {skipped_no_whatsapp}")
    if errors:
        print(f"  Scrape errors:                  {errors}")
    print()

    # Preview
    if final_results:
        print(f"  {_INFO} Preview (first 10 of {len(final_results)} unique companies):")
        print(f"  {'Company Name':<25} {'Contact Person':<22} {'Contact Email':<30}")
        print(f"  {'-'*25} {'-'*22} {'-'*30}")
        for r in final_results[:10]:
            name_trunc = r["Company Name"][:23] + ".." if len(r["Company Name"]) > 23 else r["Company Name"]
            person_trunc = r["Contact Person"][:20] + ".." if len(r["Contact Person"]) > 20 else r["Contact Person"]
            email_trunc = r["Contact Email"][:28] + ".." if len(r["Contact Email"]) > 28 else r["Contact Email"]
            print(f"  {name_trunc:<25} {person_trunc:<22} {email_trunc:<30}")

    print(f"\n  To import into Google Sheets:")
    print(f"    1. Open sheets.google.com and create a new spreadsheet")
    print(f"    2. File > Import > Upload > select the CSV file")
    print(f"    3. Choose 'Replace current sheet' or 'New sheet'")
    print(f"    4. The WhatsApp Link column has clickable hyperlinks")
    print(f"\n  Each result includes a clickable WhatsApp link.")
    print(f"  Open the CSV file in Excel or any spreadsheet app to see the full results.")


if __name__ == "__main__":
    main()
