"""
Page Quality Scorecard — deterministic, rule-based assessment of product and
category pages across three lenses:

  seo          — title/meta/H1/canonical, thin content, breadcrumbs, links
  revenue_ctr  — rich-result schema (Product/Offer/AggregateRating), above-fold
                 CTA, price/availability — the levers that drive SERP CTR & sales
  cro          — images, alt text, description depth, category product links

Pure function so it runs from both the evaluation workflow (PageAsset) and the
web layer (cached page_metadata dict). No LLM — every finding is checkable.
"""

import re

_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_ALT_RE = re.compile(r'<img\b[^>]*\balt\s*=\s*"[^"]+"', re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b[^>]*\bhref", re.IGNORECASE)
# Breadcrumbs can appear as a nav/list with a breadcrumb class/aria-label or as
# BreadcrumbList schema — the crawler's breadcrumb_links field misses many.
_BREADCRUMB_RE = re.compile(
    r'breadcrumb|aria-label\s*=\s*"[^"]*breadcrumb|itemtype\s*=\s*"[^"]*BreadcrumbList',
    re.IGNORECASE,
)


def _has_breadcrumbs(breadcrumb_links, schema_types, above_fold_html, body_html):
    """Detect breadcrumbs from any reliable signal, not just crawler-extracted
    breadcrumb_links (which misses non-standard markup like Magento's)."""
    if breadcrumb_links:
        return True
    if _has_schema(schema_types, "BreadcrumbList"):
        return True
    html = f"{above_fold_html or ''} {body_html or ''}"
    return bool(_BREADCRUMB_RE.search(html))

# Grade bands
def _grade(score):
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _has_schema(schema_types, *names):
    have = {str(s).lower() for s in (schema_types or [])}
    return any(n.lower() in have for n in names)


def evaluate_page_quality(
    *,
    url="",
    asset_type="other",
    title="",
    meta_description="",
    h1="",
    canonical_url="",
    word_count=0,
    schema_types=None,
    above_fold_html="",
    body_html="",
    internal_outlinks=None,
    breadcrumb_links=None,
    has_crawl_data=True,
):
    """Return a scorecard dict: score, grade, findings[], and coverage flag."""
    asset_type = (asset_type or "other").lower()
    schema_types = schema_types or []
    outlinks = internal_outlinks or []
    breadcrumbs = breadcrumb_links or []
    title = title or ""
    meta_description = meta_description or ""
    h1 = h1 or ""

    findings = []
    score = 100

    def penalize(points, dimension, severity, label, detail, fix):
        nonlocal score
        score -= points
        findings.append({
            "dimension": dimension, "severity": severity,
            "label": label, "detail": detail, "fix": fix, "points": points,
        })

    # ── SEO ──────────────────────────────────────────────
    if not title.strip():
        penalize(12, "seo", "high", "Missing title tag",
                 "No <title> found.", "Add a 50–60 char title targeting the page's primary query.")
    else:
        n = len(title)
        if n > 60:
            penalize(4, "seo", "medium", "Title too long",
                     f"{n} chars — Google truncates past ~60.", "Trim the title to ≤60 characters.")
        elif n < 30:
            penalize(3, "seo", "low", "Title very short",
                     f"{n} chars — leaving CTR/keyword room on the table.", "Expand the title toward 50–60 chars.")

    if not meta_description.strip():
        penalize(8, "seo", "medium", "Missing meta description",
                 "No meta description — Google auto-generates a snippet.", "Write a 140–160 char description with a benefit + call to action.")
    else:
        n = len(meta_description)
        if n > 165 or n < 70:
            penalize(3, "seo", "low", "Meta description length off",
                     f"{n} chars (ideal 140–160).", "Rewrite the meta description to ~150 chars.")

    if not h1.strip():
        penalize(8, "seo", "high", "Missing H1",
                 "No H1 heading detected.", "Add a single H1 with the page's primary keyword.")

    if not (canonical_url or "").strip():
        penalize(5, "seo", "medium", "No canonical tag",
                 "No canonical URL declared.", "Add a self-referencing canonical tag.")

    thin_threshold = 150 if asset_type == "category" else 120
    if has_crawl_data and word_count and word_count < thin_threshold:
        penalize(8, "seo", "high", "Thin content",
                 f"{word_count} words (below {thin_threshold}).",
                 "Add unique, useful copy — buying guidance, specs, or category intro — to reduce thinness.")

    if has_crawl_data and not _has_breadcrumbs(breadcrumbs, schema_types, above_fold_html, body_html):
        penalize(3, "seo", "low", "No breadcrumbs",
                 "No breadcrumb navigation detected.", "Add breadcrumb navigation (also enables BreadcrumbList rich results).")

    if has_crawl_data and not outlinks:
        penalize(4, "seo", "medium", "No internal links out",
                 "Page links to no other internal pages.", "Add internal links to related products/categories or supporting content.")

    # ── REVENUE / CTR (rich-result schema is the big lever) ──
    if has_crawl_data:
        if asset_type == "product":
            if not _has_schema(schema_types, "Product"):
                penalize(12, "revenue_ctr", "high", "No Product schema",
                         "No Product structured data — ineligible for product rich results.",
                         "Add Product JSON-LD (name, image, description, brand, sku).")
            if not _has_schema(schema_types, "Offer", "AggregateOffer"):
                penalize(8, "revenue_ctr", "high", "No price/availability schema",
                         "No Offer markup — price & availability won't show in search.",
                         "Add Offer JSON-LD with price, priceCurrency, and availability.")
            if not _has_schema(schema_types, "AggregateRating", "Review"):
                penalize(10, "revenue_ctr", "high", "No rating/review schema",
                         "No AggregateRating — you lose star ratings in the SERP (a large CTR lever).",
                         "Add AggregateRating/Review JSON-LD once you have reviews; enable review collection.")
        elif asset_type == "category":
            if not _has_schema(schema_types, "ItemList"):
                penalize(6, "revenue_ctr", "medium", "No ItemList schema",
                         "No ItemList markup for the product grid.",
                         "Add ItemList JSON-LD enumerating the listed products.")

        if not _has_schema(schema_types, "BreadcrumbList"):
            penalize(4, "revenue_ctr", "low", "No BreadcrumbList schema",
                     "No breadcrumb structured data — misses breadcrumb rich results.",
                     "Add BreadcrumbList JSON-LD.")

        if above_fold_html and not _ANCHOR_RE.search(above_fold_html):
            penalize(6, "revenue_ctr", "medium", "No above-fold link/CTA",
                     "No clickable <a> above the fold — buyers see no immediate path.",
                     "Add a real anchor CTA (Add to Cart / Shop / view links) in the hero region, not a JS-only span.")

    # ── CRO / content ────────────────────────────────────
    if has_crawl_data and body_html:
        imgs = _IMG_RE.findall(body_html)
        if len(imgs) == 0:
            penalize(5, "cro", "medium", "No images",
                     "No <img> elements found on the page.", "Add product/lifestyle imagery.")
        else:
            with_alt = _IMG_ALT_RE.findall(body_html)
            if len(with_alt) < len(imgs):
                penalize(4, "cro", "low", "Images missing alt text",
                         f"{len(imgs) - len(with_alt)} of {len(imgs)} images lack alt text.",
                         "Add descriptive alt text (helps image SEO and accessibility).")

    if asset_type == "category" and has_crawl_data:
        product_links = sum(1 for l in outlinks if ".html" in str(l.get("target_url", "")).lower())
        if product_links and product_links < 8:
            penalize(6, "cro", "medium", "Few product links",
                     f"Only ~{product_links} product links — thin category.",
                     "Ensure the category lists a healthy set of products (merchandising + internal linking).")

    score = max(0, min(100, score))
    # Order findings by points desc (biggest wins first)
    findings.sort(key=lambda f: f["points"], reverse=True)

    by_dim = {"seo": 0, "revenue_ctr": 0, "cro": 0}
    for f in findings:
        by_dim[f["dimension"]] = by_dim.get(f["dimension"], 0) + 1

    return {
        "url": url,
        "asset_type": asset_type,
        "score": score,
        "grade": _grade(score),
        "findings": findings,
        "by_dimension": by_dim,
        "limited": not has_crawl_data,
    }
