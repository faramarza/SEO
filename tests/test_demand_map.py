"""Pure-logic tests for scripts/demand_map.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.demand_map import tokens, best_match, is_branded, monthly_profit, break_even_roas  # noqa: E402


def test_tokens_drop_stopwords_and_brand():
    t = tokens("Guidecraft Kids Wooden Unit Blocks set", {"guidecraft"})
    assert t == {"wooden", "unit", "block"}


def test_best_match():
    prods = [{"name": "Guidecraft Tabletop Light Box"}, {"name": "Guidecraft Unit Blocks - 218 pc"}]
    for p in prods:
        p["_tokens"] = tokens(p["name"], {"guidecraft"})
    p, s = best_match("wooden unit blocks", prods, {"guidecraft"})
    assert p["name"].startswith("Guidecraft Unit") and round(s, 2) == 0.67
    p, s = best_match("light table for kids", prods, {"guidecraft"})
    assert p["name"].endswith("Light Box") and s == 1.0
    p, s = best_match("guidecraft", prods, {"guidecraft"})
    assert p is None and s == 0.0


def test_branded_and_money():
    assert is_branded("guide craft light table", {"guide craft", "guidecraft"})
    assert not is_branded("light table for kids", {"guide craft", "guidecraft"})
    extra, profit = monthly_profit(1000, 0.0, 0.02, 100.0, 0.466)
    assert round(extra, 6) == 100.0 and round(profit, 6) == round(100 * 0.02 * 100 * 0.466, 6)
    assert round(break_even_roas(0.466), 2) == 2.15


def test_margin_merge_and_brand_filter():
    from scripts.demand_map import product_margin, merge_rows, brand_filter
    cfg = {"paid_search": {"card_fee": 0.03, "default_margin": 0.15,
           "brand_margins": {"guide craft": {"margin": 0.496}},
           "product_lines": {"name_trains": {"margin": 0.30, "url_patterns": ["name-train"]}}}}
    assert round(product_margin({"brand": "Guide Craft", "name": "x", "url": "/x.html"}, cfg), 3) == 0.466
    assert round(product_margin({"brand": "Maple Landmark", "name": "x",
                                 "url": "/5-letter-name-train.html"}, cfg), 3) == 0.27
    assert round(product_margin({"brand": "", "name": "x", "url": "/y.html"}, cfg), 3) == 0.12
    rows = merge_rows([{"kw": "A", "profit": 5, "vol": 10}, {"kw": "a", "profit": 9, "vol": 10},
                       {"kw": "b", "profit": 1, "vol": 99}])
    assert [r["profit"] for r in rows] == [9, 1]
    prods = [{"brand": "Guide Craft", "name": "x"}, {"brand": "Tag Toys", "name": "y"}]
    assert len(brand_filter(prods, "guidecraft")) == 1 and len(brand_filter(prods, "*")) == 2


def test_realism_rules():
    from scripts.demand_map import is_head_term, target_ctr, TARGET_CTR, PAGE1_CTR
    assert is_head_term("blocks", set()) and not is_head_term("toddler tower", set())
    assert not is_head_term("wooden unit blocks", set())
    assert target_ctr(6.0) == TARGET_CTR and target_ctr(None) == PAGE1_CTR and target_ctr(35) == PAGE1_CTR


def test_plural_stopwords_and_step_stool():
    from scripts.demand_map import best_match
    prods = [{"name": "Heirloom Name Puzzle Step Stool"}]
    prods[0]["_tokens"] = tokens(prods[0]["name"])
    p, s = best_match("childs step stool", prods, set())
    assert p and s == 1.0


def test_store_merchandise_never_leaks_and_filters():
    from scripts.demand_map import source_rows, is_informational, norm_brand
    cfg = {"paid_search": {"card_fee": 0.03, "default_margin": 0.15, "brand_margins": {}, "product_lines": {}}}
    prods = [{"sku": "S", "name": "Toddler Sensory Table", "brand": "Whitney Brothers", "price": 300, "url": "/s.html"}]
    demand = {"keywords": [{"keyword": "jellycat stuffed animals", "volume": 74000},
                           {"keyword": "convertible car seat", "volume": 110000},
                           {"keyword": "toddler sensory table", "volume": 9900}]}
    rows = source_rows({"domain": "store.com", "brand": "*"}, demand, prods, {}, 0.01, cfg, 30)
    assert [r["kw"] for r in rows] == ["toddler sensory table"]
    assert is_informational("how to play backgammon") and not is_informational("water table")
    assert norm_brand("Guide Craft") == norm_brand("guidecraft") == "Guidecraft"
    assert norm_brand("ISeeMe Books") == "I See Me"


def _run():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v(); print(f"  PASS {k}")


if __name__ == "__main__":
    _run()
