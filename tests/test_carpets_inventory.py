from scripts.carpets_inventory import build, sku_of


def test_item_numbers_keep_their_zeros():
    assert sku_of(36.1, "0.00") == "36.10"
    assert sku_of(13728, "General") == "13728"
    assert sku_of(72.77, "0.00") == "72.77"
    assert sku_of(1.5, "0.000") == "1.500"


def test_rules():
    rows = build([(13728, "General", "Calming Circles", "CFKC", 19, None),
                  (10.21, "0.00", "Drive & Play", "CFKC", 0, "ETA 3 Weeks"),
                  (17726, "General", "Alphabet Calming", "DISC", 3, None),
                  (None, "General", "junk", None, "x", None)])
    r = {x["sku"]: x for x in rows}
    assert r["13728"]["qty"] == 19 and r["13728"]["is_in_stock"] == 1
    assert r["10.21"]["is_in_stock"] == 0
    assert r["17726"]["is_in_stock"] == 1 and "discontinued" in r["17726"]["note"]
    assert len(rows) == 3
