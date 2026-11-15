import json
from datetime import datetime, timezone
from scripts.product_demand import page_action, owner_page, norm_tokens


def test_page_action():
    s = [{"search": "wooden toy chest", "volume": 3600, "you": 3}, {"search": "wooden toy box", "volume": 4400, "you": 6}]
    a, step = page_action(s, "Personalized Toy Chest with Free Shipping (2026)",
                          "A Solid Wood Toy Chest Engraved with Your Child's Name")
    assert a == "retitle" and "wooden toy chest" in step
    a, _ = page_action(s, "Personalized Wooden Toy Chest for Kids", "")
    assert a == "links"
    assert norm_tokens("classroom carpets") == norm_tokens("Classroom Rugs")


def test_owner_page_prefers_specific_category():
    products = [{"url": "/a.html", "name": "A", "cats": [1, 2]}, {"url": "/b.html", "name": "B", "cats": [1, 2]}]
    cats = {1: {"name": "Classroom Rugs", "active": True, "url_key": "classroom-rugs"},
            2: {"name": "Alphabet Rugs", "active": True, "url_key": "alphabet-themed-carpets"}}
    url, name, kind = owner_page([0, 1], products, cats, {1: 700, 2: 105})
    assert url == "/alphabet-themed-carpets.html" and kind == "category"
    assert owner_page([0], products, cats, {})[2] == "product"


def test_plan_feeds_governor(tmp_path):
    from src.analysis.action_plan import _from_product_demand
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(), "pages": [
        {"page": "/personalized-toy-chest.html", "page_name": "Personalized Toy Chest", "kind": "product",
         "action": "retitle", "step": "Title and H1 don't say ...", "title": "Personalized Toy Chest",
         "value": 141.0, "searches": [{"search": "wooden toy box", "volume": 4400, "you": 6}]}]}))
    out = []
    _from_product_demand(out, path=p)
    assert len(out) == 1 and out[0]["category"] == "demand" and "wooden toy box" in out[0]["title"]
