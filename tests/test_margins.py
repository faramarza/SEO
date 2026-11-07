"""Tests for pricesheet margin ingestion + the Carpets for Kids freight card.
Plain asserts in test_* functions (project runs these via importlib; no pytest
dependency required)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.margins import (  # noqa: E402
    money, is_xl, compute_margins, recommended_line_margin,
)
from src.analysis import shipping_carpets as sc  # noqa: E402


def test_money_parsing():
    assert money("$1,234.50") == 1234.5
    assert money("280.46") == 280.46
    assert money("") is None
    assert money(None) is None
    assert money("n/a") is None


def test_is_xl():
    assert is_xl("8ft x 12ft") is True
    assert is_xl("8ft 3in x 11ft 8in") is True      # vendor lists this as XL
    assert is_xl("8ft 4in x 13ft 4in") is True
    assert is_xl("7ft 6in x 12ft") is False         # reworked to dodge surcharge
    assert is_xl("4ft 5in x 5ft 10in") is False
    assert is_xl("12in Squares") is False


def test_classify_size():
    assert sc.classify_size("7ft 6in x 12ft")[0] == "large"
    assert sc.classify_size("8ft x 12ft")[0] == "xlarge"
    assert sc.classify_size("6ft 9in x 9ft 5in")[0] == "medium"
    assert sc.classify_size("4ft x 6ft")[0] == "small"
    assert sc.classify_size("12in Squares")[0] == "kit"


def test_ship_cost_surcharge_and_zone():
    # XL includes the +$60 seasonal surcharge when enabled
    on = sc.ship_cost("8ft x 12ft", zone=1, season_surcharge=True)
    off = sc.ship_cost("8ft x 12ft", zone=1, season_surcharge=False)
    assert round(on - off, 2) == 60.0
    assert off == 106.95                      # zone 1 XL base
    # non-XL never gets the surcharge
    assert sc.ship_cost("7ft 6in x 12ft", zone=1, season_surcharge=True) == 39.95
    # zone 'avg' is the mean across the 7 zones
    assert sc.ship_cost("4ft x 6ft", zone="avg") == round(sum(sc.RATES["small"]) / 7, 2)


def test_compute_margins_gross_vs_net():
    recs = [
        {"sku": "A", "title": "", "cost": 200.0, "map": 300.0, "msrp": 350.0,
         "size": "8ft x 12ft", "ship_weight": 40, "xl": True},
        {"sku": "B", "title": "", "cost": 100.0, "map": 200.0, "msrp": 240.0,
         "size": "6ft x 9ft", "ship_weight": 19, "xl": False},
    ]
    gross = compute_margins(recs, sell_basis="map")
    # A gross = (300-200)/300 = .3333 ; B = (200-100)/200 = .5
    assert gross["skus"]["A"]["margin"] == 0.3333
    assert gross["skus"]["B"]["margin"] == 0.5
    assert gross["summary"]["xl_skus"] == 1

    # net with the carpets freight card: A is XL (expensive freight) -> margin drops
    net = compute_margins(recs, sell_basis="map", ship_fn=sc.make_ship_fn(zone="avg"))
    assert net["skus"]["A"]["margin"] < gross["skus"]["A"]["margin"]
    assert net["skus"]["A"]["shipping"] > 100     # XL freight incl surcharge
    assert net["skus"]["B"]["shipping"] == sc.ship_cost("6ft x 9ft", zone="avg")


def test_compute_margins_store_prices():
    # store price differs from MAP; margin must use the store price, not MAP.
    recs = [
        {"sku": "A", "cost": 200.0, "map": 300.0, "msrp": 350.0, "size": "", "xl": False},
        {"sku": "B", "cost": 100.0, "map": 200.0, "msrp": 240.0, "size": "", "xl": False},
    ]
    price_map = {"A": 400.0}   # A sells higher than MAP in the store; B missing
    res = compute_margins(recs, sell_basis="store", price_map=price_map)
    assert res["skus"]["A"]["sell"] == 400.0          # used store price
    assert res["skus"]["A"]["sell_source"] == "store"
    assert res["skus"]["A"]["margin"] == round((400 - 200) / 400, 4)
    assert res["skus"]["B"]["sell"] == 200.0          # fell back to MAP
    assert res["skus"]["B"]["sell_source"] == "fallback"
    assert res["summary"]["price_sources"] == {"store": 1, "fallback": 1}


def test_recommended_line_margin_is_median():
    recs = [
        {"sku": "A", "cost": 70, "map": 100, "msrp": None, "size": "", "xl": False},
        {"sku": "B", "cost": 60, "map": 100, "msrp": None, "size": "", "xl": False},
        {"sku": "C", "cost": 50, "map": 100, "msrp": None, "size": "", "xl": False},
    ]
    res = compute_margins(recs, sell_basis="map")
    # margins .30/.40/.50 -> median .40
    assert recommended_line_margin(res["summary"]) == 0.4


def test_price_map_from_catalog_csv():
    import os
    import tempfile
    from src.analysis.margins import price_map_from_catalog_csv
    csv_text = (
        "sku,price,special_price,name,additional_attributes\n"
        "5506,280.46,,CFK Rug A,brand_google=Carpets for Kids\n"
        "5512,400.00,300.00,CFK Rug B,brand_google=Carpets for Kids\n"
        "5656,33.99,,PlanToys Game,brand_google=Plan Toys\n"   # SKU collision, non-CFK
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        f.write(csv_text)
        path = f.name
    try:
        pm = price_map_from_catalog_csv(path, brand_contains="Carpets for Kids")
        # 5512 uses special (300<400); 5656 excluded by brand despite matching SKU
        assert pm == {"5506": 280.46, "5512": 300.0}, pm
        pm2 = price_map_from_catalog_csv(path)   # no brand filter -> collision included
        assert "5656" in pm2
    finally:
        os.unlink(path)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    _run()
