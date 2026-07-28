"""Unified Action Plan — the "just tell me what to do" layer.

Every other analysis finds a specific class of opportunity. This one reads them
ALL, normalizes each into a single dead-simple task with:
  - exact point-by-point steps,
  - the concrete benefit,
  - the expected value (real $ where measured, else the traffic at stake),
  - the effort,
  - how long it typically takes to SHOW the benefit in your data, and
  - a `metric` + `baseline` so the tool can automatically check on the expected
    date whether it worked and tweak the task.

Then it ranks them so #1 is the single highest-value thing to do first. Nothing
here fabricates numbers — every value/reach comes from the underlying analysis,
which is grounded in the site's own GSC/GA4/crawl data.
"""

from datetime import datetime, timedelta

# Typical days until the benefit is visible in data, per category. Ecommerce SEO
# reality: Google must re-crawl, then the metric has to accumulate.
TIME_TO_IMPACT = {
    "ctr":       (21, "2–4 weeks"),
    "cro":       (35, "3–6 weeks"),
    "schema":    (14, "1–3 weeks"),
    "merchant":  (14, "1–3 weeks"),
    "reviews":   (21, "2–4 weeks"),
    "striking":  (28, "3–6 weeks"),
    "decay":     (14, "~2 weeks"),
    "orphan":    (28, "3–6 weeks"),
    "pruning":   (45, "4–8 weeks"),
}

EFFORT = {
    "ctr": "Quick (~20 min)", "cro": "Medium (a few hours)",
    "schema": "Quick (~30 min)", "merchant": "Quick (~30 min)",
    "reviews": "Ongoing (set up once)", "striking": "Medium (~1 hour)",
    "decay": "Medium (a few hours)", "orphan": "Quick (~20 min)",
    "pruning": "Quick (review + act)",
}


def _tti(cat):
    days, label = TIME_TO_IMPACT.get(cat, (28, "3–6 weeks"))
    return days, label


def _task(cat, url, title, steps, benefit, value, reach, metric,
          baseline, asset_type="other", dedup_extra=""):
    days, label = _tti(cat)
    return {
        "category": cat,
        "url": url,
        "asset_type": asset_type,
        "title": title,
        "steps": steps,
        "benefit": benefit,
        "expected_value": round(value or 0, 2),
        "reach": int(reach or 0),
        "effort": EFFORT.get(cat, "Medium"),
        "time_to_impact_days": days,
        "time_to_impact_label": label,
        "metric": metric,          # what to re-measure at review
        "baseline": baseline,      # value now, to compare against
        "dedup_key": f"plan:{cat}|{url}|{dedup_extra}",
    }


def _from_ctr(ctr, out):
    for r in (ctr.get("rows") or [])[:8]:
        steps = [
            f"Open the page and its current title: {r.get('current_title') or '(fetch the page first)'}.",
            f"Rewrite the <title> so it clearly contains the words “{r.get('query','')}” "
            f"(that's what searchers type) and leads with one concrete draw — age range, "
            f"material, free shipping, or a number if it's a list.",
            "Rewrite the meta description to promise the specific value and end with a nudge to click.",
            "Use Playbook → CTR Recovery → “Rewrite” to generate grounded options if you want a head start.",
            "Publish, then request indexing in Google Search Console for this URL.",
        ]
        benefit = (f"This page already ranks #{r.get('position')} for “{r.get('query','')}” "
                   f"but is under-clicked. Earning a normal click-through recovers about "
                   f"{r.get('lost_clicks')} clicks/mo" +
                   (f" (~${r.get('lost_revenue'):,.0f}/mo)" if r.get("lost_revenue") else "") + ".")
        out.append(_task(
            "ctr", r.get("url",""),
            f"Rewrite the title for “{r.get('query','')}” (ranks #{r.get('position')}, under-clicked)",
            steps, benefit, r.get("lost_revenue", 0), r.get("impressions", 0),
            {"type": "query_clicks", "url": r.get("url",""), "query": r.get("query","")},
            {"clicks": r.get("clicks", 0), "ctr": r.get("actual_ctr", 0)},
            r.get("asset_type","other"), r.get("query","")))


