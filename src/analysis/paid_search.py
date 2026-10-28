"""Paid Search agent — a weekly, READ-ONLY report that tells the store owner where
their Google Ads money is working, leaking, or overlapping with free organic
traffic. It never changes Google Ads, Magento, or anything else — it only suggests;
the owner applies changes by hand.

Everything in this module is a PURE function: it takes already-fetched data
(Google Ads Sheet rows, GSC query data, GA4 paid data) plus config, and returns
plain dicts. No network, no I/O, no side effects — so the money math is unit-tested
in isolation. The six outputs:

  1. new_keyword_candidates  — organic queries driving clicks to ad-eligible
                               product pages that aren't bid on yet.
  2. paying_for_free_traffic — ad search terms where we ALSO rank organically at
                               position <= 2 (query-level aggregate position).
  3. negative_keyword_suggestions — search terms spending over a threshold with
                               zero conversions.
  4. campaign_profit         — cost, conversion value, ROAS vs break-even for the
                               campaign's product line, estimated profit, and a
                               reconciliation of Ads conversions vs GA4 google/cpc
                               purchases.
  5. headline_ideas          — highest organic-CTR queries/titles as <=30-char ad
                               headline candidates (data-only, never invented).
  6. device_split            — GA4 paid conversion rate by device.

Plus a tax-inclusion check: if Google Ads' conversion value looks like it includes
sales tax, the report says so, because the owner's margins are on pre-tax revenue.

Guiding rule from the rest of Governor: NEVER fabricate. When an input is missing,
a section returns empty with an honest note rather than a guessed number.
"""

from __future__ import annotations

import re
from typing import Optional


# ── Config helpers ───────────────────────────────────────────────────────────

def _ps_config(config: dict) -> dict:
    return ((config or {}).get("paid_search") or {})


def _card_fee(config: dict) -> float:
    return float(_ps_config(config).get("card_fee", 0.03) or 0.0)


def _thresholds(config: dict) -> dict:
    t = _ps_config(config).get("thresholds") or {}
    return {
        "negative_kw_min_spend": float(t.get("negative_kw_min_spend", 5.0)),
        "attribution_gap_pct": float(t.get("attribution_gap_pct", 25)),
        "free_traffic_max_position": float(t.get("free_traffic_max_position", 2.0)),
        "headline_max_chars": int(t.get("headline_max_chars", 30)),
        "tax_match_tolerance_pct": float(t.get("tax_match_tolerance_pct", 2.0)),
    }


def _product_lines(config: dict) -> dict:
    return _ps_config(config).get("product_lines") or {}


def _default_margin(config: dict) -> float:
    return float(_ps_config(config).get("default_margin", 0.15) or 0.15)


# ── Money math (the unit-tested core) ────────────────────────────────────────

def break_even_roas(margin: float, card_fee: float) -> Optional[float]:
    """The ROAS (conversion value / cost) at which an ad campaign exactly breaks
    even, given a gross margin already net of shipping and a card-processing fee.
    Profit = conv_value*(margin - fee) - cost; profit = 0 when conv_value/cost =
    1/(margin - fee). Returns None when margin <= fee (no ad spend can ever profit)."""
    net = float(margin) - float(card_fee)
    if net <= 0:
        return None
    return 1.0 / net


def estimated_profit(conversion_value: float, cost: float,
                     margin: float, card_fee: float) -> float:
    """Gross profit from a campaign's attributed conversions, net of the card fee
    and the ad cost. Margin is on (pre-tax) revenue, so the caller must pass a
    tax-exclusive conversion_value for this to be honest."""
    return float(conversion_value) * (float(margin) - float(card_fee)) - float(cost)


def roas(conversion_value: float, cost: float) -> Optional[float]:
    """Return-on-ad-spend = conversion value / cost. None when cost is 0 (undefined,
    not infinite — a campaign with spend 0 has no ROAS to judge)."""
    cost = float(cost)
    if cost <= 0:
        return None
    return float(conversion_value) / cost


# ── Line mapping (URL pattern, with campaign-name override) ───────────────────

