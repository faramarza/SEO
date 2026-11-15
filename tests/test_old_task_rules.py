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
