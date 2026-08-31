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
    "winnable":  (28, "3–6 weeks"),
    "links":     (60, "6–10 weeks"),
    "consolidation": (21, "2–4 weeks"),
}

EFFORT = {
    "ctr": "Quick (~20 min)", "cro": "Medium (a few hours)",
    "schema": "Quick (~30 min)", "merchant": "Quick (~30 min)",
    "reviews": "Ongoing (set up once)", "striking": "Medium (~1 hour)",
    "decay": "Medium (a few hours)", "orphan": "Quick (~20 min)",
    "pruning": "Quick (review + act)", "content": "Large (write an article)",
    "geo": "Medium (a few hours)", "brand": "Medium (~1 hour)",
    "winnable": "Medium (~1 hour)",
    "links": "Medium (send prepared emails)",
    "consolidation": "Quick (~1 hour, one redirect rule)",
}

# Hours of hands-on work, used for the ROI ranking (value per hour per week).
EFFORT_HOURS = {
    "ctr": 0.35, "schema": 0.5, "merchant": 0.5, "orphan": 0.35, "pruning": 0.5,
    "reviews": 1.0, "brand": 1.0, "striking": 1.0, "geo": 3.0, "cro": 3.0,
    "decay": 3.0, "content": 8.0, "winnable": 1.0,
    "links": 1.5, "consolidation": 1.0,
}
# Baseline impact (in $-equivalent points) for foundational tasks that have no
# measured revenue and little/no reach, so a quick 30-min schema fix still ranks
# sensibly instead of sinking to zero.
CATEGORY_BASE = {
    "merchant": 8, "schema": 8, "brand": 8, "geo": 6,
    "decay": 6, "winnable": 6, "orphan": 5, "striking": 5, "reviews": 4,
    "content": 4, "pruning": 3,
}

# When a task has NO measured revenue we fall back to a reach proxy (a slice of
# its impressions). But not all reach converts to money at the same rate. A CTR
# or CRO fix acts on a click/session you've ALREADY earned; a review or other
# trust-signal only nudges a small fraction of viewers by a small amount. This
# factor discounts the reach proxy by how much of it realistically becomes
# revenue — so an unmeasured trust-signal (reviews especially) stops ranking as
# if it were top-line money. 1.0 = full credit; 0.15 = a real but modest lever.
REACH_YIELD = {
    "reviews": 0.15,   # stars are a real but small CTR/CVR nudge — not a growth engine
    "geo": 0.30, "brand": 0.55, "schema": 0.55, "pruning": 0.45,
    "merchant": 0.60, "orphan": 0.60, "content": 0.55,
    "striking": 0.75, "winnable": 0.75, "decay": 0.80,
    "links": 0.75, "consolidation": 0.80,
}
# Is this a "knock it out now" quick win? Used for batching + the weekly view.
QUICK_CATS = {"ctr", "schema", "merchant", "orphan", "pruning"}

# Categories that are ALWAYS genuine growth levers — they can never be demoted to
# the "while you're at it" strip no matter how small their measured value, because
# their value is future traffic/structure that 28-day money can't see.
NEVER_MINOR = {"winnable", "striking", "content", "cro", "decay", "orphan", "geo",
               "links", "consolidation"}
# Below this $-equivalent impact, a NON-lever task (a cheap CTR/schema/reviews
# harvest) is "minor" — real, but it must never headline "do this next". A $10/mo
# brand-CTR fix lands here; a $200/mo money-page fix does not.
MINOR_IMPACT_FLOOR = 15.0
# The store's own brand — CTR "fixes" on these are navigational noise (you already
# own them) and must never be a headline lever.
BRAND_HINTS = ("alphabet train", "alphabet-trains", "alphabettrains")


def _is_brand_query(q):
    ql = (q or "").lower()
    return any(b in ql for b in BRAND_HINTS)


