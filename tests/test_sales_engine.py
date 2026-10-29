"""Tests for the sales engine — the money-leak finder + idiot-proof daily plan.

Pure functions over the data the evaluation already persists; no flask, no
network, no LLM. The load-bearing guarantee under test is NO FABRICATION: with
missing data the engine omits leaks / dollar figures rather than inventing them.
"""
from datetime import date, timedelta

from src.analysis.sales_engine import (
    build_sales_leaks, build_daily_plan, sales_readiness, _store_profile, _is_b2b,
    site_funnel, landing_leaks, product_links_for, product_shop_block,
    TARGET_CLOSE, MIN_CARTS, TARGET_ATC, MIN_VIEWS,
)


_CATALOG = [
    {"asset_type": "product", "url": "https://x/montessori-stacking-ring.html",
     "title": "Montessori Stacking Ring"},
    {"asset_type": "product", "url": "https://x/5-letter-name-train.html",
     "title": "5 Letter Name Train"},
    {"asset_type": "product", "url": "https://x/classroom-rug-8x12.html",
     "title": "Carpets for Kids Classroom Rug 8ft x 12ft"},   # B2B — must be excluded
    {"asset_type": "blog", "url": "https://x/blog/tips.html", "title": "Montessori Tips"},
]


def test_product_links_match_by_topic_exclude_b2b():
    page = {"landing_page": "/blog/best-montessori-toys.html"}
    out = product_links_for(page, _CATALOG, {})
    urls = [m["url"] for m in out]
    assert "https://x/montessori-stacking-ring.html" in urls   # topic match
    assert "https://x/classroom-rug-8x12.html" not in urls     # B2B excluded
    assert "https://x/blog/tips.html" not in urls              # not a product


def test_product_links_empty_when_no_match():
    page = {"landing_page": "/blog/holiday-recipes.html"}
    assert product_links_for(page, _CATALOG, {}) == []


def test_classify_page_links_counts_products_vs_categories():
    from src.analysis.sales_engine import classify_page_links
    html = ('<a href="https://x/name-train.html">Shop</a>'
            '<a href="/montessori-toys">Montessori Toys</a>'   # category (relative)
            '<a href="/name-train.html">dup</a>'               # dup of product -> not double-counted
            '<a href="/blog/other.html">other</a>')            # neither
    info = classify_page_links(html,
                               product_urls=["https://x/name-train.html"],
                               category_urls=["https://x/montessori-toys"])
    assert info["product_links"] == 1 and info["category_links"] == 1
    assert info["has_product_links"] and info["has_category_links"]


def test_classify_page_links_excludes_nav_boilerplate():
    from src.analysis.sales_engine import classify_page_links
    html = ('<a href="/name-train.html">in-content</a>'
            '<a href="/montessori-toys">nav category</a>')
    # the nav category link is boilerplate (on every page) -> excluded
    info = classify_page_links(html,
                               product_urls=["https://x/name-train.html"],
                               category_urls=["https://x/montessori-toys"],
                               exclude_paths={"/montessori-toys"})
    assert info["product_links"] == 1 and info["category_links"] == 0


def test_chrome_strip_counts_in_content_links_also_in_nav():
    # THE bug: the store's nav links to the same categories its articles do, so
    # name-based stripping wrongly zeroed real in-content links. After stripping
    # the header/nav/footer STRUCTURE, the 2 in-content category links are counted.
    from src.analysis.sales_engine import classify_page_links
    html = ('<nav><a href="/montessori-toys.html">menu</a>'
            '<a href="/name-trains.html">menu</a></nav>'
            '<div class="post-content"><a href="/montessori-toys.html">Shop Montessori Toys</a>'
            '<a href="/name-trains.html">Explore Name Trains</a></div>'
            '<footer><a href="/name-trains.html">footer link</a></footer>')
    info = classify_page_links(html, product_urls=[],
                               category_urls=["https://x/montessori-toys.html",
                                              "https://x/name-trains.html"])
    assert info["category_links"] == 2 and info["product_links"] == 0


