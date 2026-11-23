"""Stock Update page: vendor detection, diff, and preview -> apply -> undo on a fake store."""
import io
import json

import pytest

from src.analysis.vendor_stock import VendorFileError, detect_vendor, diff

app_mod = pytest.importorskip("web.app")


def test_detect_vendor():
    assert detect_vendor("Guidecraft_Inventory_111926.csv", b"Name,Description,Item Status,Qty On hand,Qty Available") == "guidecraft"
    assert detect_vendor("wb.csv", b"Inventory Update - No Parts,11/20/2026\nitem,description,Available Inventory") == "whitney"
    assert detect_vendor("cfk.xlsx", b"PK\x03\x04") == "carpets"
    with pytest.raises(VendorFileError):
        detect_vendor("orders.csv", b"order,total")


def test_diff_only_real_changes():
    rows = [{"sku": "A", "qty": 0, "is_in_stock": 0, "note": "back 12/25"},
            {"sku": "B", "qty": 7, "is_in_stock": 1, "note": ""},
            {"sku": "C", "qty": 5, "is_in_stock": 1, "note": ""},
            {"sku": "D", "qty": 9, "is_in_stock": 1, "note": ""},
            {"sku": "ZZ", "qty": 1, "is_in_stock": 1, "note": ""}]
    cur = {"A": {"qty": 3, "in_stock": True}, "B": {"qty": 0, "in_stock": False},
           "C": {"qty": 5, "in_stock": True}, "D": {"qty": 2000, "in_stock": True}}
    d = diff(rows, cur)
    assert (d["out"], d["back"], d["qty_only"], d["unchanged"]) == (1, 1, 1, 1)
    assert d["not_in_store"] == ["ZZ"]
    assert d["changes"][0]["sku"] == "A"   # out-of-stock changes listed first


class FakeMC:
    configured = True
    store = {}

    def get_stock(self, skus):
        return {s: dict(FakeMC.store[s]) for s in skus if s in FakeMC.store}

    def set_stock(self, items):
        for i in items:
            FakeMC.store[i["sku"]].update(qty=float(i["qty"]), in_stock=bool(i["in_stock"]))
        return "source-items"


def test_preview_apply_undo(monkeypatch, tmp_path):
    import src.data_sources.magento_client as m
    FakeMC.store = {"G3012": {"qty": 80.0, "in_stock": True, "name": "Rainbow Blocks", "item_id": 1},
                    "G1121": {"qty": 10.0, "in_stock": True, "name": "Wedgies", "item_id": 2}}
    monkeypatch.setattr(m, "MagentoClient", FakeMC)
    monkeypatch.setattr(app_mod, "_STOCK_DIR", tmp_path)
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "test"
    csv = (b"Name,Description,Item Status,Qty On hand,Qty Available,Minnesota Back In Stock Date,"
           b"Minnesota Back In Stock Qty,North Carolina Back In Stock Date,North Carolina Back In Stock Qty\n"
           b"G1121,Wedgies Career Set,Active,504,504,,,,\n"
           b"G3012,Rainbow Blocks - Crystal Bead,Active - Do Not Order,81,80,,,12/22/2026,256\n"
           b"G9999,Not in store,Active,5,5,,,,\n")
    r = c.post("/api/stock/preview", data={"file": (io.BytesIO(csv), "Guidecraft_Inventory.csv")},
               content_type="multipart/form-data")
    if r.status_code in (302, 401):
        pytest.skip("login wall in this app config")
    plan = r.get_json()
    assert plan["vendor"] == "guidecraft" and plan["out"] == 1 and plan["qty_only"] == 1
    assert plan["not_in_store"] == ["G9999"]
    assert FakeMC.store["G3012"]["in_stock"] is True          # preview writes nothing
    a = c.post("/api/stock/apply", json={"id": plan["id"]}).get_json()
    assert a["success"] and a["not_confirmed"] == []
    assert FakeMC.store["G3012"] == {"qty": 0.0, "in_stock": False, "name": "Rainbow Blocks", "item_id": 1}
    assert FakeMC.store["G1121"]["qty"] == 504.0
    assert c.post("/api/stock/apply", json={"id": plan["id"]}).status_code == 400   # no double apply
    u = c.post("/api/stock/undo", json={"id": plan["id"]}).get_json()
    assert u["success"] and FakeMC.store["G3012"]["qty"] == 80.0 and FakeMC.store["G3012"]["in_stock"] is True
    assert FakeMC.store["G1121"]["qty"] == 10.0
    assert c.post("/api/stock/apply", json={"id": "../../etc"}).status_code == 404   # no path tricks
