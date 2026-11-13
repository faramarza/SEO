from scripts.guidecraft_inventory import build


def row(sku, avail, status="Active", back=""):
    return {"Name": sku, "Item Status": status, "Qty Available": avail,
            "North Carolina Back In Stock Date": back}


def test_rules():
    rows = build([
        row("G5058", "384"),
        row("G3012", "83", status="Active - Do Not Order"),
        row("G16837", "0", back="11/23/2026"),
        row("G16836US", "181"),
        row("G6110-1", "2"), row("G6110-2", "0"),
        row("G97078-1", "14"), row("G97078-2", "11"),
    ])
    by = {r["sku"]: r for r in rows}
    assert by["G5058"]["qty"] == 384 and by["G5058"]["is_in_stock"] == 1
    assert by["G3012"]["qty"] == 0 and by["G3012"]["is_in_stock"] == 0
    assert by["G16837"]["is_in_stock"] == 0 and "11/23" in by["G16837"]["note"]
    assert "G16836US" in by
    assert by["G6110"]["qty"] == 0 and by["G97078"]["qty"] == 11
    assert "G6110-1" not in by
