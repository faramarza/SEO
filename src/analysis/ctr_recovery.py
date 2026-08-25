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


from src.analysis.site_benchmarks import achievable_ctr
from src.analysis.growth_playbook import _is_system_page

STOP = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
        "best", "top", "buy", "shop", "online", "sale", "cheap", "your", "you"}


def _words(text: str) -> set:
    return {w for w in "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
            if len(w) > 2 and w not in STOP}


def _stem(w: str) -> str:
    """Crude stem so plural/singular and simple variants match: 'trains'→'train',
    'guides'→'guide', 'boxes'→'box'. Prevents false 'keyword missing' calls when
    the title has 'Alphabet Trains' and the query is 'alphabet train'."""
    for suf in ("ies", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[:-3] + "y" if suf == "ies" else w[:-len(suf)]
    return w


def _query_in_title(query: str, title: str) -> bool:
    """True if the query's meaningful words are substantially present in the
    title — a grounded signal of whether the title even speaks to the query.
    Matches on stems so 'train' counts as present in 'Alphabet Trains'."""
    qw = _words(query)
    if not qw:
        return True
    tw = {_stem(w) for w in _words(title)}
    hit = sum(1 for w in qw if _stem(w) in tw)
    return hit >= max(1, len(qw) - 1)  # allow one missing word


def _diagnose(query, title, has_crawl, has_rating_schema, position, asset_type="other"):
    """Why is CTR low here? Grounded, specific reasons — in priority order.
    Star-rating advice only applies to PRODUCT pages: Google shows review stars
    for one specific item, never for a category/listing page (marking a whole
    category with AggregateRating is ineligible and against their guidelines),
    and a homepage or blog post can't carry them either."""
    reasons = []
    if not has_crawl:
        return ["Page not crawled — fetch it to see the live title/meta."]
    title_ok = _query_in_title(query, title)
    is_product = asset_type == "product"
    if not title_ok:
        reasons.append(f"Your title doesn't clearly match “{query}” — searchers "
                       f"don't see their words, so they skip your result.")
    if is_product and not has_rating_schema:
        reasons.append("No star rating in your result — competitors with stars "
                       "pull the click. Add reviews + AggregateRating schema.")
    if title_ok and not (is_product and not has_rating_schema):
        if is_product:
            reasons.append("Title matches and stars are present — the meta description "
                           "or the offer (price/shipping) is likely losing the click. "
                           "Sharpen the value proposition.")
        elif asset_type == "category":
            reasons.append("Category pages can't show star ratings — the snippet is "
                           "the whole lever here. Use a title that signals selection "
                           "(“Wooden Name Puzzles — 30+ Personalized Designs”) and a "
                           "meta description with your concrete offer (personalization, "
                           "price range, shipping).")
        elif (position or 0) > 1.5:
            reasons.append("Your title already targets this — the real issue is that "
                           "you only rank #{:.1f}. If it's your brand/name, make sure "
                           "THIS page owns position 1 (see Playbook → Brand & Merchant) "
                           "— something is outranking you.".format(position or 0))
        else:
            reasons.append("Your title already targets this — sharpen the meta "
                           "description with a concrete draw and a reason to click.")
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
            expected = achievable_ctr(pos)
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
                "reasons": _diagnose(query, title, has_crawl, has_rating, pos, asset_type),
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
