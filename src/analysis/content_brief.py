"""Content creator — Phase 4 backbone: the grounded brief + output contract.

This module is the deterministic, no-network, no-fabrication half of the content
creator. Given a scheduled topic (a pillar or an article from the Phase-3
cadence), it assembles everything a draft MUST be built from and checked against,
straight from the store's real data:

  • the topic (title, keyword, intent, outline) — from the real content-gap
    article where one exists, or a plain structural scaffold for a pillar;
  • the REQUIRED interlinks — this new page is a spoke, so it must link UP to its
    cluster's hub and reference real sibling product pages (Phase-2 logic);
  • the real catalog products in the family (URL + title) to ground every
    "Shop the Setup" CTA — never an invented product or price;
  • candidate authoritative EXTERNAL sources (real organizations) to cite, so the
    page borrows authority — the exact URL is confirmed by a human in review;
  • the exact Magento OUTPUT BLOCKS the draft must come back in (separate meta
    title / meta description / URL key / H1 / body HTML / FAQ schema), and the
    hard anti-fabrication rules the LLM draft is held to.

The actual prose is written by the LLM step in the web layer, tightly constrained
by what this module produces. Verification (`verify_interlinks`) later checks the
published page actually carries the links the brief required. All pure functions.
"""
from __future__ import annotations

from src.analysis.topical_authority import (
    build_cluster_map, _family_matchers, _family_slug, _norm_url, _anchor_for,
)
from src.analysis.content_gap import _distinctive, _sig


# Real, well-known authoritative organizations relevant to child development,
# early education, play and Montessori. These are CANDIDATES to cite — the drafter
# links to a relevant page on one of them and the human confirms the exact URL in
# review. We never fabricate a deep link; we point at genuine institutions.
EXTERNAL_AUTHORITY_CANDIDATES = [
    {"name": "American Academy of Pediatrics (HealthyChildren.org)",
     "domain": "healthychildren.org", "good_for": "child development, play, safety"},
    {"name": "National Association for the Education of Young Children (NAEYC)",
     "domain": "naeyc.org", "good_for": "early-childhood education, developmentally appropriate practice"},
    {"name": "CDC — Child Development / Milestones",
     "domain": "cdc.gov", "good_for": "developmental milestones by age"},
    {"name": "ZERO TO THREE",
     "domain": "zerotothree.org", "good_for": "infant/toddler development"},
    {"name": "Association Montessori Internationale (AMI)",
     "domain": "montessori-ami.org", "good_for": "authentic Montessori principles"},
    {"name": "American Montessori Society (AMS)",
     "domain": "amshq.org", "good_for": "Montessori method and classrooms"},
    {"name": "National Institute for Play",
     "domain": "nifplay.org", "good_for": "the science and value of play"},
    {"name": "Harvard Center on the Developing Child",
     "domain": "developingchild.harvard.edu", "good_for": "brain development, executive function"},
]

# How a draft must come back — the separate, cut-and-paste Magento blocks.
OUTPUT_BLOCKS = [
    {"key": "meta_title", "label": "Meta Title", "max_chars": 60,
     "magento": "Page → SEO → Meta Title"},
    {"key": "meta_description", "label": "Meta Description", "max_chars": 155,
     "magento": "Page → SEO → Meta Description"},
    {"key": "url_key", "label": "URL Key", "max_chars": 75,
     "magento": "Page → SEO → URL Key"},
    {"key": "h1", "label": "H1 / Page Title", "max_chars": 70,
     "magento": "Page → Page Title (on-page H1)"},
    {"key": "body_html", "label": "Body HTML", "max_chars": None,
     "magento": "Content → Show/Hide Editor → paste raw HTML"},
    {"key": "faq_jsonld", "label": "FAQPage JSON-LD", "max_chars": None,
     "magento": "end of body, or Design → Layout XML"},
]

TARGET_PRODUCTS = 3     # real product pages to surface as CTAs / link-downs
TARGET_EXTERNAL = (1, 3)  # min/max authoritative external links


def _family_block(results: list, product_families: list, family_name: str):
    cmap = build_cluster_map(results, product_families)
    for f in cmap["families"]:
        if f["family"].lower() == (family_name or "").lower():
            return f
    return None


def _pillar_scaffold(family: str) -> dict:
    """A STRUCTURAL outline for a pillar when there's no keyword-backed article —
    format only, not facts. The drafter fills it from grounded inputs."""
    fam = family.strip()
    return {
        "title": f"{fam.title()}: The Complete Guide",
        "primary_keyword": fam.lower(),
        "intent": "informational",
        "word_count_target": 1800,
        "outline": [
            f"What {fam} are and who they're for",
            f"How {fam} support learning / development",
            f"How to choose the right one (ages, materials, features)",
            "Our range — what we make and how it's personalized",
            "Care, safety and FAQs",
        ],
        "supporting_keywords": [],
    }


