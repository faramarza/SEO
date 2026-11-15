from scripts.product_demand import product_seeds, match_products


def test_seeds_include_qualified_variants():
    s = product_seeds("Montessori Tummy Time Mirror", ["Montessori Mirrors"])
    for want in ("tummy time mirror", "montessori mirror", "baby mirror", "kids mirror",
                 "classroom mirror", "montessori mirrors"):
        assert want in s, want
    assert "mirror" not in s   # bare noun drags in bathroom mirrors


def test_seeds_drop_brand_and_noise():
    s = product_seeds("Guidecraft 10 Piece Mirror Blocks Set")
    assert "mirror blocks" in s and "montessori blocks" in s
    assert not any("guidecraft" in x or "piece" in x for x in s)


def test_match_prefers_most_specific_seed():
    seeds = {"mirror": {0, 1}, "tummy time mirror": {0}, "montessori mirror": {0, 1}}
    assert match_products("best tummy time mirror", seeds, []) == {0}
    assert match_products("montessori mirrors for baby", seeds, []) == {0, 1}
