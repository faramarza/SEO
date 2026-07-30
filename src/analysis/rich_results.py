"""Rich-Results / Structured-Data engine.

The one SERP advantage a small store can win WITHOUT domain authority: rich
results (star ratings, price, availability, FAQ accordions, breadcrumbs) and the
structured data that also makes pages quotable by AI answer engines.

Grounding principle (applied everywhere in this tool): CHECK FIRST WHAT THE PAGE
ALREADY HAS. Magento — like Yoast/Rank Math — emits a lot of schema by default,
usually nested inside a top-level @graph with the money types (Offer,
AggregateRating, Brand) inside the Product node. The crawler now walks that whole
structure (see simple_crawler._collect_schema_types), so `schema_types` is
accurate. This engine reads it and only ever recommends/generates the types that
are genuinely MISSING for a given page. It never tells you to add schema you have.

It also never fabricates data Google treats as spam: AggregateRating/Review markup
is only recommended when the page can back it with real reviews, and generated
JSON-LD uses the page's own crawled fields (name, url, breadcrumb trail,
description) with clearly-marked placeholders where a value genuinely isn't known.
"""

import json

from src.analysis.growth_playbook import _is_system_page


# Rich-result-eligible schema types, with why each one matters on the SERP / to
# answer engines. Keyed by the canonical @type name (lower-cased for matching).
RICH_RESULT_TYPES = {
    "product": "Product rich result — makes the page eligible for the visual "
               "product treatment (image, price, rating) in search.",
    "offer": "Price & availability in the SERP — shoppers see the price and "
             "'In stock' before they click.",
    "aggregaterating": "Star ratings in the SERP — one of the largest organic "
                       "CTR levers there is. Requires real reviews.",
    "breadcrumblist": "Breadcrumb trail in the SERP instead of a raw URL — "
                      "cleaner listing, small CTR lift.",
    "itemlist": "Category/list rich result — signals a curated product list to "
                "search and to AI answer engines building comparison lists.",
    "faqpage": "FAQ accordion under your listing (extra SERP real estate) and "
               "direct Q&A that AI engines quote.",
    "howto": "How-to rich result with steps — extra SERP space and highly "
             "quotable by AI answer engines.",
    "article": "Article/BlogPosting eligibility (Top-stories, date, author) and "
               "clean attribution when AI engines cite the guide.",
}

# What each page TYPE should carry. AggregateRating is conditional (needs reviews)
# and handled separately. FAQPage/HowTo are content-conditional (only when the
# page actually poses questions / describes steps).
_EXPECTED_BY_ASSET = {
    "product":  ["Product", "Offer", "BreadcrumbList"],
    "category": ["ItemList", "BreadcrumbList"],
    "blog":     ["Article", "BreadcrumbList"],
    "article":  ["Article", "BreadcrumbList"],
    "guide":    ["Article", "BreadcrumbList"],
}

_IMPACT = {
    "aggregaterating": "high", "product": "high", "offer": "high",
    "faqpage": "medium", "itemlist": "medium", "article": "medium",
    "howto": "medium", "breadcrumblist": "low",
}

# The crawler reads SERVER HTML only — it does not execute JavaScript. Magento
# themes and SEO/schema extensions very commonly inject JSON-LD client-side (via
# a script or GTM), which a raw-HTML crawl cannot see. So a "not detected" result
# is NOT proof the schema is absent: Google renders JS and may already see it.
# Every gap we report carries this caveat and a verify-first instruction, so the
# tool never again sends the operator to add schema the site already has. The
# authoritative check is Google's Rich Results Test (it renders JS like Google).
VERIFY_NOTE = ("Not found in the page's server HTML. This crawler doesn't run "
               "JavaScript, and Magento themes/extensions often inject schema via "
               "JS — so this may already be live. VERIFY with Google's Rich "
               "Results Test before adding anything, to avoid duplicate markup.")


def _has_crawl_signal(page: dict) -> bool:
    """True if the page was actually fetched and parsed — so we can honestly say
    what schema it does/doesn't have. Prefer the explicit has_crawl_data flag, but
    don't depend on it: a GSC-only page has an empty title/0 words/no headings,
    while any crawled page has at least a title or word count. This keeps the audit
    working even when the flag is stale or a crawl path forgot to set it."""
    if page.get("has_crawl_data"):
        return True
    if (page.get("word_count") or 0) > 0:
        return True
    if (page.get("title") or "").strip():
        return True
    if page.get("schema_types"):
        return True
    if page.get("headings"):
        return True
    return False


