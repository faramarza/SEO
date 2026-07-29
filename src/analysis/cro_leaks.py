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


def _ga4_totals(results):
    ts = tp = tr = ta = 0.0
    for r in results:
        ts += r.get("ga4_sessions", 0) or 0
        tp += r.get("ga4_purchases", 0) or 0
        tr += r.get("ga4_revenue", 0) or 0
        ta += r.get("ga4_add_to_carts", 0) or 0
    return int(ts), int(tp), round(tr, 2), int(ta)


def find_cro_leaks(results, limit=100, system_disallow=None, fallback_aov=None,
                   cart_to_purchase=0.30, account_totals=None):
    """Money pages converting below the site's own per-type average, ranked by
    recoverable revenue. Falls back to an add-to-cart signal when organic purchase
    volume is too thin to benchmark, and explains WHY when neither is available.

    AOV comes from all channels (a business constant), but the per-page CVR
    benchmark stays ORGANIC — comparing an organic page's CVR to an all-channels
    average (inflated by paid/direct) would falsely flag it."""
    bm = site_conversion_and_aov(results, account_totals=account_totals)
    cvr_by_type = bm.get("cvr_by_type", {}) or {}          # organic, per type
    organic_cvr = bm.get("site_cvr") if bm.get("cvr_source") == "organic" else None
    site_aov = bm.get("site_aov")                          # may be all-channels
    aov_for_est = site_aov or fallback_aov
    tot_s, tot_p, tot_r, tot_atc = _ga4_totals(results)
    # Purchase-mode needs a real AOV, an organic CVR benchmark, AND enough organic
    # conversions that the benchmark isn't noise. A paid-heavy store's organic CVR
    # is ~0.1%, which can't flag leaks — use the higher-volume add-to-cart signal.
    _MIN_ORG_PURCHASES = 15
    if not (site_aov and (cvr_by_type or organic_cvr) and tot_p >= _MIN_ORG_PURCHASES):
        if tot_atc >= 20:
            return _find_cro_leaks_by_cart(results, limit, system_disallow,
                                           aov_for_est, cart_to_purchase, tot_atc)
        acct = account_totals or {}
        acct_p = acct.get("purchases", 0) or 0
        acct_r = acct.get("revenue", 0) or 0
        if acct_p > 0:
            # Sales DO exist account-wide — the organic slice is just too thin to
            # benchmark organic CRO by purchases (this store is paid/direct-heavy).
            reason = (f"GA4 shows {acct_p} purchases / ${acct_r:,.0f} across all "
                      f"channels, but organic-attributed conversions ({int(tot_p)} in "
                      f"28 days) are too few to benchmark organic CRO by purchases. "
                      f"Add-to-cart data was also insufficient for a fallback. AOV is "
                      f"still taken from your all-channel sales for revenue estimates "
                      f"elsewhere; organic CRO needs more organic conversion volume.")
        elif tot_s >= 500 and tot_p == 0 and tot_atc == 0:
            reason = (f"GA4 recorded {tot_s:,} organic sessions but ZERO purchases and "
                      f"ZERO add-to-carts — GA4 ecommerce tracking may not be firing on "
                      f"organic traffic. Verify purchase/add_to_cart events.")
        else:
            reason = (f"Not enough organic GA4 purchase data yet ({int(tot_p)} purchases, "
                      f"{tot_s:,} sessions in 28 days) to benchmark conversion — low volume.")
        return {"rows": [], "total_lost_revenue": 0, "pages_affected": 0,
                "reason_unavailable": reason,
                "ga4_totals": {"sessions": tot_s, "purchases": tot_p,
                               "revenue": tot_r, "add_to_carts": tot_atc}}

    site_cvr = organic_cvr  # organic benchmark for purchase-mode

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
        "mode": "purchase",
    }


def _find_cro_leaks_by_cart(results, limit, system_disallow, fallback_aov,
                            cart_to_purchase, tot_atc):
    """Fallback CRO signal when GA4 purchase tracking is missing but add-to-cart
    IS tracked: money pages with traffic but a low add-to-cart rate vs the site's
    own per-type ATC average. Add-to-cart is a genuine, earlier conversion signal.
    Monetized only approximately (via a config AOV and an assumed cart→purchase
    rate), clearly flagged as an estimate."""
    tot_s = sum(r.get("ga4_sessions", 0) or 0 for r in results) or 1
    site_atc_rate = tot_atc / tot_s
    # Per-type ATC rate.
    by_type = {}
    for r in results:
        t = (r.get("asset_type") or "other").lower()
        if t not in ("product", "category"):
            continue
        e = by_type.setdefault(t, [0.0, 0.0])
        e[0] += r.get("ga4_sessions", 0) or 0
        e[1] += r.get("ga4_add_to_carts", 0) or 0
    type_atc = {t: (a / s) for t, (s, a) in by_type.items() if s >= 100 and a > 0}

    rows = []
    for r in results:
        url = r.get("url", "")
        if _is_system_page(url, extra_disallow=system_disallow):
            continue
        asset_type = (r.get("asset_type") or "other").lower()
        if asset_type not in ("product", "category"):
            continue
        sessions = r.get("ga4_sessions", 0) or 0
        atc = r.get("ga4_add_to_carts", 0) or 0
        if sessions < _MIN_SESSIONS:
            continue
        bench = type_atc.get(asset_type) or site_atc_rate
        if not bench:
            continue
        page_atc_rate = atc / sessions if sessions else 0.0
        if page_atc_rate >= bench * _UNDERPERFORMANCE:
            continue
        lost_carts = (bench - page_atc_rate) * sessions
        if lost_carts < 1:
            continue
        pm = r.get("page_metadata", {}) or {}
        est_rev = (lost_carts * cart_to_purchase * fallback_aov) if fallback_aov else 0
        rows.append({
            "url": url, "asset_type": asset_type, "sessions": int(sessions),
            "purchases": None,
            "page_cvr": round(page_atc_rate * 100, 2),      # add-to-cart rate
            "benchmark_cvr": round(bench * 100, 2),
            "lost_purchases": round(lost_carts, 1),          # lost add-to-carts
            "lost_revenue": round(est_rev, 2),
            "reasons": _diagnose(r, pm, asset_type, r.get("ga4_bounce_rate"), None),
            "has_crawl_data": bool(pm.get("has_crawl_data")),
        })
    rows.sort(key=lambda x: -(x["lost_revenue"] or x["lost_purchases"]))
    return {
        "rows": rows[:limit],
        "total_lost_revenue": round(sum(x["lost_revenue"] for x in rows), 2),
        "pages_affected": len(rows),
        "mode": "add_to_cart",
        "note": ("Organic purchases are too sparse to benchmark reliably (common for "
                 "paid-heavy stores), so this ranks by ADD-TO-CART rate instead — a "
                 "higher-volume, earlier conversion signal. Columns show add-to-cart "
                 "rate; revenue is a rough estimate"
                 + (" from your all-channel AOV." if fallback_aov else
                    " (set a manual AOV in config for $ figures).")),
    }
