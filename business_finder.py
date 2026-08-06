"""
Business Finder with Key Person Email
--------------------------------------
Given a business niche/type and a location (city, region, or country),
this script:
1. Finds companies in that niche using OpenStreetMap's Overpass API (free, no key needed).
2. Visits each company's website and extracts:
   - Decision-maker names/titles from team/about pages (CEO, Founder, etc.)
   - Contact email addresses (contact/careers pages)
   - Phone numbers
3. Infers the email pattern from found company emails and generates the
   decision-maker's email address.
4. Keeps only companies that have at least one valid email — website-only
   companies are skipped, so --limit 500 really returns ~500 rows WITH
   emails instead of a handful of emails padded with website-only filler.
5. Saves results to a CSV file with contact info.

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
from urllib.parse import urlparse, urljoin, parse_qs
from email.utils import parseaddr

import html as _html_mod
import requests

# Allow importing the shared email validator from the same folder (same
# pattern as super_clean.py) so the CSV is cleaned with exactly the same
# rules the sender applies before sending.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gmail_email_sender import is_valid_target_email  # noqa: E402

# ─── Configuration ───────────────────────────────────────────────────────────────
# How long to wait between website requests (seconds).
# For high limits (500+), lower this to ~0.2 but be aware sites may rate-limit you.
# Can be overridden via the SCRAPE_DELAY environment variable. A bad value
# falls back to 0.35 instead of crashing at import (matches MAX_WORKERS etc.).


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to the default on missing/bad values."""
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


SCRAPE_DELAY = _env_float("SCRAPE_DELAY", 0.35)

# Maximum number of pages to check per website.
# Emails almost always live on contact/impressum pages, NOT the homepage, so
# we probe more pages than the old default of 6 (which never even reached
# /impressum, /imprint, /careers, /jobs or /kontakt — a big miss, especially
# for EU companies where the email is legally required on /impressum).
# Can be overridden via the MAX_PAGES_PER_SITE environment variable.
MAX_PAGES_PER_SITE = int(os.environ.get("MAX_PAGES_PER_SITE", "15"))

# Maximum concurrent workers for parallel website scraping.
# Default 30 — aggressive parallelization. Lower if you get rate-limited.
# Can be overridden via the MAX_WORKERS environment variable.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "30"))

# When --limit N is set, build a candidate pool of up to N * POOL_MULTIPLIER
# companies. The scraper only finds emails on roughly 15-40% of sites, so a
# 1:1 pool leaves the CSV padded with website-only rows that super_clean later
# deletes — a bigger pool lets the quota be filled with companies that HAVE
# emails. Can be overridden via the POOL_MULTIPLIER environment variable.
POOL_MULTIPLIER = int(os.environ.get("POOL_MULTIPLIER", "4"))

# Maximum companies to process (0 = unlimited). Helps avoid timeouts on large cities.
# Can also be set via --limit CLI argument.
MAX_COMPANIES = 0

# If the strict niche filter yields fewer than this many OSM candidates, a
# relaxed name-substring fallback runs to keep the result volume up.
RELAXED_FALLBACK_MIN = 50

# Only run the heavy catch-all Overpass query when the specific name/tag-based
# queries return fewer than this many candidates. On big regions (whole
# countries, large states) the catch-all can be extremely slow and time out.
CATCHALL_MIN_RESULTS = 100

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

# Known placeholder/fake email domains to exclude.
# NOTE: consumer providers (gmail.com, outlook.com, yahoo.com, ...) are
# deliberately NOT here — small businesses very often list a Gmail address
# on their site, and those are perfectly sendable leads. Filtering them out
# silently threw away a large share of the emails the scraper found.
SPAM_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com",
    "yourdomain.com", "email.com", "mysite.com", "yoursite.com",
    "yourwebsite.com", "mywebsite.com", "placeholder.com", "provider.com",
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
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
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
    # North America
    "1": {"name": "US/Canada", "min_digits": 10},
    "52": {"name": "Mexico", "min_digits": 10},
    # Europe
    "49": {"name": "Germany", "min_digits": 10},
    "44": {"name": "United Kingdom", "min_digits": 9},
    "33": {"name": "France", "min_digits": 9},
    "34": {"name": "Spain", "min_digits": 9},
    "39": {"name": "Italy", "min_digits": 9},
    "31": {"name": "Netherlands", "min_digits": 9},
    "32": {"name": "Belgium", "min_digits": 9},
    "41": {"name": "Switzerland", "min_digits": 9},
    "43": {"name": "Austria", "min_digits": 9},
    "48": {"name": "Poland", "min_digits": 9},
    "46": {"name": "Sweden", "min_digits": 9},
    "47": {"name": "Norway", "min_digits": 8},
    "45": {"name": "Denmark", "min_digits": 8},
    "358": {"name": "Finland", "min_digits": 9},
    "351": {"name": "Portugal", "min_digits": 9},
    "353": {"name": "Ireland", "min_digits": 9},
    "30": {"name": "Greece", "min_digits": 10},
    "36": {"name": "Hungary", "min_digits": 9},
    "40": {"name": "Romania", "min_digits": 9},
    "420": {"name": "Czech Republic", "min_digits": 9},
    "421": {"name": "Slovakia", "min_digits": 9},
    # GCC / Middle East
    "212": {"name": "Morocco", "min_digits": 9},
    "213": {"name": "Algeria", "min_digits": 9},
    "966": {"name": "Saudi Arabia", "min_digits": 9},
    "971": {"name": "UAE", "min_digits": 9},
    "974": {"name": "Qatar", "min_digits": 8},
    "965": {"name": "Kuwait", "min_digits": 8},
    "968": {"name": "Oman", "min_digits": 8},
    "973": {"name": "Bahrain", "min_digits": 8},
    "972": {"name": "Israel", "min_digits": 9},
    "90": {"name": "Turkey", "min_digits": 10},
    "20": {"name": "Egypt", "min_digits": 9},
    # Asia / Pacific
    "91": {"name": "India", "min_digits": 10},
    "61": {"name": "Australia", "min_digits": 9},
    "64": {"name": "New Zealand", "min_digits": 9},
    "65": {"name": "Singapore", "min_digits": 8},
    "60": {"name": "Malaysia", "min_digits": 9},
    "62": {"name": "Indonesia", "min_digits": 9},
    "63": {"name": "Philippines", "min_digits": 10},
    "66": {"name": "Thailand", "min_digits": 9},
    "84": {"name": "Vietnam", "min_digits": 9},
    "81": {"name": "Japan", "min_digits": 10},
    "82": {"name": "South Korea", "min_digits": 9},
    "86": {"name": "China", "min_digits": 11},
    "852": {"name": "Hong Kong", "min_digits": 8},
    "886": {"name": "Taiwan", "min_digits": 9},
    # Africa / South America
    "27": {"name": "South Africa", "min_digits": 9},
    "234": {"name": "Nigeria", "min_digits": 9},
    "254": {"name": "Kenya", "min_digits": 9},
    "55": {"name": "Brazil", "min_digits": 10},
    "54": {"name": "Argentina", "min_digits": 10},
    "56": {"name": "Chile", "min_digits": 9},
    "57": {"name": "Colombia", "min_digits": 10},
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
    # Word-boundary match, not substring: a legit domain like
    # "marketingpros.com" must NOT be dropped, only obvious junk
    # (e.g. tracking.…, newsletter.…, @mailchimp.com).
    if re.search(r'\b(tracking|analytics|marketing|newsletter|mailchimp)\b', domain):
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


# ─── Phone Number Extraction & Normalization ──────────────────────────────────────

