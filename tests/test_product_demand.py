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
                 "google earth map", "rug rug", "water table"):
        assert not qualified(junk), junk
    for good in ("tummy time mirror", "montessori mirror", "kids area rug", "toy tow truck",
                 "classroom rug", "usa map rug for classroom", "wooden name train", "kids water table"):
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
