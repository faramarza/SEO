"""Structured-data validation — does the schema a page ALREADY emits carry the
fields Google requires for the rich result (and that AI engines read)?

rich_results.py answers "which rich-result TYPES is this page missing?". This
module answers the complementary question: "for the types the page DOES emit, is
the markup COMPLETE enough to qualify?" A Product with an Offer that has no price,
a BreadcrumbList with no items, an AggregateRating with no reviewCount — these are
present-but-invalid: Google silently drops the rich result and AI engines get no
fact to quote. Google's Rich Results Test would flag them; this surfaces them from
crawl data so the operator sees them without testing every URL by hand.

Grounded in Google's structured-data requirements (required vs recommended fields
per type). Input is the per-type field-presence map captured by the crawler
(page["schema_facts"]); values are never inspected, only presence — so this never
fabricates or leaks data. Emits nothing for types the page doesn't have (that's
rich_results' job) and nothing when facts weren't captured (old evaluations).

Severity:
  error   — a REQUIRED field is missing; Google will not show the rich result.
  warning — a RECOMMENDED field is missing; eligible, but weaker / less quotable.
"""

# Verify-first caveat: the crawler reads server HTML only (no JS execution), and
# Magento themes/extensions often inject or complete schema client-side. So a
# reported gap MAY already be satisfied in the rendered DOM. Confirm with Google's
# Rich Results Test before editing, exactly as rich_results advises.
VERIFY_NOTE = ("Read from server HTML (no JavaScript executed). Magento/theme "
               "extensions may complete this field client-side — VERIFY with "
               "Google's Rich Results Test before changing markup.")


def _issue(type_name, field, severity, message):
    return {"type": type_name, "field": field, "severity": severity,
            "message": message, "verify_note": VERIFY_NOTE}


def validate_schema_facts(schema_facts: dict) -> list:
    """Return a list of validation issues for the schema FACTS a page emits.
    Empty list = everything present is complete (or nothing to validate)."""
    facts = schema_facts or {}
    issues = []

    product = facts.get("product")
    offer = facts.get("offer")
    rating = facts.get("aggregaterating")
    bc = facts.get("breadcrumblist")
    faq = facts.get("faqpage")
    article = facts.get("article")

    # ── Product ──────────────────────────────────────────────────
    if product:
        if not product.get("name"):
            issues.append(_issue("Product", "name", "error",
                "Product schema has no name — name is required for the product rich result."))
        # Offer is what carries price/availability. Product without any Offer is a
        # completeness gap (also surfaced by geo/rich_results); flag it here as the
        # reason the price rich result can't show.
        if not product.get("has_offer") and not offer:
            issues.append(_issue("Product", "offers", "error",
                "Product schema has no Offer — without it, price and availability can't appear as a rich result or be quoted by AI shopping answers."))
        if not product.get("image"):
            issues.append(_issue("Product", "image", "warning",
                "No image in Product schema — image is recommended and drives the visual product treatment in search."))

    # ── Offer ────────────────────────────────────────────────────
    if offer:
        if not offer.get("price"):
            issues.append(_issue("Offer", "price", "error",
                "Offer has no price — price is required for the price/merchant rich result."))
        if not offer.get("priceCurrency"):
            issues.append(_issue("Offer", "priceCurrency", "error",
                "Offer has no priceCurrency — required alongside price (e.g. \"USD\")."))
        if not offer.get("availability"):
            issues.append(_issue("Offer", "availability", "warning",
                "Offer has no availability — recommended (e.g. https://schema.org/InStock); shoppers see 'In stock' before clicking."))

    # ── AggregateRating (only ever present with real reviews) ────
    if rating:
        if not rating.get("ratingValue"):
            issues.append(_issue("AggregateRating", "ratingValue", "error",
                "AggregateRating has no ratingValue — required for the star rating."))
        if not rating.get("reviewCount"):
            issues.append(_issue("AggregateRating", "reviewCount", "error",
                "AggregateRating has no reviewCount/ratingCount — required; Google drops the stars without it."))

    # ── BreadcrumbList ───────────────────────────────────────────
    if bc is not None and not bc.get("item_count"):
        issues.append(_issue("BreadcrumbList", "itemListElement", "error",
            "BreadcrumbList has no itemListElement entries — the breadcrumb rich result needs the trail items."))

    # ── FAQPage ──────────────────────────────────────────────────
    if faq is not None and not faq.get("question_count"):
        issues.append(_issue("FAQPage", "mainEntity", "error",
            "FAQPage has no mainEntity questions — the FAQ rich result needs at least one Question/Answer pair."))

    # ── Article / BlogPosting ────────────────────────────────────
    if article:
        if not article.get("headline"):
            issues.append(_issue("Article", "headline", "warning",
                "Article schema has no headline — recommended for Article eligibility."))
        if not article.get("dateModified") and not article.get("datePublished"):
            issues.append(_issue("Article", "dateModified", "warning",
                "Article schema carries no dates — add datePublished/dateModified so engines can read a freshness date."))
        if not article.get("author"):
            issues.append(_issue("Article", "author", "warning",
                "Article schema has no author — recommended for attribution when AI engines cite the guide."))

    return issues


def summarize_validation(results: list, page_view_fn=None) -> dict:
    """Site-wide rollup of validation issues across evaluation results. Each result
    must expose schema_facts (directly or via page_view_fn). Returns counts by
    severity and by type.field, plus the worst offending pages, so the UI can show
    'what's broken in the schema you already have' next to 'what's missing'."""
    error_pages = []
    warning_pages = []
    by_field = {}
    n_errors = n_warnings = considered = 0

    for r in results:
        view = page_view_fn(r) if page_view_fn else r
        facts = (view or {}).get("schema_facts") or {}
        if not facts:
            continue
        considered += 1
        issues = validate_schema_facts(facts)
        if not issues:
            continue
        errs = [i for i in issues if i["severity"] == "error"]
        warns = [i for i in issues if i["severity"] == "warning"]
        n_errors += len(errs)
        n_warnings += len(warns)
        for i in issues:
            key = f"{i['type']}.{i['field']}"
            by_field[key] = by_field.get(key, 0) + 1
        entry = {"url": view.get("url", ""), "asset_type": view.get("asset_type", "other"),
                 "issues": issues}
        if errs:
            error_pages.append(entry)
        else:
            warning_pages.append(entry)

    return {
        "considered": considered,
        "pages_with_errors": len(error_pages),
        "pages_with_warnings_only": len(warning_pages),
        "error_count": n_errors,
        "warning_count": n_warnings,
        "by_field": dict(sorted(by_field.items(), key=lambda kv: -kv[1])),
        "error_pages": error_pages[:100],
        "warning_pages": warning_pages[:100],
        "verify_note": VERIFY_NOTE,
    }
