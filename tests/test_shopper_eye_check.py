from scripts.shopper_eye_check import page_findings, prechecked_addons, return_days, policy_days

PAGE = ('<p>Return Info Eligible for return within 30 days of the purchase</p>'
        '<input type="checkbox" class="panth-addon-check" checked="checked"/><img alt="Heirloom Name Puzzle Stool">'
        '<input type="checkbox" class="panth-addon-check" checked="checked"/><img alt="My Very Own Name Book">'
        '<input type="checkbox" class="panth-addon-check"/><img alt="Unticked Book">'
        '<input type="checkbox" name="persistent_remember_me" checked="checked"/>'
        '<div>358 total units currently In Stock</div><div>CUSTOM MADE TO ORDER, 12 weeks or longer</div>')
POLICY = "Alphabet Name Trains: Can be returned within 60 days of purchase. All Other Items: Can be returned within 60 days of purchase."


def test_prechecked_ignores_remember_me():
    assert prechecked_addons(PAGE) == ["Heirloom Name Puzzle Stool", "My Very Own Name Book"]


def test_days():
    assert return_days("Eligible for return within 30 days of the purchase") == [30]
    assert policy_days(POLICY, "https://x/5-letter-name-train.html") == 60


def test_findings():
    f = page_findings("https://x/5-letter-name-train.html", PAGE, POLICY)
    joined = " | ".join(f)
    assert "TICKED BY DEFAULT: 2" in joined and "made-to-order among them: Heirloom" in joined
    assert "page says [30] days, Return Policy says 60" in joined
    assert "STOCK vs MADE-TO-ORDER" in joined


def test_bundle_made_to_order_even_unticked():
    from scripts.shopper_eye_check import bundle_made_to_order
    page = ('<input type="checkbox" class="panth-addon-check"/><img alt="Personalized Coat Rack">'
            '<input type="checkbox" class="panth-addon-check"/><img alt="My Very Own Name Book">')
    assert bundle_made_to_order(page) == ["Personalized Coat Rack"]
