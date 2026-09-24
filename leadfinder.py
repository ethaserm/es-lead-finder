#!/usr/bin/env python3
"""ES Agents multi-source lead finder.

Merges several free sources into one de-duplicated list of small UK businesses
and appends the ones with a verifiable email to the "Business Queue" tab of the
outreach Google Sheet.

Sources (each optional, all merged by website domain / normalised name):
  1. OpenStreetMap via Overpass   - free, no key
  2. Google Places (Text Search)  - needs GOOGLE_PLACES_API_KEY, capped per run
  3. Companies House              - needs COMPANIES_HOUSE_API_KEY; used to confirm
                                    a business is an active limited company (PECR)

Email rule (same as the existing outreach rule): an email is only used if it is
literally written on the business's own website. Nothing is guessed. Businesses
with no website or no visible email are skipped.

Run:  python leadfinder.py --dry-run     (no Sheet writes, writes output/*.csv)
      python leadfinder.py               (appends to the Sheet)
"""
import argparse
import csv
import difflib
import html
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import date
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "ESAgentsLeadFinder/1.0 (+https://esagents.dpdns.org)"
HTML_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
UK_BBOX = (49.8, -8.7, 60.9, 1.8)  # south, west, north, east

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_ATTEMPTS = 3         # first try + at most 2 retries
OVERPASS_BACKOFF_BASE = 2     # seconds; doubles each retry, capped below
OVERPASS_BACKOFF_CAP = 10
OVERPASS_QUERY_DELAY = 4      # polite pause after every query
OVERPASS_REQUEST_TIMEOUT = (5, 25)  # connect, read: cap each attempt at ~25s
OVERPASS_SERVER_TIMEOUT = 20  # seconds the Overpass server may spend on a query
CH_GRACE_SECONDS = 90         # extra time after the crawl budget for Companies House checks
PLACE_RADIUS_M = 3000        # search radius around a town's place node when it has no boundary
_overpass_next = 0
_overpass_deadline = None     # time.monotonic() after which Overpass searches are skipped

BLOCKED_SITE_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "tiktok.com", "youtube.com", "checkatrade.com", "yell.com", "freeindex.co.uk",
    "trustatrader.com", "mybuilder.com", "ratedpeople.com", "google.com",
    "linktr.ee", "thomsonlocal.com", "bark.com", "nextdoor.com", "gov.uk",
)
NAME_STOPWORDS = {"ltd", "limited", "llp", "plc", "the", "and", "co", "company", "uk"}
JUNK_EMAIL_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "domain.com", "yourdomain.com",
    "email.com", "sentry-next.wixpress.com", "godaddy.com",
)
JUNK_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon"}
FILE_EXT_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "woff", "woff2", "ico"}
JUNK_MARKERS = ("sentry", "wixpress", "example", "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply")
DEFAULT_FREEMAIL = ("gmail.com", "googlemail.com", "outlook.com", "outlook.co.uk", "hotmail.com",
                    "hotmail.co.uk", "yahoo.com", "yahoo.co.uk")
TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk", "ac.uk", "gov.uk"}
# Companies House last_accounts.type values that mean a small company
GOOD_ACCOUNTS = {"micro-entity": "micro-entity", "small": "small",
                 "total-exemption-small": "total-exemption", "total-exemption-full": "total-exemption"}
DEFAULT_EXCLUDED_WORDS = ("plc", "group", "holdings", "holding", "bank", "council")
MIN_COMPANY_AGE_YEARS = 2

NAME_ALIASES = {"business name", "name", "business"}
COLUMN_ALIASES = {
    "name": NAME_ALIASES,
    "trade": {"trade / business type", "trade", "business type", "type"},
    "area": {"area", "town", "location"},
    "website": {"website", "url", "site"},
    "email": {"contact email", "email"},
    "status": {"status"},
    "source": {"source"},
    "company": {"company type", "company status"},
    "phone": {"phone", "telephone"},
    "date": {"date added"},
    "email_source": {"email source"},
    "why": {"why"},
}
DEFAULT_HEADER = [
    "Business Name", "Trade / Business Type", "Area", "Website", "Contact Email",
    "Company Type", "Status", "Source", "Date Added", "Email Source", "Why",
]
CANONICAL_HEADERS = {
    "website": "Website", "email": "Contact Email", "company": "Company Type",
    "source": "Source", "date": "Date Added", "email_source": "Email Source", "why": "Why",
}


