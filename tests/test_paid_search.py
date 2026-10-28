"""Tests for the Paid Search agent — focused on the money math (break-even, ROAS,
profit, tax detection) and the selection logic for each of the six outputs.

Pure and flask-free; run with the repo's homemade driver (no pytest in the sandbox):

    PYTHONPATH=. python3 -c "import importlib.util,sys; \
      s=importlib.util.spec_from_file_location('t','tests/test_paid_search.py'); \
      m=importlib.util.module_from_spec(s); sys.modules['t']=m; s.loader.exec_module(m); \
      [getattr(m,n)() for n in dir(m) if n.startswith('test_')]; print('ok')"
"""

from src.analysis.paid_search import (
    break_even_roas, estimated_profit, roas, normalize_keyword, line_for,
    margin_for, new_keyword_candidates, paying_for_free_traffic,
    negative_keyword_suggestions, campaign_profit, tax_note, headline_ideas,
    device_split, build_paid_search_report,
)


CFG = {
    "paid_search": {
        "card_fee": 0.03,
        "default_margin": 0.15,
        "product_lines": {
            "name_trains": {"margin": 0.30, "url_patterns": ["name-train", "letter-train"]},
            "books":       {"margin": 0.30, "url_patterns": ["book"]},
            "montessori":  {"margin": 0.15, "url_patterns": ["montessori"]},
            "carpets":     {"margin": 0.10, "url_patterns": ["rug", "carpet"]},
        },
        "keyword_candidate_lines": ["name_trains", "books", "stools"],
        "campaign_line_overrides": {"Name Trains Search-2026": "name_trains"},
        "thresholds": {
            "negative_kw_min_spend": 5.0, "attribution_gap_pct": 25,
            "free_traffic_max_position": 2.0, "headline_max_chars": 30,
            "tax_match_tolerance_pct": 2.0,
        },
    }
}


def _approx(a, b, tol=0.01):
    return abs(a - b) <= tol


# ── break-even / ROAS / profit ───────────────────────────────────────────────

def test_break_even_matches_business_numbers():
    assert _approx(break_even_roas(0.30, 0.03), 3.70, 0.01)   # name trains/books/stools
    assert _approx(break_even_roas(0.15, 0.03), 8.333, 0.01)  # montessori
    assert _approx(break_even_roas(0.10, 0.03), 14.285, 0.01) # carpets (0.10)


def test_break_even_none_when_fee_exceeds_margin():
    assert break_even_roas(0.03, 0.03) is None   # margin == fee -> never profitable
    assert break_even_roas(0.02, 0.03) is None


def test_roas_and_profit_math():
    # $100 spend returning $400 value at 30% margin, 3% fee.
    assert _approx(roas(400, 100), 4.0)
    # profit = 400*(0.30-0.03) - 100 = 108 - 100 = 8
    assert _approx(estimated_profit(400, 100, 0.30, 0.03), 8.0)
    # at break-even ROAS exactly, profit is ~0
    be = break_even_roas(0.30, 0.03)
    assert _approx(estimated_profit(100 * be, 100, 0.30, 0.03), 0.0, 0.01)
    assert roas(10, 0) is None   # no spend -> undefined, not infinite


# ── line mapping ─────────────────────────────────────────────────────────────

def test_campaign_override_wins_for_search_terms():
    # search-term rows have no landing page -> campaign override is the only signal
    assert line_for("Name Trains Search-2026", "", CFG) == "name_trains"


def test_url_mapping_and_unknown_falls_back_to_default_margin():
    assert line_for("", "https://x/montessori-shape-sorter.html", CFG) == "montessori"
    assert line_for("", "https://x/wooden-name-train.html", CFG) == "name_trains"
    assert line_for("", "https://x/mystery-widget.html", CFG) is None
    m, known = margin_for(None, CFG)
    assert m == 0.15 and known is False
    m2, known2 = margin_for("carpets", CFG)
    assert m2 == 0.10 and known2 is True


# ── keyword normalization ────────────────────────────────────────────────────

def test_normalize_keyword_strips_match_type():
    assert normalize_keyword("[name train]") == "name train"
    assert normalize_keyword('"name trains wooden"') == "name trains wooden"
    assert normalize_keyword("+personalized  +name   train") == "personalized name train"


# ── output 1: new keyword candidates ─────────────────────────────────────────

def test_new_keyword_candidates_excludes_existing_and_ranks_by_clicks():
    rows = [
        {"query": "wooden name train gift", "page": "https://x/name-train.html", "clicks": 40},
        {"query": "name train", "page": "https://x/name-train.html", "clicks": 99},  # already bid
        {"query": "montessori toy 1 year", "page": "https://x/montessori-x.html", "clicks": 12},  # not advertisable
        {"query": "random blog thing", "page": "https://x/blog/activities.html", "clicks": 80},  # no line
    ]
    out = new_keyword_candidates(rows, existing_keywords=["[name train]"], config=CFG)
    qs = [o["query"] for o in out]
    assert "wooden name train gift" in qs
    assert "name train" not in qs                 # already an ad keyword
    assert "montessori toy 1 year" not in qs      # Montessori isn't an advertisable line
    assert "random blog thing" not in qs          # page maps to no product line
    assert out[0]["clicks"] >= out[-1]["clicks"]  # ranked by clicks


# ── output 2: paying for free traffic (query-level position) ─────────────────

