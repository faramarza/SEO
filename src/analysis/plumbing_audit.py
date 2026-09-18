"""Technical Plumbing Audit — read-only detection of where Google's crawl and
indexing are being wasted on a Magento 2.4.7 store. Phase 1: indexation waste
(A), canonicals (B), internal linking (C).

This module is PURE and testable: it takes normalized inputs and returns
findings. It never crawls, never calls an API, and never proposes applying a
fix — fixes are described, with where they live in Magento, as proposals only.

Impact is always MEASURED — counts and ratios from the data — never an
invented traffic number. Every finding carries the count, up to 5 example
URLs, the data source it came from, the proposed fix, where that fix lives,
and the risk of making it.

Normalized inputs (app.py adapts existing crawl/GSC data into these):
  crawl_rows : [{"url","status"(int),"canonical"(str|""),"robots"(str, meta +
                 X-Robots-Tag lowercased),"depth"(int|None),"inlinks"(int),
                 "title","asset_type","in_sitemap"(bool)}]
  gsc_pages  : [{"url","clicks","impressions","position"}]
  sitemap_urls   : set[str]
  robots_disallow: [str]  (Disallow path prefixes from robots.txt)
  inspections    : {url: {"coverage","google_canonical","user_canonical",
                          "robots_state"}}  (from GSC URL Inspection; sampled)
"""

import re
from urllib.parse import urlparse, parse_qs

# ── Magento URL-junk signatures ──────────────────────────────────────────
# Query-string keys that create crawlable duplicate/near-duplicate URLs.
PARAM_KEYS = ("p", "product_list_order", "product_list_limit",
              "product_list_mode", "product_list_dir", "cat", "price", "sid",
              "limit", "mode", "dir", "order")
# Layered-navigation filter params vary per attribute; treat ANY extra query
# key on a category/anchor URL as a filter facet (the multiplicative killer).
SYSTEM_PATHS = ("/catalogsearch/", "/catalog/product/view/",
                "/catalog/category/view/", "/customer/", "/checkout/",
                "/wishlist/", "/review/", "/sendfriend/", "/catalog/product_compare/",
                "/paypal/", "/onestepcheckout/", "/downloadable/")
# Action URLs that MUST NOT be followed by a crawler (mutate cart/session);
# recorded here so the crawl layer can exclude them.
ACTION_PATTERNS = ("/checkout/cart/add", "/wishlist/index/add",
                   "/catalog/product_compare/add", "/customer/account/logout",
                   "checkout/cart/add", "wishlist/index/add")


def _q_keys(url):
    try:
        return set(parse_qs(urlparse(url).query).keys())
    except Exception:
        return set()


def is_parameter_url(url):
    ks = {k.lower() for k in _q_keys(url)}
    if not ks:
        return False
    if ks & set(PARAM_KEYS):
        return True
    # any other query key on a NON-system URL is a layered-nav facet
    return not is_system_url(url)


def is_system_url(url):
    p = urlparse(url).path.lower()
    return any(s in p for s in SYSTEM_PATHS)


def _norm(url):
    """Compare URLs ignoring scheme, www, and trailing slash."""
    m = re.match(r"https?://(?:www\.)?([^?#]*)", (url or "").rstrip("/").lower())
    return m.group(1) if m else (url or "").lower()


