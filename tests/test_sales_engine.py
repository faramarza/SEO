"""Tests for the sales engine — the money-leak finder + idiot-proof daily plan.

Pure functions over the data the evaluation already persists; no flask, no
network, no LLM. The load-bearing guarantee under test is NO FABRICATION: with
missing data the engine omits leaks / dollar figures rather than inventing them.
"""
from datetime import date, timedelta

from src.analysis.sales_engine import (
    build_sales_leaks, build_daily_plan, sales_readiness, _store_profile,
    TARGET_CLOSE, MIN_CARTS,
)

_PERSONALIZED_CFG = {"business_context": {"product_families": [
    "name trains", "personalized name puzzles", "personalized baby books"]}}


def _eval():
    """A realistic evaluation blob: a real funnel + a leaky book, a dead-traffic
    product page, a winner, and a blog page (must be ignored for sales leaks)."""
    return {
        "timestamp": "2026-10-20T00:00:00",
        "ga4_account": {"sessions": 4000, "add_to_carts": 600, "purchases": 120,
                        "revenue": 7200.0, "aov": 60.0},
        "results": [
            {"asset_type": "product", "url": "https://x.com/goodnight-book.html",
             "title": "Goodnight Little Me Book", "ga4_sessions": 200,
             "ga4_add_to_carts": 40, "ga4_purchases": 2,
             "item_added_to_cart": 52, "item_purchases": 3, "item_revenue": 180.0},
            {"asset_type": "product", "url": "https://x.com/name-train.html",
             "title": "Wooden Name Train", "ga4_sessions": 120,
             "ga4_add_to_carts": 30, "ga4_purchases": 28,
             "item_added_to_cart": 30, "item_purchases": 28, "item_revenue": 2240.0},
            {"asset_type": "product", "url": "https://x.com/shape-sorter.html",
             "title": "Montessori Shape Sorter", "ga4_sessions": 90,
             "ga4_add_to_carts": 0, "ga4_purchases": 0},
            {"asset_type": "blog", "url": "https://x.com/blog/montessori-tips.html",
             "title": "Montessori Tips", "ga4_sessions": 500,
             "ga4_add_to_carts": 0, "ga4_purchases": 0},
        ],
    }


# ── No fabrication ───────────────────────────────────────────────────────────

def test_empty_data_yields_no_leaks():
    b = build_sales_leaks({}, {}, None)
    assert b["leaks"] == []
    assert b["winners"] == []
    assert b["totals"]["recoverable_monthly"] is None
    assert b["totals"]["aov"] is None


def test_readiness_blocks_without_ecommerce_events():
    # Visits but no add_to_cart / purchase events = blind to the funnel.
    ev = {"results": [{"asset_type": "product", "ga4_sessions": 100}],
          "ga4_account": {"sessions": 100}}
    r = sales_readiness(ev, None)
    assert r["ready"] is False
    keys = {c["key"]: c["ok"] for c in r["checks"]}
    assert keys["evaluation"] is True
    assert keys["ga4_ecommerce"] is False


def test_readiness_ready_with_full_data():
    r = sales_readiness(_eval(), {"https://x.com/name-train.html": [{"t": "ok"}]})
    assert r["ready"] is True
    assert all(c["ok"] for c in r["checks"])


def test_no_aov_means_no_dollar_figure_but_leak_still_found():
    ev = _eval()
    ev["ga4_account"] = {"sessions": 4000, "add_to_carts": 600, "purchases": 120,
                         "revenue": 0, "aov": None}
    # also strip per-item revenue so there's no unit price anywhere
    for r in ev["results"]:
        r.pop("item_revenue", None)
    b = build_sales_leaks(ev, {}, None)
    checkout = next((l for l in b["leaks"] if l["id"] == "checkout_abandonment"), None)
    assert checkout is not None            # the leak is still detected
    assert checkout["dollars"] is None     # but never invented without AOV


# ── Leak detection + math ──────────────────────────────────────────────────

def test_checkout_abandonment_sized_correctly():
    b = build_sales_leaks(_eval(), {}, None)
    co = next(l for l in b["leaks"] if l["id"] == "checkout_abandonment")
    # close = 120/600 = 20%; (30%-20%) * 600 carts * $60 = $3600
    assert co["dollars"] == 3600
    assert co["severity"] == "high"
    assert co["category"] == "conversion"


def _texts(steps):
    return [s["text"] if isinstance(s, dict) else s for s in steps]


def test_leaky_product_detected_and_book_gets_preview_step():
    b = build_sales_leaks(_eval(), {}, None)
    book = next(l for l in b["leaks"] if l["id"].startswith("leaky_product:")
                and "goodnight-book" in l["id"])
    # 52 carts, 3 buys -> lost (0.30 - 3/52)*52 = ~12.6 orders * ($180/3=$60) ≈ $756
    assert book["dollars"] == 756
    txt = " ".join(_texts(book["steps"])).lower()
    assert "preview" in txt or "proof" in txt


def test_steps_have_stable_ids():
    b = build_sales_leaks(_eval(), _PERSONALIZED_CFG, None)
    for leak in b["leaks"] + b["winners"]:
        for i, s in enumerate(leak["steps"]):
            assert set(s.keys()) >= {"id", "text"}
            assert s["id"] == f"{leak['id']}#{i}"


