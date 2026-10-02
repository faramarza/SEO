"""Tests for the Start Here engine: interlink feed, cross-feed dedupe, no cap."""
from src.analysis import action_plan as ap


def _il_group(family, src, demand, orphan=True):
    return {"source_url": src, "source_title": src.split("/")[-1], "source_type": "category",
            "family": family, "demand": demand, "link_count": 1, "priority_rank": 3 if orphan else 1,
            "links": [{"target_url": src + "#hub", "target_title": "Hub", "anchor": family,
                       "direction": "spoke_to_hub", "fixes_orphan": orphan}]}


def test_interlink_plan_feeds_start_here_one_task_per_cluster_by_demand():
    interlinks = {"groups": [
        _il_group("classroom rugs", "https://x.com/rug1.html", 20052),
        _il_group("classroom rugs", "https://x.com/rug2.html", 15000),
        _il_group("name trains", "https://x.com/train1.html", 14210),
    ]}
    plan = ap.build_action_plan(interlinks=interlinks)
    il = [t for t in plan if t["category"] == "interlink"]
    assert len(il) == 2                           # ONE task per cluster, not per page
    assert plan[0]["category"] == "interlink"
    assert "classroom rugs" in plan[0]["title"].lower()   # 20k cluster leads
    assert plan[0]["tier"] == "critical"
    assert "across 2 pages" in plan[0]["title"]


def test_contradictory_duplicate_is_collapsed_to_one():
    # Same query as a striking task (you rank #11) AND a content task (you have no
    # page) — self-contradictory. Must collapse to the single highest-value one.
    striking = [{"url": "https://x.com/p.html", "query": "best toys for imaginative play",
                 "position": 11.9, "impressions": 1269, "clicks": 2,
                 "asset_type": "blog", "reasons": []}]
    content = {"suggestions": [{"is_content_gap": True,
                                "source_query": "best toys for imaginative play",
                                "impressions": 1269, "clicks": 2}]}
    plan = ap.build_action_plan(striking=striking, content=content)
    hits = [t for t in plan
            if "imaginative play" in ((t.get("metric") or {}).get("query", "").lower()
                                      + " " + t.get("title", "").lower())]
    assert len(hits) == 1
    assert hits[0]["category"] == "striking"      # kept the one you already rank for


def test_singular_plural_queries_merge():
    striking = [
        {"url": "https://x.com/a.html", "query": "montessori toys for newborn",
         "position": 9.0, "impressions": 500, "clicks": 1, "asset_type": "category", "reasons": []},
        {"url": "https://x.com/b.html", "query": "montessori toys for newborns",
         "position": 9.0, "impressions": 400, "clicks": 1, "asset_type": "category", "reasons": []},
    ]
    plan = ap.build_action_plan(striking=striking)
    newborn = [t for t in plan if "newborn" in (t.get("metric") or {}).get("query", "")]
    assert len(newborn) == 1   # singular/plural collapsed to one


def test_no_cap_by_default():
    # 80 topic clusters → the old limit=60 would truncate the plan; now all survive.
    interlinks = {"groups": [_il_group(f"family {i}", f"https://x.com/p{i}.html", 100 + i)
                             for i in range(80)]}
    plan = ap.build_action_plan(interlinks=interlinks)
    assert len([t for t in plan if t["category"] == "interlink"]) == 80  # none dropped


def test_content_gap_feed_is_not_in_start_here():
    # Even if a content_gap plan is passed, it must NOT inject tasks (cut from the
    # queue; it lives on its own page now).
    content_gap = {"articles": [{"primary_keyword": "montessori busy board",
                                 "title": "Busy Board Guide", "total_volume": 500,
                                 "role": "supporting", "est_monthly_value": 4}]}
    plan = ap.build_action_plan(content_gap=content_gap)
    assert all("busy board" not in t.get("title", "").lower() for t in plan)
