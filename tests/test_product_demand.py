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
    med, share = pd.shopping_check("bundle branch block", {})
    assert med == 20.0 and abs(share - 1 / 3) < 0.01


def test_collectibles_and_science_noise_dropped():
    from scripts.product_demand import qualified, OFF_TOPIC, KID_WORDS
    for junk in ("blind toy boxes", "nucleic acid building block", "funko pop toy", "jigsaw puzzle 1000 piece"):
        assert not (qualified(junk) and not OFF_TOPIC.search(junk)), junk
    assert not KID_WORDS.search("Pop Mart Labubu Blind Box Toy")
    assert KID_WORDS.search("Wooden Blocks for Toddlers, Ages 2+")
