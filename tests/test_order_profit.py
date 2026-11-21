import json
from pathlib import Path

from src.analysis.order_profit import order_profit, summarize, item_cost

CFG = json.loads((Path(__file__).resolve().parent.parent / "config" / "defaults.json").read_text())
CARPETS = {"13728": {"cost": 256.95, "shipping": 57.81}}


def test_rug_order_with_awin_commission():
    o = {"number": "2000010217", "status": "processing", "charged": 481.95,
         "items": [{"sku": "13728", "name": "Calming Circles 8ft x 12ft Rectangle Classroom Rug", "qty": 1,
                    "revenue": 481.95}]}
    r = order_profit(o, CFG, CARPETS, {"2000010217": 48.20})
    assert r["basis"] == "exact"
    assert r["cost"] == 314.76 and r["card_fee"] == 14.46 and r["commission"] == 48.20
    assert r["contribution"] == round(481.95 - 314.76 - 14.46 - 48.20, 2)


def test_name_train_exact_from_maple_landmark_costs():
    o = {"number": "1", "status": "complete", "charged": 39.99,
         "items": [{"sku": "5-letter", "name": "5 Letter Maple Landmark Alphabet Name Train", "qty": 1, "revenue": 39.99}]}
    r = order_profit(o, CFG, {}, {})
    assert r["basis"] == "exact"
    cost = 7 * 3.60 + 13.00                # 5 letters + engine + caboose, + drop-ship and shipping
    assert r["cost"] == round(cost, 2)
    assert r["contribution"] == round(39.99 - cost - round(0.03 * 39.99, 2), 2)
    # two trains in one order: one drop-ship fee and shipping
    o2 = dict(o, charged=79.98, items=o["items"] * 2)
    assert order_profit(o2, CFG, {}, {})["cost"] == round(2 * 7 * 3.60 + 13.00, 2)
    # the wall mount sold on its own is priced from the invoice ($12.15)
    o3 = dict(o, items=[{"sku": "wm", "name": "NameTrain Wall Mount for 8 Cars", "qty": 1, "revenue": 36.99}])
    assert order_profit(o3, CFG, {}, {})["lines"][0]["cost"] == 12.15


def test_refunded_tag_order_costs_the_card_fee():
    o = {"number": "2000010184", "status": "closed", "charged": 119.99, "refunded": 119.99,
         "items": [{"sku": "TAG1", "name": "Montessori Wooden Ball Tracker", "qty": 1, "revenue": 119.99}]}
    r = order_profit(o, CFG, {}, {})
    assert r["revenue"] == 0 and r["contribution"] == -3.6 and r["basis"] == "refunded"


def test_guidecraft_drop_ship_fee_and_unknown_items_flagged():
    o = {"number": "2", "status": "complete", "charged": 100.0,
         "items": [{"sku": "G6230", "name": "Arches and Tunnels", "qty": 1, "revenue": 100.0}]}
    r = order_profit(o, CFG, {}, {})
    assert r["cost"] == round(100 * (1 - 0.496), 2) + 5.0
    c, basis = item_cost({"sku": "X1", "name": "Mystery Gadget", "revenue": 10}, CFG, {})
    assert basis.startswith("assumed")


def test_canceled_orders_count_nothing():
    r = order_profit({"number": "3", "status": "canceled", "items": []}, CFG, {}, {})
    assert r["contribution"] == 0 and r["basis"] == "canceled"


def test_summarize():
    rows = summarize([{"revenue": 100, "contribution": 20, "k": "a"}, {"revenue": 50, "contribution": -5, "k": "b"},
                      {"revenue": 100, "contribution": 30, "k": "a"}], lambda r: r["k"])
    assert rows[0] == ("a", 2, 200, 50, 25.0)


def _train(n_letters, options, revenue=60.0):
    o = {"number": "x", "status": "complete", "charged": revenue,
         "items": [{"sku": "t", "name": f"{n_letters} Letter Maple Landmark Name Train", "qty": 1,
                    "revenue": revenue, "options": options}]}
    return order_profit(o, CFG, {}, {})


def test_every_train_matches_its_maple_landmark_invoice():
    """Car costs from invoices 136707-136974 (the $13 per order is added on top)."""
    cases = {  # order: (letters, options, invoice car/add-on total)
        "2000010181": (6, ["Color: Pastel", "Engine: Noah's Ark", "Caboose: Circus",
                           "Tracks (each track fits 3 cars): Three 8\" Straight Tracks", "Spacer: N"], 35.60),
        "2000010182": (4, ["Color: Primary", "Engine: Gray Pastel", "Caboose: Red", "2 X 8\" Straight Tracks: No",
                           "Spacer: None"], 21.60),
        "2000010193": (5, ["Engine: None", "Caboose: None", "3 X 8\" Straight Tracks: None"], 18.00),
        "2000010214": (4, ["Engine: Gray Pastel", "Caboose: None", "2 X 8\" Straight Tracks: No"], 18.00),
        "2000010212": (7, ["Engine: Noah's Ark", "Caboose: None", "3 X 8\" Straight Tracks: No"], 28.90),
        "2000010209": (8, ["Engine: Classic", "Caboose: Pink Pastel", "Special Instructions - Requests: (typed text)"], 36.10),
        "2000010208": (4, ["Engine: Black", "Caboose: Red", "2 X 8\" Straight Tracks: Yes"], 26.00),
        "2000010195": (5, ["Engine: Classic", "Caboose: Circus", "3 X 8\" Straight Tracks: Yes"], 32.00),
        "2000010224": (6, ["Engine: Black", "Caboose: Red", "Tracks (each track fits 3 cars): Three 8\" Straight Tracks",
                           "Spacer: None"], 35.40),
    }
    for order, (n, opts, invoice) in cases.items():
        r = _train(n, opts)
        assert r["basis"] == "exact", (order, r["lines"])
        assert r["cost"] == round(invoice + 13.00, 2), (order, r["cost"], invoice)


def test_unpriced_option_still_charges_the_per_order_cost():
    r = _train(5, ["Engine: Black", "Gift Box: Deluxe"])
    assert r["basis"] == "estimated" and r["cost"] == round(7 * 3.60 + 13.00, 2)