# ── Magento fix knowledge (proposals only) ───────────────────────────────
# Keyed by finding id: (proposed_fix, magento_location, risk).
FIXES = {
    "param_urls": (
        "Disallow the crawlable parameter/filter URLs and keep a self-canonical "
        "to the clean category. For layered navigation, add rel=noindex,follow or "
        "block the filter params.",
        "robots.txt (Disallow: /*?product_list_ , /*?price= , /*?p= …) + Stores › "
        "Configuration › Catalog › Catalog › Storefront (canonical = Yes). Core "
        "Magento does not noindex filters — an SEO extension (Amasty/Mirasvit/Mageworx) "
        "or a robots rule handles it.",
        "low — robots.txt is reversible; test in GSC robots tester first"),
    "system_urls": (
        "Block Magento system/action paths from crawling — they should never be "
        "indexable.",
        "robots.txt Disallow: /catalogsearch/ /checkout/ /customer/ /wishlist/ "
        "/review/ /catalog/product_compare/ (Magento's default robots covers most; "
        "yours was likely overwritten).",
        "low"),
    "gsc_not_in_sitemap": (
        "Decide per URL: if it should rank, add it to the sitemap; if it's junk, "
        "block/deindex it. Impressions on non-sitemap URLs usually means indexed "
        "junk.",
        "Marketing › SEO & Search › Site Map (regenerate on cron) / robots.txt.",
        "low"),
    "sitemap_bad": (
        "Remove URLs from the sitemap that redirect, 404, are noindexed, or "
        "canonicalize elsewhere — a sitemap should list only indexable 200s.",
        "Marketing › SEO & Search › Site Map settings + product/category visibility.",
        "low"),
    "missing_canonical": (
        "Turn on canonical tags for categories and products.",
        "Stores › Configuration › Catalog › Catalog › Storefront › 'Use Canonical "
        "Link Meta Tag for Categories/Products' = Yes.",
        "low — a standard, safe Magento setting"),
    "canonical_non200": (
        "Fix canonicals that point at redirects/404s — they waste the signal.",
        "Usually a URL-rewrite or category-path issue; check Marketing › URL Rewrites.",
        "medium — verify each target before changing"),
    "google_canonical_mismatch": (
        "Google is ignoring your declared canonical and picking its own — usually "
        "means duplicate content or a weak canonical signal. Consolidate.",
        "Investigate per URL (duplication, thin variants, parameter dupes).",
        "medium"),
    "deep_pages": (
        "Pull deep product/category pages closer to the homepage with contextual "
        "internal links and better category structure (≤3 clicks).",
        "Theme/category structure + internal links (content work, not a config).",
        "low"),
    "orphans": (
        "Add internal links to orphaned pages, or drop them from the sitemap if "
        "they shouldn't rank. Orphans get crawled rarely and rank poorly.",
        "Internal linking (content/theme work).",
        "low"),
    "underlinked_top": (
        "Your highest-impression products have too few internal links — add "
        "contextual links from strong related pages and blog posts.",
        "Internal linking (content work).",
        "low"),
    "blog_no_commercial": (
        "Blog posts that never link to a product/category leak their authority. "
        "Add a relevant product/category link to each.",
        "Content work (blog templates / posts).",
        "low"),
}

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _finding(fid, title, category, severity, urls, source, metric=None):
    urls = list(dict.fromkeys(urls))   # dedup, keep order
    fix, loc, risk = FIXES.get(fid, ("", "", ""))
    return {"id": fid, "title": title, "category": category,
            "severity": severity, "url_count": len(urls),
            "examples": urls[:5], "source": source, "metric": metric,
            "proposed_fix": fix, "magento_location": loc, "risk": risk}


# ── Checks ───────────────────────────────────────────────────────────────
def _crawl_index(crawl_rows):
    return {r["url"]: r for r in crawl_rows or []}


def check_A_indexation(crawl_rows, gsc_pages, sitemap_urls):
    """A. Indexation waste."""
    out = []
    crawl_by = _crawl_index(crawl_rows)
    all_urls = ([r["url"] for r in crawl_rows or []]
                + [g["url"] for g in gsc_pages or []])
    all_urls = list(dict.fromkeys(all_urls))

    param = [u for u in all_urls if is_parameter_url(u)]
    if param:
        out.append(_finding("param_urls",
            "Crawlable parameter / layered-navigation URLs", "A", "high",
            param, "own crawl + GSC",
            metric=f"{len(param)} parameter URLs seen"))

    system = [u for u in all_urls if is_system_url(u)]
    if system:
        out.append(_finding("system_urls",
            "Magento system/action URLs reachable or indexed", "A", "high",
            system, "own crawl + GSC"))

    sm = {_norm(u) for u in (sitemap_urls or set())}
    not_in_sm = [g["url"] for g in gsc_pages or []
                 if (g.get("impressions") or 0) > 0
                 and _norm(g["url"]) not in sm
                 and not is_parameter_url(g["url"])
                 and not is_system_url(g["url"])]
    if not_in_sm:
        not_in_sm.sort(key=lambda u: -next((g.get("impressions", 0)
                       for g in gsc_pages if g["url"] == u), 0))
        out.append(_finding("gsc_not_in_sitemap",
            "Pages with GSC impressions that are NOT in the sitemap", "A", "medium",
            not_in_sm, "GSC vs sitemap"))

    # sitemap URLs that the crawl shows are not clean 200 indexable pages
    bad = []
    for u in (sitemap_urls or set()):
        r = crawl_by.get(u)
        if not r:
            continue
        st = r.get("status")
        can = _norm(r.get("canonical") or "")
        if (st and st != 200) or "noindex" in (r.get("robots") or "") \
                or (can and can != _norm(u)):
            bad.append(u)
    if bad:
        out.append(_finding("sitemap_bad",
            "Sitemap URLs that redirect / 404 / noindex / canonicalize away",
            "A", "medium", bad, "sitemap vs crawl"))
    return out