# Phone number pattern (international + national formats)
PHONE_RE = re.compile(
    r'(?:(?:\+|00)[1-9][0-9]{0,2}[\s\-/]*(?:\(0\))?[\s\-/]*|0)'
    r'[\s\-/]*\d{2,5}[\s\-/]*\d{2,4}[\s\-/]*\d{2,4}(?:[\s\-/]*\d{2,6})?'
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
    # Check if country code is known and has enough remaining digits.
    # Note: 'digits' has no '+' prefix, so the country code starts at index 0
    # (the earlier digits[1:] indexing made every valid number fail).
    for cc_len in [3, 2, 1]:
        cc = digits[:cc_len]
        if cc in COUNTRY_CODES:
            info = COUNTRY_CODES[cc]
            remaining = len(digits) - cc_len
            if remaining < info["min_digits"]:
                return False
            # NANP (US/Canada) numbers are exactly 10 digits. Anything longer
            # is almost always several numbers scraped as one blob (e.g.
            # "252 026 0925 25" -> +1252026092525, a 13-digit fake).
            if cc == "1":
                return remaining == info["min_digits"]
            # Other numbering plans: at most one extra digit beyond the
            # minimum (e.g. Germany 10-11, Brazil 10-11). Reject blobs.
            return remaining <= info["min_digits"] + 1
    return False


# ─── Site Scraper (Emails + Phones) ─────────────────────────────────────────────

# Contact/about/team page paths probed on every site. Emails almost never live
# on the homepage alone — they sit on contact, impressum, about or team pages.
CONTACT_PATHS = [
    "", "/contact", "/contact-us", "/contactez-nous", "/contacts",
    "/about", "/about-us", "/a-propos", "/qui-sommes-nous",
    "/team", "/our-team", "/meet-the-team", "/notre-equipe",
    "/leadership", "/management", "/founders", "/board", "/executive",
    "/company", "/uber-uns", "/ueber-uns", "/unternehmen",
    "/impressum", "/imprint", "/legal-notice", "/legal",
    "/careers", "/jobs", "/kontakt", "/kontaktformular",
    "/contact.html", "/impressum.html", "/contacto", "/get-in-touch",
]

# Anchor-text/URL hints used to discover real contact pages from the homepage
# (sites name these pages arbitrarily: /company/contact, /kontakt, /contacto...)
CONTACT_LINK_HINTS = (
    "contact", "kontakt", "impressum", "imprint", "about", "team",
    "leadership", "management", "founders", "get in touch", "reach us",
    "write us", "email", "mail", "contactez", "nous contacter",
    "a-propos", "qui sommes", "uber uns", "kontaktformular",
)

# Browser-like UA for sites that block the descriptive BusinessFinder UA
# (very common on GitHub Actions datacenter IPs behind Cloudflare etc.)
_CHROME_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch_page(url: str) -> requests.Response | None:
    """GET a page, retrying with a browser UA when the descriptive UA is blocked."""
    for headers in (HEADERS, _CHROME_UA_HEADERS):
        try:
            resp = requests.get(url, headers=headers, timeout=10)
        except requests.RequestException:
            continue
        if resp.status_code in (403, 406, 429, 503):
            continue  # likely bot-blocked — retry with the browser UA
        return resp
    return None


def _deobfuscate_emails(text: str) -> str:
    """Decode common anti-scraper email obfuscation so EMAIL_RE can match:
    - HTML entities:  info&#64;domain&#46;com  ->  info@domain.com
    - Brackets:       info [at] domain [dot] com
    - Parens:         info(at)domain(dot)com
    - Spaced:         info @ domain . com
    """
    text = _html_mod.unescape(text)
    text = re.sub(r"\s*\[at\]\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(at\)\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\[dot\]\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(dot\)\s*", ".", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+@\s+", "@", text)
    text = re.sub(r"\s+\.\s+", ".", text)
    return text


def _discover_contact_links(home_html: str, base_url: str) -> list:
    """Find internal contact/about/team page links from the homepage HTML."""
    found = []
    seen = set()
    base_host = urlparse(base_url).netloc.lower()
    for m in re.finditer(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        home_html,
        re.IGNORECASE | re.DOTALL,
    ):
        href = m.group(1).strip()
        anchor = re.sub(r"<[^>]+>", " ", m.group(2)).lower()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            abs_url = urljoin(base_url, href)
        except ValueError:
            continue
        if urlparse(abs_url).netloc.lower() != base_host:
            continue  # external link
        hint = f"{href.lower()} {anchor}"
        if not any(h in hint for h in CONTACT_LINK_HINTS):
            continue
        norm = abs_url.rstrip("/")
        if norm not in seen:
            seen.add(norm)
            found.append(norm)
    return found


def scrape_site(base_url: str) -> dict:
    """
    Scrape a website for emails, phone numbers, and decision-maker names/titles.

    Emails are hunted on the homepage plus up to MAX_PAGES_PER_SITE contact /
    about / team / impressum pages — both a fixed list of common paths AND
    real contact-page links discovered from the homepage. Common email
    obfuscation is decoded, and a browser UA is tried when sites block the
    descriptive scraper UA (frequent on GitHub Actions runner IPs).

    Returns:
    {
      "emails": set of email strings,
      "phones": list of normalized phone strings (international format),
      "people": list of dicts with {name, title, score} from most senior first,
    }
    """
    result = {
        "emails": set(),
        "phones": [],
        "people": [],
    }

    base_url = normalize_url(base_url)
    if not base_url:
        return result

    home = base_url.rstrip("/") + "/"

    # Start with the homepage (always fetched first), then contact pages
    # discovered from its links, then the fixed common-path list.
    seen_urls = {home}
    urls = [home]

    home_resp = _fetch_page(home)
    if home_resp is not None and home_resp.status_code == 200:
        home_text = home_resp.text
        for u in _discover_contact_links(home_text, home):
            if u not in seen_urls:
                seen_urls.add(u)
                urls.append(u)

    for p in CONTACT_PATHS:
        u = urljoin(home, p.lstrip("/")).rstrip("/")
        if u not in seen_urls:
            seen_urls.add(u)
            urls.append(u)

    # Cap total pages probed per site (homepage + up to MAX_PAGES_PER_SITE)
    urls = urls[:MAX_PAGES_PER_SITE + 1]

    seen_phones = set()
    all_html_chunks = []

    def _harvest(text: str):
        """Extract emails + phones from one page's HTML into `result`."""
        all_html_chunks.append(text)

        # ── Emails (decode obfuscation first) ──
        raw_emails = EMAIL_RE.findall(_deobfuscate_emails(text))
        for em in raw_emails:
            if is_valid_email(em) and not is_spam_or_irrelevant(em):
                result["emails"].add(em.lower())

        # ── Phone numbers ──
        raw_phones = PHONE_RE.findall(text)
        for p in raw_phones:
            normalized = normalize_phone(p)
            if normalized and normalized not in seen_phones:
                if normalized.startswith('+') and not is_valid_phone(normalized):
                    continue
                seen_phones.add(normalized)
                result["phones"].append(normalized)

    # The homepage was already fetched above for link discovery — reuse its
    # HTML for email/phone extraction instead of fetching it a second time.
    if home_resp is not None and home_resp.status_code == 200:
        _harvest(home_resp.text)

    for page_url in urls[1:]:
        try:
            resp = _fetch_page(page_url)
            if resp is None or resp.status_code != 200:
                continue
            _harvest(resp.text)
        except requests.RequestException:
            continue

        time.sleep(SCRAPE_DELAY)

    # ── People detection (once, using all collected HTML) ──
    all_html = '\n'.join(all_html_chunks)
    result["people"] = extract_people_from_html(all_html)

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

    # Multi-word UI/navigation phrases that are not people
    "about us", "view all", "our team", "meet the team", "our story",
    "learn more", "read more", "get in touch", "our mission",
    "our vision", "our values", "our clients", "our partners",
    "our work", "our approach", "our people", "our company",
    "our products", "our services", "team member", "get started",
    "sign up", "our firm", "our family", "meet our team",
    "our leadership", "our phoenix", "our solutions",
}

# Single words that never belong in a person name (UI labels, department
# headings, org suffixes). Used to reject junk like "Additive Manufacturing"
# or "Kino Catechetical Institute" that the name detector picks up.
NON_NAME_WORDS = {
    "about", "us", "view", "all", "our", "team", "story", "learn",
    "more", "read", "get", "touch", "mission", "vision", "values",
    "clients", "partners", "work", "approach", "people", "leadership",
    "company", "products", "services", "contact", "careers", "jobs",
    "login", "sign", "register", "news", "blog", "portfolio",
    "projects", "solutions", "manufacturing", "engineering",
    "department", "division", "gallery", "menu", "home", "join",
    "apply", "privacy", "terms", "cookies", "search", "navigation",
    "footer", "header", "powered", "copyright", "institute",
    "university", "college", "school", "center", "centre",
    "foundation", "association", "council", "society", "authority",
    "ministry", "laboratory", "corporate", "office", "group",
    "limited", "gmbh", "llc", "inc", "company", "ltd", "plc",
    "systems", "technologies", "technology", "software", "consulting",
    "research", "state", "additive", "catechetical", "phoenix",
    "global", "international", "digital", "cloud", "network", "data",
}


def _is_real_person_name(name: str) -> bool:
    """Reject UI labels and department/org headings passed off as names."""
    name_lower = name.lower().strip()
    if name_lower in NON_NAMES:
        return False
    words = name_lower.split()
    if not words or len(words) > 3:
        return False
    return not any(w in NON_NAME_WORDS for w in words)


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
            if _is_real_person_name(name) and name_lower not in seen_names:
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
            if _is_real_person_name(name) and name_lower not in seen_names:
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
            if _is_real_person_name(name) and name_lower not in seen_names:
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


# ─── Google Organic Business Discovery (free, no API key) ───────────────────────
# Searches Google for "{niche} in {location}" and extracts business names + websites
# from the organic search results. This is free but needs polite rate-limiting.
# Google may block aggressive scraping — the function uses a 2-second delay.

_GOOGLE_BIZ_LOCK = threading.Lock()
_GOOGLE_BIZ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# Regex to extract business names and websites from Google SERP
# Google SERP structure varies, but business listings often appear in specific patterns
# (Removed — unused; business extraction uses h3_pattern below)


def search_google_businesses(niche: str, location: str, limit: int = 50) -> list:
    """
    Search Google for '{niche} in {location}' and extract business names + websites.

    This is a free, no-API-key alternative to Yelp/HERE for discovering businesses.
    Uses a global lock to be polite (max 1 Google search per 2 seconds).
    Returns a list of dicts: {name, website, source: "Google"}.

    Note: Google may block excessive scraping. This is best-effort.
    The function extracts from organic search results and Google Maps panels.
    """
    results = []
    seen_domains = set()

    # Short/ambiguous niches (e.g. "IT", "AI", "SEO") return unrelated
    # businesses when searched bare. Add a "companies" variant first to
    # disambiguate (e.g. "IT companies in Paris").
    queries = []
    if len(niche.strip()) <= 4:
        queries += [
            f"{niche} companies in {location}",
            f"{niche} companies {location}",
            f"best {niche} companies in {location}",
            f"top {niche} companies in {location}",
        ]
    queries += [
        f"{niche} in {location}",
        f"{niche} {location}",
        f"best {niche} in {location}",
        f"top {niche} in {location}",
    ]

    for query in queries:
        with _GOOGLE_BIZ_LOCK:
            time.sleep(2.0)  # Polite delay — 2 seconds between Google queries
            try:
                resp = requests.get(
                    "https://www.google.com/search",
                    params={"q": query, "hl": "en", "num": 10},
                    headers=_GOOGLE_BIZ_HEADERS,
                    timeout=10,
                )
            except requests.RequestException:
                continue

        if resp.status_code != 200:
            continue

        html = resp.text

        # Strategy 1: Extract from organic results (h3 tags with links)
        # Google wraps result titles in <h3> with <a> inside
        h3_pattern = re.compile(
            r'<h3[^>]*>\s*<a[^>]*href="(https?://[^"\s]+)"[^>]*>\s*([^<]{3,})\s*</a>\s*</h3>',
            re.IGNORECASE
        )
        for match in h3_pattern.finditer(html):
            url = match.group(1)
            raw_name = match.group(2).strip()
            name = raw_name.replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"')
            domain = urlparse(url).netloc.lower()
            if any(skip in domain for skip in ["google.com", "youtube.com", "facebook.com",
                                                "instagram.com", "twitter.com", "linkedin.com",
                                                "pinterest.com", "yelp.com", "maps.google.com"]):
                continue
            if domain in seen_domains or not name:
                continue
            seen_domains.add(domain)
            results.append({
                "name": name,
                "website": normalize_url(url),
                "source": "Google",
            })
            if len(results) >= limit:
                break

        if len(results) >= limit:
            break

    return results


# ─── DuckDuckGo Organic Business Discovery (free, no API key) ────────────────────
# Google frequently blocks datacenter IPs (GitHub Actions runners). DuckDuckGo's
# HTML endpoint is a reliable fallback that needs no API key.


def search_duckduckgo_businesses(niche: str, location: str, limit: int = 100) -> list:
    """
    Search DuckDuckGo HTML for '{niche} in {location}' and extract business
    names + websites from the organic results.

    Returns a list of dicts: {name, website, source: "DuckDuckGo"}.
    Best-effort — DDG may throttle or return no usable results.
    """
    results = []
    seen_domains = set()

    queries = [
        f"{niche} companies in {location}",
        f"{niche} in {location}",
        f"best {niche} companies in {location}",
        f"top {niche} in {location}",
    ]

    for query in queries:
        try:
            resp = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=_GOOGLE_BIZ_HEADERS,
                timeout=10,
            )
        except requests.RequestException:
            continue

        if resp.status_code != 200:
            continue

        # DDG HTML results: <a rel="nofollow" class="result__a" href="...">Title</a>
        # Links are wrapped in a redirect: //duckduckgo.com/l/?uddg=<encoded-url>
        for m in re.finditer(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            resp.text,
            re.IGNORECASE | re.DOTALL,
        ):
            href = m.group(1)
            raw_name = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            name = _html_mod.unescape(raw_name).strip()
            if "uddg=" in href:
                # parse_qs already URL-decodes the uddg value — do NOT unquote
                # again or percent-encoded URLs get corrupted (double-decode).
                qs = parse_qs(urlparse(href).query)
                href = qs.get("uddg", [href])[0] if qs else href
            domain = urlparse(href).netloc.lower()
            if not domain or not name:
                continue
            if any(skip in domain for skip in ["google.com", "youtube.com", "facebook.com",
                                                "instagram.com", "twitter.com", "linkedin.com",
                                                "pinterest.com", "yelp.com", "duckduckgo.com"]):
                continue
            if domain in seen_domains:
                continue
            seen_domains.add(domain)
            results.append({
                "name": name,
                "website": normalize_url(href),
                "source": "DuckDuckGo",
            })
            if len(results) >= limit:
                break

        if len(results) >= limit:
            break

    return results


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


