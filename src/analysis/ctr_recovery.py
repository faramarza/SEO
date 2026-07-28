"""CTR Recovery Clinic.

The biggest leak visible in a small-but-established store's own data: pages that
ALREADY rank on page 1 but get clicked far below the site's own CTR curve for
that position. The rankings and impressions already exist — recovering the clicks
needs no new authority, just a title/meta that earns the click (and, where
relevant, star ratings / price showing in the result).

Everything here is measured from GSC + the site's own CTR curve (site_benchmarks)
— never an industry assumption when the site has its own data. The diagnosis for
each leak is grounded in the actual page (is the query in the title? does the
page emit rating schema?), so the fix is specific, not generic.

Pure functions over the evaluation `results` list. Monetization (lost revenue) is
left to the caller so this stays free of the web layer's business params.
"""

from urllib.parse import urlparse

from src.analysis.site_benchmarks import site_expected_ctr
from src.analysis.growth_playbook import _is_system_page

STOP = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
        "best", "top", "buy", "shop", "online", "sale", "cheap", "your", "you"}


def _words(text: str) -> set:
    return {w for w in "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
            if len(w) > 2 and w not in STOP}


def _query_in_title(query: str, title: str) -> bool:
    """True if the query's meaningful words are substantially present in the
    title — a grounded signal of whether the title even speaks to the query."""
    qw = _words(query)
    if not qw:
        return True
    tw = _words(title)
    hit = len(qw & tw)
    return hit >= max(1, len(qw) - 1)  # allow one missing word


def _diagnose(query, title, has_crawl, has_rating_schema, position):
    """Why is CTR low here? Grounded, specific reasons — in priority order."""
    reasons = []
    if not has_crawl:
        reasons.append("Page not crawled — fetch it to see the live title/meta.")
        return reasons
    if not _query_in_title(query, title):
        reasons.append(f"Your title doesn't clearly match “{query}” — searchers "
                       f"don't see their words, so they skip your result.")
    if not has_rating_schema:
        reasons.append("No star rating in your result — competitors with stars "
                       "pull the click. Add reviews + AggregateRating schema.")
    if _query_in_title(query, title) and has_rating_schema:
        reasons.append("Title matches and stars are present — the meta description "
                       "or the offer (price/shipping) is likely what's losing the "
                       "click. Sharpen the value proposition.")
    return reasons


def find_ctr_recovery(results, min_impressions=100, position_ceiling=10.0,
                      underperformance=0.70, min_lost_clicks=3.0,
                      limit=100, system_disallow=None):
    """Queries ranking on page 1 (<= position_ceiling) whose actual CTR is below
    `underperformance` × the site's expected CTR at that position, costing at
    least `min_lost_clicks` clicks over the window.

    Returns rows sorted by lost_clicks desc, each with the grounded diagnosis and
    the page's current title/meta so a rewrite can be proposed.
    """
    rows = []
    for r in results:
        url = r.get("url", "")
        if _is_system_page(url, extra_disallow=system_disallow):
            continue
        pm = r.get("page_metadata", {}) or {}
        has_crawl = bool(pm.get("has_crawl_data"))
        title = pm.get("title", "") or ""
        meta = pm.get("meta_description", "") or ""
        schema_types = {str(s).lower() for s in (pm.get("schema_types") or [])}
        has_rating = "aggregaterating" in schema_types or "review" in schema_types
        asset_type = (r.get("asset_type") or "other").lower()

        for q in (r.get("top_queries") or []):
            impr = q.get("impressions", 0) or 0
            pos = q.get("position", 0) or 0
            if impr < min_impressions or pos <= 0 or pos > position_ceiling:
                continue
            actual_ctr = (q.get("ctr", 0) or 0) / 100.0  # stored as percentage
            expected = site_expected_ctr(pos)
            if expected <= 0:
                continue
            if actual_ctr >= expected * underperformance:
                continue  # clicking at/above what we'd expect — not a leak
            lost_clicks = (expected - actual_ctr) * impr
            if lost_clicks < min_lost_clicks:
                continue
            query = q.get("query", "")
            rows.append({
                "url": url,
                "asset_type": asset_type,
                "query": query,
                "position": round(pos, 1),
                "impressions": impr,
                "clicks": q.get("clicks", 0) or 0,
                "actual_ctr": round(actual_ctr * 100, 2),
                "expected_ctr": round(expected * 100, 2),
                "lost_clicks": round(lost_clicks, 1),
                "current_title": title,
                "current_meta": meta,
                "has_crawl_data": has_crawl,
                "has_rating_schema": has_rating,
                "reasons": _diagnose(query, title, has_crawl, has_rating, pos),
            })

    # Keep the single worst query per (url) so we don't list the same page 5×;
    # a page's biggest leak is the one to act on first.
    best_by_url = {}
    for row in rows:
        cur = best_by_url.get(row["url"])
        if cur is None or row["lost_clicks"] > cur["lost_clicks"]:
            best_by_url[row["url"]] = row
    deduped = sorted(best_by_url.values(), key=lambda x: -x["lost_clicks"])

    total_lost = round(sum(x["lost_clicks"] for x in deduped), 1)
    return {
        "rows": deduped[:limit],
        "total_lost_clicks": total_lost,
        "pages_affected": len(deduped),
    }
