"""Pure-logic tests for scripts/line_demand.py: lines come from YOUR catalog and only
searches for YOUR version (qualified) are valued."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.line_demand import (  # noqa: E402
    product_types, category_lines, line_qualifiers, qualified_seeds, value,
)


def _p(name, brand="", cats=()):
    return {"name": name, "brand": brand, "price": 80, "cats": list(cats)}


def test_product_types_from_own_names():
    prods = [_p("Heirloom Name Puzzle Stool", "Cubbyhole Toys"),
             _p("Two Step Name Puzzle Stool", "Hollow Woodworks"),
             _p("Whitney Plus Two Tub Sand & Water Table", "Whitney Brothers"),
             _p("Toddler Sand and Water Table", "Whitney Brothers")]
    t = product_types(prods, {"whitney", "brothers", "cubbyhole", "toys", "hollow", "woodworks"})
    assert "puzzle stool" in t and "water table" in t
    assert all(len(v) >= 2 for v in t.values())


def test_qualifiers_describe_your_version():
    stools = [_p("Heirloom Name Puzzle Stool"), _p("Two Step Name Puzzle Stool")]
    q = line_qualifiers(stools)
    assert "personalized" in q and "name" in q and "classroom" not in q
    seeds = qualified_seeds("step stool", q)
    assert "personalized step stool" in seeds and "step stool" not in seeds   # generic never valued
    wb = [_p("Plus Two Tub Sand & Water Table", "Whitney Brothers")]
    assert "classroom" in line_qualifiers(wb) and "wooden" in line_qualifiers(wb)
    plastic = [_p("Shape Sorter")]
    assert line_qualifiers(plastic) == []                                    # nothing distinguishes it


def test_category_lines_skip_brand_pages():
    cats = {1: {"name": "Classroom Rugs", "active": True, "url_key": "classroom-rugs"},
            2: {"name": "Shop by Brand", "active": True, "url_key": "brands"},
            3: {"name": "Old", "active": False, "url_key": "old"}}
    prods = [_p("a", cats=[1, 2, 3]), _p("b", cats=[1])]
    out = category_lines(cats, prods)
    assert list(out) == ["classroom rugs"] and out["classroom rugs"][0] == "/classroom-rugs.html"


def test_value_realistic_goal():
    extra, profit = value(1000, None, 0, 0, 0.01, 100, 0.3)          # not ranking -> page-1 goal
    assert round(extra, 6) == 25.0 and round(profit, 6) == 7.5
    extra, _ = value(1000, 12, 2, 500, 0.01, 100, 0.3)               # pages 1-2 -> top 3
    assert round(extra, 6) == round(1000 * (0.10 - 2 / 500), 6)


def test_covering_page_and_singular():
    from scripts.line_demand import covering_page, singular_key
    cat_lines = {"personalized step stools": ("/personalized-step-stools.html", {0, 1, 5}),
                 "gifts": ("/gifts.html", set(range(50)))}
    assert covering_page({0, 1}, cat_lines) == "/personalized-step-stools.html"
    assert covering_page({7, 8}, {"x": ("/x.html", {1})}) is None
    assert singular_key("name trains") == singular_key("name train")


def test_sku_margin_from_invoices():
    from scripts.demand_map import product_margin
    cfg = {"paid_search": {"card_fee": 0.03, "default_margin": 0.15, "brand_margins": {},
           "product_lines": {}, "sku_margins": {"CH-001-1NSS": {"margin": 0.268}}}}
    assert round(product_margin({"sku": "CH-001-1NSS", "brand": "Cubbyhole", "name": "x", "url": "/x"}, cfg), 3) == 0.238
