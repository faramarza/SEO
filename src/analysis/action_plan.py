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
    "content":   (70, "6–12 weeks"),
    "geo":       (28, "3–6 weeks"),
    "brand":     (21, "2–4 weeks"),
}

EFFORT = {
    "ctr": "Quick (~20 min)", "cro": "Medium (a few hours)",
    "schema": "Quick (~30 min)", "merchant": "Quick (~30 min)",
    "reviews": "Ongoing (set up once)", "striking": "Medium (~1 hour)",
    "decay": "Medium (a few hours)", "orphan": "Quick (~20 min)",
    "pruning": "Quick (review + act)", "content": "Large (write an article)",
    "geo": "Medium (a few hours)", "brand": "Medium (~1 hour)",
}

# Hours of hands-on work, used for the ROI ranking (value per hour per week).
EFFORT_HOURS = {
    "ctr": 0.35, "schema": 0.5, "merchant": 0.5, "orphan": 0.35, "pruning": 0.5,
    "reviews": 1.0, "brand": 1.0, "striking": 1.0, "geo": 3.0, "cro": 3.0,
    "decay": 3.0, "content": 8.0,
}
# Baseline impact (in $-equivalent points) for foundational tasks that have no
# measured revenue and little/no reach, so a quick 30-min schema fix still ranks
# sensibly instead of sinking to zero.
CATEGORY_BASE = {
    "reviews": 15, "merchant": 8, "schema": 8, "brand": 8, "geo": 6,
    "decay": 6, "orphan": 5, "striking": 5, "content": 4, "pruning": 3,
}
# Is this a "knock it out now" quick win? Used for batching + the weekly view.
QUICK_CATS = {"ctr", "schema", "merchant", "orphan", "pruning"}


def _tti(cat):
    days, label = TIME_TO_IMPACT.get(cat, (28, "3–6 weeks"))
    return days, label


def _task(cat, url, title, steps, benefit, value, reach, metric,
          baseline, asset_type="other", dedup_extra="", auto_review=True):
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
        "auto_review": auto_review,  # False = operator self-reports (can't auto-measure)
        "dedup_key": f"plan:{cat}|{url}|{dedup_extra}",
    }


