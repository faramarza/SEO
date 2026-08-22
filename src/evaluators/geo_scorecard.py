"""GEO Citation Readiness Scorecard — how likely an AI answer engine (ChatGPT,
Gemini, Claude, Google AI Overviews / Perplexity) is to CITE this page as a
source, and exactly what to add to improve that.

Grounded in Generative Engine Optimization research:
  - Török (2026): content FORMAT and EVIDENCE signals drive citation —
    research/comparison/review pages are cited most, bare product/pricing
    pages least; evidence density, structure, freshness and authority are the
    operative features.
  - Pinterest GEO (2025): answer-oriented, retrieval-optimized content
    (predict the question, structure the answer) wins in generative search.

Rule-based and deterministic — every finding is checkable from crawl data.
GEO best practice is still emerging, so output is framed as DIRECTIONAL
guidance, not hard rules. Pure function: runs from the eval workflow (asset)
and the web layer (cached page_metadata).

Dimensions:
  evidence      — statistics, data, specificity, sources (the #1 citation driver)
  structure     — subheadings, lists, tables, chunking (extractability)
  answerability — question-form headings, FAQ/HowTo/Article schema, direct answers
  freshness     — last-updated signals, current-year references
  authority     — DA/PA (passed in from Moz, optional)
"""

import re

_NUM_RE = re.compile(r"(?<!\w)(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(%|\s?(?:percent|kg|lbs|oz|inch|cm|mm|months?|years?|weeks?))?", re.IGNORECASE)
_PCT_MONEY_RE = re.compile(r"[%$£€]")
_EVIDENCE_LANG_RE = re.compile(
    r"\b(accord(?:ing|s) to|study|studies|research|survey|data|statistic|report|"
    r"according|evidence|based on|found that|shows that|experts?|source[:\s]|"
    r"cited|reference|clinical|tested|reviewed by)\b", re.IGNORECASE)
_UNIT_WORDS_RE = re.compile(
    r"\b(percent|kg|lbs?|oz|ounces?|pounds?|inch(?:es)?|cm|mm|ft|feet|foot|"
    r"months?|years?|weeks?|days?|hours?|minutes?|ages?|reviews?|ratings?|stars?|"
    r"pieces?|pcs|pack|count|out of)\b", re.IGNORECASE)
_LIST_RE = re.compile(r"<(ul|ol)\b", re.IGNORECASE)
_LI_RE = re.compile(r"<li\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(2025|2026)\b")
_UPDATED_RE = re.compile(r"\b(last updated|updated on|last modified|reviewed on|as of)\b", re.IGNORECASE)
_COMPARISON_RE = re.compile(r"\b(vs\.?|versus|compare|comparison|best|top \d+|guide|how to|what is|why|review)\b", re.IGNORECASE)


def _has_schema(schema_types, *names):
    have = {str(s).lower() for s in (schema_types or [])}
    return any(n.lower() in have for n in names)


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


def _count_stats(text):
    """Count QUALIFIED quantitative facts — the specifics AI engines actually
    quote — not every digit on the page. A bare number (a year, a list count, a
    phone fragment, a SKU) is not evidence; a number is counted only when it
    carries a unit/percent, or sits next to a currency symbol, a measurement word,
    or sourcing language. This stops thin pages from scoring 'evidence-rich' on
    nav/boilerplate digits (the old count-every-number bug)."""
    if not text:
        return 0
    qualified = 0
    for m in _NUM_RE.finditer(text):
        if (m.group(2) or "").strip():        # number already carries %/unit
            qualified += 1
            continue
        s, e = max(0, m.start() - 20), min(len(text), m.end() + 20)
        window = text[s:e]
        if (_PCT_MONEY_RE.search(window) or _UNIT_WORDS_RE.search(window)
                or _EVIDENCE_LANG_RE.search(window)):
            qualified += 1
    return qualified


