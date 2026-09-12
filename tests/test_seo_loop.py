"""SEO loop safety + logic tests: the denylist must be structural, selection
must respect every exclusion, and the verify thresholds must stay asymmetric."""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data_sources.magento_client import (   # noqa: E402
    MagentoClient, MagentoError, _assert_safe_payload, _slug_of)
from src.analysis import seo_loop as sl        # noqa: E402


# ------------------------------------------------------------- denylist
def test_denylist_rejects_price():
    bad = {"product": {"custom_attributes": [
        {"attribute_code": "price", "value": "0.01"}]}}
    try:
        _assert_safe_payload(bad)
        assert False, "price write must be rejected"
    except MagentoError:
        pass


def test_denylist_rejects_top_level_fields():
    for field in ("price", "url_key", "status", "name", "sku", "visibility"):
        bad = {"product": {field: "x"}}
        try:
            _assert_safe_payload(bad)
            assert False, f"top-level {field} must be rejected"
        except MagentoError:
            pass


def test_payload_builder_only_meta():
    p = MagentoClient._meta_payload("product", "T", "D")
    _assert_safe_payload(p)  # must not raise
    codes = {a["attribute_code"] for a in p["product"]["custom_attributes"]}
    assert codes == {"meta_title", "meta_description"}


def test_sku_echo_allowed_but_rename_refused():
    p = MagentoClient._meta_payload("product", "T", "D")
    p["product"]["sku"] = "ABC-1"
    _assert_safe_payload(p, path_sku="ABC-1")  # identifier echo — fine
    try:
        _assert_safe_payload(p, path_sku="OTHER-SKU")
        assert False, "sku differing from path must be refused"
    except MagentoError:
        pass


def test_empty_write_rejected():
    try:
        MagentoClient._meta_payload("product")
        assert False
    except MagentoError:
        pass


def test_slug_of():
    assert _slug_of("https://x.com/wooden-name-puzzles.html") == "wooden-name-puzzles"
    assert _slug_of("https://x.com/blog/some-post/") == "some-post"
    assert _slug_of("https://x.com/a/b?utm=1") == "b"


# ------------------------------------------------------------- selection
def _ctr_row(url, query="q", impr=500, pos=6.0, actual=0.5):
    return {"url": url, "query": query, "impressions": impr, "position": pos,
            "actual_ctr": actual, "asset_type": "category"}


def test_selection_filters():
    state = {"experiments": []}
    rows = [
        _ctr_row("http://x/good.html", "name puzzles", impr=800, pos=5.0),
        _ctr_row("http://x/low-impr.html", impr=100),          # below floor
        _ctr_row("http://x/page2.html", pos=14.0),             # not page 1
        _ctr_row("http://x/money.html", impr=900, pos=4.0),    # has revenue
        _ctr_row("http://x/dupe.html", impr=900, pos=4.0),     # dup loser
        _ctr_row("http://x/brand.html", query="alphabet trains toys",
                 impr=900, pos=4.0),                            # brand query
        _ctr_row("http://x/enable-cookies", impr=900, pos=4.0),  # system page
        {"url": "http://x/blog/post", "query": "q", "impressions": 900,
         "position": 4.0, "actual_ctr": 0.5,
         "asset_type": "blog"},                                  # not writable
    ]
    picked, funnel = sl.select_candidates(
        rows, [], state,
        revenue_by_url={"http://x/money.html": 120.0},
        dup_losers={"http://x/dupe.html"})
    assert [c["url"] for c in picked] == ["http://x/good.html"], picked
    # The funnel accounts for every rejection.
    assert funnel["eligible"] == 1 and funnel["selected"] == 1
    assert sum(funnel["rejections"].values()) == len(rows) - 1, funnel


def test_selection_skips_open_and_reverted():
    state = {"experiments": [
        {"url": "http://x/open.html", "status": "applied"},
        {"url": "http://x/reverted.html", "status": "reverted",
         "reverted_at": datetime.now().isoformat()},
    ]}
    rows = [_ctr_row("http://x/open.html", impr=900, pos=4.0),
            _ctr_row("http://x/reverted.html", impr=900, pos=4.0),
            _ctr_row("http://x/fresh.html", impr=400, pos=6.0)]
    picked, _f = sl.select_candidates(rows, [], state)
    assert [c["url"] for c in picked] == ["http://x/fresh.html"]