def _present_set(page: dict) -> set:
    """Lower-cased set of @type values the page ALREADY emits."""
    return {str(s).lower() for s in (page.get("schema_types") or [])}


def _has(present: set, *names) -> bool:
    return any(n.lower() in present for n in names)


def _question_headings(page: dict) -> list:
    return [h for h in (page.get("headings") or []) if str(h).strip().endswith("?")]


def _looks_howto(page: dict) -> bool:
    """Heuristic: the page is genuinely a procedure (numbered steps) — only then
    is HowTo honest markup. Gate on the title/H1 topic plus explicit step
    signals in the body headings, so a listicle with an incidental 'how to
    choose?' sub-heading doesn't get flagged."""
    topic = ((page.get("title") or "") + " " + (page.get("h1") or "")).lower()
    heads = " ".join(str(h) for h in (page.get("headings") or [])).lower()
    topic_howto = topic.startswith("how to") or "step-by-step" in topic or "diy" in topic
    step_signals = ("step 1", "step 2", "step-by-step", "assemble", "instructions")
    return topic_howto or any(s in heads for s in step_signals)


def _asset_type(page: dict) -> str:
    return (page.get("asset_type") or "other").lower()


def _page_name(page: dict) -> str:
    return (page.get("h1") or page.get("title") or "").strip()


def _page_desc(page: dict) -> str:
    d = (page.get("meta_description") or page.get("content_preview") or "").strip()
    return d[:300]


def _breadcrumb_items(page: dict) -> list:
    """ItemListElement built from the page's own crawled breadcrumb links, so the
    generated BreadcrumbList reflects the real nav trail — not a guess."""
    items = []
    trail = page.get("breadcrumb_links") or []
    for i, link in enumerate(trail, start=1):
        name = (link.get("anchor_text") or "").strip()
        url = (link.get("target_url") or "").strip()
        if not name:
            continue
        el = {"@type": "ListItem", "position": i, "name": name}
        if url:
            el["item"] = url
        items.append(el)
    # Always end on the current page.
    if _page_name(page):
        items.append({"@type": "ListItem", "position": len(items) + 1,
                      "name": _page_name(page), "item": page.get("url", "")})
    return items


