"""Content-gap battle plan — turn "what topics they rank for and you don't" into a
concrete, prioritized set of articles to write.

Pure functions (no network). The DataForSEO Labs pull happens in the web layer and
feeds `compute_gap`; everything here is deterministic and unit-tested. Never
invents a keyword — it only clusters and prioritizes real ranked-keyword data.
"""
from __future__ import annotations
import re

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
    """Tokens that actually characterize a topic (drop stopwords + generic terms)."""
    return {t for t in _tokens(kw) if t not in _GENERIC and len(t) > 2}


def _blocked(kw: str) -> bool:
    return bool({t for t in _tokens(kw)} & _BLOCK)


def compute_gap(competitor_keywords: list, your_keywords: list,
                min_volume: int = 30, relevance: bool = True) -> list:
    """Keywords the competitor(s) rank for that YOU don't (or barely).

    competitor_keywords / your_keywords: [{keyword, volume, position, cpc, ...}].

    RELEVANCE FILTER (crucial): a competitor's keyword set is full of brands
    (jellycat), and unrelated products (car seats, pacifiers) you don't sell.
    Anchored to the distinctive vocabulary of YOUR OWN ranked keywords, we keep
    only gap topics that share a real term with what you actually rank for — so
    the plan is "expand your wheelhouse," not "write about everything they sell."
    Returns the gap keywords (dedup, real demand), highest-volume first."""
    yours = {_norm(k.get("keyword")) for k in (your_keywords or [])}
    vocab = set()
    for k in (your_keywords or []):
        vocab |= _distinctive(k.get("keyword"))
    use_relevance = relevance and len(vocab) >= 5   # need a real footprint to anchor

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
        if use_relevance and not (_distinctive(kw) & vocab):
            continue                                  # not in your wheelhouse — skip
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
        supporting = [k["keyword"] for k in kws[1:8]]
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
        "articles": plan,
        "article_count": len(plan),
        "total_volume": total_vol,
        "total_est_value": round(sum(a["est_monthly_value"] for a in plan), 2),
    }
