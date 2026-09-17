"""What-if keyword tester: candidate generation, editability (ignore/add),
and on-demand measurement reusing the Link Targets engine."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis import whatif as wf  # noqa: E402


def test_core_phrase_strips_generic_lead():
    assert wf.core_phrase("kids lock box", "/x") == "lock box"
    assert wf.core_phrase("best montessori toys", "/x") == "montessori toys"
    # no query → derive from slug
    assert wf.core_phrase("", "https://x.com/little-lock-box.html") == "little lock box"


def test_suggest_candidates_grounded_first_then_niche():
    cs = wf.suggest_candidates(
        "https://x.com/little-lock-box.html",
        gsc_queries=["kids lock box", "toy lock box"],
        head_query="kids lock box")
    qs = [c["query"] for c in cs]
    # real queries appear and are tagged as grounded
    assert "kids lock box" in qs
    assert cs[0]["source"] == "your Search Console"
    # niche variants of the core phrase are generated + tagged suggested
    assert "montessori lock box" in qs
    assert "wooden lock box" in qs
    assert any(c["source"] == "suggested" for c in cs)
    # no duplicates
    assert len(qs) == len(set(qs))


def test_page_candidates_respects_ignore_and_add():
    store = {"pages": {}}
    url = "https://x.com/p.html"
    # ignore a suggestion, add a custom one
    wf._page(store, url)["removed"] = ["montessori lock box"]
    wf._page(store, url)["added"] = ["busy board lock box"]
    rows = wf.page_candidates(store, url, ["kids lock box"], "kids lock box")
    qs = [r["query"] for r in rows]
    assert "montessori lock box" not in qs          # ignored
    assert "busy board lock box" in qs              # added
    added = next(r for r in rows if r["query"] == "busy board lock box")
    assert added["source"] == "you"


def test_page_candidates_sorts_winnable_first():
    store = {"pages": {}}
    url = "https://x.com/p.html"
    p = wf._page(store, url)
    p["added"] = ["wall term", "winnable term"]
    p["removed"] = []
    p["measurements"] = {
        "wall term": {"verdict": "out_of_reach", "links_needed": 2000},
        "winnable term": {"verdict": "authority_gap", "links_needed": 8},
    }
    rows = wf.page_candidates(store, url, [], "")
    # measured rows sort by fewest links first
    measured = [r for r in rows if r.get("measurement")]
    assert measured[0]["query"] == "winnable term"


def test_measure_candidate_uses_engine_and_reports_position():
    # Own page ranks #14 (off page 1) → normal gap math applies.
    def serp(q):
        return {"organic_results": [
            {"position": 1, "url": "https://compa.com/a"},
            {"position": 2, "url": "https://compb.com/b"},
            {"position": 14, "url": "https://x.com/p.html"},
        ]}
    rd = {"https://x.com/p.html": 20, "https://compa.com/a": 45,
          "https://compb.com/b": 40}

    def rdfn(u):
        return {"available": True, "count": rd[u]}

    res = wf.measure_candidate("https://x.com/p.html", "montessori lock box", serp, rdfn)
    assert res["query"] == "montessori lock box"
    assert res["own_position"] == 14
    # standard formula: page1 median 42 vs your page's 20 → +22 (reachable)
    assert res["you"] == 20 and res["page1"] == 42 and res["links_needed"] == 22
    assert res["verdict"] == "authority_gap"


def test_measure_candidate_already_ranking_never_demands_links():
    # The '#9 but +52,000 links' bug: if the page already ranks on page 1, the
    # links-needed number must be suppressed no matter how big the (poisoned)
    # page-1 median is.
    def serp(q):
        return {"organic_results": [
            {"position": 1, "url": "https://giant.com/a"},
            {"position": 9, "url": "https://x.com/p.html"},   # own page ranks #9
        ]}
    rd = {"https://x.com/p.html": 695, "https://giant.com/a": 53029,
          "giant.com": 53029, "x.com": 695}

    def rdfn(u):
        return {"available": True, "count": rd.get(u, 0)}

    res = wf.measure_candidate("https://x.com/p.html", "childs lockable box", serp, rdfn)
    assert res["own_position"] == 9
    assert res["verdict"] == "already_ranking"
    assert res["links_needed"] == 0
    assert "already rank" in res["note"].lower()


def test_measure_candidate_not_ranking_but_outlinks_is_content_gap():
    # 'Not ranking, yet you out-link them' must NOT read as a win — it's a
    # content/relevance gap (you have the authority, not the page).
    def serp(q):
        return {"organic_results": [
            {"position": 1, "url": "https://compa.com/a"},
            {"position": 2, "url": "https://compb.com/b"},
        ]}  # own page absent → not ranking
    rd = {"https://x.com/p.html": 695, "https://compa.com/a": 200,
          "https://compb.com/b": 214, "x.com": 695, "compa.com": 200,
          "compb.com": 214}

    def rdfn(u):
        return {"available": True, "count": rd.get(u, 0)}

    res = wf.measure_candidate("https://x.com/p.html", "wooden lock box", serp, rdfn)
    assert res["own_position"] is None
    assert res["links_needed"] == 0
    assert res["verdict"] == "content_gap"
    assert "content" in res["note"].lower() and "not" in res["note"].lower()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
