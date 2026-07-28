"""Reviews & Ratings priority engine.

Reviews are the highest-leverage structural investment for a trusted brand: star
ratings lift SERP click-through, on-page reviews lift conversion, and the
AggregateRating schema is exactly the gap the Rich Results engine flags. But you
can't collect reviews for 1000 products at once — so this ranks the products that
have NO review schema by the real demand and revenue already flowing through
them, telling the operator which 20 to run a review-collection campaign on first.

No invented uplift figures — the priority is driven by the page's OWN measured
impressions, clicks, and revenue. The point is sequencing a finite effort toward
the pages where reviews will touch the most traffic and money.
"""

from src.analysis.growth_playbook import _is_system_page


def _has_reviews(pm) -> bool:
    schema = {str(s).lower() for s in (pm.get("schema_types") or [])}
    return "aggregaterating" in schema or "review" in schema


def find_review_priorities(results, limit=100, system_disallow=None):
    """Product (and category) pages with NO review/rating schema, ranked by the
    real traffic + revenue at stake. Also reports how many money pages already
    have reviews, so coverage is visible."""
    missing = []
    have = 0
    money_pages = 0
    impr_at_stake = 0
    rev_at_stake = 0.0

    # First pass to scale the priority blend by the site's own maxima, so the
    # score reflects THIS site's distribution rather than arbitrary weights.
    candidates = []
    for r in results:
        url = r.get("url", "")
        if _is_system_page(url, extra_disallow=system_disallow):
            continue
        asset_type = (r.get("asset_type") or "other").lower()
        if asset_type not in ("product", "category"):
            continue
        pm = r.get("page_metadata", {}) or {}
        if not pm.get("has_crawl_data"):
            continue  # can't claim schema is missing on a page we didn't parse
        money_pages += 1
        if _has_reviews(pm):
            have += 1
            continue
        candidates.append((r, pm, asset_type))

    max_impr = max((r.get("gsc_impressions", 0) or 0 for r, _, _ in candidates), default=0) or 1
    max_rev = max((r.get("ga4_revenue", 0) or 0 for r, _, _ in candidates), default=0) or 1

    for r, pm, asset_type in candidates:
        impr = r.get("gsc_impressions", 0) or 0
        rev = r.get("ga4_revenue", 0) or 0
        clicks = r.get("gsc_clicks", 0) or 0
        # Priority: proven money weighted higher than raw demand, both normalized
        # to the site's own range so the blend is data-driven.
        score = 0.6 * (rev / max_rev) + 0.4 * (impr / max_impr)
        impr_at_stake += impr
        rev_at_stake += rev
        missing.append({
            "url": r.get("url", ""),
            "asset_type": asset_type,
            "impressions": impr,
            "clicks": clicks,
            "revenue": round(rev, 2),
            "priority_score": round(score, 4),
        })

    missing.sort(key=lambda x: -x["priority_score"])
    coverage_pct = round(100 * have / money_pages, 1) if money_pages else 0.0
    return {
        "rows": missing[:limit],
        "missing_count": len(missing),
        "have_reviews": have,
        "money_pages": money_pages,
        "coverage_pct": coverage_pct,
        "impressions_at_stake": int(impr_at_stake),
        "revenue_at_stake": round(rev_at_stake, 2),
    }
