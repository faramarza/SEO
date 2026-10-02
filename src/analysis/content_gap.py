"""Content-gap battle plan — turn "what topics they rank for and you don't" into a
concrete, prioritized set of articles to write.

Pure functions (no network). The DataForSEO Labs pull happens in the web layer and
feeds `compute_gap`; everything here is deterministic and unit-tested. Never
invents a keyword — it only clusters and prioritizes real ranked-keyword data.
"""
from __future__ import annotations
import re

# Bump when the gap logic changes in a way that invalidates cached plans (e.g. the
# relevance filter / brand blocklist / focus theme). A cached plan without the
# current version is ignored so old, pre-fix garbage never shows again.
PLAN_VERSION = 12

_STOP = {
    "the", "a", "an", "for", "and", "or", "to", "of", "in", "on", "with", "best",
    "top", "your", "you", "my", "is", "are", "how", "what", "why", "vs", "&",
    "from", "at", "by", "kids", "kid", "child", "children", "toddler", "toddlers",
    "baby", "babies", "year", "years", "old", "olds", "month", "months",
}
# Commercial-intent markers → these terms convert; they rank higher in the plan.
_COMMERCIAL = ("buy", "shop", "price", "cheap", "sale", "gift", "gifts", "for sale",
               "personalized", "custom", "deal", "best", "review", "reviews",
               "near me", "store", "discount")
# Clearly informational — a store rarely wins these and they don't sell directly.
_INFORMATIONAL = ("what is", "what are", "how to", "how do", "why ", "guide",
                  "ideas", "method", " vs ", "versus", "benefits", "meaning",
                  "definition", "examples", "printable", "diy")