def generate_jsonld(page: dict, missing_type: str) -> dict:
    """Ready-to-paste JSON-LD for one MISSING type, grounded in the page's own
    crawled fields. Placeholders are UPPER_CASE and explained in `_note` so the
    operator fills real values (never fabricated ratings/prices)."""
    t = missing_type.lower()
    url = page.get("url", "")
    name = _page_name(page)
    desc = _page_desc(page)
    ctx = "https://schema.org"

    if t == "product":
        markup = {"@context": ctx, "@type": "Product", "name": name or "PRODUCT_NAME",
                  "url": url}
        if desc:
            markup["description"] = desc
        markup["image"] = "PRODUCT_IMAGE_URL"
        markup["brand"] = {"@type": "Brand", "name": "BRAND_NAME"}
        markup["sku"] = "SKU"
        return {"markup": markup,
                "_note": "Magento usually emits Product already — only add if the "
                         "crawl truly found none. Fill image/brand/sku from the "
                         "product; keep it in sync with the on-page price."}
    if t == "offer":
        return {"markup": {"@context": ctx, "@type": "Product", "name": name or "PRODUCT_NAME",
                           "url": url,
                           "offers": {"@type": "Offer", "priceCurrency": "USD",
                                      "price": "PRICE", "availability":
                                      "https://schema.org/InStock", "url": url}},
                "_note": "Nest Offer inside the existing Product node. Price MUST "
                         "match the visible price or Google disqualifies the rich "
                         "result."}
    if t == "aggregaterating":
        return {"markup": {"@context": ctx, "@type": "Product", "name": name or "PRODUCT_NAME",
                           "url": url,
                           "aggregateRating": {"@type": "AggregateRating",
                                               "ratingValue": "REAL_AVG_RATING",
                                               "reviewCount": "REAL_REVIEW_COUNT"}},
                "_note": "ONLY publish once you collect real reviews — ratingValue "
                         "and reviewCount must reflect genuine on-page reviews, or "
                         "it's structured-data spam. Enable a reviews extension "
                         "first."}
    if t == "breadcrumblist":
        return {"markup": {"@context": ctx, "@type": "BreadcrumbList",
                           "itemListElement": _breadcrumb_items(page)},
                "_note": "Built from the page's actual breadcrumb trail."}
    if t == "itemlist":
        return {"markup": {"@context": ctx, "@type": "ItemList", "url": url,
                           "name": name or "CATEGORY_NAME",
                           "itemListElement": [
                               {"@type": "ListItem", "position": 1,
                                "url": "PRODUCT_URL_1"},
                               {"@type": "ListItem", "position": 2,
                                "url": "PRODUCT_URL_2"}]},
                "_note": "Enumerate the products shown in the category grid, in "
                         "display order."}
    if t == "faqpage":
        qs = _question_headings(page)
        entities = [{"@type": "Question", "name": q.strip(),
                     "acceptedAnswer": {"@type": "Answer",
                                        "text": "ANSWER_FOR_" + q.strip()[:40]}}
                    for q in qs[:6]] or [
                    {"@type": "Question", "name": "QUESTION_SHOPPERS_ASK",
                     "acceptedAnswer": {"@type": "Answer", "text": "CONCISE_ANSWER"}}]
        return {"markup": {"@context": ctx, "@type": "FAQPage",
                           "mainEntity": entities},
                "_note": "Questions pre-filled from the page's own question-form "
                         "headings. Answer text must appear visibly on the page."}
    if t == "howto":
        return {"markup": {"@context": ctx, "@type": "HowTo",
                           "name": name or "HOW_TO_TITLE",
                           "step": [{"@type": "HowToStep", "text": "STEP_1"},
                                    {"@type": "HowToStep", "text": "STEP_2"}]},
                "_note": "Each step must correspond to real on-page step content."}
    if t == "article":
        return {"markup": {"@context": ctx, "@type": "BlogPosting",
                           "headline": name or "ARTICLE_TITLE", "url": url,
                           "description": desc or "ARTICLE_DESCRIPTION",
                           "datePublished": "YYYY-MM-DD",
                           "dateModified": "YYYY-MM-DD",
                           "author": {"@type": "Organization", "name": "BRAND_NAME"}},
                "_note": "Set dateModified to the real last-updated date — it "
                         "drives the freshness signal AI engines reward."}
    return {"markup": {"@context": ctx, "@type": missing_type}, "_note": ""}


def analyze_page_schema(page: dict) -> dict:
    """Per-page schema audit. Returns present/eligible/missing, where `missing`
    lists ONLY rich-result types genuinely absent for this page's type, each with
    grounded generated JSON-LD. Returns None for system/asset URLs and pages with
    no crawl data (we can't claim schema is missing on a page we never parsed)."""
    url = page.get("url", "")
    if _is_system_page(url):
        return None
    if not _has_crawl_signal(page):
        return None

    at = _asset_type(page)
    present = _present_set(page)
    present_relevant = sorted(s for s in present if s in RICH_RESULT_TYPES)

    expected = list(_EXPECTED_BY_ASSET.get(at, []))
    # Content-conditional types apply to any page that exhibits the signal.
    if _question_headings(page):
        expected.append("FAQPage")
    if at in ("blog", "article", "guide") and _looks_howto(page):
        expected.append("HowTo")
    # AggregateRating is a product money-type, but only honest with real reviews;
    # we flag it as an OPPORTUNITY (needs data) rather than a plain gap.
    ratings_opportunity = at == "product" and not _has(present, "AggregateRating", "Review")

    missing = []
    for typ in expected:
        if _has(present, typ):
            continue
        low = typ.lower()
        gen = generate_jsonld(page, typ)
        missing.append({
            "type": typ,
            "impact": _IMPACT.get(low, "low"),
            "why": RICH_RESULT_TYPES.get(low, ""),
            "requires_data": None,
            "verify_first": True,
            "verify_note": VERIFY_NOTE,
            "jsonld": json.dumps(gen["markup"], indent=2, ensure_ascii=False),
            "note": gen.get("_note", ""),
        })

    if ratings_opportunity:
        gen = generate_jsonld(page, "AggregateRating")
        missing.append({
            "type": "AggregateRating",
            "impact": "high",
            "why": RICH_RESULT_TYPES["aggregaterating"],
            "requires_data": "Needs real reviews — enable review collection first; "
                             "do not publish fabricated ratings.",
            "verify_first": True,
            "verify_note": VERIFY_NOTE,
            "jsonld": json.dumps(gen["markup"], indent=2, ensure_ascii=False),
            "note": gen.get("_note", ""),
        })

    return {
        "url": url,
        "asset_type": at,
        "present": present_relevant,      # rich-result types the page ALREADY has
        "missing": missing,               # only genuine gaps, with markup
        "covered": len(missing) == 0,
    }


