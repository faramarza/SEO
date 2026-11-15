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


def test_how_steps_are_concrete():
    from scripts.product_demand import how_steps, suggest_title, find_link_sources
    posts = [{"name": "Circle Time Activities", "url_key": "circle-time",
              "content": "<p>A good classroom rug defines the space for circle time with young children.</p>"},
             {"name": "Already Links", "url_key": "x",
              "content": '<p>See our <a href="/classroom-rugs.html">classroom rugs</a> for circle time.</p>'}]
    src = find_link_sources(posts, "/classroom-rugs.html", "classroom rug")
    assert len(src) == 1 and src[0]["post"] == "Circle Time Activities"
    assert '<a href="/classroom-rugs.html">classroom rug</a>' in src[0]["paste"]
    g = {"action": "links", "page": "/classroom-rugs.html", "kind": "category", "page_name": "Classroom Rugs",
         "searches": [{"search": "classroom rug", "volume": 14800}]}
    steps = how_steps(g, posts)
    assert "Circle Time Activities" in steps[0] and "paste" in steps[0]
    t = suggest_title("wooden toy chest", "Personalized Toy Chest")
    assert t.startswith("Personalized Wooden Toy Chest") and "|" not in t and len(t) <= 60
    r = how_steps({"action": "retitle", "kind": "product", "page_name": "Personalized Toy Chest", "sku": "HW-Toy-Chest",
                   "searches": [{"search": "wooden toy chest", "volume": 3600}]}, [])
    assert "HW-Toy-Chest" in r[0] and "Meta Title" in r[1]