# --------------------------------------------------------------------------- helpers
def log(msg):
    print(msg, flush=True)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def env_flag(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def norm_name(name):
    text = html.unescape(name or "").lower().replace("&", " and ")
    tokens = [t for t in re.findall(r"[a-z0-9]+", text) if t not in NAME_STOPWORDS]
    return "".join(tokens)


def host_of(url):
    if not url:
        return ""
    if "//" not in url:
        url = "http://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def clean_site(url):
    """Return a usable website URL or '' (social pages / aggregators are rejected)."""
    url = (url or "").strip()
    if not url:
        return ""
    if "//" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    host = host_of(url)
    if not host or "." not in host:
        return ""
    if any(host == b or host.endswith("." + b) for b in BLOCKED_SITE_HOSTS):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def clean_secret(value):
    """Strip whitespace and any BOM/zero-width marker from a secret."""
    return (value or "").replace("\ufeff", "").replace("\u200b", "").strip()


def _words(text):
    return re.findall(r"[a-z0-9]+", html.unescape(text or "").lower().replace("&", " and "))


def chain_keys(cfg):
    """Each chain becomes (word list, joined string); matching is whole-word, not substring."""
    keys = []
    for chain in cfg.get("excluded_chains", []):
        words = [w for w in _words(chain) if w not in NAME_STOPWORDS]
        if words:
            keys.append((words, "".join(words)))
    return keys


def is_chain(lead, keys):
    name_words = [w for w in _words(lead.name) if w not in NAME_STOPWORDS]
    host = re.sub(r"[^a-z0-9]", "", host_of(lead.website))
    for words, joined in keys:
        n = len(words)
        if any(name_words[i:i + n] == words for i in range(len(name_words) - n + 1)):
            return True
        if len(joined) >= 6 and joined in host:
            return True
    return False


def in_uk(lat, lon):
    if lat is None or lon is None:
        return True  # cannot tell; keep
    return UK_BBOX[0] <= lat <= UK_BBOX[2] and UK_BBOX[1] <= lon <= UK_BBOX[3]


class Lead:
    def __init__(self, name, trade, area, website="", phone="", address="", source=""):
        self.name = (name or "").strip()
        self.trade = trade
        self.area = area
        self.website = clean_site(website)
        self.phone = phone or ""
        self.address = address or ""
        self.sources = {source} if source else set()
        self.email = ""
        self.company = ""  # e.g. "Ltd (active) 01234567"
        self.osm_emails = []  # addresses written in the OpenStreetMap tags
        self.email_source = ""
        self.why = ""
        self.review_reasons = []  # why this lead is not Pending; empty means every check passed

    @property
    def key(self):
        return host_of(self.website) or norm_name(self.name)

    def merge(self, other):
        self.sources |= other.sources
        self.osm_emails += [e for e in other.osm_emails if e not in self.osm_emails]
        for attr in ("website", "phone", "address"):
            if not getattr(self, attr) and getattr(other, attr):
                setattr(self, attr, getattr(other, attr))


# --------------------------------------------------------------------------- source 1: OpenStreetMap
def overpass_query(query, session):
    """POST a query, rotating mirrors and backing off exponentially on 429/5xx/timeouts."""
    global _overpass_next
    for attempt in range(OVERPASS_ATTEMPTS):
        if _overpass_deadline is not None and time.monotonic() >= _overpass_deadline:
            log("    overpass time budget used up; skipping")
            return None
        url = OVERPASS_URLS[_overpass_next % len(OVERPASS_URLS)]
        host = host_of(url)
        retry = False
        try:
            resp = session.post(url, data={"data": query}, headers={"User-Agent": UA},
                                timeout=OVERPASS_REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            log(f"    overpass {host} error: {type(exc).__name__}")
            retry = True
        else:
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log(f"    overpass {host} returned invalid JSON")
                    retry = True
            elif resp.status_code in (429, 406, 500, 502, 503, 504):
                log(f"    overpass {host} returned {resp.status_code}")
                retry = True
            else:
                log(f"    overpass {host} returned {resp.status_code}")
                retry = True  # try another mirror rather than give up
        _overpass_next += 1  # next attempt goes to the next mirror
        if retry and attempt < OVERPASS_ATTEMPTS - 1:
            wait = min(OVERPASS_BACKOFF_BASE * 2 ** attempt, OVERPASS_BACKOFF_CAP)
            log(f"    retrying on {host_of(OVERPASS_URLS[_overpass_next % len(OVERPASS_URLS)])} in {wait}s")
            time.sleep(wait)
    log("    overpass: giving up on this query after all retries")
    return None


def overpass_leads(trade, town, session):
    tags = trade.get("osm", [])
    if not tags:
        return []
    area_name = (town["osm"] if isinstance(town, dict) else town).replace("\\", "").replace('"', "")
    bbox = ",".join(str(n) for n in UK_BBOX)
    in_area = "".join(f'nwr["{k}"="{v}"](area.a);' for k, v in tags)
    near_place = "".join(f'nwr["{k}"="{v}"](around.p:{PLACE_RADIUS_M});' for k, v in tags)
    # Boundary area when OSM has one, else a radius around the town's place node; UK-only.
    query = (
        f'[out:json][timeout:{OVERPASS_SERVER_TIMEOUT}];'
        f'rel["name"="{area_name}"]["boundary"="administrative"]({bbox});map_to_area->.a;'
        f'node["name"="{area_name}"]["place"~"^(city|town)$"]({bbox})->.p;'
        f"({in_area}{near_place});out center tags;"
    )
    data = overpass_query(query, session)
    time.sleep(OVERPASS_QUERY_DELAY)  # stay well inside the public servers' fair-use limits
    if data is None:
        return None  # search failed; caller records it for a retry on a later run
    label = town["name"] if isinstance(town, dict) else town
    leads = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        center = el.get("center", {})
        lat = el.get("lat", center.get("lat"))
        lon = el.get("lon", center.get("lon"))
        if not in_uk(lat, lon):
            continue
        website = tags.get("website") or tags.get("contact:website") or ""
        phone = tags.get("phone") or tags.get("contact:phone") or ""
        addr = " ".join(filter(None, [tags.get("addr:street"), tags.get("addr:city"), tags.get("addr:postcode")]))
        lead = Lead(name, trade["label"], label, website, phone, addr, "OpenStreetMap")
        tagged = tags.get("email") or tags.get("contact:email") or ""
        lead.osm_emails = [e.strip().lower() for e in re.split(r"[;,]", tagged)
                           if EMAIL_RE.fullmatch(e.strip())]
        leads.append(lead)
    return leads


# --------------------------------------------------------------------------- source 2: Google Places
class GoogleBudget:
    def __init__(self, max_requests):
        self.max = max_requests
        self.used = 0

    def take(self):
        if self.used >= self.max:
            return False
        self.used += 1
        return True


def google_leads(trade, town, session, api_key, budget, pages=1):
    label = town["name"] if isinstance(town, dict) else town
    body = {
        "textQuery": f"{trade['places']} in {label}, UK",
        "regionCode": "GB",
        "languageCode": "en",
        "pageSize": 20,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,places.websiteUri,places.formattedAddress,"
            "places.nationalPhoneNumber,places.businessStatus,nextPageToken"
        ),
    }
    leads = []
    for _ in range(pages):
        if not budget.take():
            log("    google request cap reached; skipping")
            break
        try:
            resp = session.post("https://places.googleapis.com/v1/places:searchText",
                                json=body, headers=headers, timeout=30)
        except requests.RequestException as exc:
            log(f"    google error: {exc}")
            break
        if resp.status_code != 200:
            log(f"    google returned {resp.status_code}: {resp.text[:200]}")
            break
        data = resp.json()
        for place in data.get("places", []):
            if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                continue
            name = (place.get("displayName") or {}).get("text")
            if not name:
                continue
            leads.append(Lead(name, trade["label"], label, place.get("websiteUri", ""),
                              place.get("nationalPhoneNumber", ""),
                              place.get("formattedAddress", ""), "Google Maps"))
        token = data.get("nextPageToken")
        if not token:
            break
        body["pageToken"] = token
    return leads


# --------------------------------------------------------------------------- source 3: Companies House
def key_shape(key):
    """Describe a key without revealing it: length and suspicious characters."""
    quote_chars = "\"'`"
    return (
        f"length={len(key)} quotes={any(c in key for c in quote_chars)} "
        f"whitespace={any(c.isspace() for c in key)} equals={'=' in key} "
        f"non_ascii={any(ord(c) > 127 for c in key)} "
        f"control={any(ord(c) < 32 or ord(c) == 127 for c in key)}"
    )


def companies_house_self_test(session, api_key):
    """Search 'Tesco' and log only the HTTP status and whether the key was accepted.

    Returns True (accepted), False (rejected: 400/401/403) or None (inconclusive).
    """
    log(f"companies house key shape: {key_shape(api_key)}")
    try:
        resp = session.get(
            "https://api.company-information.service.gov.uk/search/companies",
            params={"q": "Tesco", "items_per_page": 1},
            auth=(api_key, ""), timeout=20,
        )
    except requests.RequestException as exc:
        log(f"companies house self-test: request failed ({type(exc).__name__}); inconclusive")
        return None
    if resp.status_code == 200:
        log("companies house self-test: HTTP 200, key accepted")
        return True
    body = (resp.text or "").replace(api_key, "***").replace("\n", " ")[:200]
    if resp.status_code in (400, 401, 403):
        log(f"companies house self-test: HTTP {resp.status_code}, key REJECTED; body: {body}")
        return False
    log(f"companies house self-test: HTTP {resp.status_code}, inconclusive; body: {body}")
    return None


def _ch_get(session, api_key, path, params=None):
    """GET one Companies House resource with polite pacing. Returns parsed JSON or None."""
    try:
        resp = session.get("https://api.company-information.service.gov.uk" + path,
                           params=params, auth=(api_key, ""), timeout=20)
    except requests.RequestException as exc:
        log(f"    companies house error: {type(exc).__name__}")
        return None
    time.sleep(0.6)  # stay under the API rate limit
    if resp.status_code == 429:
        log("    companies house rate limited; waiting 60s")
        time.sleep(60)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def companies_house_lookup(lead, session, api_key):
    """Find the business's active company and fetch its profile. Returns a dict, or None if no match."""
    data = _ch_get(session, api_key, "/search/companies", {"q": lead.name, "items_per_page": 5})
    if not data:
        return None
    target = norm_name(lead.name)
    town = (lead.area or "").lower()
    for item in data.get("items", []):
        if item.get("company_status") != "active":
            continue
        ratio = difflib.SequenceMatcher(None, target, norm_name(item.get("title", ""))).ratio()
        addr = (item.get("address_snippet") or "").lower()
        if not (ratio >= 0.92 or (ratio >= 0.8 and town and town in addr)):
            continue
        info = {
            "number": item.get("company_number", ""), "title": item.get("title", ""),
            "status": item.get("company_status"), "type": item.get("company_type"),
            "created": item.get("date_of_creation"), "accounts_type": None, "sic": [], "profile": False,
        }
        profile = _ch_get(session, api_key, f"/company/{info['number']}")
        if profile:
            last = (profile.get("accounts") or {}).get("last_accounts") or {}
            info.update(
                status=profile.get("company_status", info["status"]), type=profile.get("type", info["type"]),
                created=profile.get("date_of_creation", info["created"]), accounts_type=last.get("type"),
                sic=profile.get("sic_codes") or [], profile=True,
            )
        return info
    return None


def assess_company(info, lead, cfg, today=None):
    """Decide whether a lead's company is a real, small, established Ltd.

    Returns (reasons, facts): reasons is empty only when every check passes (the lead can be
    Pending); otherwise it lists why the lead must be reviewed. facts is a short "why" note.
    """
    today = today or date.today()
     if not info:
        return [], "no active Companies House match (sole trader / unregistered — allowed)"
    reasons, facts = [], []
    if info.get("type") != "ltd":
        reasons.append(f"not a private Ltd ({info.get('type') or 'unknown type'})")
    if info.get("status") != "active":
        reasons.append(f"status {info.get('status')}")
    if not info.get("profile"):
        reasons.append("company profile unavailable")

    words = set(_words(info.get("title", ""))) | set(_words(lead.name))
    hits = sorted(words & set(cfg.get("excluded_company_words", DEFAULT_EXCLUDED_WORDS)))
    if hits:
        reasons.append(f"name contains '{hits[0]}'")

    try:
        created = date.fromisoformat(info.get("created") or "")
    except ValueError:
        created = None
    if created is None:
        reasons.append("no incorporation date")
    elif (today - created).days < MIN_COMPANY_AGE_YEARS * 365:
        reasons.append(f"incorporated under {MIN_COMPANY_AGE_YEARS} yrs ago")
    else:
        facts.append(f"active {(today - created).days // 365} yrs")

    acct = info.get("accounts_type")
    if acct in GOOD_ACCOUNTS:
        facts.insert(0, f"{GOOD_ACCOUNTS[acct]} accounts")
    else:
        reasons.append(f"accounts: {acct or 'none filed'}")

    trade_sic = next((t.get("sic", []) for t in cfg.get("trades", []) if t["label"] == lead.trade), [])
    match = next((c for c in info.get("sic", []) if any(str(c).startswith(p) for p in trade_sic)), None)
    if match:
        facts.append(f"SIC {match} fits {lead.trade}")
    else:
        reasons.append("SIC does not fit trade" + (f" ({', '.join(info['sic'][:2])})" if info.get("sic") else ""))
    return reasons, ", ".join(facts) if not reasons else "; ".join(reasons)


# --------------------------------------------------------------------------- email extraction
# An address is only ever taken from text that is really on the page (a mailto link, visible
# text, an obfuscated "name [at] domain" written out on the page, or Cloudflare's own encoding of
# an address on the page) or from an OpenStreetMap tag. Nothing is constructed or guessed.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
CF_EMAIL_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
LINK_RE = re.compile(r'href=["\']([^"\'#]+)["\']', re.I)
MAILTO_RE = re.compile(r'href=["\']mailto:([^"\'?>\s]+)', re.I)
CODE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<!--.*?-->", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
KIND_LABEL = {
    "mailto": "mailto link", "text": "visible text", "obfuscated": "obfuscated text on page",
    "cloudflare": "Cloudflare-encoded address on page",
}
MX_UNKNOWN_REASON = "mx_lookup_failed"
# rejection reasons, weakest to strongest: a lead reports the furthest one any of its addresses reached
REJECT_ORDER = ["third_party", "duplicate", "suppressed", "mx_lookup_failed", "no_mx"]


def decode_cf_email(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def is_junk_email(email):
    local, _, domain = email.partition("@")
    tld = domain.rsplit(".", 1)[-1]
    return bool(
        not local or not domain or len(email) > 80 or tld in FILE_EXT_TLDS
        or local in JUNK_LOCAL_PARTS
        or any(m in local or m in domain for m in JUNK_MARKERS)
        or any(domain.endswith(j) for j in JUNK_EMAIL_DOMAINS)
        or re.search(r"\d+x\d*$", local) or local.startswith("u00")
    )


def scan_page(page_html):
    """Return ([(email, kind)], junk_count) for one page; kind says how it appeared on the page."""
    body = CODE_RE.sub(" ", page_html)  # ignore scripts, styles and comments
    found, junk = {}, set()

    def add(raw, kind):
        email = unquote(html.unescape(raw)).strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(email):
            return
        if is_junk_email(email):
            junk.add(email)
        else:
            found.setdefault(email, kind)

    for raw in MAILTO_RE.findall(body):
        add(raw, "mailto")
    text = html.unescape(TAG_RE.sub(" ", body))
    for raw in EMAIL_RE.findall(text):
        add(raw, "text")
    spelled = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    spelled = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", spelled, flags=re.I)
    if spelled != text:
        for raw in EMAIL_RE.findall(spelled):
            add(raw, "obfuscated")
    for hexstr in CF_EMAIL_RE.findall(page_html):
        decoded = decode_cf_email(hexstr)
        if decoded:
            add(decoded, "cloudflare")
    return list(found.items()), len(junk)


def extract_emails(page_html):
    return [email for email, _ in scan_page(page_html)[0]]


def domain_of(email):
    return email.rpartition("@")[2].lower()


def registrable_domain(host):
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def email_matches_site(email, site_host):
    return registrable_domain(domain_of(email)) == registrable_domain(site_host)


def rank_emails(candidates, site_host, freemail):
    """Order (email, source) pairs: the business's own domain first, then freemail, then by a
    preference for generic mailboxes."""
    prefixes = ("info", "hello", "enquiries", "contact", "office", "admin", "sales")

    def key(item):
        email = item[0]
        own = 0 if email_matches_site(email, site_host) else (1 if domain_of(email) in freemail else 2)
        local = email.partition("@")[0]
        pref = prefixes.index(local) if local in prefixes else len(prefixes)
        return own, pref

    return sorted(candidates, key=key)  # sorted() is stable, so page order breaks ties


_mx_cache = {}


def mx_status(domain):
    """'ok' if the domain has a real MX record, 'none' if it definitely has not, 'error' if DNS failed."""
    domain = domain.lower().strip(".")
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        import dns.exception
        import dns.resolver
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 6
        resolver.timeout = 3
        try:
            answers = resolver.resolve(domain, "MX")
            status = "ok" if any(r.exchange.to_text() != "." for r in answers) else "none"  # "." = null MX
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            status = "none"
        except (dns.exception.DNSException, OSError):
            status = "error"
    except ImportError:
        status = "error"
    _mx_cache[domain] = status
    return status


class EmailRules:
    """Read-only lists the email checks need. `known_emails` is shared with the caller on purpose."""

    def __init__(self, freemail=DEFAULT_FREEMAIL, suppressed_emails=(), suppressed_domains=(), known_emails=()):
        self.freemail = set(freemail)
        self.suppressed_emails = set(suppressed_emails)
        self.suppressed_domains = {registrable_domain(d) for d in suppressed_domains} - self.freemail  # never block all of gmail
        self.known_emails = known_emails

    def is_suppressed(self, email):
        return email in self.suppressed_emails or registrable_domain(domain_of(email)) in self.suppressed_domains

    def domain_suppressed(self, host):
        return registrable_domain(host) in self.suppressed_domains


def resolve_email(crawler, lead, rules):
    """Find one deliverable, on-site email for a lead. Returns (email, source, reject_reason)."""
    site_host = host_of(lead.website)

    def domain_ok(email):
        return domain_of(email) in rules.freemail or email_matches_site(email, site_host)

    candidates = [(e, "OpenStreetMap tag") for e in lead.osm_emails if not is_junk_email(e)]
    site_found, junk_seen, fetched = crawler.collect(lead.website, domain_ok)
    candidates += site_found
    if not candidates:
        if junk_seen:
            return "", "", "junk_only"
        return "", "", "no_email_found" if fetched else "site_unavailable"
    seen, reasons = set(), []
    for email, source in rank_emails(candidates, site_host, rules.freemail):
        if email in seen:
            continue
        seen.add(email)
        if not domain_ok(email):
            reasons.append("third_party")
        elif rules.is_suppressed(email):
            reasons.append("suppressed")
        elif email in rules.known_emails:
            reasons.append("duplicate")
        else:
            domain = domain_of(email)
            mx = "ok" if domain in rules.freemail else mx_status(domain)
            if mx == "ok":
                return email, source, ""
            reasons.append("no_mx" if mx == "none" else MX_UNKNOWN_REASON)
    return "", "", max(reasons, key=REJECT_ORDER.index)


class SiteCrawler:
    def __init__(self, session):
        self.session = session
        self.robots = {}
        self.last_hit = {}

    def allowed(self, url):
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rp = self.robots.get(origin)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(origin + "/robots.txt", headers=HTML_HEADERS, timeout=(5, 8))
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                elif r.status_code in (404, 410):
                    rp.parse([])
                else:
                    rp.disallow_all = True
            except requests.RequestException:
                rp.parse([])
            self.robots[origin] = rp
        return rp.can_fetch(UA, url)

    def get(self, url):
        if not self.allowed(url):
            return ""
        host = urlparse(url).netloc
        wait = 1.0 - (time.time() - self.last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self.session.get(url, headers=HTML_HEADERS, timeout=(5, 10), stream=True)
            self.last_hit[host] = time.time()
            if resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", "").lower():
                return ""
            raw = resp.raw.read(1_500_000, decode_content=True)
            return raw.decode(resp.encoding or "utf-8", errors="replace")
        except requests.RequestException:
            return ""

    def collect(self, website, accept):
        """Return ([(email, source)], junk_seen, fetched) from the homepage and, only while no
        acceptable address has turned up, up to three contact/about pages linked from it."""
        found, junk_total = [], 0

        def take(page, label):
            nonlocal junk_total
            emails, junk = scan_page(page)
            junk_total += junk
            found.extend((e, f"Website {label} ({KIND_LABEL[kind]})") for e, kind in emails)

        home = self.get(website)
        if not home:
            return [], 0, False
        take(home, "homepage")
        if not any(accept(e) for e, _ in found):
            host, targets = host_of(website), []
            for href in LINK_RE.findall(home):
                full = urljoin(website, href)
                p = urlparse(full)
                if (p.scheme in ("http", "https") and host_of(full) == host
                        and re.search(r"contact|about", p.path, re.I) and full not in targets):
                    targets.append(full)
            for url in targets[:3]:
                page = self.get(url)
                if not page:
                    continue
                path = urlparse(url).path or "/"
                take(page, f"{'contact' if re.search('contact', path, re.I) else 'about'} page {path}")
                if any(accept(e) for e, _ in found):
                    break
        return found, junk_total, True


# --------------------------------------------------------------------------- Google Sheet
def find_header(values):
    for idx, row in enumerate(values[:12]):
        if any(str(c).strip().lower() in NAME_ALIASES for c in row):
            return idx
    return None


def col_index(header, key):
    aliases = COLUMN_ALIASES[key]
    for i, cell in enumerate(header):
        if str(cell).strip().lower() in aliases:
            return i
    return None


def load_known(sh, tabs):
    names, emails, hosts = set(), set(), set()
    for tab in tabs:
        try:
            ws = sh.worksheet(tab)
        except Exception:
            continue
        values = ws.get_all_values()
        h = find_header(values)
        if h is None:
            continue
        header = values[h]
        n_i, e_i, w_i = col_index(header, "name"), col_index(header, "email"), col_index(header, "website")
        for row in values[h + 1:]:
            if n_i is not None and n_i < len(row) and row[n_i].strip():
                names.add(norm_name(row[n_i]))
            if e_i is not None and e_i < len(row) and row[e_i].strip():
                emails.add(row[e_i].strip().lower())
            if w_i is not None and w_i < len(row) and row[w_i].strip():
                hosts.add(host_of(row[w_i]))
        log(f"  known from '{tab}': {len(values) - h - 1} rows")
    return names, emails, hosts


def prepare_queue_tab(ws):
    """Return (header_row_index, header list). Creates a header if the tab is empty, adds missing columns."""
    values = ws.get_all_values()
    h = find_header(values)
    if h is None:
        ws.update(range_name="A1", values=[DEFAULT_HEADER])
        return 0, list(DEFAULT_HEADER)
    header = list(values[h])
    for key in ("website", "email", "company", "source", "date", "email_source", "why"):
        if col_index(header, key) is None:
            header.append(CANONICAL_HEADERS[key])
            ws.update_cell(h + 1, len(header), CANONICAL_HEADERS[key])
            log(f"  added column '{CANONICAL_HEADERS[key]}' to the Business Queue tab")
    return h, header


def sheet_safe(value):
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def row_for(lead, header, status):
    values = {
        "name": lead.name, "trade": lead.trade, "area": lead.area, "website": lead.website,
        "email": lead.email, "status": status, "source": "Lead Finder (" + " + ".join(sorted(lead.sources)) + ")",
        "company": lead.company or "Not confirmed", "phone": lead.phone, "date": date.today().strftime("%d/%m/%Y"),
        "email_source": lead.email_source, "why": lead.why,
    }
    row = [""] * len(header)
    for key, val in values.items():
        i = col_index(header, key)
        if i is not None:
            row[i] = sheet_safe(val)
    return row


# --------------------------------------------------------------------------- orchestration
def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load_state():
    path = os.path.join(HERE, "state.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"cursor": 0}


def save_state(state):
    with open(os.path.join(HERE, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def pick_combos(cfg, cursor, count):
    combos = [(t, town) for town in cfg["towns"] for t in cfg["trades"]]
    picks = [combos[(cursor + i) % len(combos)] for i in range(min(count, len(combos)))]
    return picks, (cursor + count) % len(combos)


def combo_label(trade, town):
    return [trade["label"], town["name"] if isinstance(town, dict) else town]


def pick_with_retries(cfg, state, count):
    """Searches that failed last run go first (up to half the slots), then normal rotation."""
    combos = [(t, town) for town in cfg["towns"] for t in cfg["trades"]]
    by_label = {tuple(combo_label(t, town)): (t, town) for t, town in combos}
    retries = []
    for item in state.get("retry", []):
        combo = by_label.get(tuple(item))
        if combo and combo not in retries:
            retries.append(combo)
    retries = retries[:count // 2]
    rotation, next_cursor = pick_combos(cfg, state.get("cursor", 0), count - len(retries))
    picks = retries + [c for c in rotation if c not in retries]
    return picks, next_cursor


def crawl_emails(work, leads, workers, deadline):
    """Run `work(lead)` for many leads at once. Returns {id(lead): result} for those that finished.

    Sites are fetched in parallel, but SiteCrawler still waits 1s between hits to the same host
    and honours robots.txt. Leads not finished by the deadline are dropped and found again later.
    """
    def safe(lead):
        try:
            return work(lead)
        except Exception as exc:  # one bad site must not stop the run
            log(f"    crawl error on {host_of(lead.website)}: {type(exc).__name__}")
            return "", "", "error"

    results = {}
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {pool.submit(safe, lead): lead for lead in leads}
    try:
        for fut in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
            results[id(futures[fut])] = fut.result()
    except FuturesTimeout:
        log("run time budget used up; stopping the crawl early")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def lead_status(lead, ch_key):
    """Pending only when Companies House confirms a small, established, active Ltd that fits the trade."""
    if not ch_key:
        return "Review - no Companies House check"
    if lead.company.startswith("Ltd") and not lead.review_reasons:
        return "Pending"
    return "Review - " + "; ".join(lead.review_reasons or ["not confirmed Ltd"])


def load_suppression(sh, tabs):
    """Every address written anywhere in the suppression tabs (contacted, bounced, opted out...)."""
    import gspread
    emails = set()
    for tab in tabs:
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            log(f"  WARNING: suppression tab '{tab}' not found in the sheet")
            continue
        values = ws.get_all_values()
        found = {e.lower() for row in values for cell in row for e in EMAIL_RE.findall(cell)}
        h = find_header(values)
        cols = [c for c in values[h] if c] if h is not None else "no header row detected"
        log(f"  suppression from '{tab}': {len(values)} rows, {len(found)} addresses; columns: {cols}")
        emails |= found
    return emails, {domain_of(e) for e in emails}


def next_retry_queue(failed, state):
    already_retried = {tuple(x) for x in state.get("retry", [])}
    return [f for f in failed if tuple(f) not in already_retried]


def run(dry_run, combos_per_run, max_new):
    global _overpass_deadline
    started = time.monotonic()
    _overpass_deadline = started + env_int("OVERPASS_BUDGET_SECONDS", 240)
    run_deadline = started + env_int("RUN_BUDGET_SECONDS", 380)
    cfg = load_config()
    session = requests.Session()
    google_key = clean_secret(os.environ.get("GOOGLE_PLACES_API_KEY"))
    ch_key = clean_secret(os.environ.get("COMPANIES_HOUSE_API_KEY"))
    sheet_id = clean_secret(os.environ.get("SHEET_ID"))
    sa_json = clean_secret(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    budget = GoogleBudget(env_int("GOOGLE_MAX_REQUESTS", 12))
    max_sites = env_int("MAX_SITES_PER_RUN", 120)
    freemail = cfg.get("freemail_domains", DEFAULT_FREEMAIL)

    log(f"sources: OpenStreetMap=on, Google Places={'on' if google_key else 'off (no key)'}, "
        f"Companies House={'on' if ch_key else 'off (no key)'}")
    if ch_key and companies_house_self_test(session, ch_key) is False:
        log("WARNING: Companies House key rejected - disabling the check for this run.")
        ch_key = ""
    if not ch_key:
        log("WARNING: no usable Companies House key - every lead will be marked 'Review', none 'Pending'.")

    sh = ws = None
    known_names, known_emails, known_hosts = set(), set(), set()
    suppressed_emails, suppressed_domains = set(), set()
    header = list(DEFAULT_HEADER)
    if sheet_id and sa_json:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(sa_json))
        sh = gc.open_by_key(sheet_id)
        log(f"tabs in the sheet: {[w.title for w in sh.worksheets()]}")
        known_names, known_emails, known_hosts = load_known(sh, [cfg["queue_tab"], cfg["tracker_tab"]])
        suppressed_emails, suppressed_domains = load_suppression(
            sh, cfg.get("suppression_tabs", [cfg["tracker_tab"], "Replies"]))
        if not dry_run:
            try:
                ws = sh.worksheet(cfg["queue_tab"])
            except Exception:
                ws = sh.add_worksheet(cfg["queue_tab"], rows=1000, cols=12)
            _, header = prepare_queue_tab(ws)
    else:
        log("no SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON set - cannot de-duplicate or suppress against your sheet.")
        if not dry_run:
            sys.exit("Refusing to run without sheet credentials (use --dry-run to test).")

    state = load_state()
    picks, next_cursor = pick_with_retries(cfg, state, combos_per_run)

    pool, failed = {}, []
    for trade, town in picks:
        label = town["name"] if isinstance(town, dict) else town
        log(f"searching: {trade['label']} in {label}")
        found = overpass_leads(trade, town, session)
        if found is None:
            failed.append(combo_label(trade, town))
            log("  openstreetmap: FAILED, skipped (will retry on the next run)")
            found = []
        else:
            log(f"  openstreetmap: {len(found)}")
        if google_key:
            g = google_leads(trade, town, session, google_key, budget)
            log(f"  google places: {len(g)}")
            found += g
        for lead in found:
            # merge by website host first, then by name
            existing = pool.get(lead.key) or pool.get(norm_name(lead.name))
            if existing:
                existing.merge(lead)
            else:
                pool[lead.key] = lead
    log(f"searches skipped after failures: {len(failed)} of {len(picks)}")
    # A search that already failed as a retry is not queued again, so a permanently
    # failing one (e.g. a huge city) cannot keep taking slots from the rotation.
    failed = next_retry_queue(failed, state)
    log(f"unique businesses this run: {len(pool)}")

    rules = EmailRules(freemail, suppressed_emails, suppressed_domains, known_emails)
    stats = Counter()
    chains = chain_keys(cfg)
    candidates = []
    for lead in pool.values():
        if is_chain(lead, chains):
            stats["skipped: national chain"] += 1
        elif not lead.website:
            stats["skipped: no own website"] += 1
        elif norm_name(lead.name) in known_names or host_of(lead.website) in known_hosts:
            stats["skipped: already in the sheet"] += 1
        elif rules.domain_suppressed(host_of(lead.website)):
            stats["skipped: domain suppressed (contacted/bounced/opted out)"] += 1
        else:
            candidates.append(lead)
    log(f"with a website and not already contacted/queued/suppressed: {len(candidates)}")

    crawler = SiteCrawler(session)
    results = crawl_emails(lambda lead: resolve_email(crawler, lead, rules),
                           candidates[:max_sites], env_int("CRAWL_WORKERS", 8), run_deadline)
    crawled = len(results)
    stats["not crawled (time or site cap)"] += len(candidates) - crawled
    new_leads = []
    for lead in candidates:
        if len(new_leads) >= max_new:
            break
        if id(lead) not in results:
            continue
        email, source, reason = results[id(lead)]
        if not email:
            stats[f"email rejected: {reason}"] += 1
            continue
        if email in known_emails:  # another lead in this run already claimed it
            stats["email rejected: duplicate"] += 1
            continue
        if ch_key and time.monotonic() >= run_deadline + CH_GRACE_SECONDS:
            log("out of time for Companies House checks; remaining leads left for a later run")
            break
        lead.email, lead.email_source = email, source
        known_emails.add(email)
        if ch_key:
            info = companies_house_lookup(lead, session, ch_key)
            if info and info.get("type") == "ltd" and info.get("status") == "active":
                lead.company = f"Ltd (active) {info['number']}"
            lead.review_reasons, note = assess_company(info, lead, cfg)
        else:
            note = "Companies House not checked"
        own = email_matches_site(email, host_of(lead.website))
        lead.why = f"{note}; {'own-domain' if own else 'freemail'} email, MX ok"
        new_leads.append(lead)
        status = lead_status(lead, ch_key)
        stats["accepted: Pending" if status == "Pending" else f"accepted but Review: {status[9:]}"] += 1
        # emails stay out of the log: Actions logs can be public
        log(f"  + {lead.name} [{host_of(lead.website)}] {status}")

    rows, preview = [], []
    for lead in new_leads:
        status = lead_status(lead, ch_key)
        rows.append(row_for(lead, header, status))
        preview.append([lead.name, lead.trade, lead.area, lead.website, lead.email, lead.email_source,
                        lead.company or "Not confirmed", status, lead.why, ", ".join(sorted(lead.sources))])

    os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
    out_path = os.path.join(HERE, "output", f"leads_{date.today().isoformat()}.csv")
    with open(out_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if fh.tell() == 0:
            writer.writerow(["Business Name", "Trade", "Area", "Website", "Email", "Email source",
                             "Company", "Status", "Why", "Sources"])
        writer.writerows(preview)

    log("filter results:")
    for key in sorted(stats):
        log(f"  {key}: {stats[key]}")
    pending = sum(1 for r in preview if r[7] == "Pending")
    log(f"new leads with a deliverable on-site email: {len(rows)} (Pending: {pending}); crawled {crawled} sites; "
        f"google requests used: {budget.used}")
    if rows and ws is not None and not dry_run:
        ws.append_rows(rows, value_input_option="RAW")
        log(f"appended {len(rows)} rows to '{cfg['queue_tab']}'")
    if not dry_run:
        state["cursor"] = next_cursor
        state["retry"] = failed
        save_state(state)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="do not write to the Sheet or save state")
    ap.add_argument("--combos", type=int, default=env_int("COMBOS_PER_RUN", 6))
    ap.add_argument("--max-new", type=int, default=env_int("MAX_NEW_PER_RUN", 40))
    args = ap.parse_args()
    run(args.dry_run or env_flag("DRY_RUN", False), args.combos, args.max_new)


if __name__ == "__main__":
    main()