def _from_cro(cro, out):
    for r in (cro.get("rows") or [])[:6]:
        steps = ["Do these in order (highest impact first):"] + \
                [f"{i+1}. {rz}" for i, rz in enumerate(r.get("reasons", []))] + \
                ["Change ONE thing at a time so you can tell what moved the needle.",
                 "Watch this page's purchase rate in GA4 over the next few weeks."]
        benefit = (f"This page gets {r.get('sessions'):,} sessions but converts at "
                   f"{r.get('page_cvr')}% vs your {r.get('benchmark_cvr')}% average — "
                   f"closing the gap is about ${r.get('lost_revenue'):,.0f}/window.")
        out.append(_task(
            "cro", r.get("url",""),
            f"Fix conversion on a page converting below your average ({r.get('page_cvr')}%)",
            steps, benefit, r.get("lost_revenue", 0), r.get("sessions", 0),
            {"type": "page_cvr", "url": r.get("url","")},
            {"page_cvr": r.get("page_cvr", 0), "sessions": r.get("sessions", 0)},
            r.get("asset_type","product")))


def _from_reviews(reviews, out):
    for r in (reviews.get("rows") or [])[:5]:
        steps = [
            "Turn on a post-purchase review request (email or SMS) for recent buyers of this product.",
            "Seed with any genuine off-site reviews you can attribute to it.",
            "Once real reviews are on the page, add AggregateRating/Review JSON-LD "
            "(copy it from Playbook → Rich Results).",
            "Never publish a rating you can't back with real reviews.",
        ]
        benefit = (f"Reviews here touch {r.get('impressions'):,} impressions" +
                   (f" and ${r.get('revenue'):,.0f} revenue" if r.get("revenue") else "") +
                   ". Stars lift click-through in search and conversion on the page.")
        out.append(_task(
            "reviews", r.get("url",""),
            "Collect reviews for a high-traffic product that has none",
            steps, benefit, 0, r.get("impressions", 0),
            {"type": "has_rating_schema", "url": r.get("url","")},
            {"has_rating_schema": False},
            r.get("asset_type","product")))


def _from_rich(rich, out):
    for p in (rich.get("pages") or [])[:6]:
        # Only the highest-impact missing type per page, to keep the plan short.
        gaps = sorted(p.get("missing", []),
                      key=lambda m: {"high":0,"medium":1,"low":2}.get(m.get("impact"),3))
        if not gaps:
            continue
        g = gaps[0]
        steps = [
            f"This page is missing {g['type']} structured data (the crawler confirmed it's not there).",
            f"Why it matters: {g.get('why','')}",
        ]
        if g.get("requires_data"):
            steps.append(f"⚠ {g['requires_data']}")
        steps += ["Paste this JSON-LD (fill any UPPER_CASE with the page's real values):",
                  g.get("jsonld",""),
                  "Validate with Google's Rich Results Test, then watch GSC → Enhancements."]
        out.append(_task(
            "schema", p.get("url",""),
            f"Add {g['type']} schema (missing)",
            steps, f"{g.get('why','')}", 0, 0,
            {"type": "schema_present", "url": p.get("url",""), "schema_type": g["type"]},
            {"present": False},
            p.get("asset_type","other"), g["type"]))


def _from_merchant(bm, out):
    merch = (bm or {}).get("merchant", {})
    for r in (merch.get("rows") or [])[:5]:
        steps = [
            f"This product isn't eligible for Google's free product listings — missing: {', '.join(r.get('missing', []))}.",
            "Add Product JSON-LD (name, image, brand, sku) and Offer (price, priceCurrency, availability) "
            "matching the visible page — grab it from Playbook → Rich Results.",
            "Verify/enable Google Merchant Center free product listings.",
        ]
        benefit = (f"Makes a product with {r.get('impressions'):,} impressions eligible "
                   f"for free product listings / Shopping at no ad cost.")
        out.append(_task(
            "merchant", r.get("url",""),
            "Make a product feed-ready for free listings",
            steps, benefit, 0, r.get("impressions", 0),
            {"type": "schema_present", "url": r.get("url",""), "schema_type": "Offer"},
            {"present": False}, "product"))


