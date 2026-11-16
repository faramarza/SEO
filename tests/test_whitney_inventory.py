from scripts.whitney_inventory import build


def test_whitney_rules():
    rows = [{"item": "WB0109", "description": "NATURE VIEW READING HAVEN", "Available Inventory": "170",
             "Availabilty Date": "1/0/1900", "Discontinue Date": "1/0/1900"},
            {"item": "WB0285R", "description": "ROLLING ART CART", "Available Inventory": "0",
             "Availabilty Date": "12/21/2026", "Discontinue Date": "1/0/1900"},
            {"item": "WB0152", "description": "*OB PRESCHOOL STEM CART", "Available Inventory": "0",
             "Availabilty Date": "12/21/2026", "Discontinue Date": "12/31/2026"},
            {"item": "WB0541", "description": "ob*NATURE VIEW POND TABLE 20H", "Available Inventory": "3",
             "Availabilty Date": "1/0/1900", "Discontinue Date": "12/31/2026"},
            {"item": "WB4375", "description": "BLOCK CART AND BUILDING BLOCK SET", "Available Inventory": "1",
             "Availabilty Date": "1/0/1900", "Discontinue Date": "12/31/2026"},
            {"item": "Page -1 of 1", "description": "N:\\Common", "Available Inventory": ""}]
    r = {x["sku"]: x for x in build(rows)}
    assert r["WB0109"]["qty"] == 170 and r["WB0109"]["is_in_stock"] == 1
    assert r["WB0285R"]["is_in_stock"] == 0 and r["WB0285R"]["note"] == "back 12/21/2026"
    assert r["WB0152"]["is_in_stock"] == 0 and "obsolete" in r["WB0152"]["note"]
    assert r["WB0541"]["qty"] == 0   # obsolete even with stock left
    assert r["WB4375"]["is_in_stock"] == 1 and "discontinued after 12/31/2026" in r["WB4375"]["note"]
    assert "Page -1 of 1" not in r