def test_paying_for_free_traffic_uses_query_level_position():
    terms = [
        {"search_term": "name train", "cost": 12.0, "clicks": 6},
        {"search_term": "letter train wooden", "cost": 3.0, "clicks": 2},
    ]
    positions = {
        "name train": {"position": 1.4, "clicks": 300, "impressions": 5000},
        "letter train wooden": {"position": 7.8, "clicks": 10, "impressions": 900},
    }
    out = paying_for_free_traffic(terms, positions, CFG)
    flagged = {o["search_term"] for o in out}
    assert "name train" in flagged            # pos 1.4 <= 2 -> paying for free
    assert "letter train wooden" not in flagged  # pos 7.8 -> genuinely needs the ad


# ── output 3: negatives ──────────────────────────────────────────────────────

def test_negative_keywords_spend_over_threshold_zero_conv():
    terms = [
        {"search_term": "free name train template", "cost": 9.5, "conversions": 0, "clicks": 7},
        {"search_term": "name train", "cost": 20.0, "conversions": 2, "clicks": 10},  # converts
        {"search_term": "cheap thing", "cost": 2.0, "conversions": 0, "clicks": 3},   # under $5
    ]
    out = negative_keyword_suggestions(terms, CFG)
    names = [o["search_term"] for o in out]
    assert names == ["free name train template"]   # only the >$5, 0-conv term
    assert out[0]["wasted_cost"] == 9.5


# ── output 4: campaign profit + reconciliation ───────────────────────────────

def test_campaign_profit_verdict_and_reconciliation():
    kw = [
        {"campaign": "Name Trains Search-2026", "cost": 100.0,
         "conversions": 3, "conversion_value": 500.0},
    ]
    # ROAS = 5.0, break-even for name trains = 3.70 -> profitable
    res = campaign_profit(kw, CFG, ga4_cpc={"purchases": 5})
    c = res["campaigns"][0]
    assert c["product_line"] == "name_trains"
    assert _approx(c["roas"], 5.0) and _approx(c["break_even_roas"], 3.70, 0.01)
    assert c["verdict"] == "profitable"
    # profit = 500*0.27 - 100 = 35
    assert _approx(c["estimated_profit"], 35.0)
    # reconciliation: Ads 3 vs GA4 5 -> gap = (3-5)/5 = -40% -> flagged
    recon = res["reconciliation"]
    assert recon["flag"] is True and _approx(recon["gap_pct"], -40.0)


def test_campaign_losing_when_below_break_even():
    kw = [{"campaign": "Montessori Test", "cost": 100.0,
           "conversions": 1, "conversion_value": 300.0}]  # ROAS 3.0 < 8.33
    res = campaign_profit(kw, CFG, ga4_cpc=None)
    c = res["campaigns"][0]
    assert c["product_line"] is None and c["margin_assumed"] is True   # default margin
    assert c["verdict"] == "losing"
    assert res["reconciliation"] is None   # no GA4 -> no recon, honestly absent


# ── tax detection ────────────────────────────────────────────────────────────

def test_tax_note_detects_inclusive_value():
    # Ads value 540 ≈ revenue 500 + tax 40 -> includes tax
    note = tax_note(540.0, {"revenue": 500.0, "tax": 40.0}, CFG)
    assert note["status"] == "includes_tax"
    assert _approx(note["effective_tax_pct"], 8.0, 0.1)


def test_tax_note_detects_exclusive_value_and_handles_missing():
    note = tax_note(500.0, {"revenue": 500.0, "tax": 40.0}, CFG)
    assert note["status"] == "excludes_tax"
    assert tax_note(540.0, None, CFG) is None            # no GA4 -> can't tell
    assert tax_note(540.0, {"revenue": 0, "tax": 0}, CFG) is None


# ── output 5: headlines ──────────────────────────────────────────────────────

def test_headline_ideas_respect_char_cap_and_dedupe():
    rows = [
        {"query": "personalized wooden name train set for toddlers and up", "ctr": 0.20,
         "clicks": 50, "page": "https://x/name-train.html"},
        {"query": "name train", "ctr": 0.18, "clicks": 40, "page": "https://x/name-train.html"},
    ]
    titles = {"https://x/name-train.html": "Wooden Name Trains"}
    out = headline_ideas(rows, titles, CFG)
    assert all(h["chars"] <= 30 for h in out)            # never exceeds cap
    assert out[0]["truncated"] is True                   # long query was trimmed
    assert len({h["headline"].lower() for h in out}) == len(out)  # deduped


# ── output 6: device split ───────────────────────────────────────────────────

def test_device_split_conversion_rate():
    out = device_split({
        "mobile": {"sessions": 200, "purchases": 2},
        "desktop": {"sessions": 100, "purchases": 4},
    })
    by = {o["device"]: o for o in out}
    assert _approx(by["mobile"]["conversion_rate_pct"], 1.0)
    assert _approx(by["desktop"]["conversion_rate_pct"], 4.0)
    assert out[0]["device"] == "mobile"   # busiest (most sessions) first


# ── assembly: missing inputs degrade honestly ────────────────────────────────

def test_report_degrades_without_fabricating():
    rep = build_paid_search_report(
        search_term_rows=[], keyword_rows=[], existing_keywords=[],
        query_page_rows=[], query_positions={}, query_rows=[], page_titles={},
        ga4_cpc=None, ga4_devices={}, config=CFG)
    assert rep["available"] is False
    assert any("Google Ads" in n for n in rep["notes"])
    assert rep["new_keyword_candidates"] == []
    assert rep["campaign_profit"]["campaigns"] == []