def _tokens(kw: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", (kw or "").lower()) if t and t not in _STOP]


def _norm(kw: str) -> str:
    return re.sub(r"\s+", " ", (kw or "").strip().lower())


# Generic e-commerce / child-product tokens that don't, on their own, establish
# that a keyword is in YOUR wheelhouse ("toys" is true of half the internet).
_GENERIC = {"toys", "toy", "gift", "gifts", "set", "sets", "cheap", "sale", "buy",
            "shop", "online", "store", "new", "play", "gear", "stuff", "things",
            "products", "product", "item", "items", "idea", "ideas"}


# Hard brand-safety blocklist: any keyword containing one of these is dropped
# outright, regardless of relevance — a kids' store never writes about these.
_BLOCK = {"sex", "sexual", "porn", "porno", "xxx", "nsfw", "nude", "nudes", "adult",
          "escort", "erotic", "fetish", "drug", "drugs", "weed", "cannabis", "marijuana",
          "vape", "cbd", "gun", "guns", "ammo", "firearm", "weapon", "knife", "gambling",
          "casino", "betting", "crypto", "loan", "loans", "viagra", "cialis", "kill",
          "suicide", "abortion"}


def _distinctive(kw: str) -> set:
    """Tokens that actually characterize a topic (drop stopwords, generic terms,
    and anything that starts with a digit — '10ft', '24w', '2025', '135' etc. are
    rug dimensions / sizes scraped from product URLs, not topic words. Left in,
    they bloat the catalogue-relevance vocabulary so badly that almost any keyword
    finds a coincidental match and the relevance filter rubber-stamps junk)."""
    return {t for t in _tokens(kw)
            if t not in _GENERIC and len(t) > 2 and not t[0].isdigit()}


# Brand / manufacturer names a store RESELLS but should never write an "authority"
# article about (writing "jelly cats" for a shop that stocks Jellycat is pointless
# and off-brand). A content topic dominated by one of these is dropped.
_BRANDS = {
    "jellycat", "jelly", "lovevery", "lego", "duplo", "playmobil", "hasbro",
    "mattel", "fisher-price", "fisherprice", "melissa", "doug", "hydro", "flask",
    "stanley", "sophie", "bruder", "schleich", "squishmallow", "squishmallows",
    "funko", "nerf", "barbie", "hotwheels", "paw", "patrol", "bluey", "peppa",
    "disney", "marvel", "pokemon", "minecraft", "roblox", "bartholomew", "yoto",
    "tonies", "grimms", "grimm", "hape", "janod", "plantoys", "haba", "maileg",
}


def _blocked(kw: str) -> bool:
    toks = {t for t in _tokens(kw)}
    return bool(toks & _BLOCK) or bool(toks & _BRANDS)


def _too_generic(kw: str) -> bool:
    """A short, broad HEAD TERM, not an article topic. A keyword carrying only ONE
    real CONCEPT in a 1–3-word phrase ("doll", "baby doll", "dolls doll", "play
    kitchen", "montessori toys") is a category/head keyword a small site won't win
    with a single article, and it's not a real informational topic — it keeps
    surfacing as junk. A genuine long-tail topic carries more than one concept, or
    real qualifying length ("montessori toys for 2 year olds", "wooden name
    puzzle"). Keep those; drop the bare heads.

    Concepts are counted on the SINGULARIZED distinctive tokens (via _sig) so that
    plural/singular repeats of the same word — "dolls doll", "doll dolls" — count
    as one concept, not two. That repeated-form trick is exactly how head terms
    kept sneaking past a raw token count."""
    if not _distinctive(kw):
        return True  # only generic/short tokens, e.g. "toys 3"
    concepts = _sig(kw)          # singularized distinctive tokens → unique concepts
    n_words = len([w for w in re.split(r"[^a-z0-9]+", (kw or "").lower()) if w])
    return len(concepts) <= 1 and n_words <= 3


def _sig(kw: str) -> frozenset:
    """A dedup signature: distinctive tokens, singularized, so 'dolls doll' and
    'dolls for dolls' collapse to the same thing instead of both becoming H2s."""
    return frozenset(t[:-1] if t.endswith("s") and len(t) > 3 else t
                     for t in _distinctive(kw))


def compute_gap(competitor_keywords: list, your_keywords: list,
                min_volume: int = 30, relevance: bool = True, focus: list = None,
                relevance_vocab: set = None) -> list:
    """Keywords the competitor(s) rank for that YOU don't (or barely).

    competitor_keywords / your_keywords: [{keyword, volume, position, cpc, ...}].

    RELEVANCE FILTER (crucial): a competitor's keyword set is full of brands
    (jellycat) and unrelated products (car seats, baby dolls) you don't sell. To
    keep only topics you could actually sell into, relevance is anchored to
    `relevance_vocab` — the vocabulary of what you ACTUALLY HAVE PAGES FOR (your
    sitemap/catalog), passed in by the caller. That's the ground truth of your
    catalog, without the noise of stray GSC impressions for things you don't
    carry. Falls back to your ranked-keyword vocabulary only when no catalog vocab
    is supplied. Returns the gap keywords (dedup, real demand), highest-volume first."""
    yours = {_norm(k.get("keyword")) for k in (your_keywords or [])}
    if relevance_vocab:
        vocab = {t for t in relevance_vocab if t not in _GENERIC and len(t) > 2}
    else:
        vocab = set()
        for k in (your_keywords or []):
            vocab |= _distinctive(k.get("keyword"))
    use_relevance = relevance and len(vocab) >= 5   # need a real footprint to anchor
    # FOCUS: when set (e.g. ["montessori"]), keep only gap keywords that contain a
    # focus term. This is the strong lever for a broad store — it scopes the plan
    # to the topic you actually want to win instead of every category a broad
    # competitor happens to sell.
    focus = [f.lower().strip() for f in (focus or []) if f and f.strip()]

    best = {}
    for k in competitor_keywords or []:
        kw = _norm(k.get("keyword"))
        if not kw or kw in yours:
            continue
        vol = int(k.get("volume") or 0)
        if vol < min_volume:
            continue
        if _blocked(kw):
            continue                                  # brand-safety: never suggest these
        if focus and not any(f in kw for f in focus):
            continue                                  # outside your focus theme — skip
        dk = _distinctive(kw)
        # Drop junk fragments and short broad head terms ("baby doll", "doll",
        # "montessori toys") — not article topics a small site can win. See
        # _too_generic for the rule.
        if _too_generic(kw):
            continue
        if use_relevance:
            # Require REAL overlap with your pages/GSC vocabulary, not one stray
            # shared word. At least half of the keyword's distinctive tokens must
            # be things you actually rank for / have pages about. This kills
            # "jelly cats" (0 of {jelly,cats}) and "car seat for convertible cars"
            # (1 of 4 = 0.25) while keeping "montessori bookshelf".
            if (len(dk & vocab) / len(dk)) < 0.5:
                continue
        cur = best.get(kw)
        if not cur or vol > cur["volume"]:
            best[kw] = {"keyword": k.get("keyword"), "volume": vol,
                        "cpc": k.get("cpc") or 0,
                        "competitor_position": k.get("position") or 0}
    return sorted(best.values(), key=lambda x: -x["volume"])


def _intent(kw: str) -> str:
    low = " " + (kw or "").lower() + " "
    if any(m in low for m in _INFORMATIONAL):
        return "informational"
    # A shopping query on a store's turf (no explicit info marker) defaults to
    # commercial — that's the ground where a shop wins and sells.
    return "commercial"


def cluster_topics(gap_keywords: list, min_shared: int = 1) -> list:
    """Group gap keywords into article-sized topic clusters by shared significant
    tokens (greedy, highest-volume seeds first). One cluster = one article.

    The niche's own name ("montessori" for this store) appears in nearly every
    keyword, so it doesn't distinguish topics — any token present in >40% of the
    gap keywords is dropped from the clustering signature so topics actually
    separate instead of collapsing into one blob."""
    gap_keywords = list(gap_keywords or [])
    n = len(gap_keywords)
    df = {}
    for k in gap_keywords:
        for t in set(_tokens(k["keyword"])):
            df[t] = df.get(t, 0) + 1
    # Only the truly ubiquitous niche term (e.g. "montessori", ~0.9 DF on real
    # data) is dropped; genuine category tokens like "toys" (~0.3 DF) are kept.
    common = {t for t, c in df.items() if n >= 8 and c / n > 0.6}

    clusters = []
    for k in sorted(gap_keywords, key=lambda x: -x["volume"]):
        toks = set(_tokens(k["keyword"]))
        sig = toks - common
        if sig:                       # cluster on distinguishing tokens only
            toks = sig
        if not toks:
            continue
        placed = None
        best_overlap = 0
        for c in clusters:
            ov = len(toks & c["_tokens"])
            if ov >= min_shared and ov > best_overlap:
                best_overlap = ov
                placed = c
        if placed:
            placed["keywords"].append(k)
            placed["_tokens"] |= toks
            placed["volume"] += k["volume"]
        else:
            clusters.append({"keywords": [k], "_tokens": set(toks),
                             "volume": k["volume"]})
    for c in clusters:
        c.pop("_tokens", None)
        c["keywords"].sort(key=lambda x: -x["volume"])
    return sorted(clusters, key=lambda c: -c["volume"])


def sanity_messages(topics: list, store_name: str = "the store",
                    families: list = None, catalog_vocab: list = None) -> tuple:
    """Build (system, user) for an AI SANITY pass over proposed topics. The model
    is a strict vetting judge — it never writes content, it only decides keep/drop.
    This is the backstop for what deterministic rules can't anticipate: phrases
    that aren't real ("dolls doll"), or terms with no relevance to THIS store's
    catalogue. Grounded in the store's real families + page vocabulary."""
    families = families or []
    catalog_vocab = catalog_vocab or []
    system = (
        "You are a strict editorial vetting assistant for an e-commerce store's blog "
        "content plan. You do NOT write content — you only judge whether each proposed "
        "blog TOPIC is worth keeping. Drop a topic if ANY of these is true:\n"
        "  • it is not a real, grammatical phrase a person would actually search "
        "(e.g. 'dolls doll', 'toys 3', word salad, duplicated words);\n"
        "  • it is a broad one-word / head term no small store can win with one article;\n"
        "  • it is irrelevant to what THIS store actually sells;\n"
        "  • it is a brand / manufacturer name rather than a topic.\n"
        "Keep a topic ONLY if it is a sensible, specific, on-brand article topic this "
        "store could write something genuinely useful about and sell into. When a topic "
        "is clearly nonsensical or irrelevant, DROP it (keep=false).\n"
        'Return ONLY JSON (no prose, no fences): {"verdicts":[{"term":"<exact topic '
        'text>","keep":true|false,"reason":"<short>"}]} — exactly one entry per topic.'
    )
    ctx = [f"Store: {store_name}."]
    if families:
        ctx.append("Product families it sells: " + ", ".join(families) + ".")
    if catalog_vocab:
        ctx.append("Words that appear in its real page URLs (its catalogue "
                   "vocabulary): " + ", ".join(sorted(set(catalog_vocab))[:60]) + ".")
    ctx.append("\nVet these proposed blog topics:")
    for i, t in enumerate(topics, 1):
        ctx.append(f"{i}. {t}")
    return system, "\n".join(ctx)


def apply_sanity(clusters: list, verdicts: list) -> tuple:
    """Filter topic clusters by the AI verdicts. A cluster is judged by its primary
    keyword. Conservative: only an EXPLICIT keep=false drops a cluster — a topic the
    model didn't return is kept, so a partial/garbled response can never silently
    gut the plan. Returns (kept_clusters, dropped[{term, reason}])."""
    vmap = {}
    for v in (verdicts or []):
        term = _norm((v or {}).get("term", ""))
        if term:
            vmap[term] = v
    kept, dropped = [], []
    for c in clusters:
        kws = c.get("keywords") or []
        primary = kws[0]["keyword"] if kws else ""
        v = vmap.get(_norm(primary))
        if v is not None and v.get("keep") is False:
            dropped.append({"term": primary, "reason": (v.get("reason") or "").strip()})
        else:
            kept.append(c)
    return kept, dropped


def _title_for(primary: str, intent: str) -> str:
    p = primary.strip()
    if intent == "commercial":
        return p[:1].upper() + p[1:]           # a collection/product page title
    return f"{p[:1].upper() + p[1:]}: A Complete Guide"


def build_plan(clusters: list, aov: float = 53.0, cvr: float = 0.02,
               margin: float = 0.5, comp_word_count: int = 1800,
               max_articles: int = 40) -> dict:
    """Turn topic clusters into a ranked write-list: per article a primary keyword,
    supporting keywords (→ H2 outline), a word-count target, intent, a hub/spoke
    role for interlinking, and a priority (sales-weighted). Deterministic."""
    plan = []
    for c in clusters[:max_articles]:
        kws = c["keywords"]
        primary = kws[0]["keyword"]
        # Dedupe supporting keywords by meaning-signature so near-identical junk
        # ("dolls doll", "dolls for dolls") collapses to one clean H2, and drop
        # anything that just restates the primary.
        seen_sigs = {_sig(primary)}
        supporting = []
        for k in kws[1:]:
            s = _sig(k["keyword"])
            if not s or s in seen_sigs:
                continue
            seen_sigs.add(s)
            supporting.append(k["keyword"])
            if len(supporting) >= 7:
                break
        intent = _intent(primary)
        # Sales-weighted priority: commercial terms convert; volume matters; a term
        # the competitor barely holds (weak position) is easier to take. Volume is
        # CAPPED so one giant head term can't produce a fantasy $/mo or dominate —
        # a single new article realistically captures a small slice of page 1.
        intent_w = 1.0 if intent == "commercial" else 0.35
        capped_vol = min(c["volume"], 3000)
        est_clicks = capped_vol * 0.03             # a modest page-1 capture
        est_value = round(min(est_clicks * cvr * aov * margin, 400.0), 2)
        priority = round(min(c["volume"], 5000) * intent_w, 1)
        wc = comp_word_count if intent == "informational" else max(600, comp_word_count // 2)
        outline = ["Intro: directly answer / frame " + f"“{primary}”"]
        outline += [f"H2: {s}" for s in supporting]
        outline += ["FAQ (3–5 questions with FAQ schema)", "Clear link(s) to the matching products"]
        plan.append({
            "primary_keyword": primary,
            "supporting_keywords": supporting,
            "keywords_covered": len(kws),
            "total_volume": c["volume"],
            "intent": intent,
            "title": _title_for(primary, intent),
            "word_count_target": wc,
            "outline": outline,
            "est_monthly_value": est_value,
            "priority": priority,
        })
    plan.sort(key=lambda a: -a["priority"])
    # Hub-and-spoke: the highest-priority broad topic is the pillar; the rest link
    # up to it and it links down to them.
    if plan:
        plan[0]["role"] = "pillar (hub)"
        pillar_kw = plan[0]["primary_keyword"]
        for a in plan[1:]:
            a["role"] = "supporting"
            a["links_to_pillar"] = pillar_kw
        plan[0]["links_to_spokes"] = [a["primary_keyword"] for a in plan[1:]]
    total_vol = sum(a["total_volume"] for a in plan)
    return {
        "version": PLAN_VERSION,
        "articles": plan,
        "article_count": len(plan),
        "total_volume": total_vol,
        "total_est_value": round(sum(a["est_monthly_value"] for a in plan), 2),
    }
