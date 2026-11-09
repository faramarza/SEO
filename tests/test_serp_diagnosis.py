"""Tests for the SERP verdict logic in scripts/serp_diagnosis.py (no API needed)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.serp_diagnosis import classify_result, verdict  # noqa: E402


def test_classify_result():
    assert classify_result("https://www.amazon.com/x", "Montessori toys") == "marketplace"
    assert classify_result("https://www.alphabet-trains.com/a.html", "") == "own"
    assert classify_result("https://shop.com/blog/best-toys", "x") == "editorial"
    assert classify_result("https://www.babylist.com/hello", "x") == "editorial"
    assert classify_result("https://shop.com/products/drum", "Best drum") == "shop"
    assert classify_result("https://shop.com/montessori-toys", "Montessori Toys") == "shop"


def test_verdict_order():
    p = "/montessori-toys.html"
    assert verdict(["marketplace"] * 4 + ["shop"] * 6, [], p)[0] == "B"
    assert verdict(["editorial"] * 7 + ["shop"] * 3, [], p)[0] == "I"
    # a blog target facing articles is NOT a mismatch
    assert verdict(["editorial"] * 7 + ["shop"] * 3, [], "/blog/x")[0] == "L"
    own = ["https://www.alphabet-trains.com/other.html"]
    assert verdict(["shop"] * 9 + ["own"], own, p)[0] == "D"
    assert verdict(["shop"] * 10, [], p, own_words=200, comp_words=[900, 1000, 1100])[0] == "C"
    assert verdict(["shop"] * 10, [], p, own_words=900, comp_words=[900, 1000],
                   own_rd=1, comp_rd=[30, 40, 50])[0] == "F"
    assert verdict(["shop"] * 10, [], p, own_words=900, comp_words=[900, 1000],
                   own_rd=20, comp_rd=[30, 40, 50])[0] == "L"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    _run()