def _norm(s: str) -> str:
    return (s or "").strip().lower()


def line_for(campaign: str, landing_url: str, config: dict) -> Optional[str]:
    """Map a campaign / landing page to a product-line key. Campaign-name override
    wins (it's the only signal for search-term rows, which have no landing page);
    otherwise match the landing URL against each line's url_patterns. None when
    nothing matches (caller falls back to the default margin, honestly flagged)."""
    overrides = {_norm(k): v for k, v in
                 (_ps_config(config).get("campaign_line_overrides") or {}).items()}
    camp = _norm(campaign)
    if camp in overrides:
        return overrides[camp]
    url = _norm(landing_url)
    if url:
        for line, spec in _product_lines(config).items():
            for pat in (spec.get("url_patterns") or []):
                if _norm(pat) and _norm(pat) in url:
                    return line
    return None


def margin_for(line: Optional[str], config: dict) -> tuple[float, bool]:
    """(margin, is_known) for a line key. Unknown line -> default margin, is_known
    False so the report can flag 'margin assumed'."""
    lines = _product_lines(config)
    if line and line in lines:
        return float(lines[line].get("margin", _default_margin(config))), True
    return _default_margin(config), False


# ── Keyword normalization (match term <-> bid keyword) ────────────────────────

def normalize_keyword(kw: str) -> str:
    """Strip match-type decoration ([exact], "phrase", +broad) and collapse spaces
    so a search term can be compared to a bid keyword regardless of match type. The
    broad-match '+' modifier prefixes each word, so it's removed at every word
    boundary, not just at the ends."""
    s = _norm(kw)
    s = s.replace("[", "").replace("]", "").replace('"', "")
    s = re.sub(r"(^|\s)\+", r"\1", s)   # drop +broad modifiers on each token
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# ── Output 1: new keyword candidates ─────────────────────────────────────────

def new_keyword_candidates(query_page_rows: list, existing_keywords: list,
                           config: dict, limit: int = 25) -> list:
    """Organic queries (GSC) whose clicks land on an ADVERTISABLE product line's
    pages but aren't bid on yet. Advertisable lines come from config
    (keyword_candidate_lines, default name trains / books / stools — the lines whose
    break-even ROAS is low enough to be worth bidding on; Montessori/carpets are
    excluded by default). query_page_rows: [{query, page, clicks, ...}]. Ranked by
    clicks. Pages that don't map to an advertisable line are skipped."""
    have = {normalize_keyword(k) for k in (existing_keywords or [])}
    allowed = set(_ps_config(config).get("keyword_candidate_lines")
                  or ["name_trains", "books", "stools"])
    agg: dict[str, dict] = {}
    for r in (query_page_rows or []):
        q = (r.get("query") or "").strip()
        page = r.get("page") or ""
        if not q:
            continue
        line = line_for("", page, config)
        if not line or line not in allowed:
            continue  # only suggest keywords for pages tied to an advertisable line
        if normalize_keyword(q) in have:
            continue  # already an ad keyword
        clicks = int(r.get("clicks") or 0)
        cur = agg.setdefault(q, {"query": q, "clicks": 0, "lines": set(), "top_page": page})
        cur["clicks"] += clicks
        cur["lines"].add(line)
    out = [{"query": v["query"], "clicks": v["clicks"],
            "product_lines": sorted(v["lines"]), "example_page": v["top_page"]}
           for v in agg.values()]
    out.sort(key=lambda x: -x["clicks"])
    return out[:limit]


# ── Output 2: paying for free traffic (QUERY-LEVEL organic position) ──────────

