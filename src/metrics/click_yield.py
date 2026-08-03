"""Click-yield analysis — the honest "you rank but don't get clicked" engine.

The regular evaluation only builds page-assets from GSC's TOP 100 pages by
impressions, so it is structurally blind to the long tail. This module pulls the
FULL GSC page set (every page with an impression) and answers the two questions
that the top-100 view cannot:

  1. YIELD — of everything Google shows, how much converts to clicks?
       productive (≥1 click) / ranking-no-click (shown, never clicked) /
       barely-visible (<30 impr).  A store can have huge impressions and a
       catastrophic sitewide CTR; this surfaces that.

  2. WINNABLE — category pages ranking position 4–20 for GENERIC (non-brand)
       demand with ~0 clicks. These are the reseller-winnable queries (no brand
       incumbent) that are one SERP-page from real clicks. Brand and exact-
       product-name queries (the reseller trap — you rank but the brand/Amazon
       wins the click) are excluded ON PURPOSE.

Each winnable page is enriched with its real current <title>/meta (fetched live,
so rewrites build on real words, never invented ones), an honest query-front-
loaded title rewrite (or "title already fine — fix position" when the query is
present), and internal-link SOURCE pages found by topical overlap in GSC (no
crawl). Results are cached to data/click_yield.json.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "click_yield.json"

RANKING_MIN_IMPR = 30          # floor to call a page "ranking but not clicked"
WINNABLE_POS = (4.0, 20.0)     # position band where a push actually pays
WINNABLE_MIN_IMPR = 150        # a winnable query needs real demand
TREND_DAYS = 28                # momentum window: recent 28d vs prior 28d
TREND_MIN_DELTA = 3.0          # ignore sub-3-position wobble (GSC position is noisy)
MAX_TITLE = 60
UA = "Mozilla/5.0 (compatible; AlphabetTrains-SEO-Crawler/1.0; +click-yield)"

# Brand / navigational tokens = the reseller trap; a query containing one is a
# brand's own name and cannot be won by a reseller. Extend via config if needed.
BRAND_TOKENS = {
    "iseeme", "i see me", "guidecraft", "guide craft", "storytime",
    "melissa", "doug", "hape",
}
STOP = {"the", "a", "an", "for", "to", "of", "and", "with", "your", "you", "in",
        "on", "best", "top", "buy", "shop", "kids", "kid", "toddler", "toddlers",
        "baby", "babies", "toys", "toy", "gift", "gifts"}


# --------------------------------------------------------------------- helpers
def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in STOP and len(w) > 2}


def _is_brand(query: str, extra: Optional[set] = None) -> bool:
    ql = (query or "").lower()
    toks = BRAND_TOKENS | (extra or set())
    return any(b in ql for b in toks)


# Search operators (site:, inurl:, …) and bare domains aren't demand — they're
# diagnostic/navigational noise. A page "ranks" for `site:yourdomain.com` because
# the operator lists the whole site, not because anyone wants that page.
_OPERATOR_RE = re.compile(
    r"\b(site|inurl|intitle|intext|allintitle|allinurl|cache|related|filetype|link)\s*:",
    re.I)
_URLISH_RE = re.compile(r"https?://|www\.|\.(com|net|org|io|co|shop|store)\b", re.I)


def _site_identity(config: dict):
    """Return (domain, {self/brand forms}) derived from the GSC property so we can
    exclude the store's OWN domain and brand name — those are navigational, not
    winnable category demand."""
    prop = (config or {}).get("data_sources", {}).get("gsc", {}).get("property_url", "") or ""
    dom = re.sub(r"^sc-domain:", "", prop)
    dom = re.sub(r"^https?://", "", dom).replace("www.", "").strip("/").lower()
    name = dom.rsplit(".", 1)[0] if "." in dom else dom  # e.g. "alphabet-trains"
    forms = {dom, name, name.replace("-", " "), name.replace("-", "")}
    return dom, {f for f in forms if f and len(f) > 2}


def _is_junk_query(query: str, self_forms: set) -> bool:
    """Operator/navigational/self-referential — never a winnable demand query."""
    ql = (query or "").strip().lower()
    if not ql:
        return True
    if _OPERATOR_RE.search(ql) or _URLISH_RE.search(ql):
        return True
    return any(f in ql for f in self_forms)


_TITLE_SMALL = {"a", "an", "and", "the", "for", "to", "of", "with", "in", "on",
                "or", "by", "your"}


def _title_case(s: str) -> str:
    """Natural title case: capitalize significant words, keep small joining words
    lowercase (except first). No forced ALL-CAPS, no separators."""
    words = (s or "").split()
    out = []
    for i, w in enumerate(words):
        out.append(w if (i and w.lower() in _TITLE_SMALL) else (w[:1].upper() + w[1:]))
    return " ".join(out)


def _title_rewrite(query: str, current_title: str) -> Optional[str]:
    """Decide whether the page's <title> genuinely needs to change to capture this
    query — and if so, propose a NATURAL, readable title, never a mechanical
    "Query | Old Title" mash-up.

    Two honest cases:
      • The title ALREADY carries the query's meaningful words (ignoring stopwords
        like kids/toys/best and word order/hyphenation) → return None. The title
        isn't the problem; the gap is position/authority, so the fix is internal
        links, not a retitle. (This is the "Montessori Problem-Solving Toys" vs
        "kids problem solving toys" case — the title is fine.)
      • The title truly MISSES the query's core words → suggest a clean, natural
        title that leads with the searcher's phrase, Title-Cased, with NO pipes/
        dashes/colons (Google rewrites over-templated titles and they read as
        boilerplate), no mid-word truncation, and no fabricated brand suffix. The
        operator can weave their brand in naturally; if even the bare phrase won't
        fit in 60 chars, defer to the grounded CTR-rewrite tool (return None)."""
    q = (query or "").strip()
    cur = (current_title or "").strip()
    if not q:
        return None
    # Meaningful-token overlap (stopwords excluded, so "kids"/"toys"/"best" don't
    # force a rewrite, and "problem-solving" == "problem solving").
    if cur and not (_tokens(q) - _tokens(cur)):
        return None
    natural = _title_case(q)
    if len(natural) <= MAX_TITLE:
        return natural
    return None  # can't fit a natural title deterministically — defer to the LLM tool


def _fetch(url: str, timeout: int = 18) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _extract_meta(html: str):
    def tag(pat):
        m = re.search(pat, html, re.I | re.S)
        return unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""
    title = tag(r"<title[^>]*>(.*?)</title>")
    h1 = tag(r"<h1[^>]*>(.*?)</h1>")
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                  html, re.I | re.S)
    meta = unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""
    return title, meta, h1


def _client_from_config(config: dict):
    from src.data_sources.gsc_client import GSCClient
    gsc = (config or {}).get("data_sources", {}).get("gsc", {})
    cred = gsc.get("credentials_path") or ""
    if cred and not Path(cred).is_absolute():
        cred = str(PROJECT_ROOT / cred)
    return GSCClient(site_url=gsc.get("property_url", ""), credentials_path=cred or None)


# ----------------------------------------------------------------- computation
def compute_click_yield(config: dict, days: int = 90, max_winnable: int = 15,
                        fetch_titles: bool = True, extra_brand_tokens=None) -> dict:
    """Pull the full GSC page set, compute yield buckets + enriched winnable pages.
    Returns a JSON-serializable dict (also written to CACHE_PATH by save())."""
    extra_brand = {t.lower() for t in (extra_brand_tokens or [])}
    _site_dom, self_forms = _site_identity(config)
    client = _client_from_config(config)
    pages = client.get_page_data(days=days)  # {url: {clicks,impressions,ctr,position,queries[:10]}}

    if not pages:
        return {"available": False, "reason": "GSC returned no pages (check credentials/property).",
                "generated_at": datetime.now().isoformat(timespec="seconds"), "days": days}

    n = len(pages)
    total_clicks = sum(p.get("clicks", 0) for p in pages.values())
    total_impr = sum(p.get("impressions", 0) for p in pages.values())
    productive = [u for u, p in pages.items() if p.get("clicks", 0) >= 1]
    ranking_noclick = [u for u, p in pages.items()
                       if p.get("clicks", 0) == 0 and p.get("impressions", 0) >= RANKING_MIN_IMPR]
    barely = [u for u, p in pages.items()
              if p.get("clicks", 0) == 0 and 1 <= p.get("impressions", 0) < RANKING_MIN_IMPR]

    # topical-overlap index for internal-link sourcing (crawl-free)
    src_index = {u: set().union(*[_tokens(q["query"]) for q in p.get("queries", [])] or [set()])
                 for u, p in pages.items()}

    # pick the best non-brand winnable query per page
    cands = []
    lo, hi = WINNABLE_POS
    for url, p in pages.items():
        if p.get("clicks", 0) > 1 or "shop-by-brands" in url:
            continue
        best = None
        for q in p.get("queries", []):
            if _is_brand(q["query"], extra_brand) or _is_junk_query(q["query"], self_forms):
                continue
            if q["impressions"] < WINNABLE_MIN_IMPR or not (lo <= q["position"] <= hi):
                continue
            if best is None or q["impressions"] > best["impressions"]:
                best = q
        if best:
            cands.append((url, best))
    cands.sort(key=lambda x: -x[1]["impressions"])
    cands = cands[:max_winnable]

    winnable = []
    for url, q in cands:
        title = meta = h1 = ""
        if fetch_titles:
            try:
                title, meta, h1 = _extract_meta(_fetch(url))
            except Exception:
                pass
            time.sleep(0.35)
        rewrite = _title_rewrite(q["query"], title)
        # internal-link sources: pages ranking for terms overlapping the query
        qtok = _tokens(q["query"])
        srcs = []
        for surl, stok in src_index.items():
            if surl == url or "shop-by-brands" in surl:
                continue
            overlap = qtok & stok
            if overlap:
                weight = len(overlap) + (1 if "/blog/" in surl else 0)
                srcs.append((weight, surl, sorted(overlap)))
        srcs.sort(key=lambda x: -x[0])
        winnable.append({
            "url": url,
            "path": urlparse(url).path or url,
            "query": q["query"],
            "impressions": q["impressions"],
            "position": round(q["position"], 1),
            "current_title": title,
            "current_meta": meta,
            "title_rewrite": rewrite,           # None => title already targets the query
            "meta_needs_query": bool(meta) and q["query"].lower() not in (meta or "").lower(),
            "meta_missing": not bool(meta),
            "link_sources": [{"path": urlparse(s[1]).path or s[1], "shares": s[2]}
                             for s in srcs[:4]],
            "asset_type": _asset_type_of(url),
            "trend": "unknown",  # filled below from two GSC windows
        })

    # POSITION MOMENTUM — the one thing a single snapshot can't show: is each
    # winnable page rising toward the click zone or falling away? Diff a recent
    # 28-day window against the prior 28 days. A page that's SLIPPING is urgent to
    # defend (it may fall off page 2); a RISING one needs only a light push. Only
    # moves past TREND_MIN_DELTA count, so we react to real movement, not GSC noise.
    try:
        cur28 = client.positions_for_window(end_offset_days=0, days=TREND_DAYS)
        prev28 = client.positions_for_window(end_offset_days=TREND_DAYS, days=TREND_DAYS)
        for w in winnable:
            key = (w["url"], w["query"])
            c, p = cur28.get(key), prev28.get(key)
            if c is None or p is None:
                w["trend"] = "new"  # not enough history to judge
                continue
            delta = round(p - c, 1)          # + = rose (position number fell), - = dropped
            w["position_delta"] = delta
            w["position_now28"] = round(c, 1)
            w["position_prev28"] = round(p, 1)
            w["trend"] = ("down" if delta <= -TREND_MIN_DELTA
                          else "up" if delta >= TREND_MIN_DELTA else "flat")
    except Exception:
        for w in winnable:
            w["trend"] = "unknown"

    return {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "totals": {
            "pages_with_impressions": n,
            "impressions": total_impr,
            "clicks": total_clicks,
            "sitewide_ctr": round((total_clicks / total_impr * 100), 3) if total_impr else 0.0,
        },
        "buckets": {
            "productive": len(productive),
            "ranking_no_click": len(ranking_noclick),
            "barely_visible": len(barely),
        },
        "winnable": winnable,
    }


def _asset_type_of(url: str) -> str:
    u = url.lower()
    if "/blog/" in u or "/article" in u:
        return "blog"
    if u.rstrip("/").endswith(".html") and "-" in urlparse(u).path:
        # heuristic: category/collection landing vs product — treat listing-y slugs as category
        slug = urlparse(u).path.rsplit("/", 1)[-1]
        if any(k in slug for k in ("toys", "rugs", "stools", "puzzle", "books", "decor", "tables")):
            return "category"
    return "other"


# ------------------------------------------------------------------- cache i/o
def save_click_yield(data: dict, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_click_yield(path: Path = CACHE_PATH) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def winnable_plan_input(cy: Optional[dict]) -> list:
    """Shape the cached winnable pages for action_plan._from_winnable."""
    if not cy or not cy.get("available"):
        return []
    return cy.get("winnable", [])
