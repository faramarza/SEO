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


def test_gap_verdict_range():
    v, lo, hi, note = lt.gap_verdict(own_rd=2, comp_rds=[9, 14, 31],
                                     emulable_slots=3, total_slots=8)
    assert v == "authority_gap" and (lo, hi) == (7, 29)
    assert "9–31" in note and "necessary, not sufficient" in note


def test_gap_verdict_close():
    v, lo, hi, _ = lt.gap_verdict(own_rd=8, comp_rds=[9, 10, 12],
                                  emulable_slots=3, total_slots=8)
    assert v == "close" and lo == 1 and hi == 4


def test_gap_verdict_parity_says_links_not_the_fix():
    v, lo, hi, note = lt.gap_verdict(own_rd=40, comp_rds=[9, 14, 31],
                                     emulable_slots=3, total_slots=8)
    assert v == "parity" and lo == hi == 0
    assert "NOT the constraint" in note


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
        return {"available": True, "links": [{}] * rd_counts[u]}

    t = lt.compute_target(target, serp, rd)
    assert t["verdict"] == "authority_gap"
    assert t["own_rd"] == 3 and t["non_emulable_slots"] == 1
    assert (t["gap_lo"], t["gap_hi"]) == (5, 17)
    assert len(t["competitors"]) == 3


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
