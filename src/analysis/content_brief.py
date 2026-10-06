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

import functools
import os
import re

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
TARGET_EXTERNAL = (2, 3)  # min/max authoritative external links — aim for 3


@functools.lru_cache(maxsize=1)
def house_style_block() -> str:
    """The operator's house <style> block, lifted verbatim from the sample article
    template so every draft inherits the same brand styling (hero, benefit grid,
    numbered cards, product-integration callouts, tip/warning boxes). Empty string
    if the template file is missing (the drafter then falls back to plain HTML)."""
    path = os.path.join(os.path.dirname(__file__), "article_template.html")
    try:
        html = open(path, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"<style[^>]*>.*?</style>", html, re.S | re.I)
    return m.group(0).strip() if m else ""


# The house article structure, described generically (the sample is a listicle,
# but these components apply to any topic). The class names match house_style_block
# so reusing them inherits the styling.
HOUSE_STRUCTURE = (
    "HOUSE TEMPLATE — build body_html in THIS structure, reusing these exact class "
    "names so the page inherits the brand styling:\n"
    "1) Begin body_html with the provided <style> block reproduced VERBATIM.\n"
    "2) Hero: <div class=\"hero-dance\"> with the <h2> title and a one-line promise.\n"
    "3) <div class=\"quick-nav\"> with in-page jump links to the main sections.\n"
    "4) A short intro, then a <div class=\"benefits-box\"> whose <div class=\"benefit-grid\"> "
    "holds several <div class=\"benefit-item\"> cards (emoji icon + bold heading + one line) "
    "— the 'why it matters' section. Put ONE real, cited statistic here (or a [VERIFY] flag).\n"
    "5) The main content at real depth. For a LIST/“best/top/N” article: a "
    "<div class=\"age-filter-buttons\"> of <button class=\"age-filter-btn\"> age filters, then "
    "the numbered collection — each item a <div class=\"game-card\"> containing a "
    "<span class=\"game-number\">, a heading with an <span class=\"age-tag\">Ages X–Y</span>, "
    "the how-to, and a <div class=\"materials-box\"> listing what's needed. For a guide/"
    "comparison/pillar: use clear <h2>/<h3> sections instead of cards, same depth.\n"
    "6) Weave 1–2 <div class=\"product-integration\"> callouts between sections that feature "
    "the real products provided — each with its own <h3> heading (e.g. 'Shop the Setup'), a "
    "line of copy, and the product link(s).\n"
    "7) Use <div class=\"tip-box\"> for pro tips and <div class=\"warning-box\"> for safety notes.\n"
    "8) End with an FAQ section under <h2 id=\"faq\">: each item a plain <h3> question followed "
    "by a <p> answer. Do NOT wrap FAQ items in .game-card (cards are for the main list only). "
    "Return the matching faq_jsonld block separately.\n"
    "Match the SAMPLE'S DEPTH — this is a comprehensive article, not a summary."
)

# Age numbers ("1 year old", "0-3 months") must NOT be read as list counts, so we
# strip them before testing for a listicle.
_AGE_NUM_RE = re.compile(
    r"\b\d{1,3}\s*[-– ]?\s*\d{0,3}\s*(?:year|yr|month|mo|week)s?(?:[-\s]?olds?)?\b", re.I)
_AGE_RE = re.compile(
    r"(newborns?|infants?|bab(?:y|ies)|toddlers?|preschool(?:er)?s?|kindergarten(?:ers?)?"
    r"|\d+\s*[-– ]?\s*(?:month|year|week)s?(?:[-\s]?olds?)?)", re.I)
_LIST_KW_RE = re.compile(
    r"\b(best|top|ideas|activities|games|ways|examples|tips|types|list|ultimate|checklist)\b", re.I)


def _strip_age(s: str) -> str:
    return _AGE_NUM_RE.sub(" ", s or "")


def _is_listicle(title: str, primary_keyword: str) -> bool:
    s = _strip_age(((title or "") + " " + (primary_keyword or "")).strip())
    # A list keyword, OR a standalone number that ISN'T an age (ages were stripped).
    return bool(_LIST_KW_RE.search(s) or re.search(r"\b\d{1,3}\b", s))


