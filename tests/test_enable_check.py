"""Disabled products that pay: margin math, and check -> turn on -> undo on a fake store."""
import time

import pytest

from src.analysis.enable_check import evaluate, freight, load_costs

app_mod = pytest.importorskip("web.app")
RULES = {"truck_min": 350, "truck_per_lb": 2.73, "ground_base": 15, "ground_per_lb": 0.85, "min_margin": 0.25}


def test_freight_model():
    assert freight("Truck", 77, RULES) == 350            # light truck item pays the minimum
    assert freight("Truck", 199, RULES) == pytest.approx(543.27)
    assert freight("Ground", 53, RULES) == pytest.approx(60.05)


def test_evaluate_matches_the_manual_review():
    costs = {"WS3520M": {"via": "Ground", "lb": 53, "cost": 440.9, "name": "Table"},
             "WB0438": {"via": "Truck", "lb": 60, "cost": 352.96, "name": "Cabinet"},
             "WBOOS": {"via": "Ground", "lb": 10, "cost": 10, "name": "x"},
             "WBON": {"via": "Ground", "lb": 10, "cost": 10, "name": "y"}}
    prods = {"WS3520M": {"status": 2, "price": 829.0}, "WB0438": {"status": 2, "price": 831.0},
             "WBOOS": {"status": 2, "price": 100.0}, "WBON": {"status": 1, "price": 100.0}}
    stock = {"WS3520M": {"qty": 1, "in_stock": True}, "WB0438": {"qty": 4, "in_stock": True},
             "WBOOS": {"qty": 0, "in_stock": False}}
    r = evaluate(prods, stock, costs, RULES)
    assert [x["sku"] for x in r["turn_on"]] == ["WS3520M"] and r["turn_on"][0]["margin"] == 36.6
    assert [x["sku"] for x in r["keep_off"]] == ["WB0438"] and r["keep_off"][0]["margin"] == 12.4
    assert r["out_of_stock"] == 1                         # enabled WBON is ignored


def test_cost_file_loads_from_config():
    rules, costs = load_costs(app_mod.load_config())
    assert rules["truck_min"] == 350 and len(costs) > 300 and costs["WS3520M"]["cost"] == 440.9


class FakeMC:
    configured = True
    prods = {}

    def products_status(self, skus):
        return {s: dict(FakeMC.prods[s]) for s in skus if s in FakeMC.prods}

    def get_stock(self, skus):
        return {s: {"qty": 3, "in_stock": True} for s in skus}

    def set_status(self, sku, enabled, attribute_set_id, type_id):
        assert attribute_set_id == 4 and type_id == "simple"
        FakeMC.prods[sku]["status"] = 1 if enabled else 2


def _wait(c, pid, busy):
    for _ in range(200):
        s = c.get(f"/api/enable/status?id={pid}").get_json()
        if s.get("status") != busy:
            return s
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_check_turn_on_undo(monkeypatch, tmp_path):
    import src.data_sources.magento_client as mcm
    FakeMC.prods = {"WS3520M": {"status": 2, "price": 829.0, "name": "Table", "attribute_set_id": 4, "type_id": "simple"},
                    "WB0438": {"status": 2, "price": 831.0, "name": "Cab", "attribute_set_id": 4, "type_id": "simple"}}
    monkeypatch.setattr(mcm, "MagentoClient", FakeMC)
    monkeypatch.setattr(app_mod, "_ENABLE_DIR", tmp_path)
    c = app_mod.app.test_client()
    pid = c.post("/api/enable/check").get_json()["id"]
    s = _wait(c, pid, "checking")
    assert s["status"] == "checked" and [x["sku"] for x in s["turn_on"]] == ["WS3520M"]
    # a SKU that wasn't offered can't be turned on
    assert "error" in c.post("/api/enable/apply", json={"id": pid, "skus": ["WB0438"]}).get_json()
    c.post("/api/enable/apply", json={"id": pid, "skus": ["WS3520M", "WB0438"]})
    s = _wait(c, pid, "applying")
    assert s["status"] == "applied" and s["written"] == ["WS3520M"] and not s["not_confirmed"]
    assert FakeMC.prods["WS3520M"]["status"] == 1 and FakeMC.prods["WB0438"]["status"] == 2
    c.post("/api/enable/undo", json={"id": pid})
    s = _wait(c, pid, "undoing")
    assert s["status"] == "undone" and s["restored"] == 1 and FakeMC.prods["WS3520M"]["status"] == 2


def test_status_write_guard():
    from src.data_sources.magento_client import MagentoClient, MagentoError
    mc = MagentoClient(base_url="https://x", token="t", store_code="alphabettrains")
    sent = []
    mc._write = lambda path, payload: sent.append((path, payload))
    mc.set_status("WB 1/2", True, 4, "simple")
    assert sent == [("/products/WB%201%2F2", {"product": {"sku": "WB 1/2", "attribute_set_id": 4, "type_id": "simple", "status": 1}})]
