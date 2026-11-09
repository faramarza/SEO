"""Tests for the hero-row picker (no Magento/GA4 needed)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.montessori_top_picks import (  # noqa: E402
    rank, margin_for, section_html, eff_price, display_order,
)

BRANDS = {"guide craft": {"margin": 0.496}, "tag toys": {"margin": 0.168},
          "whitney brothers": {"margin": 0.347}}


def _p(sku, brand, price=50.0, cats=("Montessori Toys For 2 Year Olds",),
       enabled=True, in_stock=True, name=None):
    return {"sku": sku, "name": name or f"Toy {sku}", "brand": brand, "price": price,
            "categories": list(cats), "enabled": enabled, "in_stock": in_stock,
            "url_key": sku.lower()}


def test_eff_price_uses_lower_special():
    assert eff_price("100", "80") == 80.0
    assert eff_price("100", "") == 100.0
    assert eff_price("100", "120") == 100.0


def test_margin_sources():
    assert margin_for("X", "Guidecraft", {}, BRANDS, 0.15) == (0.496, "brand")
    assert margin_for("X", "Guide Craft", {"X": 0.4}, BRANDS, 0.15) == (0.4, "sku")
    assert margin_for("X", "Unknown Co", {}, BRANDS, 0.15) == (0.15, "default")


def test_profit_beats_revenue_and_filters():
    prods = [
        _p("TAG1", "Tag Toys"),                       # $1000 rev x .168 = 168
        _p("GC1", "Guide Craft"),                     # $500 rev x .496 = 248 -> wins
        _p("OOS", "Guide Craft", in_stock=False),     # excluded
        _p("OFF", "Guide Craft", enabled=False),      # excluded
        _p("RUG", "Guide Craft", cats=("Classroom Rugs",)),  # not Montessori
    ]
    sales = {"toy tag1": {"revenue": 1000, "purchased": 10},
             "toy gc1": {"revenue": 500, "purchased": 5},
             "toy oos": {"revenue": 9999, "purchased": 99}}
    picks, ranked = rank(prods, sales, {}, BRANDS, 0.15, n=8)
    assert [r["sku"] for r in picks] == ["GC1", "TAG1"]
    assert len(ranked) == 2


def test_brand_cap_and_min_margin():
    prods = [_p(f"G{i}", "Guide Craft") for i in range(5)] + [_p("LOW", "Nobody")]
    picks, _ = rank(prods, {}, {}, BRANDS, 0.10, n=8, brand_cap=3, min_margin=0.15)
    assert len(picks) == 3                         # cap 3, LOW excluded by margin
    assert all(r["sku"].startswith("G") for r in picks)


def test_section_claims_only_when_true():
    sold = [{"sku": "A", "purchased": 3}, {"sku": "B", "purchased": 1}]
    unsold = [{"sku": "A", "purchased": 3}, {"sku": "B", "purchased": 0}]
    assert "buy most" in section_html(sold)
    assert "buy most" not in section_html(unsold)
    h = section_html(sold)
    assert "`value`:`A, B`" in h and 'products_count="2"' in h
    assert "—" not in h


def test_name_trains_excluded_by_default():
    prods = [_p("10-letter-name-train", "Maple Landmark", name="10 Letter Name Train"),
             _p("G1", "Guide Craft")]
    picks, _ = rank(prods, {}, {}, BRANDS, 0.15)
    assert [r["sku"] for r in picks] == ["G1"]
    picks, _ = rank(prods, {}, {}, BRANDS, 0.15, exclude="")
    assert len(picks) == 2


def test_stock_check_skips_sold_out():
    prods = [_p("G1", "Guide Craft"), _p("G2", "Guide Craft"), _p("G3", "Guide Craft")]
    for p in prods:
        p["stock_known"] = False
    sales = {"toy g1": {"revenue": 900}, "toy g2": {"revenue": 500}, "toy g3": {"revenue": 100}}
    stock = {"G1": (False, 0.0), "G2": (True, 4.0), "G3": (None, None)}
    picks, ranked = rank(prods, sales, {}, BRANDS, 0.15, n=2, stock_check=stock.get)
    assert [r["sku"] for r in picks] == ["G2", "G3"]
    assert ranked[0]["stock_status"] == "OUT"
    assert picks[1]["stock_status"] == "UNCHECKED"


def test_duplicate_skus_counted_once():
    prods = [_p("G1", "Guide Craft"), _p("G1", "Guide Craft"), _p("G2", "Guide Craft")]
    picks, ranked = rank(prods, {}, {}, BRANDS, 0.15)
    assert [r["sku"] for r in ranked] == ["G1", "G2"] or len(ranked) == 2
    assert len({r["sku"] for r in picks}) == len(picks)


def test_display_order_best_seller_then_price():
    picks = [{"sku": "BIG", "purchased": 3, "profit": 780, "price": 524},
             {"sku": "STOOL", "purchased": 59, "profit": 714, "price": 77},
             {"sku": "CHEAP", "purchased": 3, "profit": 100, "price": 67},
             {"sku": "MID", "purchased": 6, "profit": 350, "price": 118}]
    assert [r["sku"] for r in display_order(picks)] == ["STOOL", "CHEAP", "MID", "BIG"]


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    _run()