# ─── Smart Niche → OSM Tag Mapping ─────────────────────────────────────────────
# Maps common niche keywords to exact OSM tag:value pairs so the Overpass query
# catches businesses even when the niche word isn't in their name.
# E.g. "fitness gym" -> amenity=gym, leisure=fitness_centre, leisure=sports_centre
# Add more mappings as needed for your niches.
NICHE_OSM_TAGS = {
    # Fitness & Health
    "fitness": ["leisure=fitness_centre", "leisure=sports_centre", "amenity=gym", "sport=fitness"],
    "gym": ["leisure=fitness_centre", "amenity=gym"],
    "fitness gym": ["leisure=fitness_centre", "amenity=gym", "leisure=sports_centre"],
    "gymnasium": ["leisure=fitness_centre", "amenity=gym"],
    "yoga": ["leisure=fitness_centre", "amenity=gym", "leisure=yoga"],
    "yoga studio": ["leisure=fitness_centre", "amenity=gym"],
    "pilates": ["leisure=fitness_centre", "amenity=gym"],
    "crossfit": ["leisure=fitness_centre", "sport=crossfit"],
    "personal trainer": ["leisure=fitness_centre", "sport=personal_trainer"],

    # Food & Dining
    "restaurant": ["amenity=restaurant", "amenity=fast_food", "amenity=food_court"],
    "cafe": ["amenity=cafe", "amenity=coffee_shop", "shop=coffee"],
    "coffee shop": ["amenity=cafe", "shop=coffee"],
    "bakery": ["shop=bakery", "amenity=bakery"],
    "pizza": ["amenity=restaurant", "amenity=fast_food", "cuisine=pizza"],
    "bar": ["amenity=bar", "amenity=pub", "amenity=nightclub"],
    "pub": ["amenity=pub", "amenity=bar"],
    "fast food": ["amenity=fast_food"],
    "ice cream": ["amenity=ice_cream", "shop=ice_cream"],

    # Health & Medical
    "dentist": ["amenity=dentist", "healthcare=dentist"],
    "doctor": ["amenity=doctors", "healthcare=doctor"],
    "clinic": ["amenity=clinic", "healthcare=clinic"],
    "hospital": ["amenity=hospital", "healthcare=hospital"],
    "pharmacy": ["amenity=pharmacy", "healthcare=pharmacy"],
    "optician": ["shop=optician", "healthcare=optometrist"],
    "physiotherapist": ["healthcare=physiotherapist", "amenity=physiotherapist"],
    "veterinary": ["amenity=veterinary", "healthcare=veterinary"],

    # Beauty & Personal Care
    "beauty salon": ["shop=hairdresser", "shop=beauty", "shop=cosmetics"],
    "hairdresser": ["shop=hairdresser", "shop=barber"],
    "barber": ["shop=barber", "shop=hairdresser"],
    "nail salon": ["shop=beauty", "shop=cosmetics"],
    "spa": ["shop=beauty", "leisure=spa", "amenity=spa"],
    "massage": ["shop=massage", "leisure=massage", "amenity=massage"],

    # Accommodation
    "hotel": ["tourism=hotel", "tourism=guest_house", "tourism=hostel", "tourism=motel"],
    "hostel": ["tourism=hostel"],
    "motel": ["tourism=motel"],
    "guest house": ["tourism=guest_house"],
    "bed and breakfast": ["tourism=guest_house", "tourism=bed_and_breakfast"],
    "apartment": ["tourism=apartment", "tourism=apart_hotel"],

    # Real Estate & Property
    "real estate": ["office=real_estate", "office=estate_agent", "shop=estate_agent"],
    "real estate agent": ["office=real_estate", "office=estate_agent"],
    "property": ["office=real_estate", "office=property_management"],
    "property management": ["office=property_management"],
    "architect": ["office=architect", "shop=architect"],

    # Automotive
    "car rental": ["amenity=car_rental", "shop=car_rental"],
    "car dealer": ["shop=car", "shop=car_dealer"],
    "car repair": ["shop=car_repair", "amenity=car_repair"],
    "auto repair": ["shop=car_repair", "amenity=car_repair"],
    "mechanic": ["shop=car_repair", "amenity=car_repair"],
    "car wash": ["amenity=car_wash", "shop=car_wash"],
    "gas station": ["amenity=fuel", "shop=fuel"],
    "petrol station": ["amenity=fuel"],
    "parking": ["amenity=parking"],
    "taxi": ["amenity=taxi", "shop=taxi"],

    # Tech & Business Services
    "it": ["office=it", "office=software", "office=technology",
           "office=telecommunication", "office=web_design", "office=research"],
    "it company": ["office=it", "office=software", "office=technology"],
    "it services": ["office=it", "office=software", "office=technology"],
    "it consulting": ["office=it", "office=software", "office=consulting"],
    "tech": ["office=technology", "office=it", "office=software"],
    "tech company": ["office=technology", "office=it", "office=software"],
    "technology": ["office=technology", "office=it", "office=software"],
    "technology company": ["office=technology", "office=it", "office=software"],
    "software": ["office=software", "office=it"],
    "software company": ["office=software", "office=it"],
    "software development": ["office=software", "office=it"],
    "software developer": ["office=software", "office=it"],
    "web development": ["office=software", "office=web_design", "office=it"],
    "web design": ["office=web_design", "office=software", "office=it"],
    "web developer": ["office=web_design", "office=software", "office=it"],
    "app development": ["office=software", "office=it"],
    "mobile development": ["office=software", "office=it"],
    "programming": ["office=software", "office=it"],
    "developer": ["office=software", "office=it"],
    "computer": ["shop=computer", "office=it", "shop=electronics"],
    "computer services": ["office=it", "shop=computer"],
    "computer repair": ["shop=computer", "shop=electronics"],
    "cybersecurity": ["office=it", "office=security"],
    "cloud": ["office=it", "office=software"],
    "cloud computing": ["office=it", "office=software"],
    "data": ["office=it", "office=software"],
    "telecom": ["office=telecommunication", "office=it"],
    "telecommunications": ["office=telecommunication", "office=it"],
    "e-commerce": ["shop=electronics", "office=it", "office=software"],
    "ecommerce": ["shop=electronics", "office=it", "office=software"],
    "startup": ["office=startup", "office=company"],
    "coworking": ["amenity=coworking", "office=coworking"],
    "marketing agency": ["office=marketing", "office=advertising"],
    "advertising": ["office=advertising", "office=marketing"],
    "consulting": ["office=consulting", "office=management_consulting"],
    "law firm": ["office=lawyer", "office=attorney", "office=legal"],
    "lawyer": ["office=lawyer", "office=attorney", "office=legal"],
    "accountant": ["office=accountant", "office=accounting"],
    "insurance": ["office=insurance", "shop=insurance"],
    "bank": ["amenity=bank", "office=bank"],
    "financial": ["office=financial", "office=insurance", "amenity=bank"],
    "recruitment": ["office=recruitment", "office=employment_agency"],
    "employment agency": ["office=employment_agency", "office=recruitment"],

    # Education
    "school": ["amenity=school", "amenity=college", "amenity=university"],
    "university": ["amenity=university", "amenity=college"],
    "college": ["amenity=college", "amenity=university"],
    "language school": ["amenity=language_school", "office=language_school"],
    "driving school": ["amenity=driving_school"],
    "kindergarten": ["amenity=kindergarten", "amenity=nursery"],
    "tutoring": ["amenity=tutoring", "office=tutoring"],
    "training center": ["amenity=training_center", "office=training"],

    # Entertainment & Culture
    "cinema": ["amenity=cinema", "leisure=cinema"],
    "theatre": ["amenity=theatre", "leisure=theatre"],
    "museum": ["tourism=museum", "amenity=museum"],
    "nightclub": ["amenity=nightclub", "leisure=nightclub"],
    "bowling": ["leisure=bowling_alley", "amenity=bowling_alley"],
    "amusement park": ["leisure=amusement_park", "tourism=theme_park"],

    # Shopping & Retail
    "supermarket": ["shop=supermarket", "shop=grocery"],
    "grocery": ["shop=grocery", "shop=supermarket", "shop=convenience"],
    "convenience store": ["shop=convenience", "shop=grocery"],
    "clothing store": ["shop=clothes", "shop=fashion"],
    "bookstore": ["shop=books", "shop=stationery"],
    "electronics store": ["shop=electronics", "shop=computer"],
    "furniture store": ["shop=furniture", "shop=home_improvement"],
    "hardware store": ["shop=hardware", "shop=doityourself"],
    "pharmacy": ["shop=chemist", "amenity=pharmacy"],
    "gift shop": ["shop=gift", "shop=novelty"],
    "jewelry": ["shop=jewelry", "shop=watches"],
    "shoe store": ["shop=shoes", "shop=clothes"],
    "sporting goods": ["shop=sports", "shop=outdoor"],
    "department store": ["shop=department_store", "shop=mall"],

    # Professional Services
    "photographer": ["shop=photographer", "office=photographer"],
    "travel agency": ["shop=travel_agency", "office=travel_agent"],
    "printing": ["shop=copying", "shop=printing"],
    "laundry": ["shop=laundry", "shop=dry_cleaning", "amenity=laundry"],
    "dry cleaning": ["shop=dry_cleaning", "shop=laundry"],
    "moving": ["shop=moving", "office=moving"],
    "electrician": ["office=electrician", "shop=electrician"],
    "plumber": ["office=plumber", "shop=plumber"],

    # Logistics & Transport
    "logistics": ["office=logistics", "office=transport", "amenity=logistics"],
    "courier": ["office=courier", "amenity=courier"],
    "delivery": ["office=delivery", "amenity=delivery"],
    "freight": ["office=freight", "office=logistics"],
    "warehouse": ["building=warehouse", "office=warehouse"],
    "shipping": ["office=shipping", "office=logistics"],

    # Home Services
    "cleaning": ["office=cleaning", "shop=cleaning"],
    "gardening": ["shop=garden_centre", "office=gardening"],
    "landscaping": ["office=landscaping", "office=gardening"],
    "construction": ["office=construction", "building=construction"],
    "contractor": ["office=contractor", "office=construction"],
    "renovation": ["office=renovation", "office=construction"],
    "interior design": ["office=interior_design", "shop=interior_design"],
    "pest control": ["office=pest_control", "shop=pest_control"],
    "security": ["office=security", "shop=security"],

    # Events & Hospitality
    "event planning": ["office=event_planner", "office=events"],
    "wedding planner": ["office=wedding_planner", "office=event_planner"],
    "catering": ["amenity=catering", "office=catering"],
    "food truck": ["amenity=food_truck", "amenity=fast_food"],
    "party rental": ["shop=party_supplies", "office=party_rental"],
}

