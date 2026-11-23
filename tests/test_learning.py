import json

from src.analysis import learning as L
from src.analysis import scheduled_checks as sc


def test_classify_real_change_log_wording():
    assert L.classify("intro + FAQ: Kids Chairs") == "intro_faq"
    assert L.classify("new page: Baby Gifts") == "new_page"
    assert L.classify('meta title "Name Trains, Personalized, Wooden and Made in the USA"') == "retitle"
    assert L.classify('link "personalized name puzzles" -> /wooden-name-puzzles.html') == "links"
    assert L.classify("17 Guidecraft products restocked") == "assortment"


def test_rates_start_neutral_and_move_with_evidence():
    r = L.rates({})
    assert r["retitle"]["factor"] == 1.0
    grades = {str(i): {"verdict": "WORKED", "type": "intro_faq"} for i in range(6)}
    grades.update({f"r{i}": {"verdict": "DID NOT WORK", "type": "retitle"} for i in range(6)})
    grades["x"] = {"verdict": "NO DATA", "type": "retitle"}
    r = L.rates(grades)
    assert r["intro_faq"]["rate"] == round(7 / 8, 3) and r["intro_faq"]["factor"] == 1.5
    assert r["retitle"]["graded"] == 6 and r["retitle"]["factor"] == 0.5
    one = L.rates({"a": {"verdict": "WORKED", "type": "links"}})["links"]
    assert one["factor"] < 1.5   # one lucky result can't swing it all the way


def test_start_here_ranking_uses_what_was_learned(monkeypatch):
    from src.analysis import action_plan as ap
    monkeypatch.setattr(L, "factors", lambda path=None: {"retitle": 0.5, "intro_faq": 1.5, "links": 1.0,
                                                         "new_page": 1.0, "assortment": 1.0, "other": 1.0})
    def task(action):
        return {"category": "demand", "action_type": action, "expected_value": 100.0, "reach": 0,
                "asset_type": "category", "time_to_impact_days": 28}
    a, b = task("retitle"), task("content")
    ap._score_task(a)
    ap._score_task(b)
    assert b["lever_score"] == 3 * a["lever_score"] and a["learned_factor"] == 0.5


def test_control_group_removes_the_season():
    e = {"pages": ["/kids-chairs.html"], "queries": ["kids chair"]}
    ctl = {"pages": ["/kids-tables.html"], "queries": ["kids table"]}
    r = lambda p, q, pos: {"page": "https://x" + p, "query": q, "position": pos, "impressions": 500, "clicks": 5}
    before = [r("/kids-chairs.html", "kids chair", 12), r("/kids-tables.html", "kids table", 12)]
    season = [r("/kids-chairs.html", "kids chair", 9), r("/kids-tables.html", "kids table", 9)]
    assert sc.grade(e, before, season)["verdict"] == "WORKED"                # alone it looks like it worked
    assert sc.grade_with_control(e, before, season, ctl)["verdict"] == "UNCLEAR"   # but so did the untouched page
    real = [r("/kids-chairs.html", "kids chair", 6), r("/kids-tables.html", "kids table", 11)]
    g = sc.grade_with_control(e, before, real, ctl)
    assert g["verdict"] == "WORKED" and "net effect +5.0" in g["why"]


def test_choose_control():
    t = {"key": "/a.html", "template": "category", "line": "Furniture", "value": 500}
    cands = [t, {"key": "/b.html", "template": "category", "line": "Rugs", "value": 480},
             {"key": "/c.html", "template": "category", "line": "Furniture", "value": 300},
             {"key": "NEW:x", "template": "guide", "create": True, "value": 500}]
    assert L.choose_control(cands, t)["key"] == "/c.html"     # same line first
