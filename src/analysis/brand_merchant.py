"""Brand SERP + Merchant/feed-readiness audit.

Two realistic ecommerce presence checks for a respectable brand — no Amazon-scale
ambition, just making sure the brand looks like a real brand where it counts:

1. Brand SERP: do you actually own your own name in search? From GSC, find the
   branded queries (they contain your brand terms) and flag any where you DON'T
   sit at the top or where click-through is oddly low — a sign someone/something
   is intercepting demand that's rightfully yours.

2. Merchant / free product listings readiness: products can appear in Google's
   free product listings / Shopping only if the page carries the structured data
   Merchant Center expects (Product + Offer with price/availability). This flags
   products that aren't feed-ready, ranked by the traffic they already pull.

Everything is grounded in the site's own GSC + crawl data. No fabricated numbers.
"""

from src.analysis.growth_playbook import _is_system_page


def _is_brand_query(query: str, brand_terms: list) -> bool:
    q = (query or "").lower()
    for t in brand_terms:
        t = (t or "").lower().strip()
        if not t:
            continue
        # match whole term, and a spaceless variant (alphabettrains)
        if t in q or t.replace(" ", "") in q.replace(" ", ""):
            return True
    return False


# Modifiers that signal DEAL-HUNTER intent, not navigation to your store. These
# people want a coupon aggregator (Honey, RetailMeNot); they rarely click a
# store's own page, so a low CTR here is expected and NOT a brand-ownership fix.
_DEAL_MODIFIERS = ("discount", "coupon", "coupons", "cashback", "promo",
                   "promo code", "code", "deal", "deals", "voucher", "sale",
                   "offer", "offers", "free shipping")


def _is_homepage(url):
    try:
        return (urlparse(url.lower()).path.rstrip("/") or "/") == "/"
    except Exception:
        return False


def _deal_intent(query):
    q = (query or "").lower()
    return any(m in q for m in _DEAL_MODIFIERS)