def get_niche_tags(niche: str) -> list:
    """Return the deduplicated list of 'key=value' OSM tags mapped for a niche.

    Checks the full niche phrase first, then each individual keyword, so
    e.g. "fitness gym" matches leisure=fitness_centre, amenity=gym, ...
    """
    niche_lower = niche.lower().strip()
    tags = []

    # Check full niche phrase first
    if niche_lower in NICHE_OSM_TAGS:
        tags.extend(NICHE_OSM_TAGS[niche_lower])

    # Check individual keywords
    keywords = extract_niche_keywords(niche)
    for kw in keywords:
        if kw in NICHE_OSM_TAGS:
            for tag in NICHE_OSM_TAGS[kw]:
                if tag not in tags:
                    tags.append(tag)

    return tags


def get_osm_tag_queries(niche: str, bbox_coords) -> str:
    """Build tag-specific Overpass sub-queries from the niche mapping."""
    tags = get_niche_tags(niche)

    if not tags:
        return ""

    south, north, west, east = bbox_coords
    lines = []
    for tag_val in tags:
        if "=" in tag_val:
            tag_key, tag_value = tag_val.split("=", 1)
            lines.append(f'      node["{tag_key}"="{tag_value}"]({south},{west},{north},{east});')
            lines.append(f'      way["{tag_key}"="{tag_value}"]({south},{west},{north},{east});')
            lines.append(f'      relation["{tag_key}"="{tag_value}"]({south},{west},{north},{east});')

    return "\n".join(lines)


