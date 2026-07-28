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


def brand_serp_audit(results, brand_terms, min_impressions=20):
    """Branded queries where you don't clearly own the result. Aggregates brand
    demand and flags weak spots (not #1, or low CTR for a brand query)."""
    total_brand_impr = 0
    total_brand_clicks = 0
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
    for q in seen.values():
        if q["impressions"] < min_impressions:
            continue
        issues = []
        if q["position"] > 2.0:
            issues.append(f"You rank #{q['position']} for your own brand query — "
                          f"something is outranking you (reseller, marketplace, or "
                          f"an unwanted page). You should own position 1.")
        elif q["position"] > 1.3:
            issues.append(f"Not solidly #1 (avg #{q['position']}) — tighten the "
                          f"page that should own this brand query.")
        if q["position"] <= 2.0 and q["ctr"] < 30 and q["impressions"] >= 50:
            issues.append(f"Low CTR ({q['ctr']}%) for a brand query — your listing "
                          f"(title/sitelinks/rich result) isn't compelling, or ads/"
                          f"others are siphoning the click.")
        if issues:
            flags.append({**q, "issues": issues})

    flags.sort(key=lambda x: -x["impressions"])
    brand_ctr = round(100 * total_brand_clicks / total_brand_impr, 1) if total_brand_impr else 0.0
    return {
        "brand_queries": len(seen),
        "brand_impressions": int(total_brand_impr),
        "brand_clicks": int(total_brand_clicks),
        "brand_ctr": brand_ctr,
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
        products += 1
        schema = {str(s).lower() for s in (pm.get("schema_types") or [])}
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