def _from_ctr(ctr, out):
    for r in (ctr.get("rows") or [])[:8]:
        reasons = r.get("reasons") or []
        # Lead with the grounded diagnosis, then a fix that matches it. Only tell
        # them to ADD the keyword to the title if it's actually missing — not when
        # the title already contains it (the old bug on the homepage/brand query).
        title_missing = any("title doesn't clearly match" in rz for rz in reasons)
        steps = [f"Current title: {r.get('current_title') or '(fetch the page first)'}."]
        steps += [f"Why it's under-clicked: {rz}" for rz in reasons]
        if title_missing:
            steps.append(f"Rewrite the <title> so it clearly contains “{r.get('query','')}” "
                         f"(what searchers type) and leads with one concrete draw — age, "
                         f"material, free shipping, or a number if it's a list.")
        else:
            steps.append("Your title already targets this query — focus on the meta "
                         "description: promise the specific value and end with a nudge to "
                         "click. (If the reason above is about ranking/brand, fix that first.)")
        steps += [
            "Use Playbook → CTR Recovery → “Rewrite” to generate grounded title/meta options.",
            "Publish, then request indexing in Google Search Console for this URL.",
        ]
        benefit = (f"This page already ranks #{r.get('position')} for “{r.get('query','')}” "
                   f"but is under-clicked. Earning a normal click-through recovers about "
                   f"{r.get('lost_clicks')} clicks/mo" +
                   (f" (~${r.get('lost_revenue'):,.0f}/mo)" if r.get("lost_revenue") else "") + ".")
        _title = (f"Rewrite the title for “{r.get('query','')}” (ranks #{r.get('position')}, under-clicked)"
                  if title_missing else
                  f"Win back clicks on “{r.get('query','')}” (ranks #{r.get('position')}, under-clicked)")
        out.append(_task(
            "ctr", r.get("url",""),
            _title,
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
        # Only ACTIONABLE schema gaps — skip anything blocked on a prerequisite
        # (e.g. AggregateRating needs real reviews first). Blocked schema is a
        # trap as a top task: it tells you to add stars you can't honestly add
        # yet. Reviews are handled by the review-collection task, which sequences
        # it correctly (collect reviews → then the schema follows).
        gaps = [m for m in p.get("missing", []) if not m.get("requires_data")]
        gaps = sorted(gaps,
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


def _from_content(content, out):
    sugs = [s for s in (content.get("suggestions") or []) if s.get("is_content_gap")]
    for s in sugs[:6]:
        q = s.get("source_query", "")
        vol = s.get("ahrefs_volume", 0)
        steps = [
            f"Write a dedicated article targeting “{q}” — real demand your site has "
            f"no page for yet.",
            "Make it a genuinely useful guide/listicle (if it's a 'best/top' query, "
            "put a NUMBER in the title, e.g. “12 Best …”).",
            "Answer the actual question in the first paragraph (quotable by AI engines), "
            "then cover the sub-topics shoppers care about.",
            "Link from the article down to the matching product/category pages, and "
            "add a link to it from a related existing page so it isn't an orphan.",
            "Publish and request indexing in Search Console.",
        ]
        reach = s.get("impressions", 0) or vol or 0
        benefit = (f"“{q}” has real demand" +
                   (f" ({s.get('impressions'):,} impressions" if s.get("impressions") else
                    (f" (~{vol:,} monthly searches" if vol else " (search demand")) +
                   ") but no dedicated content — this captures top-of-funnel traffic and "
                   "funnels it to your products.")
        out.append(_task(
            "content", "",
            f"Write content for “{q}” (demand with no page)",
            steps, benefit, 0, reach,
            {"type": "query_clicks_any", "query": q},
            {"clicks": s.get("clicks", 0)},
            "blog", q))


def _from_orphans(oc, out):
    for r in ((oc or {}).get("orphans") or [])[:5]:
        link_from = r.get("link_from") or []
        steps = [f"This page has {r.get('impressions',0):,} impressions but ZERO internal inbound links, so it can't rank well or be discovered."]
        if link_from:
            steps.append("Add contextual links to it from these specific related pages:")
            steps += [f"• {lf.get('title', lf.get('url',''))} ({lf.get('url','')})" for lf in link_from[:5]]
        else:
            steps.append("Link it from a relevant category/nav or a related article (no strong topical match was found automatically).")
        steps.append("Use anchor text that describes THIS page; place links inside content, not nav/footer.")
        out.append(_task(
            "orphan", r.get("url",""),
            "Rescue an orphan page (has demand, no internal links)",
            steps, "Internal links let this page rank and get discovered — orphaned pages wither.",
            0, r.get("impressions", 0),
            {"type": "page_clicks", "url": r.get("url","")},
            {"clicks": r.get("clicks", 0)},
            r.get("asset_type","other")))


def _from_pruning(pruning, out):
    for r in (pruning or [])[:4]:
        disp = r.get("disposition", "prune")
        tgt = r.get("redirect_target") or {}
        if disp == "merge_redirect":
            steps = [f"Merge any useful content from this dead page into “{tgt.get('title','a stronger page')}” ({tgt.get('url','')}).",
                     f"301-redirect this URL to that page to preserve residual equity.",
                     f"Rationale: {r.get('reason','')}", "Review before executing — this is a suggestion."]
            title = f"Merge & 301 a dead page into a stronger one"
        else:
            steps = [f"Prune this dead page ({r.get('impressions',0)} impr, {r.get('word_count',0)} words) — noindex it or remove and 410.",
                     f"Rationale: {r.get('reason','')}", "Review before executing — this is a suggestion."]
            title = "Prune a dead-weight page"
        out.append(_task(
            "pruning", r.get("url",""), title, steps,
            "Removing/merging dead-weight pages concentrates your topical authority.",
            0, r.get("impressions", 0),
            {"type": "manual"}, {}, r.get("asset_type","other"), auto_review=False))


def _from_geo(geo, out):
    for p in ((geo or {}).get("pages") or [])[:5]:
        if p.get("score", 100) >= 70:
            continue
        fixes = p.get("top_findings", []) or []
        steps = [f"Raise this page's AI-citation readiness (currently {p.get('score')}/100) so ChatGPT/Gemini/AI Overviews can cite it."]
        steps += [f"• {f.get('fix','')}" for f in fixes[:4]]
        steps.append("Then re-check Playbook → GEO.")
        out.append(_task(
            "geo", p.get("url",""),
            f"Make a page AI-citable (GEO {p.get('score')}/100)",
            steps, "AI answer engines cite well-structured, evidence-rich pages — this is where discovery is heading.",
            0, p.get("gsc_impressions", 0),
            {"type": "geo_score", "url": p.get("url","")},
            {"score": p.get("score", 0)},
            p.get("asset_type","other")))


def _from_brand(bm, out):
    for f in ((bm or {}).get("brand", {}).get("flags") or [])[:4]:
        steps = [f"“{f.get('query','')}” is a search for your own brand, but " +
                 "; ".join(f.get("issues", [])) + "."]
        steps += [
            "Make sure the RIGHT page (usually your homepage or the exact product) is the strong answer for this query.",
            "Tighten that page's title so it clearly owns the brand term; add Organization/Sitelinks-friendly structure.",
            "Check Google for anyone bidding on your brand name or a reseller/marketplace outranking you, and act on it.",
        ]
        out.append(_task(
            "brand", f.get("ranking_url",""),
            f"Reclaim your brand query “{f.get('query','')}”",
            steps, "Branded searches are your highest-intent traffic — you should own them, not leak them to resellers or ads.",
            0, f.get("impressions", 0),
            {"type": "query_position", "url": f.get("ranking_url",""), "query": f.get("query","")},
            {"position": f.get("position", 0)},
            "other", f.get("query","")))


# Not all traffic is worth the same. Impressions on an informational blog post
# (low buying intent) are worth far less than on a money page — so a reach-based
# proxy must be weighted by the page's commercial value, or high-impression blog
# busywork (e.g. internal-linking every article) floats to the top.
ASSET_INTENT_WEIGHT = {
    "product": 1.2, "category": 1.0, "guide": 0.45,
    "blog": 0.3, "article": 0.3, "other": 0.6,
}


def _score_task(t):
    """ROI = value per hour of work per week until it pays off. So a quick,
    high-value, fast-paying fix outranks a big, slow, expensive one — which is
    what 'the best use of your next hour' actually means."""
    cat = t["category"]
    # Impact in $-equivalent points. Real measured revenue is trusted as-is.
    # Otherwise use a traffic proxy WEIGHTED by the page's commercial intent, so
    # informational reach doesn't masquerade as revenue.
    if t["expected_value"] > 0:
        impact = t["expected_value"]
    else:
        w = ASSET_INTENT_WEIGHT.get((t.get("asset_type") or "other").lower(), 0.6)
        impact = max(t["reach"] * 0.01 * w, CATEGORY_BASE.get(cat, 5) * w)
    hours = EFFORT_HOURS.get(cat, 2.0)
    weeks = max(1.0, t["time_to_impact_days"] / 7.0)
    roi = impact / (hours * weeks)
    t["roi"] = round(roi, 2)
    t["is_quick"] = cat in QUICK_CATS
    # A one-line, honest "why this rank".
    fast = t["time_to_impact_days"] <= 21
    cheap = hours <= 0.6
    big = t["expected_value"] >= 200
    if cheap and (big or fast):
        t["rank_reason"] = "Quick win — high value for ~20–30 min of work"
    elif big:
        t["rank_reason"] = "High revenue impact"
    elif cheap:
        t["rank_reason"] = "Fast and cheap to do"
    elif cat == "content":
        t["rank_reason"] = "Bigger effort, slower payoff — schedule it"
    else:
        t["rank_reason"] = "Solid value for the effort"
    return roi


def build_action_plan(ctr=None, cro=None, reviews=None, rich=None,
                      brand_merchant=None, striking=None, decay=None,
                      content=None, orphans=None, pruning=None, geo=None, limit=60):
    """Aggregate EVERY subsystem into one ranked, do-this-next list."""
    out = []
    if ctr: _from_ctr(ctr, out)
    if cro: _from_cro(cro, out)
    if reviews: _from_reviews(reviews, out)
    if rich: _from_rich(rich, out)
    if brand_merchant: _from_merchant(brand_merchant, out)
    if striking: _from_striking(striking, out)
    if decay: _from_decay(decay, out)
    if content: _from_content(content, out)
    if orphans: _from_orphans(orphans, out)
    if pruning: _from_pruning(pruning, out)
    if geo: _from_geo(geo, out)
    if brand_merchant: _from_brand(brand_merchant, out)

    for t in out:
        _score_task(t)
    out.sort(key=lambda t: -t["roi"])
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
    if mtype == "manual":
        return {"status": "not_measurable",
                "detail": "This one you confirm yourself — mark it done once actioned.",
                "tweak": ""}
    if mtype == "query_clicks_any":
        # Site-wide clicks for the topic (a new article could rank on any URL);
        # the caller injects the current total under _site_query_clicks.
        now = (result or {}).get("_site_query_clicks", 0)
        return verdict(now, baseline.get("clicks"), True,
                       tweak_msg="Nothing ranks for this topic yet — make sure the new "
                                 "article puts the exact query in the title/H1, answers it "
                                 "directly, and is internally linked.")
    if mtype == "geo_score":
        # result must carry a recomputed geo score under 'geo_score' (the caller
        # supplies it); if absent, not measurable.
        now = result.get("geo_score")
        if now is None:
            return {"status": "not_measurable",
                    "detail": "Re-run the evaluation so GEO can be re-scored.", "tweak": ""}
        return verdict(now, baseline.get("score"), True,
                       tweak_msg="Citation readiness didn't move — add real statistics, "
                                 "question-form H2s + FAQ, and a clear last-updated date.")
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
