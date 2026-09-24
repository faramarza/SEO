"""Competitor gap analysis — why the page ranking #1 beats yours.

Given your page and the pages that outrank you for a query, it profiles each
(content depth, structure, exact-phrase usage, schema, page type) and reports the
concrete, ranked gaps in plain English — so "it's a content/relevance problem"
stops being an assertion and becomes evidence. Pure functions; the crawling that
feeds it happens in the web layer.
"""
from __future__ import annotations


def _median(xs):
    s = sorted(x for x in xs if x is not None)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _has(text, phrase):
    if not phrase:
        return False
    return phrase.lower().strip() in (text or "").lower()


def profile(page: dict, query: str) -> dict:
    """Turn a crawled page (title/h1/meta/word_count/headings/schema_types/
    content_preview/internal_links) into comparable features for `query`."""
    q = (query or "").lower().strip()
    heads = page.get("headings") or []
    # Headings arrive either as level dicts ({level,text}) or, from the crawler,
    # as a flat list of section-heading TEXT (already h2/h3/h4, h1 excluded). In
    # the flat case every entry IS a section heading, so count them all.
    if heads and isinstance(heads[0], dict):
        h2s = [h for h in heads if str(h.get("level")).strip() in ("2", "h2")]
    else:
        h2s = list(heads)
    st = [str(s).lower() for s in (page.get("schema_types") or [])]
    wc = int(page.get("word_count") or 0)
    p = {
        "url": page.get("url", ""),
        "word_count": wc,
        "n_headings": len(heads),
        "n_h2": len(h2s),
        "title": page.get("title", "") or "",
        "h1": page.get("h1", "") or "",
        "q_in_title": _has(page.get("title"), q),
        "q_in_h1": _has(page.get("h1"), q),
        "q_in_intro": _has(page.get("content_preview"), q),
        "schema_types": st,
        "has_faq": any("faq" in s for s in st),
        "has_article": any(s in ("article", "blogposting", "newsarticle") for s in st),
        "has_product": any(("product" in s) or ("itemlist" in s) or ("offer" in s) for s in st),
        "internal_links": int(page.get("internal_links") or 0),
    }
    p["page_type"] = _page_type(p)
    return p


def _page_type(p: dict) -> str:
    if p["has_product"] and p["word_count"] < 300:
        return "thin category / product grid"
    if p["word_count"] >= 800 and p["n_h2"] >= 3:
        return "in-depth guide"
    if p["has_article"] or (p["word_count"] >= 400 and p["n_h2"] >= 2):
        return "article / content page"
    if p["word_count"] < 250:
        return "thin page"
    return "standard page"


_CONTENT_TYPES = ("in-depth guide", "article / content page")


def analyze(mine: dict, competitors: list, query: str) -> dict:
    """mine + competitor profiles → ranked gaps + a plain-English verdict."""
    comps = [c for c in (competitors or []) if c]
    if not comps:
        return {"available": False,
                "verdict": "No competitor pages could be read for this query yet.",
                "gaps": [], "mine": mine, "competitors": []}

    med_wc = round(_median([c["word_count"] for c in comps]))
    med_h2 = round(_median([c["n_h2"] for c in comps]))
    comp_content = [c for c in comps if c["page_type"] in _CONTENT_TYPES]
    mine_is_thin_cat = mine["page_type"] in ("thin category / product grid", "thin page")

    gaps = []

    # 1) Page-type / intent mismatch — the usual "stuck for years" ceiling.
    if comp_content and len(comp_content) >= max(1, len(comps) // 2) and mine_is_thin_cat:
        gaps.append({
            "severity": "high", "field": "Page type vs. search intent",
            "title": "Google wants a content page here — yours is a product grid",
            "detail": (f"{len(comp_content)} of the top {len(comps)} results are guides/articles, "
                       f"but your page is a {mine['page_type']} with {mine['word_count']} words. For "
                       f"“{query}” Google is rewarding pages that explain the topic, and a bare "
                       "category grid structurally can't win that — which is exactly why years of small "
                       "tweaks never moved it. Fix: add real on-page content that answers the query, or "
                       "target it with a proper guide page.")})

    # 2) Content depth.
    if med_wc >= 300 and mine["word_count"] < med_wc * 0.5:
        gaps.append({
            "severity": "high", "field": "Content depth",
            "title": f"Far less content — {mine['word_count']} words vs ~{med_wc}",
            "detail": (f"The pages ranking here carry ~{med_wc} words of on-topic content; yours has "
                       f"{mine['word_count']}. Google can't judge your page the best answer to "
                       f"“{query}” when there's little text about it. Fix: expand the page to "
                       "genuinely cover the topic (not filler) — aim for the competitors' depth.")})

    # 3) Exact phrase in the title / H1.
    if not mine["q_in_h1"] and sum(c["q_in_h1"] for c in comps) >= len(comps) / 2:
        gaps.append({
            "severity": "medium", "field": "H1 heading",
            "title": "Your main heading doesn't contain the phrase",
            "detail": (f"Most ranking pages put “{query}” in their H1; yours is "
                       f"“{mine['h1'] or '(no H1 found)'}”. Fix: make the H1 lead with the exact "
                       "term (naturally).")})
    if not mine["q_in_title"] and sum(c["q_in_title"] for c in comps) >= len(comps) / 2:
        gaps.append({
            "severity": "medium", "field": "Title tag",
            "title": "Your <title> doesn't lead with the phrase",
            "detail": (f"Competitors' titles feature “{query}” up front; yours is "
                       f"“{mine['title'] or '(no title)'}”. Fix: rewrite the meta title to lead "
                       "with the exact query.")})

    # 4) Structure.
    if med_h2 >= 3 and mine["n_h2"] <= med_h2 - 2:
        gaps.append({
            "severity": "medium", "field": "Structure / sub-topics",
            "title": f"Thin structure — {mine['n_h2']} sections vs ~{med_h2}",
            "detail": (f"They break “{query}” into ~{med_h2} sub-sections (H2s) answering the "
                       f"questions searchers have; yours has {mine['n_h2']}. Fix: add H2 sections that "
                       "cover the sub-topics competitors do.")})

    # 5) Schema.
    if sum(c["has_faq"] for c in comps) >= 1 and not mine["has_faq"]:
        gaps.append({
            "severity": "low", "field": "FAQ schema",
            "title": "Competitors use FAQ schema; you don't",
            "detail": ("A ranking page uses FAQ structured data (extra SERP real estate and relevance "
                       "signal). Fix: add an FAQ section with FAQ JSON-LD.")})

    sev_rank = {"high": 0, "medium": 1, "low": 2}
    gaps.sort(key=lambda g: sev_rank.get(g["severity"], 3))

    # Plain-English "why you're stuck" — lead with the dominant structural gap.
    if not gaps:
        verdict = ("On the measurable on-page signals your page is on par with the pages that outrank "
                   "you — the gap is likely off-page (brand/authority, click-through behaviour, or "
                   "freshness) or something these signals don't capture.")
    else:
        top = gaps[0]
        verdict = top["title"] + ". " + top["detail"].split("Fix:")[0].strip()

    return {
        "available": True,
        "query": query,
        "verdict": verdict,
        "gaps": gaps,
        "mine": mine,
        "competitors": comps,
        "median_word_count": med_wc,
    }