# Extra name keywords that make a company relevant to a niche even when the
# niche word itself doesn't appear in the company name. E.g. for "IT" this
# keeps "Tech Solutions" and "Data Systems GmbH" in the results.
NICHE_NAME_TERMS = {
    "it": ["tech", "technology", "technologies", "software", "systems",
           "data", "cyber", "computer", "network", "cloud", "telecom",
           "programming", "information", "edv", "informatik", "softwarehaus"],
    "it company": ["tech", "software", "systems", "data", "cyber",
                    "computer", "network", "cloud", "programming", "information",
                    "edv", "informatik", "softwarehaus"],
    "it services": ["tech", "software", "systems", "data", "cyber",
                     "computer", "network", "cloud", "programming", "information",
                     "edv", "informatik", "softwarehaus"],
    "software": ["software", "tech", "digital", "systems", "app", "programming"],
    "software company": ["software", "tech", "digital", "systems", "app"],
    "software development": ["software", "tech", "digital", "systems", "app"],
    "tech": ["technology", "tech", "software", "digital"],
    "technology": ["technology", "tech", "software", "digital"],
    "technology company": ["technology", "tech", "software", "digital"],
    "computer": ["computer", "tech", "it", "repair", "services"],
    "web development": ["web", "software", "tech", "digital", "design"],
    "web design": ["web", "design", "digital", "tech"],
    "app development": ["app", "software", "mobile", "tech"],
    "mobile development": ["mobile", "app", "software", "tech"],
    "programming": ["programming", "software", "tech", "code"],
    "developer": ["developer", "software", "tech", "code", "programming"],
    "data": ["data", "analytics", "science", "tech"],
    "cybersecurity": ["cyber", "security", "tech", "it"],
    "cloud": ["cloud", "tech", "software", "it"],
    "cloud computing": ["cloud", "tech", "software", "it"],
    "telecom": ["telecom", "telecommunication", "communication", "network"],
    "telecommunications": ["telecom", "telecommunication", "communication", "network"],
    "e-commerce": ["ecommerce", "e-commerce", "online", "store", "shop", "web"],
    "ecommerce": ["ecommerce", "e-commerce", "online", "store", "shop", "web"],
    "startup": ["ventures", "labs", "capital", "partners", "tech", "software",
                "digital", "innovation"],
}


def is_relevant_to_niche(name: str, tags: dict, niche: str) -> bool:
    """Decide whether an OSM element actually matches the requested niche.

    Returns True when ANY of these hold:
      1. The element carries a tag from the niche->OSM tag mapping
         (e.g. office=it for the "IT" niche).
      2. The element's name contains a niche keyword as a whole word
         (word-boundary aware, so "IT" doesn't match "Fitness").
      3. The element's name contains a related term for the niche
         (e.g. "Tech Solutions" for the "IT" niche).

    This is the specificity filter that stops the broad catch-all Overpass
    query from returning restaurants, barbers and other unrelated businesses
    when you search for a niche like "IT".
    """
    tags = tags or {}
    name_lower = (name or "").lower()
    niche_lower = niche.lower().strip()

    # 1) Tag match against the niche mapping (most reliable signal).
    #    office=company is deliberately neutral: in OSM it tags "any company",
    #    so it must NOT by itself pass the filter (otherwise the catch-all
    #    query would let every generic business through, e.g. for "startup").
    for tag_val in get_niche_tags(niche):
        if "=" in tag_val:
            tag_key, tag_value = tag_val.split("=", 1)
            if tag_key == "office" and tag_value == "company":
                continue
            if (tags.get(tag_key) or "").lower() == tag_value:
                return True

    if not name_lower:
        return False

    # 2) Whole-word match on any niche keyword.
    #    Short acronym keywords ("it", "ai", "hr") are ordinary English words
    #    too ("Didn't Do It Bail Bonds" contains "It") — for keywords of
    #    2 chars or fewer, only trust them at the START of the company name
    #    (e.g. "IT Solutions", "IT-Consulting"). 3+ char keywords ("gym",
    #    "bar", "spa") still match anywhere as a whole word so legit
    #    mid-name matches like "Fit Gym" survive.
    for kw in extract_niche_keywords(niche):
        if len(kw) <= 2:
            if re.search(rf"^{re.escape(kw)}(?=[\s\-]|$)", name_lower):
                return True
        elif re.search(rf"\b{re.escape(kw)}\b", name_lower):
            return True

    # 3) Whole-word match on any related term for the niche
    for term in NICHE_NAME_TERMS.get(niche_lower, []):
        if re.search(rf"\b{re.escape(term)}\b", name_lower):
            return True

    return False