def test_recommend_for_links_is_page_specific_no_contradiction():
    from src.analysis.sales_engine import recommend_for_links
    # >=2 products -> "it's the audience"
    r = recommend_for_links("blog/info", {"product_links": 5, "category_links": 3})
    assert "already links" in r.lower() and "audience" in r.lower()
    # exactly 1 product -> must NOT claim "no links" (the old contradiction)
    r = recommend_for_links("blog/info", {"product_links": 1, "category_links": 43})
    assert "no in-content links" not in r.lower()
    assert "no links to your products" not in r.lower()
    assert "1 product" in r.lower()
    # category-only -> swap for product pages
    r = recommend_for_links("blog/info", {"product_links": 0, "category_links": 4})
    assert "category" in r.lower() and "product" in r.lower()
    # truly none -> add a shop block
    r = recommend_for_links("blog/info", {"product_links": 0, "category_links": 0})
    assert "no in-content links" in r.lower() or "shop" in r.lower()


def test_product_shop_block_is_safe_html():
    matches = [{"title": "5 Letter Name Train", "url": "https://x/5-letter-name-train.html"}]
    block = product_shop_block(matches, "Shop our name trains")
    assert "https://x/5-letter-name-train.html" in block
    assert "5 Letter Name Train" in block
    assert block.startswith("<div") and "Shop our name trains" in block
    assert product_shop_block([]) == ""   # nothing to link -> empty, never a bare box


def test_shop_block_uses_readable_name_not_raw_url():
    from src.analysis.sales_engine import _slug_name, _display_name
    # A titleless product (catalog pull had no titles) must link with a readable
    # name derived from the slug, NOT the raw URL as link text.
    block = product_shop_block([{"url": "https://alphabet-trains.com/motor-shapes.html"}])
    assert "Motor Shapes" in block             # readable name is shown
    assert ">https://" not in block            # raw URL never shown as visible text
    assert _slug_name("/motor-letters-lowercase.html") == "Motor Letters Lowercase"
    # A real title still wins; a title that's just the URL falls back to the slug.
    assert _display_name({"title": "Name Train", "url": "https://x/foo.html"}) == "Name Train"
    assert _display_name({"title": "https://x/foo.html",
                          "url": "https://x/foo.html"}) == "Foo"


def test_shop_block_renders_thumbnail_only_when_image_present():
    # A real image URL (e.g. from the sitemap) renders a thumbnail; a product
    # without one stays a clean name-only card — never a broken/empty <img>.
    block = product_shop_block([
        {"url": "https://x/motor-shapes.html",
         "image": "https://x/media/catalog/motor-shapes.jpg"},
        {"url": "https://x/motor-numbers.html"},
    ])
    assert '<img src="https://x/media/catalog/motor-shapes.jpg"' in block
    assert block.count("<img") == 1          # the second card has no image
    assert "Motor Shapes" in block and "Motor Numbers" in block


def test_landing_leaks_ranks_wasted_traffic_and_classifies():
    funnel = {
        "/blog/montessori-tips.html": {"sessions": 500, "product_view_sessions": 20},
        "/name-trains.html": {"sessions": 300, "product_view_sessions": 280},  # healthy
        "/carpets-for-kids-classroom-rug.html": {"sessions": 200, "product_view_sessions": 10},
        "/(not set)": {"sessions": 400, "product_view_sessions": 0},            # junk
        "/tiny.html": {"sessions": 5, "product_view_sessions": 0},              # below min
    }
    out = landing_leaks(funnel, {}, min_sessions=30)
    ids = [l["landing_page"] for l in out]
    assert "/blog/montessori-tips.html" in ids          # pulls traffic, ~nobody reaches a product
    assert "/name-trains.html" not in ids               # healthy view rate -> not a leak
    assert "/(not set)" not in ids                       # junk skipped
    assert "/tiny.html" not in ids                       # below min_sessions
    # ranked by wasted sessions, biggest first
    assert out[0]["wasted_sessions"] >= out[-1]["wasted_sessions"]
    blog = next(l for l in out if l["landing_page"].startswith("/blog"))
    rug = next(l for l in out if "carpets" in l["landing_page"])
    assert blog["kind"] == "blog/info" and "product" in blog["recommendation"].lower()
    assert rug["kind"] == "rug/B2B" and "quote" in rug["recommendation"].lower()