def brand_serp_audit(results, brand_terms, min_impressions=20):
    """Branded queries where you don't clearly own the result. Aggregates brand
    demand and flags weak spots — separating true navigational brand queries (act
    on these) from deal-hunter modifier queries (low value, don't chase)."""
    total_brand_impr = 0
    total_brand_clicks = 0
    # What the homepage ALREADY emits — so we never tell them to add brand schema
    # they already have (Magento/most themes emit Organization + WebSite by
    # default). Grounds the fix, same 'check first' rule as Rich Results.
    hp_schema = set()
    for r in results:
        if _is_homepage(r.get("url", "")):
            pm = r.get("page_metadata", {}) or {}
            hp_schema = {str(s).lower() for s in (pm.get("schema_types") or [])}
            break
    hp_has_org = "organization" in hp_schema
    hp_has_website = "website" in hp_schema
    seen = {}  # query -> aggregated across pages
    for r in results:
        url = r.get("url", "")
        for q in (r.get("top_queries") or []):
            query = q.get("query", "")
            if not _is_brand_query(query, brand_terms):
                continue
            impr = q.get("impressions", 0) or 0
            clicks = q.get("clicks", 0) or 0
            pos = q.get("position", 0) or 0
            total_brand_impr += impr
            total_brand_clicks += clicks
            e = seen.get(query)
            # Keep the best-ranking page for this brand query.
            if e is None or pos < e["position"]:
                seen[query] = {"query": query, "position": round(pos, 1),
                               "impressions": impr, "clicks": clicks,
                               "ctr": round((q.get("ctr", 0) or 0), 2),
                               "ranking_url": url}
            else:
                e["impressions"] = max(e["impressions"], impr)

    flags = []
    homepage_missing = 0
    for q in seen.values():
        if q["impressions"] < min_impressions:
            continue
        deal = _deal_intent(q["query"])
        q["deal_intent"] = deal
        issues = []
        if q["position"] > 2.0:
            issues.append(f"You rank #{q['position']} for your own brand query — "
                          f"something is outranking you (a reseller, marketplace, or a "
                          f"different '{brand_terms[0]}' entity — the name is generic). "
                          f"Check the live SERP; you should own position 1.")
        elif q["position"] > 1.3:
            issues.append(f"Not solidly #1 (avg #{q['position']}) — strengthen the page "
                          f"that should own this brand query.")
        # The homepage should own brand queries — flag when another page ranks.
        if not _is_homepage(q["ranking_url"]) and not deal:
            homepage_missing += 1
            issues.append("Your HOMEPAGE should own this brand query, but another page "
                          "ranks for it — strengthen the homepage's brand signals "
                          "(a title that leads with your brand + brand-anchor internal "
                          "links) and check the live SERP to see who's outranking you.")
        # Low CTR: a real signal for navigational brand queries, but EXPECTED (and
        # not actionable) for deal-hunter queries, so we don't flag it there.
        if not deal and q["position"] <= 2.0 and q["ctr"] < 30 and q["impressions"] >= 50:
            issues.append(f"Low CTR ({q['ctr']}%) for a brand query — your listing "
                          f"(title/sitelinks/rich result) isn't compelling, or ads/"
                          f"others are siphoning the click.")
        if deal and not issues:
            # Surface it, but as low-value context, not a weak spot.
            issues.append("Deal-hunter query (coupon/discount intent) — low value; "
                          "these searchers rarely click a store. Not worth chasing.")
        if issues:
            flags.append({**q, "issues": issues})

    # Real weak spots exclude deal-only rows.
    real_weak = [f for f in flags if not (f.get("deal_intent") and
                 all("Deal-hunter" in i for i in f["issues"]))]
    flags.sort(key=lambda x: (x.get("deal_intent", False), -x["impressions"]))
    brand_ctr = round(100 * total_brand_clicks / total_brand_impr, 1) if total_brand_impr else 0.0

    recommendation = None
    if homepage_missing >= 1:
        if hp_has_org and hp_has_website:
            schema_note = ("Your homepage already emits Organization + WebSite schema "
                           "(the sitelinks-search-box markup) — that box is checked, "
                           "don't re-add it. ")
        else:
            schema_note = ("First VERIFY your homepage's Organization + WebSite schema "
                           "in Google's Rich Results Test — Magento/your theme likely "
                           "already emits it (this scan can miss rendered schema), so "
                           "don't add a duplicate. ")
        recommendation = (
            "Make your HOMEPAGE own your brand. " + schema_note +
            "The levers that actually move this: (1) a homepage <title> that leads "
            "with your exact brand name; (2) brand-anchor internal links pointing to "
            "the homepage from your strongest pages; (3) Google your brand in an "
            "incognito window and see who's really outranking you — a reseller, a "
            "marketplace listing of your own products, or a different entity — and act "
            "on THAT. Note: your brand name doubles as a generic product term, so part "
            "of this SERP is real competition, not brand theft — don't over-invest.")
    return {
        "brand_queries": len(seen),
        "brand_impressions": int(total_brand_impr),
        "brand_clicks": int(total_brand_clicks),
        "brand_ctr": brand_ctr,
        "weak_spots": len(real_weak),
        "recommendation": recommendation,
        "flags": flags[:50],
    }


def merchant_readiness(results, limit=100, system_disallow=None):
    """Products missing the structured data required for Google's free product
    listings / Merchant Center, ranked by the traffic they already get."""
    not_ready = []
    ready = 0
    products = 0
    for r in results:
        url = r.get("url", "")
        if _is_system_page(url, extra_disallow=system_disallow):
            continue
        if (r.get("asset_type") or "").lower() != "product":
            continue
        pm = r.get("page_metadata", {}) or {}
        if not pm.get("has_crawl_data"):
            continue
        schema = {str(s).lower() for s in (pm.get("schema_types") or [])}
        # Ground truth: an ItemList-only page is a category listing, not a feed
        # product — don't tell it to become feed-ready as a product.
        if "itemlist" in schema and "product" not in schema:
            continue
        products += 1
        missing = []
        if "product" not in schema:
            missing.append("Product schema")
        if "offer" not in schema and "aggregateoffer" not in schema:
            missing.append("Offer (price/availability)")
        if not missing:
            ready += 1
            continue
        not_ready.append({
            "url": url,
            "impressions": r.get("gsc_impressions", 0) or 0,
            "revenue": round(r.get("ga4_revenue", 0) or 0, 2),
            "missing": missing,
        })
    not_ready.sort(key=lambda x: -x["impressions"])
    return {
        "products": products,
        "feed_ready": ready,
        "not_ready_count": len(not_ready),
        "ready_pct": round(100 * ready / products, 1) if products else 0.0,
        "rows": not_ready[:limit],
    }


def brand_merchant_audit(results, brand_terms, system_disallow=None):
    return {
        "brand": brand_serp_audit(results, brand_terms),
        "merchant": merchant_readiness(results, system_disallow=system_disallow),
    }
