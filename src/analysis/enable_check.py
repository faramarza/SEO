"""Disabled products that pay: which turned-off vendor products make enough money
to turn back on. Used by Governor's Stock Update page ("Disabled products that pay").

For each disabled store product that is in the vendor's cost list:
  profit = store price (lower special price if active) - dealer cost - freight - card fee
  freight: Truck = max(truck_min, truck_per_lb x lb), Ground = ground_base + ground_per_lb x lb
Only products in stock in the store are offered. Pure functions; the web layer reads
Magento and writes the status.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_costs(cfg: dict) -> tuple[dict, dict]:
    """(rules, {sku: {name, via, lb, cost, retail}}) for Whitney Brothers."""
    rules = (((cfg or {}).get("paid_search") or {}).get("vendor_costs") or {}).get("whitney_brothers") or {}
    path = ROOT / (rules.get("cost_file") or "config/vendor_costs/whitney_2026.json")
    return rules, json.loads(path.read_text())["items"]


def freight(via: str, lb: float, rules: dict) -> float:
    lb = float(lb or 0)
    if (via or "").lower().startswith("truck"):
        return max(float(rules.get("truck_min", 350)), float(rules.get("truck_per_lb", 2.73)) * lb)
    return float(rules.get("ground_base", 15)) + float(rules.get("ground_per_lb", 0.85)) * lb


def evaluate(products: dict, stock: dict, costs: dict, rules: dict, card_fee: float = 0.03) -> dict:
    """products: {sku: {"status", "price", "name"}} from the store (disabled ones matter).
    stock: {sku: {"qty", "in_stock"}}. Returns {"turn_on": [...], "keep_off": [...],
    "out_of_stock": n}, each row {sku, name, via, lb, price, cost, freight, profit, margin}."""
    floor = float(rules.get("min_margin", 0.25))
    on, off, oos = [], [], 0
    for sku, p in products.items():
        c = costs.get(sku)
        if not c or int(p.get("status") or 1) == 1 or not p.get("price"):
            continue
        st = stock.get(sku) or {}
        if not st.get("in_stock") or not (st.get("qty") or 0) > 0:
            oos += 1
            continue
        price = float(p["price"])
        f = freight(c["via"], c["lb"], rules)
        profit = round(price - float(c["cost"]) - f - card_fee * price, 2)
        row = {"sku": sku, "name": p.get("name") or c.get("name", ""), "via": c["via"], "lb": c["lb"],
               "price": price, "cost": float(c["cost"]), "freight": round(f, 2), "profit": profit,
               "margin": round(100 * profit / price, 1), "qty": st.get("qty")}
        (on if profit / price >= floor else off).append(row)
    on.sort(key=lambda r: -r["margin"])
    off.sort(key=lambda r: -r["margin"])
    return {"turn_on": on, "keep_off": off, "out_of_stock": oos, "min_margin": round(100 * floor)}