def paying_for_free_traffic(search_term_rows: list, query_positions: dict,
                            config: dict, limit: int = 25) -> list:
    """Ad search terms where our aggregate organic position for that query is good
    enough (<= threshold) that the paid click is likely buying a visit we'd get for
    free. query_positions: {normalized_query: {position, clicks, impressions}} at the
    QUERY level (aggregated across pages), not query x page. Ranked by ad cost."""
    th = _thresholds(config)
    maxpos = th["free_traffic_max_position"]
    out = []
    for r in (search_term_rows or []):
        term = (r.get("search_term") or r.get("query") or "").strip()
        if not term:
            continue
        pos_rec = query_positions.get(normalize_keyword(term))
        if not pos_rec:
            continue
        pos = pos_rec.get("position")
        if pos is None or float(pos) > maxpos:
            continue
        out.append({
            "search_term": term,
            "organic_position": round(float(pos), 1),
            "ad_cost": round(float(r.get("cost") or 0.0), 2),
            "ad_clicks": int(r.get("clicks") or 0),
            "organic_clicks": int(pos_rec.get("clicks") or 0),
        })
    out.sort(key=lambda x: -x["ad_cost"])
    return out[:limit]


# ── Output 3: negative keyword suggestions ───────────────────────────────────

def negative_keyword_suggestions(search_term_rows: list, config: dict,
                                 limit: int = 25) -> list:
    """Search terms that spent more than the threshold and converted zero times —
    candidates to add as negatives. Ranked by wasted cost."""
    th = _thresholds(config)
    floor = th["negative_kw_min_spend"]
    out = []
    for r in (search_term_rows or []):
        cost = float(r.get("cost") or 0.0)
        conv = float(r.get("conversions") or 0.0)
        term = (r.get("search_term") or r.get("query") or "").strip()
        if not term or cost <= floor or conv > 0:
            continue
        out.append({
            "search_term": term,
            "wasted_cost": round(cost, 2),
            "clicks": int(r.get("clicks") or 0),
            "campaign": r.get("campaign") or "",
        })
    out.sort(key=lambda x: -x["wasted_cost"])
    return out[:limit]


# ── Output 4: per-campaign profit check + attribution reconciliation ──────────

def campaign_profit(keyword_rows: list, config: dict,
                    ga4_cpc: dict = None) -> list:
    """Per campaign: cost, conversion value, ROAS vs the break-even for its product
    line, estimated profit, and (when ga4_cpc is given) a reconciliation of Ads
    conversions against GA4 google/cpc purchases, flagged when the gap exceeds the
    threshold. keyword_rows: [{campaign, cost, conversions, conversion_value, ...}].

    conversion_value is used as given. If the tax check (see tax_note) found Ads
    value to be tax-inclusive, the caller should pass tax-adjusted rows OR read the
    banner — this function does not silently alter the number."""
    fee = _card_fee(config)
    th = _thresholds(config)
    gap_th = th["attribution_gap_pct"]

    camps: dict[str, dict] = {}
    for r in (keyword_rows or []):
        name = (r.get("campaign") or "").strip() or "(unnamed campaign)"
        c = camps.setdefault(name, {"campaign": name, "cost": 0.0,
                                    "conversions": 0.0, "conversion_value": 0.0})
        c["cost"] += float(r.get("cost") or 0.0)
        c["conversions"] += float(r.get("conversions") or 0.0)
        c["conversion_value"] += float(r.get("conversion_value") or 0.0)

    # GA4 google/cpc purchases for reconciliation (whole-channel, not per campaign:
    # GA4 can't see Google Ads campaign names reliably, so we reconcile the paid
    # channel as a whole against the sum of Ads conversions).
    ga4_purchases = None
    if ga4_cpc:
        ga4_purchases = ga4_cpc.get("purchases")

    out = []
    total_ads_conv = 0.0
    for c in camps.values():
        line = line_for(c["campaign"], "", config)
        margin, known = margin_for(line, config)
        be = break_even_roas(margin, fee)
        r_oas = roas(c["conversion_value"], c["cost"])
        profit = estimated_profit(c["conversion_value"], c["cost"], margin, fee)
        verdict = ("no_spend" if r_oas is None
                   else "profitable" if (be is not None and r_oas >= be)
                   else "losing")
        total_ads_conv += c["conversions"]
        out.append({
            "campaign": c["campaign"],
            "product_line": line,
            "margin": round(margin, 3),
            "margin_assumed": not known,
            "cost": round(c["cost"], 2),
            "conversions": round(c["conversions"], 2),
            "conversion_value": round(c["conversion_value"], 2),
            "roas": round(r_oas, 2) if r_oas is not None else None,
            "break_even_roas": round(be, 2) if be is not None else None,
            "estimated_profit": round(profit, 2),
            "verdict": verdict,
        })
    out.sort(key=lambda x: (x["estimated_profit"]))

    recon = None
    if ga4_purchases is not None:
        gap_pct = _pct_gap(total_ads_conv, ga4_purchases)
        recon = {
            "ads_conversions": round(total_ads_conv, 2),
            "ga4_cpc_purchases": round(float(ga4_purchases), 2),
            "gap_pct": round(gap_pct, 1) if gap_pct is not None else None,
            "flag": (gap_pct is not None and abs(gap_pct) > gap_th),
            "threshold_pct": gap_th,
        }
    return {"campaigns": out, "reconciliation": recon}


