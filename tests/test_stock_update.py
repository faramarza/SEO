"""Stock Update page: vendor detection, diff, and preview -> apply -> undo on a fake store."""
import io
import json

import pytest

from src.analysis.vendor_stock import VendorFileError, detect_vendor, diff

app_mod = pytest.importorskip("web.app")

def _wait(c, pid):
    import time
    for _ in range(100):
        s = c.get(f"/api/stock/status?id={pid}").get_json()
        if s.get("status") not in ("applying", "undoing"):
            return s
        time.sleep(0.05)
    raise AssertionError("job did not finish")



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
    refuse = set()

    def get_stock(self, skus):
        return {s: dict(FakeMC.store[s]) for s in skus if s in FakeMC.store}

    def set_stock(self, items, progress=None):
        written, failed = [], {}
        for i in items:
            if i["sku"] in FakeMC.refuse:
                failed[i["sku"]] = "refused"
                continue
            FakeMC.store[i["sku"]].update(qty=float(i["qty"]), in_stock=bool(i["in_stock"]))
            written.append(i["sku"])
        return {"method": "stock-items", "written": written, "failed": failed}


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
    assert c.post("/api/stock/apply", json={"id": plan["id"]}).get_json()["started"]
    a = _wait(c, plan["id"])
    assert a["status"] == "applied" and a["not_confirmed"] == []
    assert FakeMC.store["G3012"] == {"qty": 0.0, "in_stock": False, "name": "Rainbow Blocks", "item_id": 1}
    assert FakeMC.store["G1121"]["qty"] == 504.0
    assert c.post("/api/stock/apply", json={"id": plan["id"]}).status_code == 400   # no double apply
    c.post("/api/stock/undo", json={"id": plan["id"]})
    u = _wait(c, plan["id"])
    assert u["status"] == "undone" and FakeMC.store["G3012"]["qty"] == 80.0 and FakeMC.store["G3012"]["in_stock"] is True
    assert FakeMC.store["G1121"]["qty"] == 10.0
    assert c.post("/api/stock/apply", json={"id": "../../etc"}).status_code == 404   # no path tricks


def test_one_refused_item_does_not_block_the_rest_and_undo_covers_what_was_written(monkeypatch, tmp_path):
    import src.data_sources.magento_client as m
    FakeMC.store = {"140-340": {"qty": 22.0, "in_stock": True, "name": "Green Floor Mat", "item_id": None},
                    "WB0109": {"qty": 170.0, "in_stock": True, "name": "Reading Haven", "item_id": 5}}
    FakeMC.refuse = {"140-340"}
    monkeypatch.setattr(m, "MagentoClient", FakeMC)
    monkeypatch.setattr(app_mod, "_STOCK_DIR", tmp_path)
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "test"
    csv = (b"Inventory Update - No Parts,11/20/2026,10:23 AM,,,\nitem,description,Available Inventory,barcode,Availabilty Date,Discontinue Date\n"
           b"140-340,GREEN FLOOR MAT,16,1,1/0/1900,1/0/1900\nWB0109,NATURE VIEW READING HAVEN,164,2,1/0/1900,1/0/1900\n")
    r = c.post("/api/stock/preview", data={"file": (io.BytesIO(csv), "wb.csv")}, content_type="multipart/form-data")
    if r.status_code in (302, 401):
        pytest.skip("login wall")
    plan = r.get_json()
    c.post("/api/stock/apply", json={"id": plan["id"]})
    a = _wait(c, plan["id"])
    assert a["status"] == "applied" and a["written"] == ["WB0109"] and "140-340" in a["failed"]
    assert FakeMC.store["WB0109"]["qty"] == 164.0 and FakeMC.store["140-340"]["qty"] == 22.0
    FakeMC.refuse = set()
    c.post("/api/stock/undo", json={"id": plan["id"]})
    u = _wait(c, plan["id"])
    assert u["restored"] == 1 and FakeMC.store["WB0109"]["qty"] == 170.0 and FakeMC.store["140-340"]["qty"] == 22.0


def test_client_fallback_needs_no_stock_id_and_reports_each_item():
    from src.data_sources.magento_client import MagentoClient, MagentoError
    mc = MagentoClient.__new__(MagentoClient)
    calls = []

    def fake_req(method, path, payload=None, scope="all"):
        calls.append((method, path))
        if path == "/inventory/source-items":
            raise MagentoError("Magento POST /inventory/source-items → 404: Request does not match any route.")
        if "BAD" in path:
            raise MagentoError("Magento PUT → 400: bad")
        return True

    mc._req = fake_req
    r = mc.set_stock([{"sku": "140-340", "qty": 16, "in_stock": True, "item_id": None},
                      {"sku": "BAD1", "qty": 1, "in_stock": True, "item_id": 3},
                      {"sku": "WB0109", "qty": 164, "in_stock": True, "item_id": 9}])
    assert r["method"] == "stock-items" and r["written"] == ["140-340", "WB0109"] and "BAD1" in r["failed"]
    assert ("PUT", "/products/140-340/stockItems/1") in calls


def test_unreadable_current_stock_is_written_but_never_guessed():
    from src.analysis.vendor_stock import diff
    d = diff([{"sku": "WB0109", "qty": 164, "is_in_stock": 1, "note": ""}],
             {"WB0109": {"qty": None, "in_stock": None, "name": "Reading Haven"}})
    c = d["changes"][0]
    assert c["old_qty"] is None and c["new_qty"] == 164 and "unreadable" in c["note"]


def test_get_stock_falls_back_to_stock_list():
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient.__new__(MagentoClient)

    def fake_req(method, path, payload=None, scope="all"):
        if path.startswith("/products?"):
            return {"items": [{"id": 11, "sku": "WB0109", "name": "Reading Haven", "extension_attributes": {}}]}
        if path.startswith("/stockItems/lowStock"):
            return {"items": [{"product_id": 11, "item_id": 7, "qty": 164, "is_in_stock": True}], "total_count": 1}
        raise AssertionError(path)

    mc._req = fake_req
    assert mc.get_stock(["WB0109"])["WB0109"] == {"qty": 164.0, "in_stock": True, "name": "Reading Haven", "item_id": 7}