def test_landing_leaks_skips_system_pages():
    funnel = {"/checkout/cart": {"sessions": 200, "product_view_sessions": 5},
              "/customer/account/login": {"sessions": 100, "product_view_sessions": 0},
              "/blog/real-post.html": {"sessions": 100, "product_view_sessions": 2}}
    out = landing_leaks(funnel, {}, min_sessions=30)
    ids = [l["landing_page"] for l in out]
    assert "/checkout/cart" not in ids and "/customer/account/login" not in ids
    assert "/blog/real-post.html" in ids


def test_landing_leaks_skips_phantom_base64_slugs():
    funnel = {"/blog/c2NyZWVuLW": {"sessions": 80, "product_view_sessions": 0},
              "/blog/ZnVuLXdheX": {"sessions": 60, "product_view_sessions": 0},
              "/blog/montessori-activities-1-month-olds": {"sessions": 90, "product_view_sessions": 2}}
    out = landing_leaks(funnel, {}, min_sessions=30)
    ids = [l["landing_page"] for l in out]
    assert "/blog/c2NyZWVuLW" not in ids and "/blog/ZnVuLXdheX" not in ids
    assert "/blog/montessori-activities-1-month-olds" in ids   # real slug kept


def test_landing_leaks_money_ranking_and_materiality_floor():
    # Site converts ~0.73% of sessions (41 purchases / 5625 sessions), AOV 60, margin 0.30.
    funnel = {
        "/": {"sessions": 673, "product_view_sessions": 45},            # big page
        "/blog/tiny-post": {"sessions": 45, "product_view_sessions": 2},  # 40-ish wasted
    }
    out = landing_leaks(funnel, {}, min_sessions=30, site_purchase_rate=41/5625,
                        aov=60.0, margin=0.30, window_days=28, min_monthly_value=50.0)
    by = {l["landing_page"]: l for l in out}
    # Homepage: ~628 wasted -> ceiling well above $50/mo -> not below floor, ranked first
    assert by["/"]["ceiling_profit_mo"] > 50 and by["/"]["below_floor"] is False
    assert out[0]["landing_page"] == "/"          # ranked by money, biggest first
    # Tiny blog post: ~43 wasted -> ceiling a few dollars/mo -> below floor
    assert by["/blog/tiny-post"]["below_floor"] is True
    assert by["/blog/tiny-post"]["ceiling_profit_mo"] < 50