def _age_context(title: str, primary_keyword: str) -> str:
    m = _AGE_RE.search(((title or "") + " " + (primary_keyword or "")).strip())
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def _listicle_count(title: str, primary_keyword: str) -> int:
    s = _strip_age(((title or "") + " " + (primary_keyword or "")))
    m = re.search(r"\b(\d{1,3})\b", s)
    return int(m.group(1)) if m else 0


# Product titles that signal a NEWBORN / very-young-infant product. The model kept
# forcing these onto older-age articles (a tummy-time mirror on a 1-year-old
# listicle), so we exclude them in CODE for any article aimed above infancy —
# don't rely on the model's judgement.
_NEWBORN_PRODUCT_RE = re.compile(
    r"\b(tummy[\s-]?time|newborn|0[\s-]*6\s*months?|infant mirror|crib (?:toy|mobile)|rattle)\b", re.I)


def _is_infant_article(age_str: str) -> bool:
    """True when the article itself targets newborns/young infants (0–12 mo), in
    which case newborn products ARE appropriate and must not be filtered out."""
    a = (age_str or "").lower()
    if not a:
        return False
    if "newborn" in a or "infant" in a:
        return True
    m = re.search(r"(\d{1,2})\s*(month|week)", a)
    if m:
        return True   # measured in months/weeks → infant
    m = re.search(r"\b(\d{1,2})\b", a)   # "0" or "1" year → still include; else older
    return bool(m and int(m.group(1)) <= 0)


def _filter_products_for_age(products: list, age_str: str) -> list:
    """Drop clearly-newborn products from an article aimed above infancy. Conservative:
    only strong newborn signals are removed, and only when the article isn't itself
    about infants. Never filters when there's no age focus."""
    if not age_str or _is_infant_article(age_str):
        return products
    kept = [p for p in products if not _NEWBORN_PRODUCT_RE.search(p.get("title", ""))]
    # Safety: if the filter would wipe out everything, keep the originals (better to
    # let the model judge than to ground on nothing).
    return kept if kept else products


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
                target: dict = None, item_type: str = "article",
                title: str = None, primary_keyword: str = None) -> dict:
    """Assemble the grounded brief + output contract for one scheduled topic.

    The draft is ALWAYS about the requested topic. Topic resolution, in order:
      • item_type == "pillar" → a structural pillar scaffold for the family;
      • a `target` gap-article (title/primary_keyword/outline/…) → use it;
      • otherwise an article built from the given `title` / `primary_keyword`.
    The family is used only for GROUNDING (which hub/products to link) — it never
    overrides the topic, so a missing gap-article can't silently turn a requested
    article into a generic family pillar. Pure — only real pages/links referenced.
    """
    fam = _family_block(results, product_families, family_name)
    is_pillar = (item_type == "pillar")
    if is_pillar:
        topic = _pillar_scaffold(family_name)
    elif target:
        topic = {
            "title": target.get("title"),
            "primary_keyword": target.get("primary_keyword"),
            "intent": target.get("intent", "informational"),
            "word_count_target": target.get("word_count_target") or 1200,
            "outline": target.get("outline") or [],
            "supporting_keywords": target.get("supporting_keywords") or [],
        }
    else:
        # Article about a specific requested topic with no matched gap-article —
        # write about THAT topic, grounded in the family, not a family pillar.
        pk = (primary_keyword or title or family_name or "").strip()
        disp = (title or (pk[:1].upper() + pk[1:] if pk else family_name) or "").strip()
        topic = {
            "title": disp,
            "primary_keyword": primary_keyword or title or family_name,
            "intent": "informational",
            "word_count_target": 1400,
            "outline": [],
            "supporting_keywords": [],
        }

    hub = (fam or {}).get("hub")
    spokes = (fam or {}).get("spokes", [])
    products = [s for s in spokes if s.get("asset_type") == "product"]
    blogs = [s for s in spokes if s.get("asset_type") == "blog"]

    # Age-gate the products in CODE: a newborn item (tummy-time mirror, rattle) must
    # never be offered to a toddler+ article — the model repeatedly forced them in.
    _age = _age_context(topic.get("title"), topic.get("primary_keyword"))
    products = _filter_products_for_age(products, _age)

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

    # Only the hub link is MANDATORY (the cluster bond that must be verified live).
    # Product link-downs are strongly encouraged in the prompt but NOT hard-required,
    # because which products fit depends on the topic/age — forcing a fixed set would
    # flag a legitimately-omitted off-age product as a "missing" link in verification.
    required_internal = []
    if link_up:
        required_internal.append({"url": link_up["url"], "direction": "up_to_hub",
                                  "anchor_suggestion": link_up["anchor_suggestion"]})

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
    "ONE CITED STATISTIC. Open the 'why it matters' section with a single concrete, "
    "compelling statistic from a named authority — and CITE it (link the source in "
    "external_links). If you cannot ground a real figure, write the sentence with a "
    "[VERIFY: stat to find — add source] placeholder instead; never invent a number.",
    "NEVER MAKE AN UNCITED AUTHORITY CLAIM. Any sentence that says 'according to "
    "<organization>' or quotes research/a statistic MUST carry a real link to that "
    "source. If you don't have a real URL for it, either drop the 'according to X' "
    "framing and state it plainly as general guidance, or write [VERIFY: add source]. "
    "Never attribute a claim to a named authority without a working link.",
    "PLACE CITATIONS INLINE. Each external authority link goes INLINE as an <a href> "
    "right on the sentence/claim it supports — do NOT gather them into a separate "
    "'Sources' or 'References' box at the end. external_links must list exactly the "
    "links you placed inline in the body, and nothing that isn't in the body.",
    "FOLLOW THE HOUSE TEMPLATE. Build body_html in the house structure provided, "
    "starting with the given <style> block reproduced VERBATIM and reusing its "
    "component classes (hero, benefits-box/benefit-item, product-integration, "
    "tip-box/warning-box, and the numbered .game-card collection with .age-tag for "
    "list articles). Return each Magento block separately; meta title ≤60 chars, "
    "meta description ≤155. Do NOT put a <title> or <meta> tag in the body.",
    "WRITE TO DEPTH. Hit the required word count with genuine substance — real "
    "how-to detail, specifics, examples — not padding or keyword stuffing. A thin "
    "draft that skips the house structure is a failure.",
    "OUR BRAND ONLY. Never name, link, or promote another retailer or competing "
    "brand. If a competitor's name slips into the SERP inputs, ignore it — the only "
    "store this page represents is ours.",
    "AGE-APPROPRIATE ONLY. When the topic targets a specific age, feature only "
    "products and advice suitable for that age; omit any provided product that "
    "doesn't fit rather than forcing it in.",
    "HUMAN-READY, NOT PUBLISHED. This is a draft for human review. Write for a real "
    "parent/educator audience; a person will elevate it with first-hand product "
    "expertise before publishing — the AI draft is the floor, not the ceiling.",
]


