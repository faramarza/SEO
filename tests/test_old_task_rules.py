from src.analysis.action_plan import build_action_plan, _from_pruning, _from_orphans, _from_ctr


def test_outreach_and_merchant_tasks_are_gone():
    plan = build_action_plan(links_info={"tier12": 5, "drafted": 2},
                             brand_merchant={"merchant": {"rows": [{"url": "https://x/a.html", "missing": ["Offer"], "impressions": 900}]}})
    cats = {t["category"] for t in plan}
    assert "links" not in cats and "merchant" not in cats


def test_pruning_only_blog_and_never_kept_pages():
    out = []
    _from_pruning([{"url": "https://x/tag-toys-dollhouse-discontinued.html", "impressions": 1},
                   {"url": "https://x/kids-tables.html", "impressions": 1},
                   {"url": "https://x/blog/old-thin-post", "impressions": 2}], out)
    assert [t["url"] for t in out] == ["https://x/blog/old-thin-post"]


def test_orphans_skip_dead_pages_and_explain_plainly():
    out = []
    _from_orphans({"orphans": [{"url": "https://x/family-doll-house-furniture.html", "title": "TAG Toys Family Dollhouse Furniture", "impressions": 50},
                               {"url": "https://x/sensory-toys.html", "title": "Sensory Toys", "impressions": 300}]}, out)
    assert [t["url"] for t in out] == ["https://x/sensory-toys.html"]
    assert "orphan" not in out[0]["title"].lower()


def test_ctr_title_advice_has_no_tails_and_says_where():
    out = []
    _from_ctr({"rows": [{"url": "https://x/a.html", "query": "wooden toy chest", "position": 3,
                         "reasons": ["title doesn't clearly match the query"], "lost_clicks": 60,
                         "impressions": 900}]}, out)
    text = " ".join(out[0]["steps"])
    assert "no tails" in text and "Catalog > " in text and "free shipping, or" not in text


def test_clean_anchor():
    from src.analysis.action_plan import clean_anchor
    assert clean_anchor("Montessori Furniture for Child-Sized Independence", "Kids Furniture", "spoke_to_hub") == "kids furniture"
    assert clean_anchor("Playroom Furniture – Perfect for Fun, Organization, and Creativity", "kids furniture", "hub_to_spoke") == "playroom furniture"
    assert len(clean_anchor("Best Montessori Toys for Two Year Olds Who Love to Build", "", "x").split()) <= 5


def test_budget_advice_uses_each_campaigns_own_break_even():
    from src.data_sources.ads_manager import recommend_paid_actions
    camp = [{"campaign_id": 1, "name": "Name Trains Search-2026", "status": "ENABLED",
             "cost": 200, "conversions": 5, "roas": 4.5}]
    flat = recommend_paid_actions(camp, break_even_roas=4.0)
    assert flat and "scale up" in flat[0]["rationale"]
    own = recommend_paid_actions(camp, break_even_roas=4.0, break_even_for=lambda c: 5.3)
    assert not any(r.get("params", {}).get("direction") == "increase" for r in own)


def test_campaign_names_map_to_their_line():
    import json
    from pathlib import Path
    from src.analysis.paid_search import line_for
    cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "defaults.json").read_text())
    assert line_for("PMax Name Trains", "", cfg) == "name_trains"
    assert line_for("PMAX - Montessori Toys", "", cfg) == "montessori"
    assert line_for("Carpets PMax", "", cfg) == "carpets"
    assert line_for("Brand Search", "", cfg) is None