def _pct_gap(a: float, b: float) -> Optional[float]:
    """Signed percent gap of a vs b, relative to b. None when b is 0."""
    b = float(b)
    if b == 0:
        return None
    return (float(a) - b) / b * 100.0


# ── Tax-inclusion check ──────────────────────────────────────────────────────

def tax_note(ads_conversion_value: float, ga4_cpc: dict, config: dict) -> Optional[dict]:
    """Decide whether Google Ads' conversion value looks tax-INCLUSIVE, by comparing
    it to GA4 google/cpc purchaseRevenue (pre-tax) and purchaseRevenue + taxAmount.
    Returns a dict the report renders as a banner, or None when we can't tell
    (missing GA4 revenue/tax). Never guesses — only reports a clear match."""
    if not ga4_cpc:
        return None
    revenue = ga4_cpc.get("revenue")
    tax = ga4_cpc.get("tax")
    if revenue is None or not revenue:
        return None
    revenue = float(revenue)
    tax = float(tax or 0.0)
    adsv = float(ads_conversion_value or 0.0)
    if adsv <= 0:
        return None
    tol = _thresholds(config)["tax_match_tolerance_pct"] / 100.0
    with_tax = revenue + tax

    def close(a, b):
        return b > 0 and abs(a - b) / b <= tol

    includes_tax = tax > 0 and close(adsv, with_tax) and not close(adsv, revenue)
    excludes_tax = close(adsv, revenue)
    if includes_tax:
        eff_tax_pct = (tax / revenue * 100.0) if revenue else 0.0
        # ROAS the owner sees vs the pre-tax ROAS that matches their margins.
        return {
            "status": "includes_tax",
            "ads_conversion_value": round(adsv, 2),
            "ga4_revenue_pretax": round(revenue, 2),
            "ga4_tax": round(tax, 2),
            "effective_tax_pct": round(eff_tax_pct, 1),
            "message": (f"Google Ads conversion value (${adsv:,.0f}) matches your GA4 "
                        f"revenue PLUS ~${tax:,.0f} sales tax (~{eff_tax_pct:.0f}%). Your "
                        "margins are on pre-tax revenue, so the ROAS shown here is "
                        "flattering and profit is overstated — discount conversion value "
                        f"by ~{eff_tax_pct:.0f}% to compare against break-even."),
        }
    if excludes_tax:
        return {
            "status": "excludes_tax",
            "ads_conversion_value": round(adsv, 2),
            "ga4_revenue_pretax": round(revenue, 2),
            "message": ("Google Ads conversion value looks tax-exclusive (matches your "
                        "pre-tax GA4 revenue) — ROAS here is apples-to-apples with your "
                        "margins."),
        }
    return {
        "status": "unclear",
        "ads_conversion_value": round(adsv, 2),
        "ga4_revenue_pretax": round(revenue, 2),
        "ga4_tax": round(tax, 2),
        "message": ("Couldn't cleanly match Google Ads conversion value to GA4 revenue "
                    "with or without tax — treat ROAS with caution and check your "
                    "conversion tracking."),
    }


# ── Output 5: headline ideas (data-only, <=30 chars) ─────────────────────────