def test_b2b_opportunity_excludes_zero_view_phantom_carts():
    from src.analysis.sales_engine import b2b_opportunity
    # Mirrors the real data: some rugs have real views+carts (credible), others have
    # carts but ZERO views (attribution ghosts that must NOT inflate the headline).
    prods = [
        {"title": "Circletime Rug", "url": "https://x/circletime.html",
         "views": 7, "add_to_carts": 2, "orders": 0},     # credible
        {"title": "Colorful Rows Rug", "url": "https://x/colorful.html",
         "views": 6, "add_to_carts": 3, "orders": 0},     # credible
        {"title": "KidSoft Tranquil Trees", "url": "https://x/kidsoft.html",
         "views": 0, "add_to_carts": 8, "orders": 0},     # 0 views -> phantom
        {"title": "8x12 Tan Rug", "url": "https://x/tan.html",
         "views": 0, "add_to_carts": 4, "orders": 0},     # 0 views -> phantom
        {"title": "World Map Rug", "url": "https://x/worldmap.html",
         "views": 10, "add_to_carts": 2, "orders": 1},    # converted -> not stuck
    ]
    opp = b2b_opportunity(prods)
    assert opp["add_to_carts"] == 5            # 2 + 3 credible only, NOT 17
    assert opp["stuck_count"] == 2             # the two viewed-and-carted rugs
    assert opp["unverified_carts"] == 12 and opp["unverified_count"] == 2
    assert "attribution noise" in opp["what"].lower()
    assert all(t["title"] in ("Circletime Rug", "Colorful Rows Rug") for t in opp["top"])
    assert "dollars" not in opp                 # no fabricated $ figure
    # All carts are phantom (no views anywhere) -> no credible signal -> no opportunity.
    assert b2b_opportunity([{"title": "x", "views": 0, "add_to_carts": 9, "orders": 0}]) is None
    assert b2b_opportunity([]) is None


def test_recommend_for_storefront_pages_is_merchandising_not_links():
    from src.analysis.sales_engine import recommend_for_links
    for kind in ("home", "category"):
        r = recommend_for_links(kind, {"product_links": 0, "category_links": 0})
        assert "merchandising" in r.lower() or "storefront" in r.lower()
        assert "not missing links" in r.lower()


def test_category_hub_advises_subcategories_not_products():
    # A super-category hub (links to several sub-categories, no products) must get
    # funnel-to-sub-category advice, NOT "paste a product list".
    from src.analysis.sales_engine import recommend_for_links
    r = recommend_for_links("category", {"product_links": 0, "category_links": 4})
    assert "hub" in r.lower() and "sub-categor" in r.lower()
    assert "don't paste a flat product list" in r.lower()
    # A leaf category (no sub-category links) still gets the merchandising advice.
    leaf = recommend_for_links("category", {"product_links": 0, "category_links": 0})
    assert "hub" not in leaf.lower()


def test_landing_leaks_empty_without_data():
    assert landing_leaks({}, {}) == []
    assert landing_leaks(None, {}) == []


