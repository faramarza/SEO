from src.analysis import page_content as pc

PRODUCTS = [
    {"sku": "NT5", "name": "5 Letter Maple Landmark Name Train", "price": 39.99, "url": "/5-letter-name-train.html", "min_age": 3},
    {"sku": "PZ1", "name": "Personalized Wooden Name Puzzle", "price": 34.95, "url": "/name-puzzle.html", "min_age": 0},
    {"sku": "BK1", "name": "My Very Own Name Personalized Baby Book", "price": 45.98, "url": "/baby-book.html", "min_age": 0},
]


def test_min_age_from_rules():
    assert pc.min_age(PRODUCTS[0], {"name train": 3}) == 3 and pc.min_age(PRODUCTS[1], {"name train": 3}) == 0


def test_validate_drops_claims_unknown_products_and_wrong_ages():
    draft = {"intro": "Our name trains are the #1 gift. Each one spells a child's name in wooden cars.",
             "sections": [{"heading": "Gifts for 1 year olds", "items": [{"sku": "NT5", "why": "Fun."},
                                                                         {"sku": "BK1", "why": "A story with their name."},
                                                                         {"sku": "ZZZ", "why": "x"}]},
                          {"heading": "For ages 3 and up", "items": [{"sku": "NT5", "why": "Spells their name."}]}],
             "faqs": [{"q": "Are they made in the USA?", "a": "Yes, made in the USA."},
                      {"q": "How long is a 5 letter train?", "a": "Seven cars with engine and caboose."}],
             "meta_title": "Personalized Gifts for Toddlers | Name Trains", "meta_description": "Gift ideas."}
    out, fixes = pc.validate(draft, PRODUCTS, "guide")
    assert out["intro"] == "Each one spells a child's name in wooden cars."
    young = out["sections"][0]
    assert [i["sku"] for i in young["items"]] == ["BK1"]           # name train (3+) moved out, ZZZ dropped
    assert out["sections"][1]["items"][0]["sku"] == "NT5"
    assert len(out["faqs"]) == 1                                     # the unsupported Made in USA claim is gone
    assert "|" not in out["meta_title"]
    assert any("ZZZ" in f for f in fixes) and any("ages 3+" in f for f in fixes)


def test_render_uses_catalog_names_prices_links():
    intro, body = pc.render({"intro": "Hi.", "sections": [{"heading": "Sets", "items": [{"sku": "PZ1", "why": "Spells it."}]}],
                             "faqs": [{"q": "Q?", "a": "A."}]}, PRODUCTS)
    assert intro == "<p>Hi.</p>"
    assert '<a href="/name-puzzle.html">Personalized Wooden Name Puzzle</a> ($34.95): Spells it.' in body
    assert "<h2>Frequently asked questions</h2><h3>Q?</h3><p>A.</p>" in body


def test_merge_keeps_owner_text_and_undo_removes_only_ours():
    owner = "<p>Our classroom chairs come in five heights.</p>"
    once = pc.merge(owner, "<p>Intro.</p>", "<h2>FAQ</h2>")
    assert once.startswith(pc.INTRO[0]) and owner in once and once.endswith(pc.BODY[1])
    twice = pc.merge(once, "<p>New intro.</p>", "<h2>New FAQ</h2>")
    assert "Intro." not in twice.replace("New intro.", "") and twice.count(pc.INTRO[0]) == 1
    assert pc.remove(twice) == owner


def test_pick_targets():
    plan = [{"page": "/kids-chairs.html", "kind": "category", "action": "content", "value": 600, "searches": [{"search": "kids chair"}]},
            {"page": None, "kind": "new", "action": "new_page", "value": 85, "line": "Gifts", "searches": [{"search": "baby gifts"}]},
            {"page": "/personalized-baby-gifts.html", "kind": "category", "action": "links", "value": 62, "line": "Gifts",
             "searches": [{"search": "personalized baby gifts"}]},
            {"page": "/x.html", "kind": "product", "action": "retitle", "value": 900, "searches": [{"search": "x"}]}]
    t = pc.pick_targets(plan, done={"/kids-chairs.html"}, limit=2)
    assert [x["key"] for x in t] == ["NEW:baby gifts", "/personalized-baby-gifts.html"]
    assert t[0]["template"] == "guide" and t[0]["create"] and t[1]["template"] == "guide"
