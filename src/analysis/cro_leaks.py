"""CRO Leak Finder.

Recovering revenue from visitors you ALREADY have is usually faster than earning
new ones. This ranks money pages (products, categories) by how far their
conversion rate falls below the site's OWN average for that page type, weighted
by how much traffic they get — so the top of the list is the most recoverable
revenue, not just the worst percentage.

Benchmarks come from the site's own GA4 (site_benchmarks) — never an industry
number. A page is only compared against a benchmark the site actually has enough
data to trust; pages the site can't benchmark are skipped rather than guessed at.
Each leak's reason is grounded in the real page (thin copy, missing product/price
schema, high bounce), so the fix is concrete.
"""

from src.analysis.site_benchmarks import site_conversion_and_aov
from src.analysis.growth_playbook import _is_system_page

# Minimum landing sessions in the window before a page's conversion rate is
# stable enough to act on.
_MIN_SESSIONS = 40
# Page CVR must be below this fraction of the benchmark to count as a leak.
_UNDERPERFORMANCE = 0.70
# Thin-content word floors by type (a product with 80 words can't sell).
_THIN_WORDS = {"product": 200, "category": 150}


def _diagnose(r, pm, asset_type, bounce, site_bounce):
    reasons = []
    schema = {str(s).lower() for s in (pm.get("schema_types") or [])}
    wc = pm.get("word_count", 0) or 0
    has_crawl = bool(pm.get("has_crawl_data"))

    if has_crawl and wc and wc < _THIN_WORDS.get(asset_type, 120):
        reasons.append(f"Thin page ({wc} words) — add buying guidance, specs, "
                       f"sizing/age, and reasons to buy so visitors don't bounce to compare.")
    if has_crawl and asset_type == "product":
        if "aggregaterating" not in schema and "review" not in schema:
            reasons.append("No visible reviews/ratings — social proof is the top "
                           "conversion lever for a trusted brand. Add reviews.")
        if "offer" not in schema:
            reasons.append("No price/availability schema — make price, stock, and "
                           "shipping unmistakable above the fold.")
    if bounce is not None and (bounce > 0.65 or (site_bounce and bounce > site_bounce * 1.25)):
        reasons.append(f"High bounce ({round(bounce*100)}%) — the above-the-fold "
                       f"(hero image, price, CTA, trust badges) or page speed is "
                       f"losing people before they engage.")
    if not reasons:
        reasons.append("Converts below your average for this page type — audit "
                       "trust signals (reviews, returns/shipping clarity), price "
                       "prominence, above-fold CTA, and image quality.")
    return reasons


def find_cro_leaks(results, limit=100, system_disallow=None):
    """Money pages converting below the site's own per-type average, ranked by
    recoverable revenue. Returns rows + totals."""
    bm = site_conversion_and_aov(results)
    cvr_by_type = bm.get("cvr_by_type", {}) or {}
    site_cvr = bm.get("site_cvr")
    site_aov = bm.get("site_aov")
    if not site_aov:
        # Without a measured AOV we can't monetize the gap honestly.
        return {"rows": [], "total_lost_revenue": 0, "pages_affected": 0,
                "reason_unavailable": "Not enough GA4 purchase data to benchmark "
                "conversion yet — needs measured AOV and per-type conversion rates."}

    # Site bounce baseline (for the diagnosis only).
    b_tot = b_n = 0.0
    for r in results:
        br = r.get("ga4_bounce_rate")
        s = r.get("ga4_sessions", 0) or 0
        if br and s:
            b_tot += br * s
            b_n += s
    site_bounce = (b_tot / b_n) if b_n else None

    rows = []
    for r in results:
        url = r.get("url", "")
        if _is_system_page(url, extra_disallow=system_disallow):
            continue
        asset_type = (r.get("asset_type") or "other").lower()
        if asset_type not in ("product", "category"):
            continue  # only money pages convert directly
        sessions = r.get("ga4_sessions", 0) or 0
        purchases = r.get("ga4_purchases", 0) or 0
        if sessions < _MIN_SESSIONS:
            continue
        bench = cvr_by_type.get(asset_type) or site_cvr
        if not bench:
            continue  # can't benchmark honestly
        page_cvr = purchases / sessions if sessions else 0.0
        if page_cvr >= bench * _UNDERPERFORMANCE:
            continue
        lost_purchases = (bench - page_cvr) * sessions
        if lost_purchases < 0.5:
            continue
        lost_revenue = lost_purchases * site_aov
        pm = r.get("page_metadata", {}) or {}
        bounce = r.get("ga4_bounce_rate")
        rows.append({
            "url": url,
            "asset_type": asset_type,
            "sessions": int(sessions),
            "purchases": round(purchases, 1),
            "page_cvr": round(page_cvr * 100, 2),
            "benchmark_cvr": round(bench * 100, 2),
            "lost_purchases": round(lost_purchases, 1),
            "lost_revenue": round(lost_revenue, 2),
            "reasons": _diagnose(r, pm, asset_type, bounce, site_bounce),
            "has_crawl_data": bool(pm.get("has_crawl_data")),
        })

    rows.sort(key=lambda x: -x["lost_revenue"])
    return {
        "rows": rows[:limit],
        "total_lost_revenue": round(sum(x["lost_revenue"] for x in rows), 2),
        "pages_affected": len(rows),
        "site_aov": site_aov,
    }