def test_store_profile_inference():
    assert _store_profile(_PERSONALIZED_CFG)["personalized"] is True
    assert _store_profile(_PERSONALIZED_CFG)["no_returns"] is True
    assert _store_profile({})["personalized"] is False
    # explicit override wins
    cfg = {"business_context": {"product_families": ["widgets"], "sells_personalized": True}}
    assert _store_profile(cfg)["personalized"] is True


def test_personalized_checkout_drops_returns_adds_preview():
    b = build_sales_leaks(_eval(), _PERSONALIZED_CFG, None)
    co = next(l for l in b["leaks"] if l["id"] == "checkout_abandonment")
    txt = " ".join(_texts(co["steps"])).lower()
    assert "preview" in txt or "proof" in txt           # trust substitute for no-returns
    assert "personaliz" in txt                          # the form-friction lever
    # never tells a no-returns store to "add a returns guarantee"
    assert "returns/guarantee" not in txt
    assert "add a returns" not in txt


def test_personalized_leaky_product_drops_generic_returns():
    b = build_sales_leaks(_eval(), _PERSONALIZED_CFG, None)
    prod = next(l for l in b["leaks"] if l["id"].startswith("leaky_product:"))
    txt = " ".join(_texts(prod["steps"])).lower()
    assert "preview" in txt or "proof" in txt
    assert "returns / satisfaction guarantee" not in txt


def test_dead_traffic_only_flags_product_pages_not_blogs():
    b = build_sales_leaks(_eval(), {}, None)
    dead = [l for l in b["leaks"] if l["id"].startswith("dead_traffic:")]
    assert any("shape-sorter" in l["id"] for l in dead)        # product w/ traffic, no sales
    assert not any("blog" in l["id"] for l in dead)            # blog is never a sales leak


def test_leaks_sorted_by_dollars_descending():
    b = build_sales_leaks(_eval(), {}, None)
    sized = [l["dollars"] for l in b["leaks"] if l["dollars"]]
    assert sized == sorted(sized, reverse=True)
    # unsized leaks come after sized ones
    first_unsized = next((i for i, l in enumerate(b["leaks"]) if l["dollars"] is None), len(b["leaks"]))
    assert all(b["leaks"][i]["dollars"] for i in range(first_unsized))


def test_winner_detected():
    b = build_sales_leaks(_eval(), {}, None)
    # name-train converts 28/120 = 23% vs site 3% -> a starved winner
    assert any("name-train" in w["id"] for w in b["winners"])
    assert all(w["dollars"] is None for w in b["winners"])     # upside, not booked money


def test_no_reviews_leak_only_for_products_without_stored_reviews():
    reviews = {"https://x.com/name-train.html": [{"t": "great"}]}
    b = build_sales_leaks(_eval(), {}, reviews)
    nr = [l for l in b["leaks"] if l["id"].startswith("no_reviews:")]
    assert any("goodnight-book" in l["id"] for l in nr)        # no reviews on file
    assert not any("name-train" in l["id"] for l in nr)        # has reviews -> not flagged


def test_min_carts_threshold_suppresses_noise():
    ev = _eval()
    ev["results"] = [{"asset_type": "product", "url": "https://x.com/tiny.html",
                      "title": "Tiny", "ga4_sessions": 50,
                      "item_added_to_cart": MIN_CARTS - 1, "item_purchases": 0}]
    b = build_sales_leaks(ev, {}, None)
    assert not any(l["id"].startswith("leaky_product:") for l in b["leaks"])


# ── Daily plan ─────────────────────────────────────────────────────────────

def test_daily_plan_three_items_top_is_biggest_leak():
    b = build_sales_leaks(_eval(), {}, None)
    plan = build_daily_plan(b, {"fixed": {}, "days": {}}, date(2026, 10, 19))
    assert len(plan["items"]) == 3
    assert plan["items"][0]["slot"] == 1
    assert plan["items"][0]["leak_id"] == "checkout_abandonment"
    assert plan["items"][0]["dollars"] == 3600


def test_marking_top_leak_fixed_promotes_next():
    b = build_sales_leaks(_eval(), {}, None)
    state = {"fixed": {"checkout_abandonment": True}, "days": {}}
    plan = build_daily_plan(b, state, date(2026, 10, 19))
    assert plan["items"][0]["leak_id"] != "checkout_abandonment"


def test_daily_plan_second_slot_rotates_by_weekday():
    b = build_sales_leaks(_eval(), {}, None)
    titles = set()
    monday = date(2026, 10, 19)  # Monday
    for i in range(7):
        plan = build_daily_plan(b, {"fixed": {}, "days": {}}, monday + timedelta(days=i))
        titles.add(plan["items"][1]["title"])
    assert len(titles) == 7  # a different pillar task each day of the week


def test_done_count_and_streak():
    b = build_sales_leaks(_eval(), {}, None)
    state = {"fixed": {}, "days": {
        "2026-10-17": {"done": ["x"]},
        "2026-10-18": {"done": ["y"]},
        "2026-10-19": {"done": ["z"]},
    }}
    # today (20th) not done yet -> streak counts the run ending yesterday = 3
    plan = build_daily_plan(b, state, date(2026, 10, 20))
    assert plan["streak"] == 3
    assert plan["done_count"] == 0
    # complete an item today -> streak 4
    item0 = plan["items"][0]["id"]
    state["days"]["2026-10-20"] = {"done": [item0]}
    plan = build_daily_plan(b, state, date(2026, 10, 20))
    assert plan["streak"] == 4
    assert plan["done_count"] == 1
    assert plan["all_done"] is False