def build_brief(results: list, product_families: list, family_name: str,
                target: dict = None, item_type: str = "article") -> dict:
    """Assemble the grounded brief + output contract for one scheduled topic.

    `target` is the Phase-3 gap article (title/primary_keyword/outline/…) when the
    item is a keyword-backed article; pass None for a pillar (a structural
    scaffold is used). Returns a dict ready for the LLM step and for later
    verification. Pure — only real pages/links are referenced; nothing invented.
    """
    fam = _family_block(results, product_families, family_name)
    is_pillar = item_type == "pillar" or not target
    topic = (target if target else _pillar_scaffold(family_name)) if not is_pillar \
        else _pillar_scaffold(family_name)
    if target and not is_pillar:
        topic = {
            "title": target.get("title"),
            "primary_keyword": target.get("primary_keyword"),
            "intent": target.get("intent", "informational"),
            "word_count_target": target.get("word_count_target") or 1200,
            "outline": target.get("outline") or [],
            "supporting_keywords": target.get("supporting_keywords") or [],
        }

    hub = (fam or {}).get("hub")
    spokes = (fam or {}).get("spokes", [])
    products = [s for s in spokes if s.get("asset_type") == "product"]
    blogs = [s for s in spokes if s.get("asset_type") == "blog"]

    # Required interlinks — a new spoke must tie into its cluster.
    link_up = None
    if hub:
        link_up = {"url": hub["url"],
                   "anchor_suggestion": _anchor_for(hub.get("title", ""), family_name)}
    # Real sibling products to surface as CTAs and link down-cluster to.
    link_products = [{"url": p["url"], "title": p["title"]}
                     for p in sorted(products, key=lambda x: -(x.get("impressions") or 0))[:TARGET_PRODUCTS]]
    # A couple of related existing articles to cross-link (if any).
    cross_links = [{"url": b["url"], "title": b["title"]}
                   for b in sorted(blogs, key=lambda x: -(x.get("impressions") or 0))[:2]]

    required_internal = []
    if link_up:
        required_internal.append({"url": link_up["url"], "direction": "up_to_hub",
                                  "anchor_suggestion": link_up["anchor_suggestion"]})
    for p in link_products:
        required_internal.append({"url": p["url"], "direction": "to_product",
                                  "anchor_suggestion": _anchor_for(p["title"], family_name)})

    return {
        "family": family_name,
        "family_slug": _family_slug(family_name),
        "item_type": "pillar" if is_pillar else "article",
        "topic": topic,
        "hub": hub,
        "link_up": link_up,
        "link_products": link_products,
        "cross_links": cross_links,
        "grounding_products": [{"url": p["url"], "title": p["title"]} for p in products],
        "required_internal_links": required_internal,
        "external_authority_candidates": EXTERNAL_AUTHORITY_CANDIDATES,
        "external_links_required": TARGET_EXTERNAL,
        "output_blocks": OUTPUT_BLOCKS,
        "drafting_rules": _DRAFTING_RULES,
        "no_catalog_match": fam is None or not products,
    }


_DRAFTING_RULES = [
    "GROUND EVERY FACT. Only state a product fact (material, size, feature, "
    "personalization option) that is given in the grounding products or that is "
    "obviously true; never invent specifications, prices, review counts or stats.",
    "FLAG, DON'T FABRICATE. Where a specific number or claim would strengthen the "
    "piece but you don't have it, write the sentence and mark the unverified part "
    "with [VERIFY: what to check] instead of inventing a value. Collect every such "
    "flag in verify_flags.",
    "CITE REAL AUTHORITY. Include 1–3 outbound links to a relevant page on one of "
    "the listed real authoritative organizations, placed where it backs a specific "
    "claim. Use the organization's real domain; do not invent a deep URL — give "
    "your best path and add it to external_links so a human confirms it in review.",
    "WIRE THE CLUSTER. Link UP to the hub once with a natural anchor, and link to "
    "the given sibling product pages where relevant (a 'Shop the Setup' style CTA). "
    "Use ONLY the real URLs provided; never link to a URL not in the brief.",
    "FORMAT FOR MAGENTO. Return each output block separately. Keep the meta title "
    "≤60 chars and the meta description ≤155 chars. The body is self-contained HTML "
    "with an inline <style> block; include an FAQ section plus a matching FAQPage "
    "JSON-LD block. Do not include a <title> tag or <meta> tags in the body — the "
    "meta fields are separate blocks.",
    "HUMAN-READY, NOT PUBLISHED. This is a draft for human review. Write for a real "
    "parent/educator audience with genuine substance; no filler, no keyword "
    "stuffing. A person will elevate it with first-hand product expertise before "
    "publishing.",
]


