"""What each order actually left in the bank: contribution profit per order.

    revenue (what the customer paid, before tax: items after discounts + shipping
             charged, minus anything refunded)
  - product cost and vendor shipping (exact where the vendor price data has it,
             else estimated from the product line's NET margin in config)
  - vendor per-order fees (Guidecraft $5 drop-ship fee)
  - card fee (config paid_search.card_fee, on what was charged incl. tax)
  - affiliate commission (Awin export, matched by order number; declined = 0)
  = contribution (before advertising)

Every item says how its cost was found: "exact", "estimated (<line> NN%)" or
"assumed (default NN%)". Nothing is silently zero. Pure functions, unit-tested;
scripts/order_profit.py fetches the data.
"""
from __future__ import annotations

import re

GUIDECRAFT_DS_FEE = 5.00   # invoice SO1026704: no freight, $5 drop-ship fee per order
WHITNEY_SKU = re.compile(r"^(WB|CH|CR|WR|WS|WX)\d|^140-", re.I)
GUIDECRAFT_SKU = re.compile(r"^G\d", re.I)


_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _count(label, value):
    """How many pieces: "Three 8\" Straight Tracks" -> 3; label "3 X 8\" ..." with Yes -> 3.
    The 8 in 8\" is a length, never a count. Pure."""
    for w, n in _WORDS.items():
        if re.search(rf"\b{w}\b", value):
            return n
    m = re.search(r"(\d+)\s*x\b", label) or re.search(r"(\d+)\s*x\b", value) \
        or re.search(r"\b(\d+)\b(?!\s*(\"|in|inch))", value)
    return int(m.group(1)) if m else 1


def addon_cost(options: list, table: dict) -> tuple[float, int, list]:
    """What a name train's chosen options add, checked against Maple Landmark invoices
    136707-136974. Returns (extra $, change in car count, [options not priced]):
      Engine/Caboose: a color = included; None = one car fewer; Noah's Ark, Circus,
        Classic, Rainbow = a $3.70 special car instead of a $3.60 one (+$0.10);
      Tracks: $2.20 each, count from the words or the "3 X" in the label;
      Oval track set $23.15; wall mount $12.15;
      typed text, colors, spacer none, special instructions: no cost. Pure."""
    cost, cars, unknown = 0.0, 0, []
    for o in options or []:
        label, _, val = (o or "").lower().partition(":")
        val = val.strip()
        if "(typed text)" in val or "instruction" in label or "name to spell" in label:
            continue
        none = re.fullmatch(r"(none|no|n|no thanks)?", val) is not None
        if label.strip() in ("engine", "caboose") or re.search(r"\b(engine|caboose)\b", label):
            if none:
                cars -= 1
            elif re.search(r"noah|circus|classic|rainbow|wagon|ark|animal|santa", val):
                cost += table["special engine or caboose"]
            continue                                   # a color: part of the train
        if none:
            continue
        if "sign" in label or "sign" in val:
            cost += table.get("street signs", 4.60)
        elif "oval" in label or "oval" in val:
            cost += table["oval"]
        elif "wall" in (label + val) and "mount" in (label + val):
            cost += table["wall mount"]
        elif "track" in label or "track" in val:
            cost += table["track"] * _count(label, val)
        elif re.search(r"noah|circus|wagon|animal|extra car", label + " " + val):
            cost += table["special car"] * _count(label, val)
        elif re.search(r"^(color|colour)$", label.strip()) or re.search(r"\b(bright|pastel|primary)\b", val):
            continue
        else:
            unknown.append(o)
    return round(cost, 2), cars, unknown


