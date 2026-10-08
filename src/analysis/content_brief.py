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


# Product titles that signal an OLDER-CHILD product (roughly 3+): a puppet theatre,
# board games, preschool/kindergarten gear, explicit "3+/ages 4" markers. The model
# kept forcing these onto young articles (a puppet theater on a 1-year-old piece), so
# exclude them in CODE for articles aimed at infants/young toddlers (≤ ~24 months).
_OLDER_PRODUCT_RE = re.compile(
    r"\b(puppets?|puppet[\s-]?theat(?:er|re)|marionette|board[\s-]?game|"
    r"preschool|pre-?k|kindergart|chess|checkers|scrabble|dominoes|"
    r"science\s*kit|chemistry\s*set|ages?\s*[3-9]|[3-9]\s*\+|[3-9]\s*years?\s*\+)\b", re.I)


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


def _article_age_months(age_str: str):
    """Approx age in months the article targets, or None. '1 year old'→12,
    '18 months'→18, 'toddler'→18, 'newborn'→1, 'preschool'→42."""
    a = (age_str or "").lower()
    if not a:
        return None
    if "newborn" in a:
        return 1
    m = re.search(r"(\d{1,2})\s*(?:month|mo|week)", a)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})\s*(?:year|yr)", a)
    if m:
        return int(m.group(1)) * 12
    if "infant" in a or "baby" in a or "babies" in a:
        return 6
    if "toddler" in a:
        return 18
    if "preschool" in a or "pre-k" in a:
        return 42
    if "kindergart" in a:
        return 60
    m = re.search(r"\b(\d{1,2})\b", a)   # a bare number means years
    if m:
        return int(m.group(1)) * 12
    return None


def _filter_products_for_age(products: list, age_str: str) -> list:
    """Keep the grounding products age-appropriate, in CODE (don't trust the model):
      • drop clearly-NEWBORN products from an article aimed above infancy;
      • drop clearly-OLDER-child (≈3+) products from a young article (≤ ~24 months).
    Conservative — only strong signals are removed, only when the article has a clear
    age, and never down to nothing (falls back to the originals)."""
    if not age_str:
        return products
    kept = products
    # 1) newborn products off an above-infancy article
    if not _is_infant_article(age_str):
        k = [p for p in kept if not _NEWBORN_PRODUCT_RE.search(p.get("title", ""))]
        kept = k or kept
    # 2) older-child (3+) products off a young (infant / young-toddler) article
    months = _article_age_months(age_str)
    if months is not None and months <= 24:
        k = [p for p in kept if not _OLDER_PRODUCT_RE.search(p.get("title", ""))]
        kept = k or kept
    return kept


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


def _clean_field_notes(text, cap: int = 8000) -> str:
    """Sanitize owner-pasted field notes / customer reviews before they become
    grounding: strip any HTML (a paste can't inject markup into the prompt or page),
    collapse whitespace, and cap length. The cleaned text is GROUNDING ONLY — it is
    never written to the published page verbatim (enforced downstream)."""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", str(text))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:cap]


def build_brief(results: list, product_families: list, family_name: str,
                target: dict = None, item_type: str = "article",
                title: str = None, primary_keyword: str = None,
                field_notes: str = None) -> dict:
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
        # Owner-pasted real notes/reviews — private grounding, never published verbatim.
        "field_notes": _clean_field_notes(field_notes),
    }


