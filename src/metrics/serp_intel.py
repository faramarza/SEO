"""Live SERP intelligence via Serper (your existing key).

For a query, this answers the two things a position number can't: WHO actually
outranks you (Amazon/marketplace vs a beatable content site), and WHICH SERP
features are siphoning your click (a Shopping pack, featured snippet, or — if
Serper catches it — an AI Overview). That turns a winnable page's low CTR from a
mystery into a diagnosis, and its position into a "beatable vs don't-bother" call.

Honest boundary: Serper does NOT reliably return AI-Overview citations (its AIO
parsing is partial). We flag an AI Overview only when Serper actually surfaces one;
comprehensive AIO-citation tracking is the DataForSEO phase-2 layer.
"""

from urllib.parse import urlparse

# Marketplaces / big-box: if these own the top, organic displacement is hard.
MARKETPLACES = ("amazon.", "etsy.", "ebay.", "walmart.", "target.", "aliexpress.",
                "temu.", "wayfair.", "kohls.", "michaels.", "macys.", "costco.")

# SERP features that pull clicks away from organic (present ABOVE/around it).
SIPHON = {
    "ai_overview": "an AI Overview answers it on-SERP (you're likely not cited)",
    "featured_snippet": "a featured snippet answers it on-SERP",
    "shopping": "a Shopping pack sits above organic",
    "knowledge_graph": "a knowledge panel takes attention",
    "top_stories": "a Top Stories carousel sits above organic",
    "people_also_ask": "a People-Also-Ask box splits attention",
}


def _domain(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _kind(dom: str, own: str) -> str:
    if own and own in dom:
        return "own"
    if any(m in dom for m in MARKETPLACES):
        return "marketplace"
    return "content"  # blog / brand / content site — the beatable kind


def analyze_query(query: str, own_domain: str, fetch_fn, api_key: str = ""):
    """Fetch the live SERP for `query` and summarize it. Returns None if the fetch
    failed / no key / quota out (caller should degrade gracefully)."""
    serp = fetch_fn(query, api_key)
    if not serp:
        return None
    own = (own_domain or "").lower().replace("www.", "")
    org = serp.get("organic_results") or []
    top5, own_pos = [], None
    for r in org[:10]:
        dom = _domain(r.get("url", ""))
        k = _kind(dom, own)
        if k == "own" and own_pos is None:
            own_pos = r.get("position")
        if len(top5) < 5:
            top5.append({"position": r.get("position"), "domain": dom, "kind": k})

    feats = serp.get("serp_features") or []
    siphons = [SIPHON[f] for f in feats if f in SIPHON]

    top3 = top5[:3]
    mkt = sum(1 for t in top3 if t["kind"] == "marketplace")
    content = sum(1 for t in top3 if t["kind"] == "content")
    if mkt >= 2:
        verdict, vnote = "hard", ("marketplaces (Amazon/Etsy/etc.) own the top 3 — very "
                                  "hard to displace organically; don't over-invest here")
    elif content >= 2:
        verdict, vnote = "beatable", ("the top is content/blog sites (no marketplace lock) "
                                      "— out-rankable with better content + internal links")
    else:
        verdict, vnote = "mixed", "a mix of marketplaces and content sites above you"

    return {
        "top5": top5,
        "top_domains": [t["domain"] for t in top5 if t["domain"]],
        "own_position_serp": own_pos,
        "siphons": siphons,
        "features": feats,
        "verdict": verdict,
        "verdict_note": vnote,
    }