def headline_ideas(query_rows: list, page_titles: dict, config: dict,
                   limit: int = 10) -> list:
    """Ad-headline candidates taken straight from our best-performing organic
    queries and page titles — no LLM, nothing invented. Each is trimmed to the
    char limit at a word boundary and deduped. query_rows: [{query, ctr, clicks,
    page}]. page_titles: {page_url: title}. Ranked by organic CTR then clicks."""
    cap = _thresholds(config)["headline_max_chars"]
    rows = sorted((query_rows or []),
                  key=lambda r: (-float(r.get("ctr") or 0.0), -int(r.get("clicks") or 0)))
    seen, out = set(), []
    for r in rows:
        for cand in (r.get("query"), page_titles.get(r.get("page") or "")):
            h = _fit_headline(cand, cap)
            if not h:
                continue
            key = h.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "headline": h,
                "chars": len(h),
                "from_query": r.get("query"),
                "ctr_pct": round(float(r.get("ctr") or 0.0) * 100, 1),
                "clicks": int(r.get("clicks") or 0),
                "truncated": bool(cand and len(cand.strip()) > cap),
            })
            break  # one headline per query row
        if len(out) >= limit:
            break
    return out


def _fit_headline(text: str, cap: int) -> str:
    """Trim to <=cap chars at a word boundary; title-case lightly. '' if unusable."""
    s = re.sub(r"\s+", " ", (text or "").strip())
    if not s:
        return ""
    if len(s) <= cap:
        return s
    cut = s[:cap]
    if " " in cut:
        cut = cut[:cut.rstrip().rfind(" ")]
    return cut.strip()


# ── Output 6: device split ───────────────────────────────────────────────────

def device_split(ga4_devices: dict) -> list:
    """Paid-traffic conversion rate by device. ga4_devices: {device: {sessions,
    purchases}}. Returns a row per device with conversion rate, busiest first."""
    out = []
    for dev, d in (ga4_devices or {}).items():
        sessions = int(d.get("sessions") or 0)
        purchases = float(d.get("purchases") or 0.0)
        cvr = (purchases / sessions * 100.0) if sessions else 0.0
        out.append({
            "device": dev,
            "sessions": sessions,
            "purchases": round(purchases, 1),
            "conversion_rate_pct": round(cvr, 2),
        })
    out.sort(key=lambda x: -x["sessions"])
    return out


# ── Top-level assembly ───────────────────────────────────────────────────────

def build_paid_search_report(*, search_term_rows: list, keyword_rows: list,
                             existing_keywords: list, query_page_rows: list,
                             query_positions: dict, query_rows: list,
                             page_titles: dict, ga4_cpc: dict, ga4_devices: dict,
                             config: dict) -> dict:
    """Assemble the full weekly report from already-fetched inputs. Each section is
    independent, so a missing input degrades that section to empty + a note rather
    than failing the whole report. Returns a dict the web layer serializes."""
    ads_total_value = sum(float(r.get("conversion_value") or 0.0) for r in (keyword_rows or []))
    tax = tax_note(ads_total_value, ga4_cpc, config)

    notes = []
    if not (search_term_rows or keyword_rows):
        notes.append("No Google Ads data — connect the Ads export Sheet (see Setup).")
    if not query_positions:
        notes.append("No GSC query data — 'paying for free traffic' can't be computed.")
    if not ga4_cpc:
        notes.append("No GA4 google/cpc data — conversion reconciliation and device "
                     "split are unavailable.")

    return {
        "available": bool(search_term_rows or keyword_rows),
        "notes": notes,
        "tax_note": tax,
        "new_keyword_candidates": new_keyword_candidates(query_page_rows, existing_keywords, config),
        "paying_for_free_traffic": paying_for_free_traffic(search_term_rows, query_positions, config),
        "negative_keyword_suggestions": negative_keyword_suggestions(search_term_rows, config),
        "campaign_profit": campaign_profit(keyword_rows, config, ga4_cpc=ga4_cpc),
        "headline_ideas": headline_ideas(query_rows, page_titles, config),
        "device_split": device_split(ga4_devices),
    }
