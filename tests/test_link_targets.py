"""Link Targets tests: grouping, gap math, verdicts (including the two
'links are NOT the answer' verdicts), ranking, and the compute pipeline
with stubbed SERP/backlink providers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis import link_targets as lt   # noqa: E402


def _row(url, query, impr, pos=12.0, lever="external"):
    return {"url": url, "query": query, "impressions": impr, "position": pos,
            "lever": lever, "asset_type": "category", "upside_clicks": 2}


def test_build_targets_groups_by_page():
    rows = [_row("http://x/a.html", "kw one", 500),
            _row("http://x/a.html", "kw two", 900),
            _row("http://x/b.html", "kw three", 300),
            _row("http://x/c.html", "internal kw", 999, lever="internal")]
    ts = lt.build_targets(rows)
    assert [t["url"] for t in ts] == ["http://x/a.html", "http://x/b.html"]
    a = ts[0]
    assert a["total_impressions"] == 1400
    assert a["keywords"][0]["query"] == "kw two"  # highest impressions first


def test_gap_verdict_single_number_on_median():
    # median of [9,14,31] is 14; own 2 → aim for ~12, ONE number.
    v, lo, hi, note = lt.gap_verdict(own_rd=2, comp_rds=[9, 14, 31],
                                     emulable_slots=3, total_slots=8)
    assert v == "authority_gap" and lo == hi == 12
    assert "about 12 more" in note


def test_gap_verdict_close():
    v, lo, hi, _ = lt.gap_verdict(own_rd=10, comp_rds=[9, 12, 14],
                                  emulable_slots=3, total_slots=8)
    assert v == "close" and lo == hi == 2  # median 12 - 10


def test_gap_verdict_parity_says_links_not_the_fix():
    v, lo, hi, note = lt.gap_verdict(own_rd=40, comp_rds=[9, 14, 31],
                                     emulable_slots=3, total_slots=8)
    assert v == "parity" and lo == hi == 0
    assert "content/relevance" in note


def test_gap_verdict_mixed_when_dispersed():
    # A 3-link page and a 9000-link page both rank → link count isn't the
    # lever; refuse to invent a target.
    v, lo, hi, note = lt.gap_verdict(own_rd=5, comp_rds=[3, 40, 9000],
                                     emulable_slots=3, total_slots=8)
    assert v == "mixed" and lo == hi == 0
    assert "isn't what's sorting" in note


def test_gap_verdict_zero_page_links_needs_domain_data():
    # Competitor pages with ~no direct links must NOT produce a parity
    # verdict from page counts alone (the bug this model version fixes).
    v, _, _, note = lt.gap_verdict(own_rd=41, comp_rds=[0, 0, 0],
                                   emulable_slots=3, total_slots=8)
    assert v == "unknown" and "domain-level" in note.lower()


def test_gap_verdict_domain_gap_reachable():
    # domain median of [180,200,210] is 200; own 160 → ~40 (<=60) reachable.
    v, lo, hi, note = lt.gap_verdict(own_rd=1, comp_rds=[0, 0, 1],
                                     emulable_slots=3, total_slots=8,
                                     own_dom_rd=160,
                                     comp_dom_rds=[180, 200, 210])
    assert v == "domain_gap" and lo == hi == 40
    assert "DOMAIN authority" in note and "any strong page" in note.lower()


def test_gap_verdict_out_of_reach():
    # a 750-domain gap is a wall, not a target.
    v, lo, hi, note = lt.gap_verdict(own_rd=1, comp_rds=[0, 0, 1],
                                     emulable_slots=3, total_slots=8,
                                     own_dom_rd=150,
                                     comp_dom_rds=[300, 900, 1200])
    assert v == "out_of_reach" and lo == hi == 0
    assert "long-tail" in note


def test_gap_verdict_domain_parity():
    v, _, _, note = lt.gap_verdict(own_rd=1, comp_rds=[0, 0, 0],
                                   emulable_slots=3, total_slots=8,
                                   own_dom_rd=500, comp_dom_rds=[120, 300, 450])
    assert v == "parity" and "NOT the constraint" in note


def test_model_version_invalidates_old_measurements():
    from datetime import datetime
    entry = {"computed_at": datetime.now().isoformat(), "model": 1}
    assert not lt.is_fresh(entry)
    entry["model"] = lt.MODEL_VERSION
    assert lt.is_fresh(entry)


def test_gap_verdict_marketplace_locked():
    v, _, _, note = lt.gap_verdict(own_rd=2, comp_rds=[],
                                   emulable_slots=0, total_slots=8)
    assert v == "marketplace_locked" and "Amazon" in note


def test_rank_targets_effort_order():
    ts = [{"verdict": "marketplace_locked", "total_impressions": 9000},
          {"verdict": "authority_gap", "total_impressions": 500},
          {"verdict": "close", "total_impressions": 100},
          {"verdict": "parity", "total_impressions": 8000}]
    ranked = lt.rank_targets(ts)
    assert [t["verdict"] for t in ranked] == \
        ["close", "authority_gap", "parity", "marketplace_locked"]


def test_is_emulable():
    assert lt.is_emulable("https://sensoryedge.com/rugs")
    assert not lt.is_emulable("https://www.amazon.com/dp/B0X")
    assert not lt.is_emulable("https://www.etsy.com/listing/1")
    assert not lt.is_emulable("https://pinterest.com/pin/2")


def test_compute_target_pipeline():
    target = {"url": "https://x.com/montessori-toys.html",
              "keywords": [{"query": "montessori toys", "position": 14,
                            "impressions": 1976}],
              "total_impressions": 1976, "asset_type": "category"}

    def serp(q):
        return {"organic_results": [
            {"position": 1, "url": "https://www.amazon.com/s?k=x"},
            {"position": 2, "url": "https://compa.com/toys"},
            {"position": 3, "url": "https://x.com/montessori-toys.html"},  # own
            {"position": 4, "url": "https://compb.com/guide"},
            {"position": 5, "url": "https://compc.com/list"},
        ], "serp_features": ["shopping"]}

    rd_counts = {"https://x.com/montessori-toys.html": 3,
                 "https://compa.com/toys": 12, "https://compb.com/guide": 8,
                 "https://compc.com/list": 20}

    def rd(u):
        return {"available": True, "count": rd_counts[u]}

    t = lt.compute_target(target, serp, rd)
    assert t["verdict"] == "authority_gap"
    assert t["own_rd"] == 3
    assert t["gap_lo"] == t["gap_hi"]  # single number (median-anchored)
    assert len(t["competitors"]) == 3
    assert t["model"] == lt.MODEL_VERSION


def test_compute_target_domain_level_path():
    target = {"url": "https://x.com/montessori-toys.html",
              "keywords": [{"query": "montessori toys", "position": 14,
                            "impressions": 1976}]}

    def serp(q):
        return {"organic_results": [
            {"position": 1, "url": "https://compa.com/toys"},
            {"position": 2, "url": "https://compa.com/other"},   # dup domain
            {"position": 3, "url": "https://compb.com/guide"},
            {"position": 4, "url": "https://compc.com/list"},
        ]}

    rd_map = {"https://x.com/montessori-toys.html": 41,
              "https://compa.com/toys": 0, "https://compb.com/guide": 0,
              "https://compc.com/list": 1,
              "x.com": 150, "compa.com": 900, "compb.com": 300,
              "compc.com": 2400}

    def rd(u):
        return {"available": True, "count": rd_map[u]}

    t = lt.compute_target(target, serp, rd)
    # Duplicate competitor domain removed; page zeros → domain-level verdict.
    assert [c["domain"] for c in t["competitors"]] == \
        ["compa.com", "compb.com", "compc.com"]
    assert t["verdict"] == "out_of_reach"  # 750-domain gap is a wall
    assert t["own_domain_rd"] == 150


def test_compute_target_budget_exhaustion_is_visible():
    target = {"url": "https://x.com/a.html",
              "keywords": [{"query": "kw", "position": 10, "impressions": 500}]}
    t = lt.compute_target(dict(target), lambda q: None, lambda u: {})
    assert t["verdict"] == "pending" and "retry" in t["note"].lower()
    t2 = lt.compute_target(
        dict(target),
        lambda q: {"organic_results": [{"position": 1, "url": "https://c.com/p"}]},
        lambda u: {"available": False, "reason": "daily cap reached (30/day)"})
    assert t2["verdict"] == "pending" and "cap" in t2["note"]


def test_gap_from_ahrefs_real_data():
    # Mirrors the montessori-toys SERP overview: your page 55, competitors 37/25,
    # a marketplace and a 563-domain brand homepage that get handled.
    csv_text = (
        "Position,URL,Domains\n"
        "2,https://montessorigeneration.com/,563\n"
        "3,https://www.amazon.com/montessori-toys,9000\n"
        "5,https://hazelandfawn.com/collections/montessori-toys,37\n"
        "8,https://montessorimethod.com/toys/,25\n"
        "14,https://alphabet-trains.com/montessori-toys.html,55\n")
    t = {"url": "https://alphabet-trains.com/montessori-toys.html",
         "keywords": [{"query": "montessori toys"}]}
    out = lt.gap_from_ahrefs(t, csv_text, "alphabet-trains.com")
    assert out["own_rd"] == 55
    # amazon excluded; competitors 563/37/25 → median 37; you have 55 > 37 → parity
    assert out["verdict"] == "parity", (out["verdict"], out["note"])


def test_gap_from_ahrefs_real_gap():
    csv_text = ("Position,URL,Referring Domains\n"
                "1,https://compa.com/x,90\n"
                "2,https://compb.com/y,70\n"
                "3,https://compc.com/z,80\n"
                "9,https://alphabet-trains.com/p.html,20\n")
    t = {"url": "https://alphabet-trains.com/p.html", "keywords": [{"query": "kw"}]}
    out = lt.gap_from_ahrefs(t, csv_text, "alphabet-trains.com")
    # median competitor 80, you 20 -> ~60 more, page-level authority_gap
    assert out["own_rd"] == 20 and out["gap_lo"] == 60 and out["verdict"] == "authority_gap"


def test_best_opportunity_finds_reachable_longtail():
    # Head term is parity; a long-tail keyword is a reachable authority_gap.
    gaps = [
        {"verdict": "parity", "query": "montessori toys", "impressions": 1976,
         "gap_lo": 0},
        {"verdict": "authority_gap", "query": "wooden montessori toys 2 year old",
         "impressions": 210, "gap_lo": 8},
        {"verdict": "out_of_reach", "query": "montessori", "impressions": 5000,
         "gap_lo": 0},
    ]
    opp = lt.best_opportunity(gaps)
    assert opp and opp["query"] == "wooden montessori toys 2 year old"
    assert opp["gap_lo"] == 8


def test_best_opportunity_none_when_all_parity():
    gaps = [{"verdict": "parity", "impressions": 100},
            {"verdict": "out_of_reach", "impressions": 50},
            {"verdict": "mixed", "impressions": 30}]
    assert lt.best_opportunity(gaps) is None


def test_best_opportunity_prefers_feasibility_then_impressions():
    # A 'close' (cheaper) win outranks an 'authority_gap' even with fewer impr.
    gaps = [{"verdict": "authority_gap", "impressions": 900, "gap_lo": 40},
            {"verdict": "close", "impressions": 100, "gap_lo": 3}]
    assert lt.best_opportunity(gaps)["verdict"] == "close"


def test_compute_page_gaps_measures_multiple_keywords():
    target = {"url": "https://x.com/p.html", "keywords": [
        {"query": "head term", "position": 12, "impressions": 2000},
        {"query": "long tail", "position": 9, "impressions": 200}]}

    serps = {
        "head term": {"organic_results": [
            {"position": 1, "url": "https://compa.com/a"},
            {"position": 2, "url": "https://compb.com/b"}]},
        "long tail": {"organic_results": [
            {"position": 1, "url": "https://smalla.com/a"},
            {"position": 2, "url": "https://smallb.com/b"}]},
    }
    rd = {"https://x.com/p.html": 20,
          "https://compa.com/a": 90, "https://compb.com/b": 80,
          "https://smalla.com/a": 25, "https://smallb.com/b": 28}

    gaps, budget_out = lt.compute_page_gaps(
        target, lambda q: serps.get(q), lambda u: {"available": True, "count": rd[u]})
    assert not budget_out and len(gaps) == 2
    assert gaps[0]["query"] == "head term" and gaps[1]["query"] == "long tail"
    # head: median 85 vs 20 -> big gap (out_of_reach); long-tail: median ~26 vs
    # 20 -> a small reachable gap.
    assert gaps[1]["verdict"] in lt.REACHABLE_VERDICTS


def test_compute_page_gaps_stops_on_budget():
    target = {"url": "https://x.com/p.html", "keywords": [
        {"query": "k1", "impressions": 500}, {"query": "k2", "impressions": 400},
        {"query": "k3", "impressions": 300}]}

    def serp(q):
        if q == "k1":
            return {"organic_results": [{"position": 1, "url": "https://c.com/p"}]}
        return {"organic_results": [{"position": 1, "url": "https://c.com/p"}]}

    def rd(u):
        return {"available": False, "reason": "daily cap reached (30/day)"}

    gaps, budget_out = lt.compute_page_gaps(target, serp, rd)
    assert budget_out and len(gaps) == 1  # stopped after the first pending


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
