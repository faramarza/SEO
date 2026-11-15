from scripts.product_demand import product_seeds, match_products, qualified, kind_of


def test_seeds_end_in_what_the_product_is():
    s = product_seeds("Montessori Tummy Time Mirror", ["Montessori Mirrors"])
    for want in ("tummy time mirror", "montessori mirror", "baby mirror", "kids mirror", "classroom mirror"):
        assert want in s, want
    assert "mirror" not in s


def test_design_is_not_the_product():
    s = product_seeds("Carpet for kids USA Map 8ft x 12ft Rectangle")
    assert "usa map rug" in s and "kids rug" in s
    assert "usa map" not in s
    assert kind_of("Mobile Garden Center") is None


def test_qualified_rejects_generic_world_searches():
    for junk in ("usa map", "tow truck near me", "garden center near me", "area rug", "book shelf",
                 "google earth map", "rug rug", "water table", "wooden table", "wooden bench",
                 "wooden cabinet"):
        assert not qualified(junk), junk
    for good in ("tummy time mirror", "montessori mirror", "kids area rug", "toy tow truck",
                 "classroom rug", "usa map rug for classroom", "wooden name train", "kids water table",
                 "wooden blocks"):
        assert qualified(good), good


def test_match_prefers_most_specific_seed():
    seeds = {"montessori mirror": {0, 1}, "tummy time mirror": {0}}
    assert match_products("best tummy time mirror", seeds, []) == {0}
    assert match_products("montessori mirrors for baby", seeds, []) == {0, 1}


def test_price_flag():
    from scripts.product_demand import price_flag
    assert price_flag(1376.99, 60) == "PRICEY"
    assert price_flag(133.53, 70) == ""
    assert price_flag(100, None) == ""


def test_off_topic_adult_furniture():
    from scripts.product_demand import OFF_TOPIC
    for junk in ("coffee wooden table", "kids table legs", "wooden cabinet in kitchen", "baby high chair",
                 "kids tablet samsung", "folding toddler table"):
        assert OFF_TOPIC.search(junk), junk
    assert not OFF_TOPIC.search("toddler table and chairs")


def test_collapse_variants():
    from scripts.product_demand import collapse_variants
    rows = [{"search": "classroom carpet rugs", "volume": 14800, "you": None},
            {"search": "classroom rugs and carpets", "volume": 14800, "you": 39},
            {"search": "classrooms carpet rug", "volume": 14800, "you": None},
            {"search": "kids rug", "volume": 12100, "you": None}]
    out = collapse_variants(rows)
    assert len(out) == 2
    big = [r for r in out if r["volume"] == 14800][0]
    assert big["variants"] == 3 and big["search"] == "classroom rugs and carpets"


def test_shopping_check_kid_share(monkeypatch):
    import scripts.product_demand as pd
    monkeypatch.setattr(pd, "_shopping_items", lambda q: [("Bundle branch block ECG poster", 20.0),
                                                           ("Cardiology chart", 15.0),
                                                           ("Kids wooden blocks set", 30.0)])
    med, share, upper = pd.shopping_check("bundle branch block", {})
    assert med == 20.0 and upper == 30.0 and abs(share - 1 / 3) < 0.01


def test_name_train_listings_count_as_kids():
    from scripts.product_demand import kid_item
    for t in ("Name Trains in Bright Colors", "Nametrain Clear Finish", "Letter Name Train",
              "Engine & Caboose Wooden Name Train"):
        assert kid_item(t, "name trains"), t
    assert not kid_item("Bundle branch block ECG poster", "bundle branch block")


def test_letter_cars_dont_make_name_trains_pricey(monkeypatch):
    import scripts.product_demand as pd
    items = [("Letter Name Train", 6.99), ("Nametrain Clear Finish", 7.0), ("Name Train Letter Cars", 8.99),
             ("Name Train 2020 Engine", 10.15), ("Personalized Montessori Wooden Toy", 16.92),
             ("Personalized Wooden Name Train", 38.99), ("Name Trains in Bright Colors", 44.95),
             ("AmishToyBox Wooden Name Train", 49.5), ("Name Trains with Track", 54.95),
             ("NameTrain: 8 Letter Name", 67.5)]
    monkeypatch.setattr(pd, "_shopping_items", lambda q: items)
    med, share, upper = pd.shopping_check("name train", {})
    assert share == 1.0 and pd.price_flag(49.99, upper) == ""


def test_collectibles_and_science_noise_dropped():
    from scripts.product_demand import qualified, OFF_TOPIC, KID_WORDS
    for junk in ("blind toy boxes", "nucleic acid building block", "funko pop toy", "jigsaw puzzle 1000 piece"):
        assert not (qualified(junk) and not OFF_TOPIC.search(junk)), junk
    assert not KID_WORDS.search("Pop Mart Labubu Blind Box Toy")
    assert KID_WORDS.search("Wooden Blocks for Toddlers, Ages 2+")


def test_toy_chest_matches_the_chest_not_every_toy():
    from scripts.product_demand import product_seeds, match_products
    seeds = {}
    for i, name in enumerate(["Personalized Toy Chest", "Guidecraft Texo 100 pc set Wooden Toy"]):
        for x in product_seeds(name):
            seeds.setdefault(x, set()).add(i)
    assert "toy chest" in seeds
    assert match_products("wooden toy chest", seeds, []) == {0}