def item_cost(item: dict, cfg: dict, carpets: dict) -> tuple[float, str]:
    """(cost incl. vendor shipping, basis) for one order line. `item` has sku, name,
    revenue (line revenue after discount) and qty. Pure."""
    ps = (cfg or {}).get("paid_search") or {}
    sku, name = str(item.get("sku") or ""), (item.get("name") or "").lower()
    qty, rev = float(item.get("qty") or 1), float(item.get("revenue") or 0)

    ml = (ps.get("vendor_costs") or {}).get("maple_landmark")
    letters = re.search(r"(\d+)\s*letter", name)
    if ml and letters and re.search(r"name ?train|nametra|letter train", name):
        add, car_change, unknown = addon_cost(item.get("options") or [], ml.get("addons") or {})
        cars = int(letters.group(1)) + int(ml.get("extra_cars", 2)) + car_change   # + engine and caboose
        txt = f"{cars} cars x ${ml['per_car']:.2f}" + (f" + add-ons ${add:.2f}" if add else "")
        if unknown:
            return (round(qty * (ml["per_car"] * cars + add), 2),
                    f"estimated (Maple Landmark: {txt}; not priced: {'; '.join(unknown)[:60]})")
        return round(qty * (ml["per_car"] * cars + add), 2), f"exact (Maple Landmark: {txt})"
    if ml and re.search(r"wall mount", name):
        return round(qty * ml.get("addons", {}).get("wall mount", 12.15), 2), "exact (Maple Landmark wall mount)"

    c = carpets.get(sku)
    if c and c.get("cost") is not None:
        return round(qty * (float(c["cost"]) + float(c.get("shipping") or 0)), 2), "exact (Carpets for Kids price list)"

    sm = (ps.get("sku_margins") or {}).get(sku)
    if isinstance(sm, dict) and "margin" in sm:
        return round(rev * (1 - float(sm["margin"])), 2), f"exact (invoice margin {sm['margin']:.0%})"

    lines = ps.get("product_lines") or {}
    brands = ps.get("brand_margins") or {}

    def from_margin(m, label):
        return round(rev * (1 - float(m)), 2), f"estimated ({label} {float(m):.0%})"

    if re.search(r"name ?train|letter train", name):
        return from_margin(lines["name_trains"]["margin"], "name trains")
    if GUIDECRAFT_SKU.match(sku) and "guide craft" in brands:
        return from_margin(brands["guide craft"]["margin"], "Guidecraft")
    if WHITNEY_SKU.match(sku) and "whitney brothers" in brands:
        return from_margin(brands["whitney brothers"]["margin"], "Whitney Brothers")
    if re.search(r"\b(rug|carpet)s?\b", name) and "carpets" in lines:
        return from_margin(lines["carpets"]["margin"], "rugs")
    if "stool" in name and "stools" in lines:
        return from_margin(lines["stools"]["margin"], "stools")
    if re.search(r"book|story", name) and "books" in lines:
        return from_margin(lines["books"]["margin"], "books")
    if "montessori" in name and "montessori" in lines:
        return from_margin(lines["montessori"]["margin"], "Montessori")
    dm = float(ps.get("default_margin", 0.15))
    return round(rev * (1 - dm), 2), f"assumed (default {dm:.0%})"


def order_profit(order: dict, cfg: dict, carpets: dict, commissions: dict) -> dict:
    """Contribution for one order. `order`: number, status, items [{sku, name, qty,
    revenue}], shipping_charged, tax, refunded, charged (grand total).
    `commissions`: {order number: commission $ owed (declined excluded)}. Pure."""
    fee_rate = float(((cfg or {}).get("paid_search") or {}).get("card_fee", 0.03))
    items = order.get("items") or []
    status = (order.get("status") or "").lower()
    item_rev = sum(float(i.get("revenue") or 0) for i in items)
    revenue = item_rev + float(order.get("shipping_charged") or 0)
    refunded = float(order.get("refunded") or 0)
    if status in ("canceled",):
        return {"number": order.get("number"), "status": status, "revenue": 0.0, "contribution": 0.0,
                "basis": "canceled", "lines": [], "note": "canceled: nothing shipped, nothing earned"}
    full_refund = status == "closed" or (refunded and refunded >= float(order.get("charged") or 0) - 0.01)
    lines, cost = [], 0.0
    for i in items:
        c, basis = item_cost(i, cfg, carpets)
        lines.append({"sku": i.get("sku"), "name": i.get("name"), "revenue": round(float(i.get("revenue") or 0), 2),
                      "cost": c, "basis": basis})
        cost += c
    if any(GUIDECRAFT_SKU.match(str(i.get("sku") or "")) for i in items):
        cost += GUIDECRAFT_DS_FEE
    ml = (((cfg or {}).get("paid_search") or {}).get("vendor_costs") or {}).get("maple_landmark")
    if ml and any("Maple Landmark" in l["basis"] for l in lines):
        cost += float(ml.get("per_order", 0))   # drop-ship fee + shipping, once per order
    card = round(fee_rate * float(order.get("charged") or revenue), 2)
    commission = float(commissions.get(str(order.get("number")), 0.0))
    if full_refund:
        # Refunded before shipping (e.g. TAG closed): no product cost, but the card
        # fee is usually not returned and any approved commission was paid.
        lines = [dict(l, cost=0.0, basis="refunded, not shipped") for l in lines]
        return {"number": order.get("number"), "status": status, "revenue": 0.0, "cost": 0.0,
                "card_fee": card, "commission": commission,
                "contribution": round(-card - commission, 2), "basis": "refunded", "lines": lines,
                "note": "fully refunded"}
    revenue -= refunded
    contribution = revenue - cost - card - commission
    bases = {l["basis"].split(" (")[0] for l in lines}
    basis = "exact" if bases == {"exact"} else ("assumed" if "assumed" in bases else "estimated")
    return {"number": order.get("number"), "status": status, "revenue": round(revenue, 2),
            "cost": round(cost, 2), "card_fee": card, "commission": round(commission, 2),
            "contribution": round(contribution, 2), "basis": basis, "lines": lines,
            "note": f"partly refunded ${refunded:,.2f}" if refunded else ""}


def summarize(results: list, key) -> list:
    """Totals grouped by key(result) -> [(group, orders, revenue, contribution, margin%)]. Pure."""
    g = {}
    for r in results:
        k = key(r)
        a = g.setdefault(k, [0, 0.0, 0.0])
        a[0] += 1
        a[1] += r.get("revenue", 0.0)
        a[2] += r.get("contribution", 0.0)
    return sorted(((k, n, round(rv, 2), round(c, 2), (c / rv * 100) if rv else None)
                   for k, (n, rv, c) in g.items()), key=lambda x: -x[3])