def length_floor(topic: dict, item_type: str, competitor: dict = None) -> int:
    """The minimum body word count for a draft: a real-article floor (1500), scaled
    up for listicles (~90 words/item) and never under the competitor median. Shared
    by the generation prompt and the auto-expand check so they agree."""
    title, pk = topic.get("title"), topic.get("primary_keyword")
    listicle = _is_listicle(title, pk)
    n = _listicle_count(title, pk) if listicle else 0
    comp = int((competitor or {}).get("target_words") or 0)
    return max(int(topic.get("word_count_target") or 0), 1500,
               (n * 90 if listicle and n else 0), comp)


def build_expand_messages(body_html: str, floor: int, brief: dict) -> tuple:
    """Prompt to DEEPEN an existing draft to the target length without changing its
    structure, styling, links, or facts. Returns (system, user); the model returns
    ONLY the expanded body_html."""
    system = (
        "You expand an existing draft article to greater depth. You NEVER change its "
        "structure, the <style> block, headings (or their ids), the FAQ, or ANY link "
        "(<a href>). You never add or alter product links or external URLs, and never "
        "invent specs, prices, or statistics — if tempted, write [VERIFY: …] instead. "
        "You only add substance WITHIN the existing sections/cards. Return ONLY the "
        "expanded body_html — no JSON, no markdown fences, no commentary."
    )
    user = (
        f"Expand this article to AT LEAST {floor} words of body copy by deepening each "
        "existing section and card with specific, grounded how-to detail, concrete "
        "examples, and parent-facing guidance. Keep the exact same <style> block, every "
        "heading with its id, the FAQ, and every existing <a href> link — add nothing "
        "new that links out. Here is the current body_html:\n\n" + (body_html or "")
    )
    return system, user


