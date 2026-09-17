"""Institutional Prospecting Agent — crawling and second-hop research.

Fetch discipline (per spec): respect robots.txt, >=1.5s between requests,
identify the operator in the User-Agent. The AMI state locator is the
enumerable anchor source; the org's own website is the only source that can
yield a VERIFIED contact — always the second hop. Nothing here guesses:
fields stay empty when a page didn't provide them.
"""

import re
import time
import urllib.robotparser
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from src.analysis.institutional import (
    AMI_BASE, AMI_SLUGS, FETCH_DELAY_S, USER_AGENT, new_prospect)

try:
    import httpx
    _HTTPX = True
except ImportError:  # pragma: no cover
    _HTTPX = False

_last_fetch = [0.0]
_robots_cache = {}
# The reason the most recent fetch() returned nothing, so callers can report
# the SPECIFIC cause (robots vs HTTP status vs network) instead of a vague
# "failed or disallowed".
_last_reason = [""]

# A plain-text activity log so the operator can SEE the crawl really fetching
# pages (every request, its outcome, and what was extracted) — instead of a
# silent job that might be failing behind the scenes. Tail it live, or read it
# from the B2B page.
LOG_PATH = Path(__file__).parent.parent.parent / "data" / "institutional_crawl.log"
_LOG_MAX_BYTES = 512 * 1024  # keep the last ~0.5 MB, trimmed on rotation


