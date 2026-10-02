"""Tests for the Start Here engine: interlink feed, cross-feed dedupe, no cap."""
from src.analysis import action_plan as ap


def _il_group(family, src, demand, n_links=1, orphans=1):
    links = [{"target_url": f"{src}#t{i}", "target_title": f"T{i}", "anchor": f"{family} {i}",
              "direction": "spoke_to_hub", "fixes_orphan": i < orphans} for i in range(n_links)]
    return {"source_url": src, "source_title": src.split("/")[-1], "source_type": "category",
            "family": family, "demand": demand, "link_count": n_links, "priority_rank": 3, "links": links}


def test_one_task_per_cluster_shows_top_five_with_counter():
    # A cluster missing many links → ONE task showing only the top 5, with the
    # remaining count — not a wall, and not hundreds of cards.
    interlinks = {"groups": [_il_group("montessori toys", "https://x.com/hub.html", 65955,
                                       n_links=187, orphans=28)]}
    plan = ap.build_action_plan(interlinks=interlinks)
    il = [t for t in plan if t["category"] == "interlink"]
    assert len(il) == 1                               # ONE task for the whole cluster
    link_count = sum(len(p["links"]) for p in il[0]["interlink"]["pages"])
    assert link_count == 5                            # only the top 5 shown
    assert il[0]["interlink"]["total"] == 187
    assert il[0]["interlink"]["remaining_after"] == 182
    assert "182 more to go" in il[0]["title"]
    # Orphan rescues are shown first.
    first_link = il[0]["interlink"]["pages"][0]["links"][0]
    assert first_link["fixes_orphan"] is True


def test_clusters_ranked_by_demand_and_queue_stays_short():
    interlinks = {"groups": [
        _il_group("classroom rugs", "https://x.com/rug.html", 20052, n_links=400, orphans=170),
        _il_group("montessori toys", "https://x.com/mt.html", 65955, n_links=187, orphans=28),
        _il_group("name trains", "https://x.com/nt.html", 14210, n_links=58, orphans=14),
    ]}
    plan = ap.build_action_plan(interlinks=interlinks)
    il = [t for t in plan if t["category"] == "interlink"]
    assert len(il) == 3                               # one per cluster — short queue
    assert "montessori toys" in il[0]["title"].lower()   # 65k cluster leads
    assert il[0]["tier"] == "critical"


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
    # 80 clusters → the old limit=60 would truncate the plan; now all survive.
    interlinks = {"groups": [_il_group(f"family {i}", f"https://x.com/p{i}.html", 2000 + i)
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