# SERP-feature CTR suppression. When a feature sits above/around organic, winning
# a better organic position yields FEWER clicks than the raw CTR curve predicts —
# the feature siphons them. Multipliers are deliberately conservative (they only
# demote), applied multiplicatively and floored so a stacked SERP can't zero a
# task out entirely. AI Overviews are the heaviest suppressor; PAA/knowledge-graph
# the lightest. Used to haircut the reach/impact of position-play tasks.
_SERP_FEATURE_SUPPRESSION = {
    "ai_overview": 0.55,
    "featured_snippet": 0.72,
    "shopping": 0.78,
    "top_stories": 0.85,
    "knowledge_graph": 0.9,
    "people_also_ask": 0.9,
}


def _serp_ctr_haircut(features) -> float:
    """Combined CTR-suppression multiplier (≤1.0) for the SERP features present.
    1.0 = no suppression; floored at 0.4 so it demotes without erasing."""
    mult = 1.0
    for f in (features or []):
        mult *= _SERP_FEATURE_SUPPRESSION.get(f, 1.0)
    return max(0.4, round(mult, 3))


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
        _t = _task(
            "ctr", r.get("url",""),
            _title,
            steps, benefit, r.get("lost_revenue", 0), r.get("impressions", 0),
            {"type": "query_clicks", "url": r.get("url",""), "query": r.get("query","")},
            {"clicks": r.get("clicks", 0), "ctr": r.get("actual_ctr", 0)},
            r.get("asset_type","other"), r.get("query",""))
        # Recoverable clicks — the measurability signal for a CTR fix (a page
        # with modest impressions but a huge CTR gap yields a readable sample).
        _t["expected_clicks"] = r.get("lost_clicks", 0) or 0
        # A CTR "fix" on the store's own brand term, or a SMALL harvest on a page
        # already at the top (nothing to gain from position, the click is a
        # commodity toss-up), is trivial — flag it so it can never headline the
        # plan. But a top-2 page leaking a real click volume is an anomaly worth
        # headlining, not a toss-up — keep it.
        if _is_brand_query(r.get("query", "")) or (
                (r.get("position") or 99) <= 2.0 and (r.get("lost_clicks") or 0) < 10):
            _t["is_brand_ctr"] = True
        out.append(_t)


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
        benefit = (f"Stars are a modest but real trust nudge — a small CTR/conversion "
                   f"lift, not a growth lever. Best treated as a set-once, run-in-the-"
                   f"background task (e.g. an automatic post-purchase request). This "
                   f"product sees {r.get('impressions'):,} impressions" +
                   (f" / ${r.get('revenue'):,.0f} revenue" if r.get("revenue") else "") +
                   ", so it's the one worth having stars on first.")
        out.append(_task(
            "reviews", r.get("url",""),
            "Set up background review collection for a top product (supporting task)",
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
            f"VERIFY FIRST: this scan reads server HTML only and can't see schema "
            f"your theme/extensions add via JavaScript. Open {p.get('url','')} in "
            f"Google's Rich Results Test — if it already lists {g['type']}, you're "
            f"done, skip this task. Only continue if it's genuinely absent.",
            f"Why {g['type']} matters (if missing): {g.get('why','')}",
        ]
        if g.get("requires_data"):
            steps.append(f"⚠ {g['requires_data']}")
        steps += ["If it's truly absent, paste this JSON-LD (fill any UPPER_CASE with the page's real values):",
                  g.get("jsonld",""),
                  "Re-run the Rich Results Test to confirm it's valid, then watch GSC → Enhancements."]
        out.append(_task(
            "schema", p.get("url",""),
            f"Verify then add {g['type']} schema (not detected in HTML)",
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


def _from_winnable(winnable, out):
    """Category pages that rank position 4–20 for GENERIC (non-brand) demand and
    earn ~0 clicks — the reseller-winnable queries (no brand incumbent) that are
    one SERP-page from real clicks. Comes from the FULL GSC page set, not the
    top-100 evaluation, so it surfaces pages the rest of the plan can't see. Each
    carries a grounded title/meta fix AND the specific internal-link sources that
    move it up — using authority the site already has, no new backlinks."""
    for r in (winnable or [])[:12]:
        q = r.get("query", "")
        pos = r.get("position", 0)
        try:
            pos = int(pos) if float(pos).is_integer() else round(float(pos), 1)
        except (TypeError, ValueError):
            pos = r.get("position", 0)
        trend = r.get("trend")
        delta = r.get("position_delta")
        declining = trend == "down" and delta is not None
        steps = [
            f"“{q}” ranks #{pos} with {r.get('impressions', 0)} impressions but ~0 clicks — "
            f"real, non-brand demand you already rank for, just below the click zone.",
        ]
        if declining:
            steps.insert(0, f"⚠️ DROPPING: this query fell ~{abs(delta):.0f} positions in the "
                            f"last 28 days (from ~#{r.get('position_prev28')} to #{r.get('position_now28')}). "
                            f"Defend it NOW — likely a lost internal link, stale content, or a "
                            f"competitor overtook you. If it keeps sliding it leaves page 2 entirely.")
        elif trend == "up" and delta is not None:
            steps.insert(0, f"↗ RISING: this query gained ~{delta:.0f} positions in 28 days — it's "
                            f"already climbing toward the click zone, so a light push finishes the job.")
        if r.get("title_rewrite"):
            steps.append(f"Reword the <title> to read naturally and lead with the "
                         f"searcher's words — e.g. “{r['title_rewrite']}”. Keep it "
                         f"natural: no “|” pipes or bolted-on separators (Google "
                         f"rewrites over-templated titles), and weave your brand in "
                         f"if it fits.")
        else:
            cur = r.get("current_title") or ""
            steps.append("Your <title> already targets this query" +
                         (f" ({cur})" if cur else "") + " — do NOT retitle. The gap is "
                         "position/authority; the internal links below are the fix.")
        if r.get("meta_missing"):
            steps.append(f"Add a meta description that leads with “{q}” + one real, specific "
                         "hook (selection, age fit, your curation) — no invented stats.")
        elif r.get("meta_needs_query"):
            steps.append(f"Front-load “{q}” into your existing meta description.")
        srcs = r.get("link_sources") or []
        if srcs:
            names = "; ".join(s.get("path", "") for s in srcs)
            steps.append(f"Add internal links with anchor “{q}” from your related pages: {names}. "
                         "This is the real lever — it channels authority you already have into "
                         "this page and pushes it toward the top 5.")
        else:
            steps.append(f"Add internal links with anchor “{q}” from your homepage and the "
                         "closest hub/category page.")
        serp = r.get("serp") or {}
        if serp:
            bits = []
            who = ", ".join((serp.get("top_domains") or [])[:3])
            if who:
                bits.append(f"Live SERP top 3: {who}.")
            if serp.get("verdict_note"):
                bits.append(serp["verdict_note"][:1].upper() + serp["verdict_note"][1:] + ".")
            if serp.get("siphons"):
                bits.append("Click siphon above organic: " + "; ".join(serp["siphons"]) + ".")
            if bits:
                steps.append("🔎 " + " ".join(bits))
        steps.append("Re-check the query's position in GSC in 3–6 weeks.")
        benefit = (f"This page ranks #{pos} for “{q}” ({r.get('impressions', 0)} impressions/window) "
                   "but earns almost no clicks because position 4–20 gets ~1% CTR. It's non-brand "
                   "category demand — winnable for a reseller (no brand owns it). Getting it into "
                   "the top 5 turns impressions you ALREADY earn into clicks, with no new backlinks.")
        # When declining, headline the CURRENT position (now28), not the 28-day
        # average — "dropped ~10 spots to #14" while the steps say #28.4 reads
        # as a contradiction (both were true: avg vs now).
        _pos_now = r.get("position_now28") or pos
        title = (f"🛡️ Defend “{q}” — dropped ~{abs(delta):.0f} spots to #{_pos_now} (non-brand demand)"
                 if declining else
                 f"Win clicks on “{q}” (ranks #{pos}, non-brand demand, ~0 clicks)")
        _t = _task(
            "winnable", r.get("url", ""),
            title,
            steps, benefit, 0, r.get("impressions", 0),
            {"type": "query_position", "url": r.get("url", ""), "query": q},
            {"position": pos},
            r.get("asset_type", "category"), q)
        _t["trend"] = trend
        if delta is not None:
            _t["position_delta"] = delta
        _t["serp_verdict"] = (r.get("serp") or {}).get("verdict")
        _t["serp_features"] = (r.get("serp") or {}).get("features") or []
        out.append(_t)


def _from_decay(decay, out):
    for r in ((decay or {}).get("decaying") or [])[:4]:
        steps = [
            f"This page fell from {r.get('peak_clicks')} to {r.get('current_clicks')} clicks.",
            f"Likely cause: {r.get('cause','')}.",
            r.get("action_hint") or "Update the content to match what currently ranks, refresh facts and the date, and re-link internally.",
            "Republish with an updated date; watch clicks recover over ~2 weeks.",
        ]
        _lost = (r.get("peak_clicks") or 0) - (r.get("current_clicks") or 0)
        benefit = (f"This page lost ~{_lost} clicks/mo (from {r.get('peak_clicks')} to "
                   f"{r.get('current_clicks')})" +
                   (f", likely from {r.get('cause')}" if r.get('cause') else "") +
                   ". A refresh to match what now ranks is one of the fastest-recovering SEO fixes.")
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
        # Not every content-gap suggestion stores its topic under source_query —
        # some use title/keyword/idea. Fall back through them, and if there's no
        # topic at all, skip: "Write content for \"\"" is a useless task.
        q = (s.get("source_query") or s.get("keyword") or s.get("title")
             or s.get("idea") or s.get("primary_keyword") or "").strip()
        if not q:
            continue
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
            steps, (f"This page has {r.get('impressions',0):,} impressions of demand but no internal "
                    f"links, so it can't rank well or be discovered — a few contextual links unlock it."),
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
                     "301-redirect this URL to that page to preserve residual equity.",
                     f"Rationale: {r.get('reason','')}", "Review before executing — this is a suggestion."]
            title = "Merge & 301 a dead page into a stronger one"
        else:
            steps = [f"Prune this dead page ({r.get('impressions',0)} impr, {r.get('word_count',0)} words) — noindex it or remove and 410.",
                     f"Rationale: {r.get('reason','')}", "Review before executing — this is a suggestion."]
            title = "Prune a dead-weight page"
        out.append(_task(
            "pruning", r.get("url",""), title, steps,
            (f"This dead-weight page ({r.get('impressions',0)} impr, {r.get('word_count',0)} words) "
             f"dilutes your topical authority — removing or merging it concentrates ranking signals "
             f"on your strong pages."),
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
            steps, (f"This page scores {p.get('score')}/100 for AI-citation readiness on "
                    f"{p.get('gsc_impressions',0):,} impressions of demand — raising it makes "
                    f"ChatGPT/Gemini/AI Overviews far likelier to cite you, where discovery is heading."),
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
            steps, (f"“{f.get('query','')}” is a search for your own brand ({f.get('impressions',0):,} "
                    f"impressions, you rank #{f.get('position','?')}) — your highest-intent traffic. "
                    f"Owning it stops leaking those buyers to resellers or ads."),
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

# THE SECOND LENS. ROI above measures near-term money efficiency — it structurally
# undervalues anything slow or indirect (new content, brand, site structure) even
# when it builds durable, compounding value. This weight captures that missing
# dimension: how much a task builds a lasting asset — traffic, brand equity,
# topical authority, audience, or a structural fix that benefits ALL future
# traffic. High = compounds over time; low = a one-time harvest. A task can be a
# poor near-term ROI and an excellent strategic bet (that's the whole point), so
# these are reported side by side and the plan is split into two horizons rather
# than collapsed into one misleading number.
STRATEGIC_WEIGHT = {
    "links": 1.0,      # earned authority compounds across every page and query
    "consolidation": 0.8,  # structural — merges split rank signals permanently
    "content": 1.0,    # new traffic + topical authority — the classic compounding asset
    "cro": 0.85,       # a structural conversion fix lifts EVERY future visitor
    "striking": 0.7,   # capture demand that's already rising
    "winnable": 0.7,   # convert impressions you already earn — non-brand demand
    "brand": 0.7,      # own your brand equity long-term
    "decay": 0.6,      # recover a compounding asset that's slipping
    "orphan": 0.6,     # site structure — helps the whole domain, not one page
    "geo": 0.6,        # the emerging AI-answer discovery channel
    "reviews": 0.4,    # trust that accrues slowly
    "merchant": 0.4,
    "schema": 0.3,
    "pruning": 0.3,
    "ctr": 0.2,        # a one-time click harvest — real money, but it doesn't compound
}


def _from_outreach(links_info, striking, out):
    """The site's #1 lever as a first-class task: send the prepared link
    outreach. Grounded in the striking-distance rows the tool itself marked
    'needs backlinks' — those queries have NO other lever left, so this task
    carries their combined demand as its reach."""
    tier12 = links_info.get("tier12", 0)
    if tier12 <= 0:
        return
    blocked = [r for r in (striking or []) if r.get("lever") == "external"]
    blocked_reach = sum(r.get("impressions", 0) or 0 for r in blocked)
    drafted = links_info.get("drafted", 0)
    top_targets = sorted(blocked, key=lambda r: -(r.get("impressions", 0) or 0))[:3]
    steps = [
        f"{tier12} tier-1/2 prospects are qualified in Playbook → Link Prospects"
        + (f" — {drafted} already {'has' if drafted == 1 else 'have'} a finished draft."
           if drafted else "."),
        "Start with the tier-1 prospects that have a contact route; personalize the "
        "first 10% of each draft, send, and mark the card ✉ Contacted.",
    ]
    if top_targets:
        steps.append("Aim links at the pages the tool marked authority-blocked: "
                     + "; ".join(f"{r.get('url','')} (“{r.get('query','')}”, "
                                 f"{r.get('impressions',0)} impr)" for r in top_targets))
    steps.append("Forum-reply prospects (💬) are posted, not emailed — answer the thread "
                 "genuinely, recommend a non-self option too, no links in the reply.")
    benefit = (f"{len(blocked)} striking-distance queries "
               f"({blocked_reach:,} impressions/mo) are blocked ONLY on authority — "
               "no title or internal link can move them further. Earned links are the "
               "single lever that unblocks them, and each one compounds across every "
               "page and future query."
               if blocked else
               "Earned links raise the whole domain's authority — the constraint the "
               "rest of this plan keeps running into.")
    out.append(_task(
        "links", "", "Send your prepared link outreach — the drafts are waiting",
        steps, benefit, 0, blocked_reach or 500,
        {"type": "self_report"}, {}, "other", "outreach", auto_review=False))


def _from_duplicates(results, out):
    """Same blog post indexed at two URL variants (/blog/X and /blog/post/X),
    each earning impressions — they compete with each other and split rank.
    Detected conservatively: identical path after collapsing the known variant
    infix, both variants with GSC impressions."""
    def norm(path):
        return path.rstrip("/").replace("/blog/post/", "/blog/")
    groups = {}
    for r in results or []:
        url = r.get("url", "")
        impr = r.get("gsc_impressions", 0) or 0
        if not url or impr <= 0:
            continue
        try:
            from urllib.parse import urlparse
            p = urlparse(url)
            key = p.netloc + norm(p.path)
        except Exception:
            continue
        groups.setdefault(key, []).append((url, impr))
    pairs = [sorted(v, key=lambda x: -x[1]) for v in groups.values()
             if len({u for u, _ in v}) > 1]
    if not pairs:
        return set()
    # The recoverable split = the demand currently landing on the losing variants.
    split = sum(sum(i for _, i in p[1:]) for p in pairs)
    total = sum(sum(i for _, i in p) for p in pairs)
    pairs.sort(key=lambda p: -sum(i for _, i in p))
    steps = ["Verify first: open both URLs of one pair — if both return 200 with the "
             "same content (no redirect), they are true duplicates competing in Google."]
    for p in pairs[:5]:
        steps.append("Duplicate: " + "  vs  ".join(f"{u} ({i:,} impr)" for u, i in p[:2]))
    steps.append("Fix with ONE redirect rule: 301 /blog/post/<slug> → /blog/<slug> "
                 "(keep whichever pattern ranks better as the target), and make each "
                 "post's canonical tag point at the kept URL.")
    steps.append("Then request re-indexing of the kept URLs in GSC.")
    out.append(_task(
        "consolidation", "",
        f"Consolidate {len(pairs)} duplicate blog URLs splitting {total:,} impressions",
        steps,
        (f"Google indexes these posts at two URLs each; the variants compete and split "
         f"rank. One redirect rule consolidates ~{split:,} impressions/mo of split "
         "demand onto single stronger pages — typically worth 1–3 positions on the "
         "merged URL."),
        0, split,
        {"type": "self_report"}, {}, "blog", "blog-post-variants", auto_review=False))
    # The losing variants — every other task aimed at one of these URLs is
    # superseded by the redirect (no point internal-linking a page about to 301).
    return {u for p in pairs for u, _ in p[1:]}


def _score_task(t):
    """ROI = value per hour of work per week until it pays off. So a quick,
    high-value, fast-paying fix outranks a big, slow, expensive one — which is
    what 'the best use of your next hour' actually means."""
    cat = t["category"]
    # Impact in $-equivalent points. Real measured revenue is trusted as-is.
    # Otherwise use a traffic proxy WEIGHTED by the page's commercial intent, so
    # informational reach doesn't masquerade as revenue.
    w = ASSET_INTENT_WEIGHT.get((t.get("asset_type") or "other").lower(), 0.6)
    if t["expected_value"] > 0:
        # Trust the measured dollars, but discount for commercial intent when
        # ranking: recovering clicks/conversions on an INFORMATIONAL page (a
        # clickbait blog post that drives top-of-funnel traffic) is worth less to
        # a store than the same dollars on a product/category page, because that
        # traffic rarely completes a purchase. Commercial pages keep full weight
        # (capped at 1.0 so products don't get artificially inflated); only
        # informational assets are marked down. The DISPLAYED payoff stays the
        # honest measured figure — this only affects where it ranks.
        impact = t["expected_value"] * min(1.0, w)
    else:
        yld = REACH_YIELD.get(cat, 1.0)
        impact = max(t["reach"] * 0.01 * w, CATEGORY_BASE.get(cat, 5) * w) * yld
    hours = EFFORT_HOURS.get(cat, 2.0)
    weeks = max(1.0, t["time_to_impact_days"] / 7.0)
    roi = impact / (hours * weeks)
    t["roi"] = round(roi, 2)
    t["is_quick"] = cat in QUICK_CATS
    # Absolute impact ($-equivalent), independent of how cheap/fast it is — so a
    # trivial-but-instant task can't masquerade as a top priority.
    t["impact"] = round(impact, 1)
    # LEVER score = how the headline list is ranked. For position/traffic plays the
    # value is future clicks, not this month's dollars, so credit them by their
    # reach (a page at pos 8-14 moved into the top 5 realistically converts ~2% of
    # its impressions to clicks) — otherwise a $10 CTR harvest outranks a
    # 1,000-impression winnable page, which is exactly the bug we're fixing.
    if cat in ("winnable", "striking", "content", "orphan", "links", "consolidation"):
        t["lever_score"] = round(max(impact, (t.get("reach") or 0) * 0.02), 1)
    elif cat == "ctr" and (t.get("expected_clicks") or 0) >= 10:
        # A big recoverable click volume is a real lever even when the $-figure
        # is tiny (small-site CVR×AOV understates a 40-click/mo recovery).
        t["lever_score"] = round(max(impact, t["expected_clicks"] * 0.4), 1)
    else:
        t["lever_score"] = round(impact, 1)
    # URGENCY: a page that's actively SLIPPING (position momentum down) is more
    # time-sensitive than a static one — defend it before it falls off page 2. Scale
    # the bump by HOW FAR it dropped: a 3-position wobble is a nudge (~1.3x), a
    # 12-position crash is a real alarm (~2.2x), capped so it can't run away.
    if t.get("trend") == "down":
        drop = abs(t.get("position_delta") or 3)
        t["lever_score"] = round(t["lever_score"] * (1.0 + min(1.2, drop / 10.0)), 1)
    # WINNABILITY: if the live SERP shows marketplaces (Amazon/Etsy) own the top 3,
    # organic displacement is unrealistic — deprioritize so effort goes to beatable
    # queries. (Only demotes; a "beatable" verdict is left at full weight.)
    if t.get("serp_verdict") == "hard":
        t["lever_score"] = round(t["lever_score"] * 0.6, 1)
    # CTR HAIRCUT: SERP features above organic (AI Overview, featured snippet,
    # shopping pack…) siphon clicks a better position would otherwise earn, so the
    # realistic payoff of a position play is lower than the reach implies. Haircut
    # the lever score by the combined suppression, and record the factor + why so
    # the UI can explain the demotion honestly. Only applies to position/traffic
    # plays that carry live SERP data.
    _feat = t.get("serp_features") or []
    if _feat and cat in ("winnable", "striking", "content", "orphan"):
        _hc = _serp_ctr_haircut(_feat)
        if _hc < 1.0:
            t["serp_ctr_haircut"] = _hc
            t["lever_score"] = round(t["lever_score"] * _hc, 1)
    # MINOR = a real but trivial cheap harvest (a $10 brand-CTR nudge, a phantom
    # schema count) that must never headline. Genuine levers are exempt.
    t["minor"] = (cat not in NEVER_MINOR) and (
        (impact < MINOR_IMPACT_FLOOR and (t.get("expected_clicks") or 0) < 10)
        or t.get("is_brand_ctr", False))
    # Second lens: strategic/compounding value. Scales with the AUDIENCE a task
    # builds or unlocks (sqrt-damped so a huge page doesn't dominate), weighted by
    # how durable that value is. Deliberately independent of near-term ROI so a
    # slow-but-compounding play (new content, a structural fix) can score high here
    # while scoring low on ROI — which is exactly the signal ROI alone misses.
    sw = STRATEGIC_WEIGHT.get(cat, 0.4)
    t["strategic_score"] = round(sw * (max(t["reach"], 1) ** 0.5), 1)
    # MEASURABILITY: on a low-demand page NO per-task verdict can ever be read —
    # the effect sits below the noise floor no matter how good the fix is. Say so
    # on the card instead of promising a measurement that will come back
    # inconclusive. Aggregate/systemic tasks are exempt (their effect shows at
    # the site level), as is anything with measured revenue.
    _aggregate = cat in ("links", "consolidation", "reviews", "brand", "content", "cro")
    t["measurable"] = bool(
        t["expected_value"] > 0 or (t.get("reach") or 0) >= 300 or _aggregate
        # A CTR fix expected to recover a real click volume clears the 20-click
        # sample on its own, whatever the raw impression count.
        or (t.get("expected_clicks") or 0) >= 10)
    if not t["measurable"]:
        t["yield_note"] = (
            f"Heads-up: this page gets ~{t.get('reach') or 0} impressions/28d — below "
            "the floor where any per-task verdict is readable. Do it only if it takes "
            "minutes, batch it with similar fixes, and don't expect the closed card "
            "to prove anything either way.")
        if cat not in NEVER_MINOR:
            t["minor"] = True
    # A one-line, honest "why this rank".
    fast = t["time_to_impact_days"] <= 21
    cheap = hours <= 0.6
    big = t["expected_value"] >= 200
    if cat == "links":
        t["rank_reason"] = "The constraint everything else runs into — authority"
    elif cat == "consolidation":
        t["rank_reason"] = "Structural fix — stops your own pages competing"
    elif not t["measurable"]:
        t["rank_reason"] = "Tiny page — batch it; won't be individually measurable"
    elif cheap and (big or fast):
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
                      content=None, orphans=None, pruning=None, geo=None,
                      winnable=None, links_info=None, results=None, limit=60):
    """Aggregate EVERY subsystem into one ranked, do-this-next list."""
    out = []
    dup_losers = set()
    if links_info:
        _from_outreach(links_info, striking, out)
    if results:
        dup_losers = _from_duplicates(results, out) or set()
    if ctr:
        _from_ctr(ctr, out)
    if cro:
        _from_cro(cro, out)
    if reviews:
        _from_reviews(reviews, out)
    if rich:
        _from_rich(rich, out)
    if brand_merchant:
        _from_merchant(brand_merchant, out)
    if striking:
        _from_striking(striking, out)
    if winnable:
        _from_winnable(winnable, out)
    if decay:
        _from_decay(decay, out)
    if content:
        _from_content(content, out)
    if orphans:
        _from_orphans(orphans, out)
    if pruning:
        _from_pruning(pruning, out)
    if geo:
        _from_geo(geo, out)
    if brand_merchant:
        _from_brand(brand_merchant, out)

    # Tasks aimed at a losing duplicate variant are superseded by the
    # consolidation redirect — linking to or retitling a page that's about to
    # 301 away is wasted work, and the winner keeps its own tasks.
    if dup_losers:
        out = [t for t in out
               if t["category"] == "consolidation" or t.get("url") not in dup_losers]

    for t in out:
        _score_task(t)
    # Order the whole plan the way the page reads it: genuine levers first (by real
    # impact), trivial "minor" harvests last — so the #N badge on each card ascends
    # with importance instead of the old ROI order (which put a #54 content gap at
    # the very top of "your biggest levers").
    out.sort(key=lambda t: (t.get("minor", False), -(t.get("lever_score") or 0)))
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

    def verdict(now, base, higher_better=True, up="improved", tweak_msg="",
                min_sample=None):
        if base in (None, 0) and now:
            return {"status": "improved", "detail": f"Now {now} (was ~0).", "tweak": ""}
        if base in (None, 0):
            return {"status": "no_change", "detail": "No measurable change yet.", "tweak": tweak_msg}
        # Sample-size honesty for COUNT metrics (clicks): a percentage on
        # single-digit counts is noise (2→3 reads "+50%"). Below the floor,
        # never claim improved/worse. Rate metrics (CVR%) pass no floor.
        if min_sample and (abs(base) + abs(now)) < min_sample:
            return {"status": "no_change",
                    "detail": f"From {base} to {now} — sample too small to call "
                              "either way; treat as no change.", "tweak": tweak_msg}
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
                                 "or get review stars showing so the listing stands out.",
                       min_sample=20)
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
                                 "what currently ranks, and add fresh internal links.",
                       min_sample=20)
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
                                 "directly, and is internally linked.",
                       min_sample=20)
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