def is_relaxed_match(name: str, niche: str) -> bool:
    """Looser relevance check used ONLY when the strict filter yields too few
    results (e.g. sparsely-tagged US cities). Accepts companies whose name
    contains a niche keyword (len >= 3) or a related term (len >= 3) as a
    substring — so "Acme Technologies" passes for the "IT" niche. Short terms
    (like "it") are excluded so names such as "Fitness" or "Digital" never
    flood back in.
    """
    name_lower = (name or "").lower()
    if not name_lower:
        return False

    for kw in extract_niche_keywords(niche):
        if len(kw) >= 3 and kw in name_lower:
            return True

    for term in NICHE_NAME_TERMS.get(niche.lower().strip(), []):
        if len(term) >= 3 and term in name_lower:
            return True

    return False


# ─── Overpass Query Builder (Dynamic — works for ANY niche) ──────────────────────

def build_overpass_queries(bbox: list, niche: str) -> tuple:
    """
    Build dynamic Overpass queries for a generic business niche.

    Uses a multi-phase approach with smart tag mapping:
    Phase 0: Tag-specific queries from niche→OSM tag mapping (most precise)
    Phase 1: Name-based matching + tag-value matching across all business types
    Phase 2: Broad catch-all for any element with name+website in the bbox
            (only runs when Phase 1 under-delivers, to avoid server timeouts
            on large regions)

    The niche string is split into keywords, and the query matches:
    - Businesses whose OSM TAG:VALUE matches the niche (e.g., leisure=fitness_centre for "fitness")
    - Businesses whose NAME contains any niche keyword
    - Businesses with relevant industry tags
    - Any element with a name+website in the bbox (broadest possible)
    """
    south, north, west, east = bbox

    # Extract keywords from the niche string
    keywords = extract_niche_keywords(niche)
    if not keywords:
        raise ValueError(f"No valid keywords extracted from niche: '{niche}'")

    # Build regex patterns
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
                        continue
                    if status == 406 and attempt == 2:
                        print(f"  {_WARN} Endpoint {endpoint} refused both User-Agents (406)")
                        last_error = e
                        break
                    if status:
                        print(f"  {_WARN} Endpoint {endpoint} failed: {e}")
                        print(f"        Status: {status}")
                    else:
                        print(f"  {_WARN} Endpoint {endpoint} failed: {e}")
                    last_error = e
                    break

        raise RuntimeError(f"All {len(OVERPASS_ENDPOINTS)} Overpass endpoints failed: {last_error}")

    # ── Phase 0: Tag-specific queries from smart niche mapping ──
    # This catches businesses even when their name doesn't contain the niche word
    tag_query = get_osm_tag_queries(niche, bbox)
    if tag_query:
        print(f"  {_ARROW} Searching with smart tag mapping for '{niche}'...")
    else:
        print(f"  {_ARROW} No tag mapping found for '{niche}' — using name-based search.")

    # ── Phase 1: Name-based + tag-value matching ──
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

      // Direct tag-value matching
      node[~"^(amenity|shop|office|leisure|sport|healthcare|tourism|catering)$"~"{name_pattern}",i]({south},{west},{north},{east});
      way[~"^(amenity|shop|office|leisure|sport|healthcare|tourism|catering)$"~"{name_pattern}",i]({south},{west},{north},{east});

      // Any company offices
      node["office"="company"]({south},{west},{north},{east});
      way["office"="company"]({south},{west},{north},{east});
      relation["office"="company"]({south},{west},{north},{east});

      // Tag-specific queries from smart mapping
      {tag_query}
    """

    # ── Phase 2: Broad catch-all — ONLY when specific queries under-deliver ──
    # On big regions (whole countries, large states) an unconditional catch-all
    # makes the Overpass server time out. So it now runs only if Phase 1 did
    # not find enough candidates, and it no longer scans buildings/landuse
    # (those rarely have company websites and only add server load).
    catchall_query = f"""
      // ANY element with a name AND website tag
      node["name"]["website"~"."]({south},{west},{north},{east});
      way["name"]["website"~"."]({south},{west},{north},{east});
      relation["name"]["website"~"."]({south},{west},{north},{east});

      // ANY element with name and contact:website
      node["name"]["contact:website"~"."]({south},{west},{north},{east});
      way["name"]["contact:website"~"."]({south},{west},{north},{east});
      relation["name"]["contact:website"~"."]({south},{west},{north},{east});
    """

    # Phase 1 first — the specific, targeted query
    print(f"     Running name/tag-based queries...")
    try:
        elements = _run_overpass(name_based_query)
    except Exception as e:
        print(f"  {_WARN} Phase 1 query failed: {e}")
        elements = []
    print(f"\n{_INFO} Found {len(elements)} candidates from name/tag-based queries.")

    # Only fall back to the heavy catch-all when Phase 1 under-delivered
    if len(elements) < CATCHALL_MIN_RESULTS:
        print(f"     Only {len(elements)} candidates — running broader catch-all query...")
        try:
            extra_elements = _run_overpass(catchall_query)
        except Exception as e:
            print(f"  {_WARN} Catch-all query failed: {e}")
            extra_elements = []

        # Deduplicate by element id
        seen_ids = {(el.get("type", ""), el.get("id")) for el in elements}
        for el in extra_elements:
            key = (el.get("type", ""), el.get("id"))
            if key not in seen_ids:
                seen_ids.add(key)
                elements.append(el)
    else:
        print(f"     Enough candidates from specific queries — skipping heavy catch-all.")

    print(f"{_INFO} Total candidates after all queries: {len(elements)}")

    return elements


# ─── CSV Writer (incremental) ────────────────────────────────────────────────────

# Clean, Google Sheets-friendly column headers
CSV_FIELDS = [
    "Company Name",
    "Website",
    "Source",
    "Phone Number",
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
    - Projects each row onto the CSV columns: rows carry internal sort keys
      (e.g. "__tier") that DictWriter would reject as unknown fields.
    """
    results = _dedup_results(results)

    try:
        with open(filename, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            rows = [{k: v for k, v in r.items() if k in CSV_FIELDS} for r in results]
            writer.writerows(rows)
    except IOError as e:
        print(f"  {_WARN} Could not save intermediate results: {e}")


# ─── Signal Handler ──────────────────────────────────────────────────────────────

def signal_handler(sig, frame):
    print(f"\n\n{_WARN} Interrupted by user. Saving results so far...")
    raise KeyboardInterrupt()


# ─── Main ────────────────────────────────────────────────────────────────────────

def csv_filename_for(niche: str, location: str) -> str:
    """Build the CSV filename for a niche + location, exactly as run_location saves it.

    Kept as its own function so other scripts (e.g. the multi-city runner) can
    predict the output filename and skip cities whose CSV already exists.
    """
    clean_location = re.sub(r'[\\/*?:"<>|, ]', '_', location).strip('_')
    clean_niche = re.sub(r'[\\/*?:"<>|, ]', '_', niche).strip('_')
    return f"businesses_{clean_niche}_{clean_location}.csv"


def run_location(niche: str, location: str, max_companies: int = 0) -> str:
    """Run the full business-finder pipeline for ONE location.

    Geocodes the location, discovers candidate businesses via OpenStreetMap /
    HERE / Google / DuckDuckGo, scrapes each website for emails, phones and
    decision-makers, and writes the results to a CSV named after the niche
    and location (only companies with at least one valid email are kept).

    ``max_companies`` is the target row count FOR THIS location (0 = no limit).
    Returns the CSV filename that was written.
    """
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
    if max_companies > 0:
        print(f"  Max companies: {max_companies}")
    print(f"{_LINE}\n")

    # Step 1: Geocode
    # NOTE: failures raise instead of sys.exit() so callers (e.g. the
    # multi-city runner) can catch them and continue with the next city.
    print(f"{_ARROW} Geocoding location...")
    try:
        bbox = geocode(location)
    except (RuntimeError, ValueError) as e:
        raise RuntimeError(f"Geocoding failed for '{location}': {e}")

    # Step 2: Query Overpass (dynamic — works for any niche)
    print(f"\n{_ARROW} Querying OpenStreetMap for '{niche}' businesses (up to 180 seconds)...")
    try:
        elements = build_overpass_queries(bbox, niche)
    except (RuntimeError, ValueError) as e:
        raise RuntimeError(f"Overpass query failed for '{location}': {e}")

    target_count = max_companies
    print(f"\n{_INFO} Found {len(elements)} candidate businesses on OpenStreetMap.")
    if target_count > 0:
        print(f"       Targeting {target_count} results — only companies")
        print(f"       with at least one valid email are kept.")
    print()

    # Step 3: Search HERE API (if API key is available — 30k free req/mo)
    here_results = here_search(
        niche, location,
        limit=min(500, (target_count or 100) * POOL_MULTIPLIER),
    )

    # Step 3b: Google organic search (free, no API key needed)
    print(f"\n{_ARROW} Searching Google for '{niche}' businesses (organic results)...")
    google_results = search_google_businesses(
        niche, location,
        limit=max(150, (target_count or 150) * POOL_MULTIPLIER),
    )
    if google_results:
        print(f"  {_INFO} Google found {len(google_results)} potential businesses.")
    else:
        print(f"       No businesses found via Google search.")

    # Step 3c: DuckDuckGo fallback (free, no API key) — Google frequently
    # blocks datacenter IPs (GitHub Actions runners), so DDG keeps the
    # candidate pool full when Google comes back empty.
    ddg_results = []
    if len(google_results) < 60:
        print(f"\n{_ARROW} Searching DuckDuckGo for '{niche}' businesses (fallback)...")
        ddg_results = search_duckduckgo_businesses(
            niche, location,
            limit=max(150, (target_count or 150) * POOL_MULTIPLIER),
        )
        if ddg_results:
            print(f"  {_INFO} DuckDuckGo found {len(ddg_results)} potential businesses.")
        else:
            print(f"       No businesses found via DuckDuckGo search.")

    # Step 4: Scrape websites for emails, phones, and key people
    results = []
    skipped_no_name = 0
    skipped_no_website = 0
    skipped_no_email = 0
    errors = 0
    here_count = 0
    google_count = 0

    # Build CSV filename: {niche}_{location}.csv
    csv_filename = csv_filename_for(niche, location)

    # Pre-filter: cheap checks (no name, no website) done sequentially first
    valid_companies = []

    # Add OSM results — but ONLY companies that are relevant to the requested
    # niche (tag match, whole-word name match, or related term match). This
    # stops the broad catch-all Overpass query from adding restaurants,
    # barbers and other unrelated businesses when you search a niche like "IT".
    skipped_not_relevant = 0
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
        # Skip social-media-only / directory "websites" — they are not real
        # company sites and scraping them yields no usable contact data.
        website_domain = urlparse(website).netloc.lower()
        if any(s in website_domain for s in (
            "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
            "x.com", "youtube.com", "tiktok.com", "yelp.com",
            "foursquare.com", "google.com",
        )):
            skipped_no_website += 1
            continue
        if not is_relevant_to_niche(name, tags, niche):
            skipped_not_relevant += 1
            continue
        valid_companies.append((name, normalize_url(website), "OSM"))

    # Fallback: if the strict filter left very few OSM candidates (common in
    # sparsely-tagged cities like Seattle), run a relaxed pass that accepts
    # companies whose name contains a niche keyword or related term as a
    # substring (not just whole-word). Keeps volume up without letting
    # clearly-unrelated businesses (Fitness, Digital) flood the results.
    # Trade-off: substring matching can admit marginal false positives
    # (e.g. "gym" matches "Gymnasium") — acceptable only as a low-yield fallback.
    if skipped_not_relevant and len(valid_companies) < RELAXED_FALLBACK_MIN:
        relaxed_added = 0
        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name")
            website = tags.get("website") or tags.get("contact:website") or ""
            if not name or not website:
                continue
            if is_relevant_to_niche(name, tags, niche):
                continue  # already accepted in the strict pass
            if is_relaxed_match(name, niche):
                valid_companies.append((name, normalize_url(website), "OSM"))
                relaxed_added += 1
        skipped_not_relevant -= relaxed_added
        if relaxed_added:
            print(f"  {_INFO} Relaxed fallback added {relaxed_added} more '{niche}' companies.")

    if skipped_not_relevant:
        print(f"  {_INFO} Skipped {skipped_not_relevant} OSM companies that don't match '{niche}'.")

    # Broad fill pass: when a target is requested and the niche-filtered pool
    # is still smaller than the target, add the remaining OSM companies that
    # have a name AND a website even if they don't match the niche. The worker
    # still needs a real site to scrape, and without this pass --limit 500
    # stops at whatever the strict+relaxed filters returned (often < 20).
    # NOTE: this pass is capped at the plain target (NOT POOL_MULTIPLIER) — it
    # is the only source of OFF-niche candidates, and scaling it would pad an
    # "IT" list with restaurants/barbers that happen to have emails. The
    # POOL_MULTIPLIER is applied only to the niche-targeted sources
    # (HERE/Google/DuckDuckGo) which return on-niche companies.
    if target_count > 0 and len(valid_companies) < target_count:
        existing_domains = {urlparse(w).netloc.lower() for _, w, _ in valid_companies}
        fill_added = 0
        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name")
            website = tags.get("website") or tags.get("contact:website") or ""
            if not name or not website:
                continue
            website_domain = urlparse(normalize_url(website)).netloc.lower()
            if any(s in website_domain for s in (
                "facebook.com", "instagram.com", "linkedin.com", "twitter.com",
                "x.com", "youtube.com", "tiktok.com", "yelp.com",
                "foursquare.com", "google.com",
            )):
                continue
            if website_domain in existing_domains:
                continue
            existing_domains.add(website_domain)
            valid_companies.append((name, normalize_url(website), "OSM (fill)"))
            fill_added += 1
            if len(valid_companies) >= target_count:
                break
        if fill_added:
            print(f"  {_INFO} Fill pass added {fill_added} more OSM companies (any niche) to reach the target.")

    # Add HERE results (dedup by name match with OSM)
    here_names = set()
    for biz in here_results:
        biz_name = biz["name"]
        biz_website = biz["website"]
        name_lower = biz_name.lower().strip()
        if name_lower in here_names:
            continue
        here_names.add(name_lower)
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

    # Add Google results (dedup by name/domain with OSM & HERE)
    google_names = set()
    for biz in google_results:
        biz_name = biz["name"]
        biz_website = biz["website"]
        biz_domain = urlparse(biz_website).netloc.lower()
        name_lower = biz_name.lower().strip()
        if name_lower in google_names:
            continue
        google_names.add(name_lower)
        # Check if a similar name or same domain exists in existing results
        already_have = any(
            name_lower == n.lower().strip() or
            biz_domain == urlparse(w).netloc.lower() or
            name_lower in n.lower() or n.lower() in name_lower
            for n, w, _ in valid_companies
        )
        if not already_have:
            valid_companies.append((biz_name, biz_website, "Google"))
            google_count += 1

    if google_count > 0:
        print(f"\n{_INFO} Added {google_count} unique {'business' if google_count == 1 else 'businesses'} from Google search.")

    # Add DuckDuckGo results (dedup by name/domain with everything above)
    ddg_names = set()
    ddg_count = 0
    for biz in ddg_results:
        biz_name = biz["name"]
        biz_website = biz["website"]
        biz_domain = urlparse(biz_website).netloc.lower()
        name_lower = biz_name.lower().strip()
        if name_lower in ddg_names:
            continue
        ddg_names.add(name_lower)
        already_have = any(
            name_lower == n.lower().strip() or
            biz_domain == urlparse(w).netloc.lower() or
            name_lower in n.lower() or n.lower() in name_lower
            for n, w, _ in valid_companies
        )
        if not already_have:
            valid_companies.append((biz_name, biz_website, "DuckDuckGo"))
            ddg_count += 1

    if ddg_count > 0:
        print(f"\n{_INFO} Added {ddg_count} unique {'business' if ddg_count == 1 else 'businesses'} from DuckDuckGo search.")

    total_valid = len(valid_companies)
    if total_valid == 0:
        print(f"\n{_WARN} No companies with names and websites found.")
        save_partial_results(csv_filename, [])
        print(f"{_CROSS} Nothing to process.")
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
            # Strip a leading www. so generated emails are user@domain.tld
            # instead of user@www.domain.tld.
            if c_domain.startswith("www."):
                c_domain = c_domain[4:]
            try:
                c_data = scrape_site(c_website)
                c_emails = c_data["emails"]
                # Clean the scraped emails with the exact validator the sender
                # and super_clean use, so junk/placeholder addresses never
                # reach the CSV.
                c_sorted = sort_emails_by_relevance(c_emails) if c_emails else []
                c_sorted = [e for e in c_sorted if is_valid_target_email(e)]

                # Only companies with at least one valid email are kept — no
                # website-only filler rows. This is what makes --limit 500
                # mean 500 rows that actually have emails.
                if not c_sorted:
                    return None, {
                        "skip": "no valid email",
                        "name": c_name,
                        "domain": c_domain,
                    }
                tier = 1

                c_people = c_data.get("people", [])
                c_person = c_title = c_email = ""
                founder_source = ""
                if c_people:
                    top = c_people[0]
                    c_person = top["name"]
                    c_title = top["title"]
                    founder_source = "website"
                    c_pattern = infer_email_pattern(set(c_sorted), c_domain)
                    c_email = generate_contact_email(c_person, c_domain,
                                                     c_pattern or '{first}.{last}')
                elif c_sorted:
                    # Prefer a non-generic address; fall back to the best
                    # scraped email so gmail/outlook-only rows (very common for
                    # small businesses) still get a usable Contact Email.
                    for em in c_sorted:
                        if em.split('@')[0].lower() not in GENERIC_EMAIL_LOCALS:
                            c_email = em
                            break
                    if not c_email:
                        c_email = c_sorted[0]

                # Try Google search for the founder — only when the site
                # itself yielded no decision-maker, so large runs stay fast
                # (the search is serialized at 1 request per second).
                if tier == 1 and not c_person:
                    google_result = search_google_founder(c_name)
                    if google_result:
                        g_name, g_title = google_result
                        g_rank = _get_title_rank(g_title)
                        current_rank = _get_title_rank(c_title) if c_person else 999
                        if g_rank < current_rank:
                            c_person = g_name
                            c_title = g_title
                            founder_source = "Google search"
                            c_pattern = infer_email_pattern(set(c_sorted), c_domain)
                            c_email = generate_contact_email(
                                c_person, c_domain,
                                c_pattern or '{first}.{last}',
                            )
                        elif not founder_source:
                            founder_source = "website"
                if not founder_source:
                    founder_source = "none"

                c_phone = c_data["phones"][0] if c_data["phones"] else ""

                row = {
                    "Company Name": _clean_csv_val(c_name),
                    "Website": _clean_csv_val(c_website),
                    "Source": _clean_csv_val(c_data_source),
                    "Phone Number": _clean_csv_val(c_phone),
                    "Contact Person": _clean_csv_val(c_person),
                    "Contact Title": _clean_csv_val(c_title),
                    "Contact Email": _clean_csv_val(c_email),
                    "Founder Source": _clean_csv_val(founder_source),
                    "All Emails Found": "; ".join(c_sorted) if c_sorted else "",
                    # Internal sort key — never written to the CSV (CSV_FIELDS
                    # controls the output columns). Lower = better quality.
                    "__tier": tier,
                }
                info = {
                    "name": c_name, "domain": c_domain,
                    "n_emails": len(c_sorted),
                    "contact_person": c_person,
                    "contact_title": c_title,
                    "contact_email": c_email,
                    "phone": c_phone,
                    "founder_source": founder_source,
                    "tier": tier,
                }
                return row, info

            except Exception as e:
                return None, {"skip": f"error: {e}", "name": c_name, "domain": c_domain}

        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_map = {executor.submit(_worker, n, w, s): (n, w, s)
                              for n, w, s in valid_companies}

                tier1_count = 0  # best-quality rows (companies with emails)

                for future in as_completed(future_map):
                    # Every written row has an email, so once the quota is
                    # filled with email rows we stop early; otherwise keep
                    # going until the candidate pool is exhausted.
                    if target_count > 0 and tier1_count >= target_count:
                        _target_reached.set()
                        for f in future_map:
                            f.cancel()
                        pending = sum(1 for f in future_map if not f.done())
                        print(f"\n  {_INFO} Reached target of {target_count} top-quality"
                              f" (email) results. {pending} unprocessed"
                              f" candidates remaining.\n")
                        break

                    row, info = future.result()

                    if row is not None:
                        tier = info.get("tier", 4)
                        if tier == 1:
                            tier1_count += 1
                        results.append(row)
                        idx = len(results)
                        progress = f"{idx}/{target_count}" if target_count > 0 else str(idx)
                        print(f"  [{progress}] {info['name']} [email]")
                        print(f"         {_ARROW} {info['domain']}")
                        print(f"         {info['n_emails']} email(s) | Result {progress}")
                        if info['contact_person']:
                            print(f"         {_ARROW} Person: {info['contact_person']}"
                                  f" ({info['contact_title']})")
                            print(f"         {_ARROW} Email: {info['contact_email']}")
                        if info['phone']:
                            print(f"         {_ARROW} Phone: {info['phone']}")

                        if idx % 5 == 0:
                            save_partial_results(csv_filename, results)
                    else:
                        skip = info.get("skip", "unknown")
                        if skip.startswith("error"):
                            errors += 1
                        elif skip == "no valid email":
                            skipped_no_email += 1

                        if skip != "cancelled":
                            print(f"  [-] {info['name']}")
                            print(f"         {_ARROW} {info['domain']}")
                            print(f"         {_WARN} Skipped — {skip.replace('_', ' ')}")

        except KeyboardInterrupt:
            _target_reached.set()

        # Final save — truncate to the requested target. Every row has an
        # email (website-only companies were skipped), so --limit 500 really
        # yields up to 500 rows with emails.
        if target_count > 0 and len(results) > target_count:
            results.sort(key=lambda r: r.get("__tier", 4))
            kept = len(results)
            results = results[:target_count]
            print(f"  {_INFO} Sorted {kept} results by quality, kept top {target_count}.")
        elif results:
            results.sort(key=lambda r: r.get("__tier", 4))
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

    print(f"  Total companies:                 {total}")
    print(f"  Companies with emails:          {with_emails}")
    print(f"  Total emails collected:         {total_emails}")
    if skipped_no_name:
        print(f"  Skipped (no name):              {skipped_no_name}")
    if skipped_no_website:
        print(f"  No website listed:              {skipped_no_website}")
    if skipped_no_email:
        print(f"  Skipped (no valid email):       {skipped_no_email}")
    if skipped_not_relevant:
        print(f"  Skipped (not in niche):         {skipped_not_relevant}")
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
    print(f"  Open the CSV file in Excel or any spreadsheet app to see the full results.")

    # Hand the written CSV filename back to callers (e.g. the multi-city runner)
    return csv_filename


def main():
    # Parse CLI args
    args = sys.argv[1:]
    if not args or "-h" in args or "--help" in args:
        print("Business Finder — Find companies in ANY niche with emails")
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

    # Single location run — the whole pipeline for this one place.
    # run_location raises RuntimeError on geocode/Overpass failure; keep the
    # original clean-exit behavior of the single-city CLI here.
    try:
        run_location(niche, location, MAX_COMPANIES)
    except RuntimeError as e:
        print(f"{_CROSS} {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