def evaluate_geo_readiness(
    *,
    url="",
    asset_type="other",
    title="",
    meta_description="",
    headings=None,
    content_preview="",
    body_html="",
    above_fold_html="",
    word_count=0,
    schema_types=None,
    schema_facts=None,
    domain_authority=None,
    page_authority=None,
    has_crawl_data=True,
    top_queries=None,
):
    """Return a GEO scorecard: score, grade, findings[] (each with a concrete
    action), by_dimension counts, and a plain-English verdict.
    """
    asset_type = (asset_type or "other").lower()
    headings = headings or []
    schema_types = schema_types or []
    schema_facts = schema_facts or {}
    title = title or ""
    # Prefer full body text; fall back to the preview/above-fold snippet.
    text_blob = " ".join(filter(None, [content_preview, _strip_tags(body_html), _strip_tags(above_fold_html)]))
    heading_text = " ".join(headings)

    findings = []
    score = 100

    def penalize(points, dimension, severity, label, detail, fix):
        nonlocal score
        score -= points
        findings.append({
            "dimension": dimension, "severity": severity,
            "label": label, "detail": detail, "fix": fix, "points": points,
        })

    if not has_crawl_data:
        return {
            "url": url, "asset_type": asset_type, "score": 0, "grade": "—",
            "findings": [], "by_dimension": {}, "limited": True,
            "verdict": "No crawl data — run an evaluation with crawl to score GEO readiness.",
        }

    # ── EVIDENCE (the #1 citation driver) ───────────────────────
    stat_count = _count_stats(text_blob)
    density = (stat_count / word_count * 1000) if word_count else 0  # stats per 1k words
    if stat_count == 0:
        penalize(18, "evidence", "high", "No evidence signals",
                 "No statistics, figures, prices, or measurable facts detected. AI engines preferentially cite pages with verifiable data.",
                 "Add concrete, specific facts: numbers, percentages, dimensions, price ranges, dated figures, study findings.")
    elif density < 3:
        penalize(9, "evidence", "medium", "Low evidence density",
                 f"Only ~{stat_count} quantitative facts across {word_count} words. Thin on the specifics AI answers quote.",
                 "Weave in more concrete data points — measurements, counts, ages, comparisons, sourced statistics.")
    if not _EVIDENCE_LANG_RE.search(text_blob):
        penalize(6, "evidence", "low", "No sourcing / expertise language",
                 "No phrases like 'according to', 'research shows', 'tested', 'reviewed by'. Generative engines favor content that signals sourced expertise.",
                 "Attribute claims ('according to…', 'a 2026 study found…') and cite where facts come from.")

    # ── STRUCTURE (extractability) ──────────────────────────────
    if len(headings) < 2:
        penalize(10, "structure", "high", "Too few subheadings",
                 f"{len(headings)} H2/H3 headings. AI engines chunk content by heading; unstructured walls of text are rarely extracted.",
                 "Break the page into clear H2/H3 sections, each answering one specific sub-question.")
    has_lists = bool(_LIST_RE.search(body_html or "")) or (_LI_RE.findall(body_html or "") != [])
    if not has_lists:
        penalize(7, "structure", "medium", "No lists",
                 "No bulleted or numbered lists detected. Lists are among the most-extracted, most-quoted structures in AI answers.",
                 "Convert key points (features, steps, criteria) into bulleted or numbered lists.")
    is_comparison = bool(_COMPARISON_RE.search(title + " " + heading_text + " " + (url or "")))
    if is_comparison and not _TABLE_RE.search(body_html or ""):
        penalize(4, "structure", "low", "Comparison page without a table",
                 "This reads like a guide/comparison but has no comparison table. Tables are highly citable for 'best/vs' queries.",
                 "Add a comparison table (options × attributes) — AI Overviews frequently lift these directly.")

    # ── ANSWERABILITY ───────────────────────────────────────────
    question_headings = [h for h in headings if h.strip().endswith("?")]
    # Real question-intent queries this page already ranks for — so the FAQ fix can
    # name the ACTUAL questions to answer, not a generic "questions shoppers ask".
    _q_words = ("what", "how", "why", "when", "which", "who", "is", "are", "can", "do", "does", "best")
    _real_qs = []
    for _q in (top_queries or []):
        _qt = (_q.get("query") if isinstance(_q, dict) else str(_q)) or ""
        if _qt and (_qt.lower().split()[:1] and _qt.lower().split()[0] in _q_words):
            _real_qs.append(_qt.strip())
    _faq_examples = ("; try: " + ", ".join(f'"{q}?"'.replace('??', '?') for q in _real_qs[:3])
                     if _real_qs else "")
    has_faq_schema = _has_schema(schema_types, "FAQPage", "QAPage")
    if not question_headings and not has_faq_schema:
        penalize(11, "answerability", "high", "No question-form content / FAQ",
                 "No question-style headings and no FAQ markup. AI answers map user questions to pages that pose and answer those questions directly.",
                 "Add an FAQ section (or question-form H2s) that answers the real questions shoppers ask, "
                 "each followed by a concise 2-3 sentence answer" + _faq_examples + ".")
    if not _has_schema(schema_types, "FAQPage", "QAPage", "HowTo", "Article", "BlogPosting"):
        penalize(6, "answerability", "medium", "No answer-oriented schema",
                 "No FAQPage / HowTo / Article structured data. This schema helps engines parse and attribute your content.",
                 "Add FAQPage JSON-LD for Q&A, Article/BlogPosting for guides, or HowTo for step content.")
    # Direct-answer lead: a concise, self-contained opening the engine can quote.
    opening = " ".join((content_preview or "").split()[:40])
    if opening and len(opening) < 25:
        penalize(3, "answerability", "low", "No direct-answer opening",
                 "The opening doesn't state a clear, quotable answer up front.",
                 "Open the page (or each section) with a direct 1-2 sentence answer before the detail — the 'inverted pyramid' AI engines quote.")

    # ── FRESHNESS ───────────────────────────────────────────────
    # A literal "Last updated" date is a blog/article convention — natural on a
    # guide, but unnatural and mildly misleading on a commerce category/product
    # page (the products change, not an "article"). So the check is page-type
    # aware: content pages should carry an update date; commerce pages only need
    # to not read as stale (a natural current-year reference), never a fake date.
    fresh = bool(_YEAR_RE.search(text_blob) or _YEAR_RE.search(title) or _YEAR_RE.search(heading_text))
    dated = bool(_UPDATED_RE.search(text_blob)) or _has_schema(schema_types, "Article", "BlogPosting")
    is_content_page = asset_type in ("blog", "article", "guide")
    if is_content_page:
        if not fresh and not dated:
            penalize(8, "freshness", "medium", "No freshness signal",
                     "No current-year reference or 'last updated' signal. Generative engines discount stale-looking sources.",
                     "Add a visible 'Last updated: <current month/year>' and current-year references; keep facts current.")
        elif not dated:
            penalize(3, "freshness", "low", "No explicit update date",
                     "Content mentions the current year but shows no explicit update date.",
                     "Surface a 'Last updated' date (and dateModified in Article schema).")
    else:
        # Commerce page: no article-style date needed — only a light nudge if the
        # copy reads undated. Never recommend stamping a fake 'Last updated' date.
        if not fresh:
            penalize(3, "freshness", "low", "No current-year reference",
                     "The copy has no current-year reference, so it can read as stale to AI engines.",
                     "Weave the current year into the description naturally (e.g. 'our 2026 Montessori collection') and keep the product selection current — do NOT stamp a 'Last updated' date on a category page.")

    # ── AUTHORITY (optional — only if Moz data supplied) ────────
    if page_authority is not None:
        try:
            pa = float(page_authority)
            if pa < 25:
                penalize(6, "authority", "medium", "Low page authority",
                         f"Page authority {pa:.0f}. AI engines lean on authoritative sources; low PA reduces selection odds.",
                         "Build internal links from strong pages and earn a few relevant external links (see Playbook → Orphans/Clusters).")
        except (TypeError, ValueError):
            pass

    # ── MACHINE-READABLE FACTS (JSON-LD field completeness) ─────
    # Type-presence is scored elsewhere (rich_results). Here we score whether the
    # schema the page DOES emit carries the machine-readable FACTS an AI answer
    # quotes — a price, an availability state, a breadcrumb trail placing the page
    # in its category. Gated on schema_facts being present, so pages evaluated
    # before facts were captured (or with genuinely no schema — handled by the
    # answerability/rich-results checks) are never falsely penalized here.
    if schema_facts:
        product = schema_facts.get("product") or {}
        offer = schema_facts.get("offer") or {}
        bc = schema_facts.get("breadcrumblist") or {}
        article = schema_facts.get("article") or {}
        if asset_type in ("product", "category"):
            if product and not offer:
                penalize(7, "machine_readable", "medium", "Product schema without an Offer",
                         "The page emits Product schema but no Offer node, so price and availability aren't machine-readable. AI shopping answers quote price/availability directly.",
                         "Nest an Offer inside the Product node with price, priceCurrency, and availability (must match the visible price).")
            elif offer and not (offer.get("price") and offer.get("availability")):
                _missing = ", ".join(k for k in ("price", "priceCurrency", "availability") if not offer.get(k))
                penalize(5, "machine_readable", "medium", "Incomplete Offer facts",
                         f"Offer schema is present but missing: {_missing}. Partial offers don't qualify for price/availability rich results or AI shopping citations.",
                         "Complete the Offer with price, priceCurrency (e.g. USD), and availability (e.g. https://schema.org/InStock).")
            if not bc.get("item_count"):
                penalize(3, "machine_readable", "low", "No breadcrumb trail in schema",
                         "No BreadcrumbList detected in the page's schema. AI engines and the SERP use breadcrumb structure to place the page in its category hierarchy.",
                         "Add BreadcrumbList JSON-LD reflecting the real nav trail (most Magento SEO extensions can emit this site-wide).")
        if asset_type in ("blog", "article", "guide") and article and not article.get("dateModified"):
            penalize(3, "machine_readable", "low", "Article schema without dateModified",
                     "Article/BlogPosting schema is present but carries no dateModified, so engines can't read a freshness date from the markup.",
                     "Add dateModified (and datePublished) to the Article JSON-LD, set to the real last-updated date.")

    # ── FORMAT NOTE (Török: product/pricing pages cite low) ─────
    format_note = None
    if asset_type in ("product", "category"):
        format_note = ("Product & category pages are rarely cited directly by AI engines "
                       "(Török 2026). Your citation vehicle is editorial content — guides, "
                       "comparisons, and reviews that reference and link to this page. Make "
                       "sure a strong guide/blog post cites this page, and add FAQ + Product "
                       "schema here so it's quotable when it IS surfaced.")

    score = max(0, min(100, score))
    findings.sort(key=lambda f: f["points"], reverse=True)
    by_dim = {}
    for f in findings:
        by_dim[f["dimension"]] = by_dim.get(f["dimension"], 0) + 1

    grade = _grade(score)
    if score >= 85:
        verdict = "Strong GEO readiness — well-structured, evidence-rich, and answer-oriented. Likely citable."
    elif score >= 70:
        verdict = "Good foundation with a few gaps — close the top findings to improve citation odds."
    elif score >= 55:
        verdict = "Moderate — the content isn't yet shaped the way AI engines quote sources."
    else:
        verdict = "Low GEO readiness — needs evidence, structure, and answer-oriented formatting to be citable."

    return {
        "url": url,
        "asset_type": asset_type,
        "score": score,
        "grade": grade,
        "findings": findings,
        "by_dimension": by_dim,
        "stat_count": stat_count,
        "evidence_density_per_1k": round(density, 1),
        "format_note": format_note,
        "verdict": verdict,
        "limited": False,
    }


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html):
    if not html:
        return ""
    return _TAG_RE.sub(" ", html)
