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


def _run():
    for k, v in sorted(globals().items()):
        if k.startswith("test_"):
            v(); print(f"  PASS {k}")


if __name__ == "__main__":
    _run()