def build_generation_messages(brief: dict, competitor: dict = None) -> tuple:
    """Turn a brief into (system_message, user_prompt) for the LLM. The system
    message encodes the anti-fabrication contract and the exact JSON output shape;
    the user prompt carries the grounded, page-specific inputs. `competitor` is the
    optional SERP coverage spec (terms/headings/questions/target length) from
    competitor_brief — when present, the draft is steered to cover what ranks."""
    blocks_desc = "\n".join(
        f"  - {b['key']}: {b['label']}"
        + (f" (≤{b['max_chars']} chars)" if b.get("max_chars") else "")
        for b in brief["output_blocks"]
    )
    rules = "\n".join(f"{i+1}. {r}" for i, r in enumerate(brief["drafting_rules"]))
    system = (
        "You are a content creator for a real e-commerce store that sells "
        "personalized children's products. You write genuinely useful, in-depth "
        "articles for parents and educators, grounded in the store's real catalog "
        "and in real authoritative sources, and formatted in the store's HOUSE "
        "TEMPLATE. You NEVER invent product specs, prices, statistics, or citations.\n\n"
        "RULES:\n" + rules + "\n\n"
        "Return ONLY a valid JSON object (no markdown fences) with these keys:\n"
        "  meta_title, meta_description, url_key, h1 (strings);\n"
        "  body_html (self-contained HTML built in the HOUSE TEMPLATE below: it MUST "
        "start with the provided <style> block reproduced verbatim, use the house "
        "component classes, feature the real products, and end with an FAQ section);\n"
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
    listicle = _is_listicle(t.get("title"), t.get("primary_keyword"))
    age = _age_context(t.get("title"), t.get("primary_keyword"))
    n_items = _listicle_count(t.get("title"), t.get("primary_keyword"))
    # Hard length floor (shared with the auto-expand check so they agree).
    floor = length_floor(t, brief["item_type"], competitor)

    style_block = house_style_block()
    lines = [
        f"TOPIC: {t['title']}",
        f"Target keyword: {t['primary_keyword']}  |  intent: {t['intent']}  |  "
        f"type: {brief['item_type']}",
        f"Product family: {brief['family']}",
        f"LENGTH: write AT LEAST {floor} words of real body copy — match the house "
        "sample's depth. Per-component budgets (this is how you reach the length with "
        "substance, not padding): intro 120+ words; EACH prose <h2> section 150–250 "
        "words; EACH product/idea card 90–140 words (4–6 sentences: what it is, what it "
        "develops at this age, how a parent uses it, a tip); each FAQ answer 40–80 words. "
        "A thin, one-line-per-card draft is a failure.",
    ]
    if age:
        lines.append(f"AGE FOCUS: this article is specifically for {age}. Every idea, "
                     f"recommendation and product MUST be genuinely appropriate for {age}. "
                     f"NEVER include a product meant for a different age (e.g. a newborn "
                     f"tummy-time item in a 1-year-old article) — leave it out entirely, "
                     f"even if that means featuring fewer of the store's products.")
    if listicle:
        want = n_items if n_items else 12
        lines.append(
            "FORMAT: this is a LIST article. The numbered .game-card items are IDEAS / "
            "recommendations / toy-types / activities — NOT one card per store product. "
            f"Produce {want} substantial cards. EACH card is 3–5 sentences: what it is, what "
            "it develops at this age, how a parent uses it, and one practical tip — a "
            "one-sentence card is a failure. Put the <span class=\"age-tag\"> on the heading. "
            "GROUNDING: only put a .materials-box or state specific materials/brand when the "
            "card features one of the REAL products listed below — and then LINK that product "
            "in the card. For a generic toy-type idea you don't stock, describe the type "
            "without inventing materials, a brand, or a link. Also weave 1–2 "
            ".product-integration callouts for real products. Never feature an off-age or "
            "invented product to reach the count.")
        # The age-filter row only makes sense across MULTIPLE ages; a single-age
        # article ("for 1 year old") has nothing to filter, so omit it there.
        if age:
            lines.append("Do NOT include the .age-filter-buttons row — this article covers "
                         "a single age, so there is nothing to filter.")
        else:
            lines.append("Include the .age-filter-buttons row with one button per age band "
                         "you actually cover, matching the .age-tag values on the cards.")
    else:
        lines.append("FORMAT: this is a guide/comparison/pillar — use clear <h2>/<h3> "
                     "sections (not numbered cards), at the same depth, with the house "
                     "hero, benefits-box, product-integration callouts and tip/warning boxes.")
    lines.append("JUMP LINKS: give every major section an id and make the .quick-nav links "
                 "point to those exact ids (e.g. <h2 id=\"faq\">), so the nav actually works.")
    if style_block:
        lines.append("\nHOUSE <style> BLOCK — reproduce this VERBATIM at the very start "
                     "of body_html:\n" + style_block)
    lines.append("\n" + HOUSE_STRUCTURE)
    if t.get("outline"):
        lines.append("Suggested outline (adapt to the house structure): " + " → ".join(t["outline"]))
    if t.get("supporting_keywords"):
        lines.append("Cover these sub-topics as sections: " + ", ".join(t["supporting_keywords"]))
    # SERP coverage — what the current top-ranking pages cover. This is how the page
    # earns its rank: include these terms, sections and questions (where genuinely
    # relevant — never stuff), grounded as always.
    if competitor:
        terms = [x.get("term") for x in (competitor.get("terms") or []) if x.get("term")]
        heads = competitor.get("headings") or []
        qs = competitor.get("questions") or []
        if terms:
            lines.append("\nSERP COVERAGE — the top-ranking pages share these terms/"
                         "entities; weave the relevant ones in naturally (never keyword-"
                         "stuff): " + ", ".join(terms[:30]))
        if heads:
            lines.append("Sections competitors cover — write a real <h2> PROSE section "
                         "for the relevant ones (developmental benefits, how to choose, "
                         "safety, by-stage, etc.), in ADDITION to any product cards. This "
                         "editorial depth is where the ranking comes from: " + " | ".join(heads[:15]))
        if qs:
            lines.append("Questions to answer (work into the body and/or the FAQ): "
                         + " | ".join(qs[:10]))
        if competitor.get("target_words"):
            lines.append(f"Competitors run ~{competitor['target_words']} words — meet or "
                         "exceed that depth with substance, not padding.")
    if brief["link_up"]:
        lines.append(f"LINK UP to the hub (pillar) once: {brief['link_up']['url']} "
                     f"(suggested anchor: “{brief['link_up']['anchor_suggestion']}”)")
    # Offer the FULL real-product set (capped) so the model can pick the ones that
    # genuinely fit the topic/age and skip the rest — rather than being forced to
    # feature a fixed 3. Never invent a product or URL outside this list.
    avail = brief.get("grounding_products") or brief["link_products"]
    if avail:
        lines.append("REAL products you MAY feature / link (Shop the Setup) — choose the "
                     "ones that genuinely fit this topic and age; use ONLY these URLs, and "
                     "do not feature one that doesn't fit:")
        for p in avail[:14]:
            lines.append(f"   • {p['title']} — {p['url']}")
    if brief["cross_links"]:
        lines.append("Related existing articles you may cross-link:")
        for c in brief["cross_links"]:
            lines.append(f"   • {c['title']} — {c['url']}")
    lo, hi = brief["external_links_required"]
    auth = (competitor or {}).get("authority_links") or []
    if auth:
        # REAL, live, pre-filtered authority URLs — cite the RELEVANT ones verbatim.
        lines.append(f"External authority links — candidates below are real & live. Cite "
                     f"the {lo}–{hi} that genuinely back a claim in THIS article (right "
                     "topic and age), linking them EXACTLY as written. RELEVANCE OVER "
                     "COUNT: skip any that don't clearly fit — do NOT cite a page just to "
                     f"reach {hi}, and never invent or alter a URL:")
        for a in auth:
            lines.append(f"   • {a['url']}" + (f" — {a['title']}" if a.get("title") else ""))
    else:
        lines.append(f"Include {lo}–{hi} external links to RELEVANT pages on real "
                     "authorities. Do NOT guess a deep URL path (it may 404) — link the "
                     "org's HOMEPAGE and add a [VERIFY] flag to confirm a deep page, e.g.:")
        for e in brief["external_authority_candidates"]:
            lines.append(f"   • {e['name']} (https://{e['domain']}/) — good for: {e['good_for']}")
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