def test_owner_is_the_category_named_in_the_search():
    from scripts.product_demand import owner_page
    cats = {1: {"name": "Montessori Toys", "active": True, "url_key": "montessori-toys"},
            2: {"name": "Sorting Toys", "active": True, "url_key": "sorting-toys"},
            3: {"name": "Classroom Rugs", "active": True, "url_key": "classroom-rugs"},
            4: {"name": "Shop by Brand", "active": True, "url_key": "brands"}}
    prods = [{"url": "/stacker.html", "name": "Montessori Shape Stacker", "cats": [2]}]
    assert owner_page([0], prods, cats, {}, "montessori educational toy")[0] == "/montessori-toys.html"
    assert owner_page([0], prods, cats, {}, "montessori toys")[0] == "/montessori-toys.html"
    assert owner_page([0], prods, cats, {}, "classroom carpet")[0] == "/classroom-rugs.html"


def test_box_matches_chest():
    from scripts.product_demand import product_seeds, match_products
    seeds = {}
    for i, n in enumerate(["Personalized Toy Chest", "Guidecraft Texo 100 pc set Wooden Toy"]):
        for x in product_seeds(n):
            seeds.setdefault(x, set()).add(i)
    assert match_products("wooden toy box", seeds, []) == {0}


def test_age_numbers_pick_the_right_category():
    from scripts.product_demand import owner_page
    cats = {1: {"name": "Montessori Toys For 1 Year Old", "active": True, "url_key": "montessori-toys-for-1-year-olds"},
            2: {"name": "Montessori Toys For 2 Year Olds", "active": True, "url_key": "montessori-toys-2-year-olds"}}
    assert owner_page([], [], cats, {}, "montessori toys for 2 year olds")[0] == "/montessori-toys-2-year-olds.html"


def test_lines():
    from scripts.product_demand import line_of
    assert line_of("/5-letter-name-train.html", "name train") == "Name trains"
    assert line_of("/classroom-rugs.html", "classroom carpet") == "Rugs"
    assert line_of("", "wooden toy box") == "Furniture"
    assert line_of("/montessori-toys.html", "montessori toys") == "Montessori"
    assert line_of("/wooden-name-puzzles.html", "name puzzle") == "Puzzles"
    assert line_of("/wall-mounted-book-shelf.html", "classroom shelf") == "Furniture"
    assert line_of("/regular-personalized-name-stool.html", "name puzzle stool") == "Stools"


def test_price_check_reaches_every_line():
    from scripts.product_demand import check_order
    rugs = [{"search": f"classroom rug {i}", "url": "/r.html", "profit": 900 - i} for i in range(80)]
    trains = [{"search": "name train", "url": "/5-letter-name-train.html", "profit": 40}]
    order = check_order(rugs + trains, per_line=5)
    assert order.index(trains[0]) < 10


def test_plan_keeps_room_for_each_line():
    from scripts.product_demand import balanced
    groups = [{"line": "Rugs", "value": 1000 - i, "page": f"/r{i}"} for i in range(30)]
    groups.append({"line": "Name trains", "value": 50, "page": "/name-trains.html"})
    out = balanced(groups, max_pages=10, per_line=4)
    assert len(out) == 10 and any(g["line"] == "Name trains" for g in out)


def test_row_status_explains():
    from scripts.product_demand import row_status
    assert "already #2" in row_status({"search": "name train", "profit": 0, "you": 2.0}, False, None)
    assert row_status({"search": "x", "profit": 50, "verified": True}, True, None) == "IN PLAN"
    assert row_status({"search": "x", "profit": 50}, False, "changed 2026-11-10: wait for results").startswith("WAIT")
    assert "cheap" in row_status({"search": "x", "flag": "CHEAP"}, False, None)


def test_aio_asks_google_to_wait_and_ignores_old_cache(monkeypatch):
    from src.data_sources import dataforseo_client as dfs
    monkeypatch.setattr(dfs, "_load_cache", lambda: {"queries": {"montessori toys": {
        "available": True, "ai_overview_present": False}}})
    assert dfs.get_cached("montessori toys") is None
    import inspect
    assert '"load_async_ai_overview": True' in inspect.getsource(dfs.fetch_aio_citation)


def test_train_and_puzzle_noise_dropped():
    from scripts.product_demand import OFF_TOPIC
    for junk in ("coal engine train", "track train status", "thomas the tank engine train toy",
                 "jigsaw puzzle game online", "personalized book embossers", "medical branch block",
                 "squishy sensory toy"):
        assert OFF_TOPIC.search(junk), junk
    for good in ("name train", "wooden name train", "personalized books for kids", "classroom rug"):
        assert not OFF_TOPIC.search(good), good


def test_gift_searches():
    from scripts.product_demand import gift_search, line_of
    for good in ("personalized toddler gifts", "first birthday gift", "gifts for 2 year old boy",
                 "baby gifts", "personalized christmas gifts for kids", "gifts for grandchildren"):
        assert gift_search(good), good
    for bad in ("gift card", "gift basket for mom", "personalized gifts for her", "teacher gifts",
                "gift wrap", "personalized gifts for men", "gift box"):
        assert not gift_search(bad), bad
    assert line_of("/5-letter-name-train.html", "personalized toddler gifts") == "Gifts"


def test_gift_page_picks_each_line_without_stools():
    from scripts.product_demand import gift_picks
    products = [{"url": "/5-letter-name-train.html", "name": "5 Letter Name Train", "price": 50},
                {"url": "/regular-personalized-name-stool.html", "name": "Name Puzzle Stool", "price": 90},
                {"url": "/pirate-book.html", "name": "Pirate Tale Personalized Storybook", "price": 37}]
    assert sorted(gift_picks([0, 1, 2], products)) == [0, 2]