def analyze_site_schema(results: list, limit: int = 150) -> dict:
    """Site-wide rich-results coverage. Confirms what's already in place and rolls
    up the genuine gaps by type, skipping system/asset pages and uncrawled pages."""
    pages = []
    gap_counts = {}
    present_counts = {}
    covered = 0
    considered = 0
    total = len(results)
    skipped_system = 0
    skipped_no_crawl = 0

    # Aggregate each missing type across pages: count, a representative markup
    # sample, and whether it's blocked on a prerequisite (reviews).
    gap_agg = {}
    for r in results:
        if _is_system_page(r.get("url", "")):
            skipped_system += 1
            continue
        if not _has_crawl_signal(r):
            skipped_no_crawl += 1
            continue
        audit = analyze_page_schema(r)
        if audit is None:
            continue
        considered += 1
        for p in audit["present"]:
            present_counts[p] = present_counts.get(p, 0) + 1
        if audit["covered"]:
            covered += 1
            continue
        for m in audit["missing"]:
            gap_counts[m["type"]] = gap_counts.get(m["type"], 0) + 1
            g = gap_agg.setdefault(m["type"], {
                "type": m["type"], "count": 0, "impact": m["impact"],
                "why": m["why"], "blocked": bool(m.get("requires_data")),
                "blocker": m.get("requires_data"), "sample_jsonld": m["jsonld"],
                "verify_first": True, "verify_note": VERIFY_NOTE})
            g["count"] += 1
        # Rank pages by impact of their biggest gap for the UI.
        top = max((_IMPACT.get(m["type"].lower(), "low") == "high")
                  for m in audit["missing"]) if audit["missing"] else False
        pages.append({**audit, "_priority": 2 if top else 1})

    pages.sort(key=lambda p: (-p["_priority"], p["url"]))
    for p in pages:
        p.pop("_priority", None)

    # Split gaps into "do-it-now" (pure template markup, no prerequisites — these
    # are the useful wins) vs "blocked" (needs reviews first). Most of these are
    # template-level: one edit covers every page of that type, so we say so.
    template_hint = {
        "article": "Add Article/BlogPosting JSON-LD to your blog-post TEMPLATE — one "
                   "edit covers all of these posts at once.",
        "breadcrumblist": "Add BreadcrumbList JSON-LD to your page template (or enable "
                          "it in your Magento SEO/schema extension) — one change covers all these pages.",
        "itemlist": "Add ItemList JSON-LD to your category template, enumerating the product grid.",
        "howto": "Add HowTo JSON-LD only to the guide pages that have real numbered steps.",
        "faqpage": "Add FAQPage JSON-LD where the page has visible Q&A content.",
    }
    actionable_now, blocked = [], []
    for g in gap_agg.values():
        g = {**g, "template_hint": template_hint.get(g["type"].lower(), "")}
        (blocked if g["blocked"] else actionable_now).append(g)
    # Do-it-now first, biggest coverage first.
    actionable_now.sort(key=lambda g: -g["count"])
    blocked.sort(key=lambda g: -g["count"])

    return {
        "considered": considered,
        "covered": covered,
        "coverage_pct": round(100 * covered / considered, 1) if considered else 0.0,
        "present_counts": dict(sorted(present_counts.items(),
                                      key=lambda kv: -kv[1])),
        "gap_counts": dict(sorted(gap_counts.items(), key=lambda kv: -kv[1])),
        "gaps_actionable": actionable_now,
        "gaps_blocked": blocked,
        "detection_caveat": VERIFY_NOTE,
        "pages": pages[:limit],
        # Diagnostics — so an empty audit can explain itself instead of
        # contradicting the "Based on N pages" header.
        "total_pages": total,
        "skipped_system": skipped_system,
        "skipped_no_crawl": skipped_no_crawl,
    }