def check_B_canonicals(crawl_rows, inspections):
    """B. Canonicals."""
    out = []
    crawl_by = _crawl_index(crawl_rows)
    html = [r for r in crawl_rows or []
            if (r.get("status") == 200) and "noindex" not in (r.get("robots") or "")]

    missing = [r["url"] for r in html if not (r.get("canonical") or "").strip()]
    if missing:
        out.append(_finding("missing_canonical",
            "Indexable pages with no canonical tag", "B", "high",
            missing, "own crawl"))

    non200 = []
    for r in html:
        can = r.get("canonical") or ""
        if not can:
            continue
        tgt = crawl_by.get(can) or next(
            (x for x in crawl_rows if _norm(x["url"]) == _norm(can)), None)
        if tgt and tgt.get("status") and tgt["status"] != 200:
            non200.append(r["url"])
    if non200:
        out.append(_finding("canonical_non200",
            "Canonical points to a non-200 (redirect/404) URL", "B", "high",
            non200, "own crawl"))

    mism = [u for u, ins in (inspections or {}).items()
            if ins.get("google_canonical") and ins.get("user_canonical")
            and _norm(ins["google_canonical"]) != _norm(ins["user_canonical"])]
    if mism:
        out.append(_finding("google_canonical_mismatch",
            "Google-selected canonical differs from your declared canonical",
            "B", "medium", mism, "GSC URL Inspection"))
    return out


def check_C_internal_links(crawl_rows, gsc_pages, sitemap_urls,
                           blog_no_commercial=None, max_depth=3):
    """C. Internal linking."""
    out = []
    inl_by = {_norm(r["url"]): (r.get("inlinks") or 0) for r in crawl_rows or []}

    deep = [r["url"] for r in crawl_rows or []
            if r.get("asset_type") in ("product", "category")
            and (r.get("depth") is not None) and r["depth"] > max_depth]
    if deep:
        out.append(_finding("deep_pages",
            f"Product/category pages deeper than {max_depth} clicks from home",
            "C", "medium", deep, "own crawl"))

    known = {_norm(u) for u in (sitemap_urls or set())} | \
            {_norm(g["url"]) for g in gsc_pages or []}
    orphans = [r["url"] for r in crawl_rows or []
               if (r.get("inlinks") or 0) == 0 and _norm(r["url"]) in known
               and r.get("status") == 200 and not is_system_url(r["url"])]
    if orphans:
        out.append(_finding("orphans",
            "Orphan pages: in sitemap/GSC but with zero internal inlinks",
            "C", "high", orphans, "own crawl"))

    # top products by impressions with < 3 inlinks
    prods = sorted([g for g in gsc_pages or []], key=lambda g: -(g.get("impressions") or 0))
    under = []
    for g in prods[:60]:
        if inl_by.get(_norm(g["url"]), 99) < 3:
            r = next((x for x in crawl_rows if _norm(x["url"]) == _norm(g["url"])), None)
            if r and r.get("asset_type") == "product":
                under.append(g["url"])
        if len(under) >= 20:
            break
    if under:
        out.append(_finding("underlinked_top",
            "Top products by impressions with fewer than 3 internal inlinks",
            "C", "medium", under, "GSC + crawl"))

    if blog_no_commercial:
        out.append(_finding("blog_no_commercial",
            "Blog posts with no link to any product or category page",
            "C", "low", list(blog_no_commercial), "own crawl"))
    return out


def junk_ratio(crawl_rows, gsc_pages):
    """Share of known URLs that are junk (parameter/system) vs pages we'd want
    ranked. Measured, not estimated."""
    urls = list(dict.fromkeys([r["url"] for r in crawl_rows or []]
                              + [g["url"] for g in gsc_pages or []]))
    if not urls:
        return {"total": 0, "junk": 0, "ratio": 0.0}
    junk = sum(1 for u in urls if is_parameter_url(u) or is_system_url(u))
    return {"total": len(urls), "junk": junk, "ratio": round(junk / len(urls), 3)}


def run_audit(crawl_rows=None, gsc_pages=None, sitemap_urls=None,
              robots_disallow=None, inspections=None, blog_no_commercial=None):
    """Run every Phase-1 check and rank findings by measured impact."""
    crawl_rows = crawl_rows or []
    gsc_pages = gsc_pages or []
    sitemap_urls = set(sitemap_urls or set())
    findings = (check_A_indexation(crawl_rows, gsc_pages, sitemap_urls)
                + check_B_canonicals(crawl_rows, inspections)
                + check_C_internal_links(crawl_rows, gsc_pages, sitemap_urls,
                                         blog_no_commercial))
    # Rank: severity, then how many URLs it affects.
    findings.sort(key=lambda f: (_SEV_RANK.get(f["severity"], 3), -f["url_count"]))
    jr = junk_ratio(crawl_rows, gsc_pages)
    summary = (
        f"Of {jr['total']} known URLs (crawled + seen in GSC), {jr['junk']} "
        f"({int(jr['ratio']*100)}%) are Magento junk — parameter, filter, or "
        "system/action URLs that shouldn't be crawled or indexed. The remaining "
        f"{jr['total']-jr['junk']} are real pages you'd want ranked. "
        + ("Crawl budget is being wasted; clean the junk before scaling content."
           if jr['ratio'] >= 0.2 else
           "Junk ratio is moderate; still worth fixing the top findings.")
    ) if jr['total'] else "No URL data yet — run a crawl first."
    return {"findings": findings, "summary": summary, "junk": jr,
            "counts": {"findings": len(findings),
                       "high": sum(1 for f in findings if f["severity"] == "high")}}
