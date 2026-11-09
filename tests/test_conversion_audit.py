"""Pure-logic tests for scripts/conversion_audit.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.conversion_audit import step_rates, fix_value, product_leaks  # noqa: E402


def test_step_rates_skip_untracked():
    c = {"visit": 1000, "reached": 500, "add_to_cart": 50, "begin_checkout": 25,
         "add_shipping_info": None, "add_payment_info": None, "purchase": 10}
    r = {k: rate for k, _, n, rate, _, _ in step_rates(c)}
    assert r["reached"] == 0.5 and r["add_to_cart"] == 0.1 and r["begin_checkout"] == 0.5
    assert r["add_shipping_info"] is None and r["add_payment_info"] is None
    assert r["purchase"] == 10 / 25          # vs last TRACKED step, not 0


def test_fix_value_one_step():
    c = {"visit": 1000, "reached": 500, "add_to_cart": 25, "begin_checkout": 12,
         "add_shipping_info": None, "add_payment_info": None, "purchase": 6}
    fv = fix_value(step_rates(c), orders=6, aov=50, margin=0.3)
    top = fv[0]
    assert top[0] == "Added to cart"          # 5% vs 15% midpoint: biggest
    assert round(top[3], 6) == 12.0            # 6 x (0.15/0.05 - 1)
    assert round(top[4], 6) == 180.0           # 12 x 50 x 0.3


def test_product_leaks():
    items = {"A": {"viewed": 100, "added_to_cart": 10, "purchased": 5, "revenue": 500},
             "B": {"viewed": 100, "added_to_cart": 2, "purchased": 1, "revenue": 80},
             "C": {"viewed": 100, "added_to_cart": 8, "purchased": 4, "revenue": 200},
             "tiny": {"viewed": 3, "added_to_cart": 0, "purchased": 0, "revenue": 0}}
    med, c2p, rows = product_leaks(items, margin=0.3)
    assert med == 0.08 and round(c2p, 4) == round(10 / 20, 4)
    assert rows[0]["name"] == "B" and all(r["name"] != "tiny" for r in rows)
    assert round(rows[0]["lost_carts"], 6) == 6.0


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    _run()