def _ga4_funnel(total=5652, viewers=1965, carts=116, checkouts=60, buys=38,
                engaged=None):
    # engaged = distinct users who reached a product any way (view/select/cart).
    # Defaults to the view_item count so the headline "reached a product" number
    # equals viewers unless a test sets a higher carousel-inclusive figure.
    f = {"_totals": {"users": total, "sessions": total * 2},
         "view_item": {"events": viewers + 800, "users": viewers},
         "select_item": {"events": viewers, "users": max(viewers // 2, 1)},
         "add_to_cart": {"events": carts + 600, "users": carts},
         "begin_checkout": {"events": checkouts, "users": checkouts},
         "purchase": {"events": buys + 7, "users": buys}}
    f["_engaged"] = {"users": engaged if engaged is not None else viewers}
    return f

_PERSONALIZED_CFG = {"business_context": {"product_families": [
    "name trains", "personalized name puzzles", "personalized baby books"]}}


def _funnel_eval():
    """Mirrors the real store: a B2B classroom rug (huge views, ~no carts) that
    must be set aside, a consumer name train with a low add-to-cart rate (the real
    leak), and a healthy consumer product that must NOT be flagged."""
    return {
        "timestamp": "2026-10-24T00:00:00",
        "ga4_account": {"sessions": 3523, "add_to_carts": 55, "purchases": 12,
                        "revenue": 720.0, "aov": 60.0},
        "results": [
            {"asset_type": "product", "url": "https://x/world-map-rug.html",
             "title": "Carpets for Kids Rug World Map Value Rug 4X6",
             "ga4_sessions": 400, "item_viewed": 572, "item_added_to_cart": 3,
             "item_purchases": 1, "item_revenue": 450.0},
            {"asset_type": "product", "url": "https://x/name-train.html",
             "title": "7 Letter Maple Landmark Name Train",
             "ga4_sessions": 39, "item_viewed": 300, "item_added_to_cart": 6,
             "item_purchases": 4, "item_revenue": 240.0},
            {"asset_type": "product", "url": "https://x/name-puzzle.html",
             "title": "Personalized Name Puzzle",
             "ga4_sessions": 50, "item_viewed": 200, "item_added_to_cart": 16,
             "item_purchases": 10, "item_revenue": 600.0},
        ],
    }


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


# ── Per-product funnel + B2B split ──────────────────────────────────────────

def test_is_b2b_classifies_classroom_rugs():
    assert _is_b2b("Carpets for Kids Rug World Map Value Rug 4X6") is True
    assert _is_b2b("World Map Classroom Rug 8ft x 12ft") is True
    assert _is_b2b("7 Letter Maple Landmark Name Train") is False
    # config override
    cfg = {"business_context": {"b2b_product_patterns": [r"wholesale"]}}
    assert _is_b2b("Wholesale Pallet", cfg) is True
    assert _is_b2b("Classroom Rug", cfg) is False  # default list no longer applies


def test_low_add_to_cart_leak_flags_consumer_product():
    b = build_sales_leaks(_funnel_eval(), _PERSONALIZED_CFG, None)
    atc = [l for l in b["leaks"] if l["id"].startswith("low_atc:")]
    # name train: 300 views, 6 carts = 2% ATC < 5% target -> flagged
    assert any("name-train" in l["id"] for l in atc)
    # healthy name puzzle: 200 views, 16 carts = 8% -> NOT flagged
    assert not any("name-puzzle" in l["id"] for l in atc)


def test_b2b_rug_set_aside_not_a_consumer_leak():
    b = build_sales_leaks(_funnel_eval(), _PERSONALIZED_CFG, None)
    assert not any("world-map-rug" in l["id"] for l in b["leaks"])   # never a leak
    assert any("world-map-rug" in x["url"] for x in b["b2b"])        # shown separately
    assert b["totals"]["b2b_count"] == 1


def test_add_to_cart_rate_prefers_item_level():
    # With item_viewed present, use the consumer item-level rate (reliable),
    # NOT the blended account ratio. Consumer carts 6+16=22, views 300+200=500.
    b = build_sales_leaks(_funnel_eval(), _PERSONALIZED_CFG, None)
    assert b["totals"]["add_to_cart_basis"] == "item"
    assert b["totals"]["add_to_cart_rate_pct"] == round(22 / 500 * 100, 2)  # 4.4%
    assert b["totals"]["add_to_cart_target_pct"] == int(TARGET_ATC * 100)


def test_add_to_cart_rate_falls_back_to_blended_without_item_views():
    ev = _funnel_eval()
    for r in ev["results"]:
        r.pop("item_viewed", None)
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, None)
    assert b["totals"]["add_to_cart_basis"] == "blended"
    assert b["totals"]["add_to_cart_rate_pct"] == round(55 / 3523 * 100, 2)  # 1.56%


def test_leak_product_not_also_a_winner():
    b = build_sales_leaks(_funnel_eval(), _PERSONALIZED_CFG, None)
    leak_urls = {u for l in b["leaks"] for u in l.get("where", [])}
    winner_urls = {u for w in b["winners"] for u in w.get("where", [])}
    assert not (leak_urls & winner_urls)   # no product is both a leak and a winner


def test_site_funnel_by_users_and_leak_step():
    ev = _funnel_eval()
    ev["ga4_funnel"] = _ga4_funnel()
    f = site_funnel(ev)
    assert f["available"] is True
    assert f["product_view_rate_pct"] == round(1965 / 5652 * 100, 1)   # ~34.8
    assert f["add_to_cart_rate_pct"] == round(116 / 1965 * 100, 1)     # ~5.9 (of viewers)
    assert f["close_rate_pct"] == round(38 / 116 * 100, 1)             # ~32.8
    assert f["leak_step"]["key"] == "view"   # low view rate is the leak, not cart/close


def test_funnel_leak_leads_do_these_today():
    ev = _funnel_eval()
    ev["ga4_funnel"] = _ga4_funnel()
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, None)
    assert b["leaks"][0]["id"] == "funnel:view"
    assert b["totals"]["add_to_cart_basis"] == "funnel"
    assert b["totals"]["add_to_cart_rate_pct"] == round(116 / 1965 * 100, 1)


