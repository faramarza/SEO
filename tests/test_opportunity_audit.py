"""Pure-logic tests for scripts/opportunity_audit.py (no Google/Magento needed)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.opportunity_audit import (  # noqa: E402
    bucket, build_ctr_curve, page_type, margin_for_path, find_cannibals,
    row_opportunity, is_brand, path_of, FALLBACK_CTR,
)


def test_bucket_and_path():
    assert bucket(1.4) == 1 and bucket(9.6) == 10 and bucket(12) == 15
    assert bucket(18) == 20 and bucket(27) == 30
    assert path_of("https://alphabet-trains.com/montessori-toys.html?x=1") == "/montessori-toys.html"


def test_curve_uses_site_data_then_fallback_and_is_monotonic():
    rows = [{"clicks": 50, "impressions": 1000, "position": 1.2},     # site 5% at #1
            {"clicks": 80, "impressions": 1000, "position": 3.0}]     # site 8% at #3 > #1
    curve, src = build_ctr_curve(rows, min_impr=500)
    assert src[1] == "site" and src[3] == "site" and src[2] == "fallback"
    # non-increasing enforced bottom-up: #1 raised to at least #2/#3
    assert curve[3] <= curve[2] <= curve[1]
    assert curve[1] >= 0.08


def test_curve_not_zeroed_by_noisy_top_bucket():
    rows = [{"clicks": 0, "impressions": 1000, "position": 1.0},      # noisy 0% at #1
            {"clicks": 20, "impressions": 1000, "position": 8.0}]     # 2% at #8
    curve, _ = build_ctr_curve(rows, min_impr=500)
    assert curve[8] == 0.02 and curve[1] >= 0.02


def test_page_type():
    cats = {"/montessori-toys.html"}
    assert page_type("/", cats) == "home"
    assert page_type("/blog/x", cats) == "blog"
    assert page_type("/montessori-toys.html", cats) == "category"
    assert page_type("/some-product.html", cats) == "product"


def test_margin_by_url_minus_card_fee():
    lines = {"carpets": {"margin": 0.364, "url_patterns": ["rug", "carpet"]}}
    assert round(margin_for_path("/classroom-rugs.html", lines, 0.15, 0.03), 3) == 0.334
    assert round(margin_for_path("/stool.html", lines, 0.15, 0.03), 3) == 0.12


def test_cannibals():
    rows = [{"query": "q", "path": "/a.html", "impressions": 600},
            {"query": "q", "path": "/b.html", "impressions": 400},
            {"query": "z", "path": "/a.html", "impressions": 99},
            {"query": "z", "path": "/c.html", "impressions": 1}]
    c = find_cannibals(rows)
    assert c["/a.html"] == {"q"} and c["/b.html"] == {"q"} and "/c.html" not in c
    small = [{"query": "t", "path": "/a.html", "impressions": 6},
             {"query": "t", "path": "/b.html", "impressions": 4}]
    assert find_cannibals(small) == {}


def test_row_opportunity():
    curve = {b: v for b, v in FALLBACK_CTR.items()}
    r = {"impressions": 1000, "clicks": 5, "position": 14.0}
    o = row_opportunity(r, curve, value_per_click=2.0)
    # A: 1000 x (2.5% - 0.5%) = 20 clicks, $40
    assert round(o["A"][0], 6) == 20.0 and round(o["A"][1], 6) == 40.0
    assert o["B"][0] > o["A"][0]
    assert row_opportunity({"impressions": 100, "clicks": 9, "position": 2.0}, curve, 1)["A"] == (0.0, 0.0)


def test_snippet_gap():
    from scripts.opportunity_audit import snippet_gap
    # 1000 impressions at #8 (typical 2.5%) with 5 clicks: 25 - 5 = 20 lost
    assert round(snippet_gap({"impressions": 1000, "clicks": 5, "position": 8.0}), 6) == 20.0


def test_brand():
    assert is_brand("Alphabet Trains name train", ["alphabet trains"])
    assert not is_brand("montessori toys", ["alphabet trains"])


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    _run()