def build_generation_messages(brief: dict) -> tuple:
    """Turn a brief into (system_message, user_prompt) for the LLM. The system
    message encodes the anti-fabrication contract and the exact JSON output shape;
    the user prompt carries the grounded, page-specific inputs."""
    blocks_desc = "\n".join(
        f"  - {b['key']}: {b['label']}"
        + (f" (≤{b['max_chars']} chars)" if b.get("max_chars") else "")
        for b in brief["output_blocks"]
    )
    rules = "\n".join(f"{i+1}. {r}" for i, r in enumerate(brief["drafting_rules"]))
    system = (
        "You are a content creator for a real e-commerce store that sells "
        "personalized children's products. You write genuinely useful articles for "
        "parents and educators that are grounded in the store's real catalog and in "
        "real authoritative sources. You NEVER invent product specs, prices, "
        "statistics, or citations.\n\n"
        "RULES:\n" + rules + "\n\n"
        "Return ONLY a valid JSON object (no markdown fences) with these keys:\n"
        "  meta_title, meta_description, url_key, h1 (strings);\n"
        "  body_html (self-contained HTML with an inline <style>, the article, a "
        "'Shop the Setup' CTA to the real product URLs, and an FAQ section);\n"
        "  faq_jsonld (a single <script type=\"application/ld+json\"> FAQPage block "
        "matching the FAQ section);\n"
        "  internal_links (array of {url, anchor} you actually placed — must be a "
        "subset of the brief's real URLs);\n"
        "  external_links (array of {url, name, claim_supported} — 1 to 3 real "
        "authoritative sources);\n"
        "  verify_flags (array of strings — every [VERIFY] item for the human).\n"
        "Output blocks to produce:\n" + blocks_desc
    )

    t = brief["topic"]
    lines = [
        f"TOPIC: {t['title']}",
        f"Target keyword: {t['primary_keyword']}  |  intent: {t['intent']}  |  "
        f"~{t['word_count_target']} words  |  type: {brief['item_type']}",
        f"Product family: {brief['family']}",
    ]
    if t.get("outline"):
        lines.append("Suggested outline (adapt as needed): " + " → ".join(t["outline"]))
    if t.get("supporting_keywords"):
        lines.append("Cover these sub-topics as sections: " + ", ".join(t["supporting_keywords"]))
    if brief["link_up"]:
        lines.append(f"LINK UP to the hub (pillar) once: {brief['link_up']['url']} "
                     f"(suggested anchor: “{brief['link_up']['anchor_suggestion']}”)")
    if brief["link_products"]:
        lines.append("REAL product pages to feature / link (Shop the Setup) — use only these URLs:")
        for p in brief["link_products"]:
            lines.append(f"   • {p['title']} — {p['url']}")
    if brief["cross_links"]:
        lines.append("Related existing articles you may cross-link:")
        for c in brief["cross_links"]:
            lines.append(f"   • {c['title']} — {c['url']}")
    lo, hi = brief["external_links_required"]
    lines.append(f"Include {lo}–{hi} external links to RELEVANT pages on real authorities, e.g.:")
    for e in brief["external_authority_candidates"]:
        lines.append(f"   • {e['name']} ({e['domain']}) — good for: {e['good_for']}")
    if brief["no_catalog_match"]:
        lines.append("NOTE: no catalog products were found for this family — do NOT "
                     "invent any. Write the editorial content and add a [VERIFY] flag "
                     "asking which product pages to link.")
    user = "\n".join(lines)
    return system, user


def verify_interlinks(published_outlinks: list, brief_or_links) -> dict:
    """After the page is published and re-crawled, check it actually carries the
    internal links the brief required. `published_outlinks` is the page's
    internal_outlinks (list of {target_url,...}); `brief_or_links` is a brief dict
    or a list of required-link dicts. Pure; returns present/missing lists."""
    if isinstance(brief_or_links, dict):
        required = brief_or_links.get("required_internal_links", [])
    else:
        required = brief_or_links or []
    present_urls = {_norm_url(o.get("target_url", "")) for o in (published_outlinks or [])}
    present, missing = [], []
    for r in required:
        (present if _norm_url(r.get("url", "")) in present_urls else missing).append(r)
    return {
        "required": len(required),
        "present": present,
        "missing": missing,
        "all_present": len(missing) == 0 and len(required) > 0,
    }