def _from_striking(striking, out):
    for r in (striking or [])[:6]:
        steps = [f"“{r.get('query','')}” ranks #{r.get('position')} — just off page 1 — with real demand."]
        steps.append(r.get("action_hint") or "Tighten on-page relevance and add internal links.")
        steps.append("Re-check the query's position in GSC in 3–6 weeks.")
        benefit = (f"Pushing “{r.get('query','')}” onto page 1 could add about "
                   f"{r.get('upside_clicks', 0)} clicks/mo — the page is already relevant.")
        out.append(_task(
            "striking", r.get("url",""),
            f"Push “{r.get('query','')}” onto page 1 (currently #{r.get('position')})",
            steps, benefit, 0, r.get("impressions", 0),
            {"type": "query_position", "url": r.get("url",""), "query": r.get("query","")},
            {"position": r.get("position", 0)},
            r.get("asset_type","other"), r.get("query","")))


def _from_decay(decay, out):
    for r in ((decay or {}).get("decaying") or [])[:4]:
        steps = [
            f"This page fell from {r.get('peak_clicks')} to {r.get('current_clicks')} clicks.",
            f"Likely cause: {r.get('cause','')}.",
            r.get("action_hint") or "Update the content to match what currently ranks, refresh facts and the date, and re-link internally.",
            "Republish with an updated date; watch clicks recover over ~2 weeks.",
        ]
        benefit = f"Refreshing decaying content is one of the highest-ROI SEO moves — recover lost clicks fast."
        out.append(_task(
            "decay", r.get("url",""),
            f"Refresh a decaying page (down {r.get('drop_pct')}% from peak)",
            steps, benefit, 0, r.get("peak_clicks", 0),
            {"type": "page_clicks", "url": r.get("url","")},
            {"clicks": r.get("current_clicks", 0)},
            r.get("asset_type","other")))


def _priority(t):
    """Rank: measured $ value first (by amount), then foundational tasks by the
    traffic they touch. Quicker wins break ties."""
    has_value = 1 if t["expected_value"] > 0 else 0
    effort_rank = {"Quick": 0, "Ongoing": 1, "Medium": 2}.get(
        t["effort"].split(" ")[0], 3)
    return (-has_value, -t["expected_value"], -t["reach"], effort_rank)


def build_action_plan(ctr=None, cro=None, reviews=None, rich=None,
                      brand_merchant=None, striking=None, decay=None, limit=40):
    """Aggregate every subsystem into one ranked, do-this-next list."""
    out = []
    if ctr: _from_ctr(ctr, out)
    if cro: _from_cro(cro, out)
    if reviews: _from_reviews(reviews, out)
    if rich: _from_rich(rich, out)
    if brand_merchant: _from_merchant(brand_merchant, out)
    if striking: _from_striking(striking, out)
    if decay: _from_decay(decay, out)

    out.sort(key=_priority)
    for i, t in enumerate(out, start=1):
        t["rank"] = i
    return out[:limit]


def review_date_for(category, from_dt=None):
    """The date the tool should auto-check whether a task worked."""
    days, _ = _tti(category)
    base = from_dt or datetime.now()
    return (base + timedelta(days=days)).date().isoformat()


def _find_query(result, query):
    ql = (query or "").lower()
    for q in (result.get("top_queries") or []):
        if (q.get("query") or "").lower() == ql:
            return q
    return None


