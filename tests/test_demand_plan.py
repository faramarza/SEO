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
    assert '<a href="/classroom-rugs.html">classroom rugs</a>' in src[0]["paste"]
    g = {"action": "links", "page": "/classroom-rugs.html", "kind": "category", "page_name": "Classroom Rugs",
         "searches": [{"search": "classroom rug", "volume": 14800}]}
    steps = how_steps(g, posts)
    assert steps[0].startswith("Open:") and "Circle Time Activities" in steps[0]
    assert steps[1].startswith("Find (Ctrl+F): ") and steps[2].startswith("Paste right after it (type one space first): <a") or \
        steps[2].startswith("Paste right after it (type one space first): See our")
    assert steps[3] == "Save." and steps[-1].startswith("Done:")
    assert all(len(x) < 220 for x in steps)   # one short line each
    t = suggest_title("wooden toy chest", "Personalized Toy Chest")
    assert t.startswith("Personalized Wooden Toy Chest") and "|" not in t and len(t) <= 60
    r = how_steps({"action": "retitle", "kind": "product", "page_name": "Personalized Toy Chest", "sku": "HW-Toy-Chest",
                   "searches": [{"search": "wooden toy chest", "volume": 3600}]}, [])
    assert r[0].startswith("Open:") and "HW-Toy-Chest" in r[0] and r[1] == "Go to: Search Engine Optimization"
    assert r[2].startswith("Meta Title:") and r[3] == "Save."


def test_natural_phrase_and_subcategory_url():
    from scripts.product_demand import natural_phrase, owner_page
    assert natural_phrase([{"search": "classroom rug large"}, {"search": "large classroom carpet"}]) == "large classroom carpet"
    cats = {1: {"name": "Large Classroom Rugs", "active": True, "url_key": "large-classroom-rugs",
                "url_path": "classroom-rugs/large-classroom-rugs"}}
    assert owner_page([], [], cats, {}, "large classroom rugs")[0] == "/classroom-rugs/large-classroom-rugs.html"


def test_aio_step():
    from scripts.product_demand import aio_step
    assert aio_step([]) == ""
    assert aio_step([{"search": "classroom rug", "present": False, "cited": False, "who": []}]) == ""  # nothing to do, say nothing
    s = aio_step([{"search": "montessori toys", "present": True, "cited": False, "who": ["montessorigeneration.com"]}])
    assert "montessorigeneration.com" in s and "FAQ" in s
    assert "already quotes" in aio_step([{"search": "x", "present": True, "cited": True, "who": []}])
