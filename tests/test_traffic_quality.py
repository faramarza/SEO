"""Tests for the bot vs human split in scripts/traffic_quality.py (no Google creds needed)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.traffic_quality import classify, is_bot_like  # noqa: E402


def _r(country, sessions, engaged, sec, buys=0, device="desktop", browser="Chrome"):
    return {"country": country, "deviceCategory": device, "browser": browser,
            "sessions": sessions, "engaged": engaged,
            "eng_rate": engaged / sessions if sessions else 0.0,
            "eng_sec": sec, "purchases": buys, "revenue": 0.0}


def test_bot_like_needs_volume_and_no_engagement():
    assert is_bot_like(_r("United States", 500, 10, 0.5)) is True
    assert is_bot_like(_r("United States", 10, 0, 0.0)) is False      # too small to judge
    assert is_bot_like(_r("United States", 500, 250, 40.0)) is False  # humans


def test_classify_buckets():
    rows = [
        _r("United States", 1000, 600, 45.0, buys=10),
        _r("United States", 400, 8, 0.4, browser="(not set)"),        # bot farm
        _r("China", 300, 3, 0.2),                                        # foreign
        _r("Canada", 100, 55, 30.0, buys=1),
    ]
    b = classify(rows)
    assert b["total"] == 1800
    assert b["foreign"] == 300
    assert b["botlike"] == 400
    assert b["clean"] == 1100
    assert b["clean_engaged"] == 655
    assert b["purchases"] == 11
    assert len(b["botlike_segments"]) == 1


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tests passed")


if __name__ == "__main__":
    _run()