def log(msg: str):
    """Append one timestamped line. Never raises — logging must not break a
    crawl."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
        with open(LOG_PATH, "a") as f:
            f.write(line)
        if LOG_PATH.stat().st_size > _LOG_MAX_BYTES:
            data = LOG_PATH.read_bytes()[-_LOG_MAX_BYTES // 2:]
            LOG_PATH.write_bytes(b"...(trimmed)...\n" + data)
    except Exception:
        pass


def read_log(max_lines: int = 300) -> str:
    """The tail of the crawl log for the UI."""
    try:
        lines = LOG_PATH.read_text().splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def _robots_ok(url: str) -> bool:
    """Best-effort robots.txt check, cached per host. Unreachable/unparseable
    robots => allow (the standard default), but the delay still applies."""
    host = urlparse(url).netloc
    if host not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = httpx.get(f"{urlparse(url).scheme}://{host}/robots.txt",
                          headers={"User-Agent": USER_AGENT}, timeout=10,
                          follow_redirects=True)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots_cache[host] = rp
    return _robots_cache[host].can_fetch(USER_AGENT, url)


def fetch(url: str, timeout=20) -> str:
    """Polite fetch: robots check + global 1.5s pacing + identified UA.
    Returns '' on any failure or disallow. Every outcome is logged so the
    operator can see real network activity."""
    if not _HTTPX:
        _last_reason[0] = "httpx not installed"
        log("FETCH skipped — httpx not installed (crawling disabled)")
        return ""
    if not url:
        _last_reason[0] = "empty url"
        return ""
    if not _robots_ok(url):
        _last_reason[0] = "robots.txt disallowed"
        log(f"ROBOTS blocked  {url}")
        return ""
    wait = FETCH_DELAY_S - (time.time() - _last_fetch[0])
    if wait > 0:
        time.sleep(wait)
    _last_fetch[0] = time.time()
    t0 = time.time()
    try:
        r = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                      follow_redirects=True)
        ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            _last_reason[0] = f"HTTP {r.status_code}"
            log(f"HTTP {r.status_code}  {url}  ({ms} ms)")
            return ""
        body = r.text or ""
        _last_reason[0] = "ok"
        log(f"OK   {len(body):>7} bytes  {ms:>5} ms  {url}")
        return body
    except Exception as e:
        _last_reason[0] = f"network error: {type(e).__name__}"
        ms = int((time.time() - t0) * 1000)
        log(f"FAIL  {type(e).__name__}: {str(e)[:120]}  {url}  ({ms} ms)")
        return ""


def last_fetch_reason() -> str:
    return _last_reason[0]


def _text_of(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


# ------------------------------------------------------------ AMI locator
_SOCIAL_HOSTS = ("facebook.", "instagram.", "twitter.", "x.com", "linkedin.",
                 "youtube.", "pinterest.", "google.", "amshq.", "amiusa.",
                 "montessori-ami.org", "mailto:", "tel:")


def parse_ami_state_page(html: str):
    """Extract (school_name, website) pairs from an AMI locator state page.
    Strategy 1: heading followed by an external link. Strategy 2: external
    links whose anchor text reads like an organization name. Returns [] when
    nothing matches — the caller flags that loudly (selector breakage)."""
    out, seen = [], set()

    # Strategy 1: heading text + the first external href after it.
    for m in re.finditer(r"(?is)<h[2-4][^>]*>(.*?)</h[2-4]>(.{0,600}?)"
                         r"<a[^>]+href=[\"'](https?://[^\"']+)[\"']", html or ""):
        name = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", m.group(1))).strip()
        href = m.group(3)
        if (not name or len(name) < 4 or len(name) > 90
                or any(s in href.lower() for s in _SOCIAL_HOSTS)):
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append((name, href))

    # Strategy 2: anchors whose text looks like a school/organization name.
    if not out:
        for m in re.finditer(r"(?is)<a[^>]+href=[\"'](https?://[^\"']+)[\"'][^>]*>(.*?)</a>",
                             html or ""):
            href, inner = m.group(1), m.group(2)
            name = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", inner)).strip()
            if (len(name) < 6 or len(name) > 90
                    or any(s in href.lower() for s in _SOCIAL_HOSTS)
                    or not re.search(r"(?i)montessori|school|academy|children",
                                     name)):
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                out.append((name, href))
    return out


def crawl_ami_state(state: str):
    """One AMI locator state page → prospect dicts. Returns (prospects, note)."""
    slug = AMI_SLUGS.get(state.upper())
    if not slug:
        log(f"STATE {state}: no AMI slug configured — skipped")
        return [], f"{state}: no AMI slug configured"
    url = AMI_BASE + slug + "/"
    log(f"STATE {state}: fetching AMI locator {url}")
    html = fetch(url)
    if not html:
        reason = last_fetch_reason()
        log(f"STATE {state}: locator fetch FAILED — {reason}")
        if reason == "HTTP 404":
            return [], (f"{state}: AMI has no per-state page (HTTP 404). Its locator is now a "
                        "single interactive map, so crawling by state can't work — use the "
                        "State licensing roster import below instead.")
        return [], f"{state}: AMI fetch failed — {reason} ({url})"
    pairs = parse_ami_state_page(html)
    log(f"STATE {state}: parsed {len(pairs)} school(s) from {len(html)} bytes")
    if not pairs:
        return [], (f"{state}: AMI page fetched ({len(html)} bytes) but ZERO "
                    "entries parsed — selector likely broken, inspect " + url)
    prospects = [new_prospect(name, "montessori_school", state,
                              f"amiusa.org locator ({state})", site)
                 for name, site in pairs]
    return prospects, f"{state}: {len(prospects)} schools from AMI locator"


# ---------------------------------------------------- AMI locator via JSON
# The live AMI locator is a Squarespace collection; ?format=json-pretty returns
# every school as a structured item (title, address, categories, and a body
# containing the school's website + administrator + email). We page through it
# once nationally and filter locally — far more reliable than scraping HTML.
_AMI_JSON_URL = "https://www.amiusa.org/school-locator1"
_US_STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "washington dc": "DC", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}


def _item_state(item) -> str:
    """2-letter state for an AMI item: from the address ('City, ST, ZIP') first,
    then the first category ('Georgia', 'Washington DC')."""
    loc = item.get("location") or {}
    a2 = loc.get("addressLine2") or loc.get("addressTitle") or ""
    m = re.search(r",\s*([A-Za-z]{2})\b(?:\s*,?\s*\d{5})?\s*$", a2.strip())
    if m and m.group(1).upper() in set(_US_STATE_ABBR.values()):
        return m.group(1).upper()
    for cat in (item.get("categories") or []):
        ab = _US_STATE_ABBR.get(cat.strip().lower())
        if ab:
            return ab
    return ""


def _item_website(item) -> str:
    """The school's own website — the first external, non-Squarespace/social
    link in the item body."""
    for href in re.findall(r'href=[\"\'](https?://[^\"\']+)[\"\']', item.get("body") or ""):
        h = href.lower()
        if ("squarespace" in h or "amiusa.org" in h
                or any(s in h for s in _SOCIAL_HOSTS)):
            continue
        return href
    return ""


def crawl_ami_schools(states=None, max_pages=100):
    """Fetch the national AMI locator (Squarespace JSON), page through it, and
    return (prospects, note). `states` = set of 2-letter abbrevs to keep, or
    None for all. Contact name/email/evidence found in the AMI listing itself
    are captured as 'listed' (the second hop still verifies from the org site)."""
    import json as _json
    want = {s.upper() for s in states} if states else None
    url = _AMI_JSON_URL + "?format=json-pretty"
    prospects, seen, pages, scanned, kept_states = [], set(), 0, 0, set()
    while url and pages < max_pages:
        pages += 1
        log(f"AMI: fetching locator JSON page {pages}")
        raw = fetch(url)
        if not raw:
            if pages == 1:
                return [], f"AMI locator fetch failed — {last_fetch_reason()}"
            break
        try:
            data = _json.loads(raw)
        except ValueError:
            return [], "AMI locator did not return JSON (Squarespace format change?)"
        items = data.get("items") or []
        scanned += len(items)
        for it in items:
            name = (it.get("title") or "").strip()
            if not name or name.lower() in seen:
                continue
            st = _item_state(it)
            if want and st not in want:
                continue
            seen.add(name.lower())
            kept_states.add(st)
            site = _item_website(it)
            p = new_prospect(name, "montessori_school", st,
                             "amiusa.org AMI school locator", site)
            # AMI lists the administrator + email right in the body — capture
            # them as a starting contact (the org's own site verifies later).
            btext = _text_of(it.get("body") or "")
            own_domain = urlparse(site).netloc.lower().removeprefix("www.") if site else ""
            cname, ctitle, cemail = _find_contact(btext, own_domain)
            if cemail:
                p["email"] = cemail
                p["contact_confidence"] = "directory"  # from AMI's listing, not yet verified
            if cname:
                p["contact_name"], p["contact_title"] = cname, ctitle
            detail = "https://www.amiusa.org" + (it.get("fullUrl") or "")
            p["evidence"] = _find_evidence(btext, detail)
            prospects.append(p)
        pag = data.get("pagination") or {}
        if pag.get("nextPage") and pag.get("nextPageOffset"):
            url = _AMI_JSON_URL + f"?format=json-pretty&offset={pag['nextPageOffset']}"
        else:
            url = None
    log(f"AMI: scanned {scanned} schools across {pages} page(s); kept "
        f"{len(prospects)}" + (f" for {sorted(want)}" if want else " (all states)"))
    note = (f"AMI locator: {len(prospects)} schools"
            + (f" in {', '.join(sorted(want))}" if want else "")
            + f" (scanned {scanned} nationally)")
    return prospects, note


# ------------------------------------------------- second hop: org website
_CONTACT_SLUGS = ("", "contact", "contact-us", "about", "about-us", "staff",
                  "our-team", "faculty", "admissions", "our-school")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMAIL_JUNK = ("example.", "sentry", "wixpress", "godaddy", "@sentry",
               "noreply", "no-reply", "@2x", ".png", ".jpg", ".gif", ".webp",
               "yourdomain", "domain.com", "email.com")
_TITLE_WORDS = (r"Head of School|Executive Director|Program Director|School "
                r"Director|Directress|Director|Principal|Administrator|Owner|Founder")

_EVIDENCE_HINTS = re.compile(
    r"(?i)montessori|toddler|primary|preschool|pre-k|kindergarten|founded|"
    r"since 19|since 20|AMI|AMS|accredit|classroom|reggio|nature|outdoor")


def _find_contact(text: str, own_domain: str):
    """(name, title, email) from a page's text — only what's actually there."""
    email = ""
    for cand in _EMAIL_RE.findall(text):
        cl = cand.lower()
        if any(j in cl for j in _EMAIL_JUNK):
            continue
        # Prefer an address on the org's own domain; keep the first otherwise.
        if own_domain and own_domain in cl:
            email = cand
            break
        email = email or cand
    name, title = "", ""
    m = re.search(r"([A-Z][a-z]+(?: [A-Z][A-Za-z'’\-.]+){1,2})\s*[,–—-]\s*"
                  rf"({_TITLE_WORDS})", text)
    if not m:
        m2 = re.search(rf"({_TITLE_WORDS})[:,]?\s+"
                       r"([A-Z][a-z]+(?: [A-Z][A-Za-z'’\-.]+){1,2})", text)
        if m2:
            title, name = m2.group(1), m2.group(2)
    else:
        name, title = m.group(1), m.group(2)
    return name, title, email


def _find_evidence(text: str, url: str):
    """One distinctive, actually-retrieved sentence about the org — the raw
    material that keeps a draft from being a mail merge."""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        s = sent.strip()
        if 40 <= len(s) <= 220 and _EVIDENCE_HINTS.search(s) \
                and not _EMAIL_RE.search(s) and "cookie" not in s.lower():
            return {"fact": s, "url": url}
    return None


def harvest_org(website: str):
    """Second hop: the org's own site. Returns a dict of updates for the
    prospect — empty fields where the site didn't provide the information."""
    upd = {"contact_name": "", "contact_title": "", "email": "",
           "contact_confidence": "none", "evidence": None}
    if not website:
        return upd
    if not website.startswith("http"):
        website = "https://" + website
    own_domain = urlparse(website).netloc.lower().removeprefix("www.")
    base = f"{urlparse(website).scheme}://{urlparse(website).netloc}"
    log(f"RESEARCH {own_domain}: visiting up to {len(_CONTACT_SLUGS)} pages")
    for slug in _CONTACT_SLUGS:
        url = website if slug == "" else urljoin(base + "/", slug)
        html = fetch(url)
        if not html or len(html) < 300:
            continue
        text = _text_of(html)
        if upd["evidence"] is None:
            upd["evidence"] = _find_evidence(text, url)
        name, title, email = _find_contact(text, own_domain)
        if email and not upd["email"]:
            upd["email"] = email
            upd["contact_confidence"] = "verified"  # from the org's own site
        if name and not upd["contact_name"]:
            upd["contact_name"], upd["contact_title"] = name, title
        if upd["email"] and upd["contact_name"] and upd["evidence"]:
            break
    log(f"RESEARCH {own_domain}: email={'yes' if upd['email'] else 'no'} "
        f"name={'yes' if upd['contact_name'] else 'no'} "
        f"evidence={'yes' if upd['evidence'] else 'no'} "
        f"→ {upd['contact_confidence']}")
    return upd