def test_channel_breakdown_splits_the_blended_funnel():
    from src.analysis.sales_engine import channel_breakdown
    chans = {
        "Organic Search": {"users": 3000, "reached": 90, "buyers": 3},    # tire-kickers
        "Paid Search":    {"users": 300,  "reached": 240, "buyers": 12},  # shoppers
        "Direct":         {"users": 500,  "reached": 300, "buyers": 20},
        "Email":          {"users": 10,   "reached": 8,   "buyers": 2},   # below min_users
    }
    out = channel_breakdown(chans, min_users=30)
    chmap = {r["channel"]: r for r in out["rows"]}
    assert "Email" not in chmap                       # too small, dropped
    assert chmap["Organic Search"]["reach_rate_pct"] == 3.0
    assert chmap["Paid Search"]["reach_rate_pct"] == 80.0
    assert out["rows"][0]["channel"] == "Organic Search"   # most users first
    # the insight must name the spread between best/worst reach channel
    assert "average" in out["insight"].lower()
    assert channel_breakdown({}, min_users=30) is None     # no data -> None, never guesses


def test_checkout_step_leak_sizes_the_payment_step():
    from src.analysis.sales_engine import _leak_checkout_step
    # cart->order healthy (41/125=33%), but checkout completion weak (41/85=48%).
    funnel = {"available": True, "_cart_users": 125, "_checkout_users": 85, "_buy_users": 41}
    leak = _leak_checkout_step({"aov": 60.0}, funnel, {})
    assert leak and leak["id"] == "checkout_step_abandonment"
    assert leak["dollars"] == round((0.60 - 41/85) * 85 * 60.0)   # ~601
    assert "44 committed buyers" in leak["what"]
    # Guard 1: when cart->order is ALSO weak, the broader cart leak owns it -> suppress.
    weak = {"available": True, "_cart_users": 200, "_checkout_users": 85, "_buy_users": 20}
    assert _leak_checkout_step({"aov": 60.0}, weak, {}) is None
    # Guard 2: healthy completion -> no leak.
    ok = {"available": True, "_cart_users": 125, "_checkout_users": 85, "_buy_users": 60}
    assert _leak_checkout_step({"aov": 60.0}, ok, {}) is None
    # No funnel -> None (never guesses).
    assert _leak_checkout_step({"aov": 60.0}, None, {}) is None


def test_healthy_close_suppresses_checkout_leak():
    ev = _funnel_eval()
    ev["ga4_funnel"] = _ga4_funnel()   # close 38/116 = 33% -> healthy
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, None)
    assert not any(l["id"] == "checkout_abandonment" for l in b["leaks"])


def test_unhealthy_close_surfaces_checkout_leak():
    ev = _funnel_eval()
    ev["ga4_funnel"] = _ga4_funnel(carts=200, buys=20)  # 10% close -> leak
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, None)
    assert any(l["id"] == "checkout_abandonment" for l in b["leaks"])


def test_no_funnel_data_returns_none():
    assert site_funnel({}) is None
    # no product engagement at all (no view_item, no _engaged) -> None
    assert site_funnel({"ga4_funnel": {"_totals": {"users": 100}}}) is None