def evaluate_review(metric, baseline, result):
    """Compare a task's baseline to the CURRENT evaluation result for its page and
    decide whether it worked. Returns {status, detail, tweak}. status is one of
    improved / no_change / worse / not_measurable / done. Pure — the caller
    supplies the latest eval result for the task's URL (or None)."""
    mtype = (metric or {}).get("type")
    if result is None:
        return {"status": "not_measurable",
                "detail": "The page isn't in the latest evaluation yet — re-run "
                          "the evaluation (with crawl) so this can be measured.",
                "tweak": "Re-run an evaluation, then this will auto-check again."}

    def verdict(now, base, higher_better=True, up="improved", tweak_msg=""):
        if base in (None, 0) and now:
            return {"status": "improved", "detail": f"Now {now} (was ~0).", "tweak": ""}
        if base in (None, 0):
            return {"status": "no_change", "detail": "No measurable change yet.", "tweak": tweak_msg}
        change = (now - base) / abs(base) if base else 0
        better = change > 0.1 if higher_better else change < -0.1
        worse = change < -0.1 if higher_better else change > 0.1
        pct = round(change * 100)
        if better:
            return {"status": "improved",
                    "detail": f"{'Up' if higher_better else 'Down'} {abs(pct)}% "
                              f"(from {base} to {now}). Keep it.", "tweak": ""}
        if worse:
            return {"status": "worse",
                    "detail": f"Moved the wrong way (from {base} to {now}).",
                    "tweak": "Revert to the previous version and try a different angle. " + tweak_msg}
        return {"status": "no_change",
                "detail": f"Roughly flat (from {base} to {now}).", "tweak": tweak_msg}

    if mtype == "query_clicks":
        q = _find_query(result, metric.get("query"))
        now = (q.get("clicks", 0) if q else 0)
        return verdict(now, baseline.get("clicks"), True,
                       tweak_msg="CTR didn't move — try a more benefit-led title, add a number, "
                                 "or get review stars showing so the listing stands out.")
    if mtype == "query_position":
        q = _find_query(result, metric.get("query"))
        now = (q.get("position", 99) if q else 99)
        base = baseline.get("position", 99)
        # lower position is better
        if now < base - 0.5:
            return {"status": "improved",
                    "detail": f"Rank improved from #{base} to #{round(now,1)}.",
                    "tweak": "" if now <= 5 else "On page 1 or close — add a couple more internal links to finish the job."}
        if now > base + 0.5:
            return {"status": "worse", "detail": f"Rank slipped from #{base} to #{round(now,1)}.",
                    "tweak": "Competitors moved — deepen the on-page relevance for this query."}
        return {"status": "no_change", "detail": f"Rank ~#{round(now,1)} (was #{base}).",
                "tweak": "Add internal links from your strongest related pages and tighten on-page relevance."}
    if mtype == "page_cvr":
        s = result.get("ga4_sessions", 0) or 0
        p = result.get("ga4_purchases", 0) or 0
        now = round(100 * p / s, 2) if s else 0
        return verdict(now, baseline.get("page_cvr"), True,
                       tweak_msg="Conversion didn't move — try the next fix on the list "
                                 "(reviews, price clarity, or the above-fold CTA).")
    if mtype == "page_clicks":
        now = result.get("gsc_clicks", 0) or 0
        return verdict(now, baseline.get("clicks"), True,
                       tweak_msg="Clicks haven't recovered — deepen the refresh to match "
                                 "what currently ranks, and add fresh internal links.")
    if mtype in ("has_rating_schema", "schema_present"):
        schema = {str(s).lower() for s in ((result.get("page_metadata", {}) or {}).get("schema_types") or [])}
        target = (metric.get("schema_type") or ("aggregaterating" if mtype == "has_rating_schema" else "")).lower()
        present = target in schema if target else bool(schema)
        if present:
            return {"status": "done", "detail": f"{target or 'schema'} is now live on the page. ✓", "tweak": ""}
        return {"status": "no_change",
                "detail": "Still not detected on the page.",
                "tweak": "The markup isn't live yet (or Google hasn't re-crawled). Confirm it's "
                         "in the page source and validate with the Rich Results Test."}
    return {"status": "not_measurable", "detail": "No automatic metric for this task.", "tweak": ""}