_DRAFTING_RULES = [
    "GROUND EVERY FACT. Only state a product fact (material, size, feature, "
    "personalization option) that is given in the grounding products or that is "
    "obviously true; never invent specifications, prices, review counts or stats.",
    "FLAG, DON'T FABRICATE. Where a specific number or claim would strengthen the "
    "piece but you don't have it, write the sentence and mark the unverified part "
    "with [VERIFY: what to check] instead of inventing a value. Collect every such "
    "flag in verify_flags.",
    "CITE ONLY PROVIDED URLS — NEVER GUESS ONE. Every external link MUST be copied "
    "EXACTLY from the authority candidates the brief gives you. NEVER invent, guess, "
    "or construct an authority URL — no made-up '.aspx', no deep paths you haven't "
    "been handed. A guessed link is a fabrication and will be stripped. If no provided "
    "source fits a claim, drop the 'according to X' framing or write [VERIFY: add "
    "source] — do not reach for a plausible-looking URL.",
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


def _prompt_ctx(brief: dict, competitor: dict = None) -> dict:
    """Derived values every prompt section may need — computed once."""
    t = brief["topic"]
    return {
        "t": t,
        "listicle": _is_listicle(t.get("title"), t.get("primary_keyword")),
        "age": _age_context(t.get("title"), t.get("primary_keyword")),
        "n_items": _listicle_count(t.get("title"), t.get("primary_keyword")),
        "floor": length_floor(t, brief["item_type"], competitor),
        "style_block": house_style_block(),
    }


# ─── PROMPT SECTIONS — one concern each. To change how the draft handles a given
# concern (length, age-fit, format, house template, SERP coverage, internal links,
# external citations), edit ONLY that section function; nothing else is affected. ──

def _sec_header(brief, competitor, ctx):
    t = ctx["t"]
    return [f"TOPIC: {t['title']}",
            f"Target keyword: {t['primary_keyword']}  |  intent: {t['intent']}  |  "
            f"type: {brief['item_type']}",
            f"Product family: {brief['family']}"]


def _sec_length(brief, competitor, ctx):
    floor = ctx["floor"]
    lines = [f"LENGTH: write AT LEAST {floor} words of real body copy — match the "
             "house sample's depth. Per-component budgets (this is how you reach the length "
             "with substance, not padding): intro 120+ words; EACH prose <h2> section 150–250 "
             "words; EACH product/idea card 90–140 words (4–6 sentences: what it is, what it "
             "develops at this age, how a parent uses it, a tip); each FAQ answer 40–80 words. "
             "A thin, one-line-per-card draft is a failure."]
    if not ctx["listicle"]:
        # A 1500-word article cannot be reached with 3–4 sections at the budgets above.
        # Mandate a concrete section count derived from the floor so the model spreads
        # the required depth across enough <h2> sections (drawn from the brief's
        # questions and sub-topics) instead of writing four thin ones.
        min_secs = max(5, (floor + 249) // 250)
        lines.append(f"SECTION COUNT: this article MUST have at least {min_secs} substantive "
                     "prose <h2> sections, PLUS the intro and the FAQ. Three or four sections "
                     f"cannot reach {floor} words — turn the brief's questions and sub-topics "
                     "into their own <h2> sections rather than cramming them into a few.")
    lines.append(f"SELF-CHECK before you finish: count the words in your body copy. If it is "
                 f"under {floor}, you are not done — add more sections or deepen existing ones "
                 "(never pad with filler) until you clear the floor. Do not submit a short draft.")
    return lines


def _sec_age(brief, competitor, ctx):
    if not ctx["age"]:
        return []
    a = ctx["age"]
    return [f"AGE FOCUS: this article is specifically for {a}. Every idea, recommendation "
            f"and product MUST be genuinely appropriate for {a}. NEVER include a product "
            "meant for a different age (e.g. a newborn tummy-time item in a 1-year-old "
            "article) — leave it out entirely, even if that means featuring fewer of the "
            "store's products."]


def _sec_format(brief, competitor, ctx):
    age, listicle, n_items = ctx["age"], ctx["listicle"], ctx["n_items"]
    out = []
    if listicle:
        want = n_items if n_items else 12
        out.append(
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
        out.append("Do NOT include the .age-filter-buttons row — this article covers a single "
                   "age, so there is nothing to filter." if age else
                   "Include the .age-filter-buttons row with one button per age band you "
                   "actually cover, matching the .age-tag values on the cards.")
    else:
        out.append("FORMAT: this is a guide/comparison/pillar — use clear <h2>/<h3> sections "
                   "(not numbered cards), at the same depth, with the house hero, benefits-box, "
                   "product-integration callouts and tip/warning boxes.")
    out.append("JUMP LINKS: give every major section an id and make the .quick-nav links point "
               "to those exact ids (e.g. <h2 id=\"faq\">), so the nav actually works.")
    return out


def _sec_house(brief, competitor, ctx):
    out = []
    if ctx["style_block"]:
        out.append("\nHOUSE <style> BLOCK — reproduce this VERBATIM at the very start of "
                   "body_html:\n" + ctx["style_block"])
    out.append("\n" + HOUSE_STRUCTURE)
    t = ctx["t"]
    if t.get("outline"):
        out.append("Suggested outline (adapt to the house structure): " + " → ".join(t["outline"]))
    if t.get("supporting_keywords"):
        out.append("Cover these sub-topics as sections: " + ", ".join(t["supporting_keywords"]))
    return out


def _sec_coverage(brief, competitor, ctx):
    if not competitor:
        return []
    out = []
    terms = [x.get("term") for x in (competitor.get("terms") or []) if x.get("term")]
    heads = competitor.get("headings") or []
    qs = competitor.get("questions") or []
    if terms:
        out.append("\nSERP COVERAGE — the top-ranking pages share these terms/entities; weave "
                   "the relevant ones in naturally (never keyword-stuff): " + ", ".join(terms[:30]))
    if heads:
        out.append("Sections competitors cover — write a real <h2> PROSE section for the "
                   "relevant ones (developmental benefits, how to choose, safety, by-stage, "
                   "etc.), in ADDITION to any product cards. This editorial depth is where the "
                   "ranking comes from: " + " | ".join(heads[:15]))
    if qs:
        out.append("Questions to answer (work into the body and/or the FAQ): " + " | ".join(qs[:10]))
    if competitor.get("target_words"):
        out.append(f"Competitors run ~{competitor['target_words']} words — meet or exceed that "
                   "depth with substance, not padding.")
    return out


def _sec_internal(brief, competitor, ctx):
    out = []
    if brief["link_up"]:
        out.append(f"LINK UP to the hub (pillar) once: {brief['link_up']['url']} "
                   f"(suggested anchor: “{brief['link_up']['anchor_suggestion']}”)")
    avail = brief.get("grounding_products") or brief["link_products"]
    if avail:
        out.append("REAL products you MAY feature / link (Shop the Setup) — choose the ones that "
                   "genuinely fit this topic and age; use ONLY these URLs, and do not feature one "
                   "that doesn't fit:")
        out += [f"   • {p['title']} — {p['url']}" for p in avail[:14]]
    if brief["cross_links"]:
        out.append("Related existing articles you may cross-link:")
        out += [f"   • {c['title']} — {c['url']}" for c in brief["cross_links"]]
    return out


def _sec_citations(brief, competitor, ctx):
    lo, hi = brief["external_links_required"]
    auth = (competitor or {}).get("authority_links") or []
    out = []
    if auth:
        out.append(f"External authority links — the candidates below are real, live, and "
                   f"relevant to child development/safety. PLACE {lo}–{hi} of them INLINE as "
                   "<a href> in the body (not in a box), each on the claim it best supports, and "
                   "SPREAD across different sections — e.g. one on the why-it-matters stat, one "
                   "in the developmental-benefits section, one in safety. Link each EXACTLY as "
                   "written; place 3 if you have 3 good spots (you do), and skip one only if it "
                   "genuinely doesn't fit. Do not list anything in external_links that you didn't "
                   "place inline:")
        out += [f"   • {a['url']}" + (f" — {a['title']}" if a.get("title") else "") for a in auth]
    else:
        out.append(f"Include {lo}–{hi} external links to RELEVANT pages on real authorities. Do "
                   "NOT guess a deep URL path (it may 404) — link the org's HOMEPAGE and add a "
                   "[VERIFY] flag to confirm a deep page, e.g.:")
        out += [f"   • {e['name']} (https://{e['domain']}/) — good for: {e['good_for']}"
                for e in brief["external_authority_candidates"]]
    if brief["no_catalog_match"]:
        out.append("NOTE: no catalog products were found for this family — do NOT invent any. "
                   "Write the editorial content and add a [VERIFY] flag asking which product "
                   "pages to link.")
    return out


def _sec_field_notes(brief, competitor, ctx):
    """Fold owner-pasted real notes / customer reviews in as PRIVATE grounding — the
    one source of first-hand, specific, E-E-A-T detail the model can't otherwise know.
    Emits nothing when no notes were pasted."""
    notes = (brief.get("field_notes") or "").strip()
    if not notes:
        return []
    return [
        "REAL FIELD NOTES / CUSTOMER REVIEWS (private grounding the store owner pasted — "
        "real observations about THESE products). Use them to add SPECIFIC, truthful, "
        "first-hand detail you could not otherwise know: how a product is actually used, "
        "what age/stage it suits, practical setup tips, genuine pros and cons. This is "
        "how you earn depth and E-E-A-T — prefer concrete detail from here over generic "
        "filler. HARD RULES:",
        "  (1) PARAPHRASE in your own words — NEVER copy a sentence verbatim. Copied runs "
        "are stripped from the page automatically, leaving a [VERIFY] gap, so rewrite.",
        "  (2) Do NOT reproduce any competitor brand, store, seller or reviewer NAME — "
        "strip them; keep only the product insight.",
        "  (3) Ground only what these notes (or the catalog) actually support. Do NOT "
        "extrapolate a claim, rating, or number beyond them; if a specific figure is "
        "tempting, write [VERIFY: confirm] instead of inventing it.",
        "  (4) Weave the detail into the relevant product cards and sections — do NOT add "
        "a separate 'Reviews' block or quote anyone.",
        "--- FIELD NOTES START ---",
        notes,
        "--- FIELD NOTES END ---",
    ]


# The prompt is composed from these, in order. Add/remove/reorder a concern here.
_PROMPT_SECTIONS = [_sec_header, _sec_length, _sec_age, _sec_format,
                    _sec_house, _sec_coverage, _sec_field_notes,
                    _sec_internal, _sec_citations]


def _build_system(brief: dict) -> str:
    """The fixed contract: output shape + anti-fabrication rules. Concern-specific
    wording lives in the _sec_* sections / _DRAFTING_RULES, not here."""
    blocks_desc = "\n".join(
        f"  - {b['key']}: {b['label']}"
        + (f" (≤{b['max_chars']} chars)" if b.get("max_chars") else "")
        for b in brief["output_blocks"])
    rules = "\n".join(f"{i+1}. {r}" for i, r in enumerate(brief["drafting_rules"]))
    return (
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
        "Output blocks to produce:\n" + blocks_desc)


def build_generation_messages(brief: dict, competitor: dict = None) -> tuple:
    """Turn a brief into (system_message, user_prompt). MODULAR: the user prompt is
    composed from the independent _sec_* concern functions (edit one concern in one
    place, nothing else moves); the system message is the fixed contract. `competitor`
    is the optional SERP coverage spec from competitor_brief."""
    ctx = _prompt_ctx(brief, competitor)
    lines = []
    for section in _PROMPT_SECTIONS:
        lines += section(brief, competitor, ctx)
    return _build_system(brief), "\n".join(lines)


def build_expand_messages(brief: dict, competitor: dict, current_body: str,
                          current_words: int, floor: int) -> tuple:
    """Prompt for an APPEND-ONLY expansion pass: add brand-new <h2> sections to lift a
    short draft to the length floor WITHOUT touching any existing content, link,
    product card, or the FAQ. Returns (system, user_prompt); the caller inserts the
    returned new_sections_html before the FAQ and re-runs the link/verbatim guards.

    This is the SAFE replacement for the old auto-expand — that one rewrote the whole
    body and clobbered citations/FAQ; this one may only ADD sections."""
    t = brief["topic"]
    age = _age_context(t.get("title"), t.get("primary_keyword"))
    deficit = max(0, floor - int(current_words or 0))
    target_add = deficit + 150   # headroom so it actually clears the floor
    prods = brief.get("grounding_products") or brief.get("link_products") or []
    prod_lines = [f"   • {p.get('title','')} — {p.get('url','')}"
                  for p in prods if p.get("url")]
    hub = brief.get("link_up") or {}
    notes = (brief.get("field_notes") or "").strip()

    system = (
        "You expand an existing, already-good article by ADDING new sections. You are "
        "FORBIDDEN from modifying, rewriting, reordering, shortening, or deleting ANY "
        "existing text, heading, link, product card, or the FAQ. You only produce NEW "
        "<h2> sections. You never invent product specs, prices, statistics, or citations. "
        "Return ONLY a JSON object: {\"new_sections_html\": \"<the new sections as HTML>\"}."
    )

    lines = [f"TOPIC: {t.get('title')}  (target keyword: {t.get('primary_keyword')})"]
    if age:
        lines.append(f"AUDIENCE AGE: {age} — every new section MUST stay age-appropriate for this age.")
    lines += [
        f"The current article body is {current_words} words; the minimum is {floor}. Write "
        f"approximately {target_add} words of BRAND-NEW material as new <h2> sections.",
        "STRICT RULES:",
        "  • Do NOT reproduce, rewrite, or restate any existing section — cover NEW "
        "angles the article doesn't yet have (e.g. how to introduce/rotate toys, "
        "play ideas by skill, setting up a play space, common mistakes, a day-in-the-life).",
        "  • Each new <h2> section: 150–250 words of genuine, specific, useful "
        "content — no filler, no repetition of what's already written.",
        "  • Use the house component classes where they fit (benefits-box, tip-box, "
        "warning-box, game-card, product-integration) so it matches the article.",
        "  • Do NOT add any EXTERNAL links or citations (the article already has them).",
        "  • Any internal link must use ONLY a product/hub URL listed below, copied "
        "EXACTLY; never invent a URL.",
        "  • Do NOT add an FAQ — the article already ends with one.",
        "  • Never invent a product spec, price, rating or statistic; mark any unknown "
        "with [VERIFY: what to check].",
    ]
    if notes:
        lines += [
            "REAL FIELD NOTES / REVIEWS (private grounding — PARAPHRASE, never copy "
            "verbatim; strip competitor/reviewer names). Use for concrete first-hand detail:",
            "--- FIELD NOTES START ---", notes, "--- FIELD NOTES END ---",
        ]
    if prod_lines:
        lines.append("Real product URLs you MAY link to (exact URLs only):")
        lines += prod_lines
    if hub.get("url"):
        lines.append(f"Hub page (optional single up-link): {hub.get('url')}")
    lines += [
        "EXISTING ARTICLE BODY — READ-ONLY. Your output is inserted BEFORE its FAQ, so "
        "do not repeat anything here and do not echo it back:",
        "--- EXISTING BODY START ---", current_body or "", "--- EXISTING BODY END ---",
    ]
    return system, "\n".join(lines)

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