def test_failed_experiments_are_retryable():
    state = {"experiments": [
        {"url": "http://x/failed.html", "status": "failed",
         "created_at": datetime.now().isoformat()},
    ]}
    picked, _f = sl.select_candidates(
        [_ctr_row("http://x/failed.html", impr=500, pos=5.0)], [], state)
    assert [c["url"] for c in picked] == ["http://x/failed.html"]


def test_drift_arm():
    win = [{"url": "http://x/slide.html", "query": "q", "impressions": 400,
            "trend": "down", "position_delta": -5.0, "asset_type": "category",
            "position_prev28": 8.0, "position_now28": 13.0},
           {"url": "http://x/wobble.html", "query": "q", "impressions": 400,
            "trend": "down", "position_delta": -1.5,   # below DRIFT_POSITIONS
            "asset_type": "category",
            "position_prev28": 8.0, "position_now28": 9.5}]
    picked, _f = sl.select_candidates([], win, {"experiments": []})
    assert [c["url"] for c in picked] == ["http://x/slide.html"]
    assert picked[0]["arm"] == "drift"


def test_selection_respects_limit():
    rows = [_ctr_row(f"http://x/p{i}.html", impr=300 + i, pos=5.0)
            for i in range(10)]
    picked, _f = sl.select_candidates(rows, [], {"experiments": []}, limit=5)
    assert len(picked) == 5
    # ranked by impressions × gap — highest impressions first here
    assert picked[0]["url"] == "http://x/p9.html"


# ---------------------------------------------------------------- verify
def _exp(status="applied", b_ctr=0.01, b_pos=8.0, site_impr=10000):
    return {"status": status,
            "baseline": {"page": {"ctr": b_ctr, "position": b_pos,
                                  "impressions": 500},
                         "site": {"impressions": site_impr}}}


def test_verify_keep_on_ctr_lift():
    v, d = sl.verify_experiment(_exp(), {"ctr": 0.015, "position": 8.0,
                                         "impressions": 500},
                                {"impressions": 10000})
    assert v == "keep", d


def test_verify_seasonal_dip_not_blamed():
    # Page CTR fell 20% but the whole site's impressions doubled (seasonal
    # query flood) — normalized, that's not the rewrite's fault.
    v, d = sl.verify_experiment(_exp(), {"ctr": 0.008, "position": 8.2,
                                         "impressions": 900},
                                {"impressions": 20000})
    assert v == "neutral", (v, d)


def test_verify_revert_needs_two_strikes():
    bad = {"ctr": 0.008, "position": 11.0, "impressions": 400}
    v1, _ = sl.verify_experiment(_exp(), bad, {"impressions": 10000})
    assert v1 == "suspect"
    v2, _ = sl.verify_experiment(_exp(status="suspect"), bad, {"impressions": 10000})
    assert v2 == "revert"


def test_verify_suspect_recovers_to_neutral():
    ok = {"ctr": 0.0095, "position": 8.3, "impressions": 450}
    v, d = sl.verify_experiment(_exp(status="suspect"), ok, {"impressions": 10000})
    assert v == "neutral", (v, d)


def test_verify_inconclusive_without_impressions():
    v, _ = sl.verify_experiment(_exp(), {"ctr": 0, "position": 0, "impressions": 0},
                                {"impressions": 10000})
    assert v == "inconclusive"


# ------------------------------------------------------------------ caps
def test_write_counters():
    today = datetime.now().isoformat()
    old = (datetime.now() - timedelta(days=30)).isoformat()
    state = {"experiments": [
        {"applied_at": today, "status": "applied"},
        {"applied_at": old, "status": "kept"},
        {"applied_at": None, "status": "proposed"},
    ]}
    assert sl.writes_today(state) == 1
    assert sl.open_writes_this_week(state) == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
