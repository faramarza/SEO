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

# Grammatical filler only — attributes/audience terms carry topical signal.
_KW_STOP = {
    "for", "the", "and", "with", "how", "what", "why", "are", "can", "from",
    "that", "this", "your", "our", "all", "has", "its", "you", "was", "get",
    "not", "but", "will", "more", "buy", "shop", "best", "top", "usa",
}


def _kw_words(text):
    """Significant (topical) tokens from a keyword phrase."""
    return [
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) > 2 and w not in _KW_STOP
    ]


def _kw_coverage(text, words):
    """Fraction of `words` (singular/plural-tolerant) present in `text`."""
    if not words:
        return 1.0
    hay = (text or "").lower()
    hits = 0
    for w in words:
        stem = w[:-1] if len(w) > 3 and w.endswith("s") else w
        if w in hay or stem in hay:
            hits += 1
    return hits / len(words)


def _primary_keyword(top_queries):
    """Highest-impression query = the page's primary target keyword."""
    if not top_queries:
        return "", []
    best = max(top_queries, key=lambda q: q.get("impressions", 0) or 0)
    kw = best.get("query", "") or ""
    return kw, _kw_words(kw)


def _has_breadcrumbs(breadcrumb_links, schema_types, above_fold_html, body_html):
    """Detect breadcrumbs from any reliable signal, not just crawler-extracted
    breadcrumb_links (which misses non-standard markup like Magento's)."""
    if breadcrumb_links:
        return True
    if _has_schema(schema_types, "BreadcrumbList"):
        return True
    html = f"{above_fold_html or ''} {body_html or ''}"
    return bool(_BREADCRUMB_RE.search(html))


# An interactive CTA can be an anchor, a real button, a form submit, or a
# JS-bound element — not just <a href>. Only true when NONE of these exist.
_INTERACTIVE_RE = re.compile(
    r"<a\b[^>]*\bhref|<button\b|type\s*=\s*\"(submit|button)\"|role\s*=\s*\"button\"|onclick\s*=",
    re.IGNORECASE,
)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# Lazy-loaded / responsive images the plain <img src> check misses.
_LAZY_IMG_RE = re.compile(r"data-src|data-lazy|<source\b|srcset", re.IGNORECASE)


def _has_above_fold_cta(above_fold_html):
    """True if the above-fold region has any interactive path (link, button,
    form submit, JS handler) — not just a literal <a href>."""
    return bool(_INTERACTIVE_RE.search(above_fold_html or ""))


def _image_findings(body_html):
    """Return (no_images, imgs_missing_alt, total_imgs).

    Counts lazy/responsive images too, and treats only images with NO alt
    attribute at all as defects — alt="" is a valid decorative image, not a bug.
    """
    tags = _IMG_TAG_RE.findall(body_html or "")
    total = len(tags)
    has_any_image = total > 0 or bool(_LAZY_IMG_RE.search(body_html or ""))
    missing_alt = sum(1 for t in tags if "alt=" not in t.lower())
    return (not has_any_image), missing_alt, total

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
    content_preview="",
    top_queries=None,
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

    # ── ON-PAGE KEYWORD PLACEMENT (Brian Dean / Backlinko) ──
    # Dean's on-page method: the primary target keyword should appear in the
    # title, H1, URL, and the opening copy. We use the highest-impression GSC
    # query as the primary keyword and check coverage of its topical words,
    # tolerating word order and singular/plural. Low-severity, gap-only — so
    # this informs without reintroducing title-test noise.
    primary_kw, kw_words = _primary_keyword(top_queries)
    if has_crawl_data and kw_words:
        if title.strip() and _kw_coverage(title, kw_words) < 0.5:
            penalize(4, "seo", "medium", "Primary keyword weak in title",
                     f"Title covers little of the top query '{primary_kw}'.",
                     f"Work the main terms of '{primary_kw}' into the title naturally (keep it 40–60 chars).")
        if h1.strip() and _kw_coverage(h1, kw_words) < 0.5:
            penalize(3, "seo", "low", "Primary keyword weak in H1",
                     f"H1 covers little of the top query '{primary_kw}'.",
                     f"Ensure the H1 reflects '{primary_kw}'.")
        slug = re.sub(r"[^a-z0-9]+", " ", (url or "").lower().rsplit("/", 1)[-1])
        if slug and _kw_coverage(slug, kw_words) == 0:
            penalize(2, "seo", "low", "Primary keyword not in URL",
                     f"URL slug contains none of '{primary_kw}'.",
                     "For NEW pages, include the primary keyword in the slug. Don't rewrite existing URLs without a 301.")
        opening = " ".join((content_preview or "").split()[:100])
        if opening and _kw_coverage(opening, kw_words) < 0.5:
            penalize(3, "seo", "low", "Primary keyword weak in opening copy",
                     f"The first 100 words barely mention '{primary_kw}'.",
                     f"Mention '{primary_kw}' (and close variants) naturally within the first paragraph.")

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

        # Only flag when we actually captured above-fold HTML and it has NO
        # interactive element at all (link, button, form submit, JS handler).
        if len(above_fold_html or "") > 40 and not _has_above_fold_cta(above_fold_html):
            penalize(6, "revenue_ctr", "medium", "No above-fold link/CTA",
                     "No clickable link or button above the fold — buyers see no immediate path.",
                     "Add a real CTA (Add to Cart / Shop / view links) in the hero region, not a JS-only span.")

    # ── CRO / content ────────────────────────────────────
    # Only run image checks when body_html is substantial (a truncated snippet
    # gives false 'no images' / 'missing alt' results).
    if has_crawl_data and len(body_html or "") > 200:
        no_images, missing_alt, total_imgs = _image_findings(body_html)
        if no_images:
            penalize(5, "cro", "medium", "No images",
                     "No images detected on the page.", "Add product/lifestyle imagery.")
        elif total_imgs >= 3 and missing_alt > total_imgs * 0.4:
            # Flag only when a meaningful share of images truly lack an alt
            # attribute (alt="" is valid for decorative images, not a defect).
            penalize(4, "cro", "low", "Images missing alt text",
                     f"{missing_alt} of {total_imgs} images have no alt attribute.",
                     "Add descriptive alt text to content images (helps image SEO and accessibility).")

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