def test_reached_a_product_uses_engaged_not_just_view_item():
    # Carousel store: 1965 opened a product page, but 3200 reached a product some
    # way (view / carousel click / add-to-cart). The headline must use the higher
    # engaged count, not undercount by only using view_item.
    ev = _funnel_eval()
    ev["ga4_funnel"] = _ga4_funnel(engaged=3200)
    f = site_funnel(ev)
    assert f["_view_users"] == 3200
    assert f["product_view_rate_pct"] == round(3200 / 5652 * 100, 1)
    # add-to-cart is now measured against everyone who reached a product
    assert f["add_to_cart_rate_pct"] == round(116 / 3200 * 100, 1)
    assert f["steps"][1]["label"] == "Reached a product"


def test_reached_a_product_falls_back_to_view_item_for_old_evals():
    # Old evaluation persisted before _engaged existed -> fall back to view_item.
    f_old = _ga4_funnel()
    f_old.pop("_engaged")
    ev = _funnel_eval()
    ev["ga4_funnel"] = f_old
    f = site_funnel(ev)
    assert f["_view_users"] == 1965
    assert f["product_view_rate_pct"] == round(1965 / 5652 * 100, 1)


def test_engaged_never_below_carts():
    # A stale/partial pull where _engaged somehow came back below the cart count
    # must not invert the funnel; engaged is floored at carts.
    ev = _funnel_eval()
    ev["ga4_funnel"] = _ga4_funnel(carts=400, engaged=100)
    f = site_funnel(ev)
    assert f["_view_users"] >= 400


def test_b2b_excluded_from_every_leak_type():
    # A classroom rug must never appear as ANY consumer leak (incl. no-reviews).
    reviews = {}  # empty review store -> no_reviews leaks are possible
    b = build_sales_leaks(_funnel_eval(), _PERSONALIZED_CFG, reviews)
    assert not any("world-map-rug" in l["id"] for l in b["leaks"])


def test_zero_attributed_purchases_is_not_a_leak():
    # 171 carts, 0 attributed purchases = GA4 attribution gap, NOT a 0% close leak.
    ev = {"ga4_account": {"sessions": 5000, "add_to_carts": 300, "purchases": 60,
                          "revenue": 3600, "aov": 60},
          "results": [{"asset_type": "product", "url": "https://x/big-boy-book.html",
                       "title": "I'm a Big Boy Book", "item_added_to_cart": 171,
                       "item_purchases": 0, "ga4_sessions": 200}]}
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, None)
    assert not any(l["id"].startswith("leaky_product:") for l in b["leaks"])


def test_junk_not_set_urls_skipped():
    ev = {"ga4_account": {"sessions": 5000, "add_to_carts": 300, "purchases": 60,
                          "revenue": 3600, "aov": 60},
          "results": [{"asset_type": "product", "url": "https://x/(not set)",
                       "title": "(not set)", "ga4_sessions": 57,
                       "item_added_to_cart": 0, "item_purchases": 0}]}
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, {})
    assert not any("(not set)" in l["id"] for l in b["leaks"])


def test_rugs_classified_b2b_by_size_and_keyword():
    assert _is_b2b("Kidsoft Tranquil Trees Green 6ft x 9ft Oval") is True
    assert _is_b2b("Carpets for Kids World Map 6ft x 9ft Rug") is True
    assert _is_b2b("8x12 Rectangular Tan Classroom Rug") is True
    assert _is_b2b("7 Letter Maple Landmark Name Train") is False
    assert _is_b2b("Heirloom Name Puzzle Stool") is False  # consumer furniture stays consumer


def test_low_atc_needs_item_viewed():
    # Without item_viewed (old evaluation), no per-product ATC leak is invented.
    ev = _funnel_eval()
    for r in ev["results"]:
        r.pop("item_viewed", None)
    b = build_sales_leaks(ev, _PERSONALIZED_CFG, None)
    assert not any(l["id"].startswith("low_atc:") for l in b["leaks"])


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
