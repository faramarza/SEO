#!/usr/bin/env python3
"""Turn Guidecraft's inventory report (CSV from their email) into a Magento stock
import file: sku, qty, is_in_stock.

    python3 scripts/guidecraft_inventory.py Guidecraft_Inventory_111226.csv
    ... --out docs/imports/guidecraft_inventory_YYYY-MM-DD.csv

Rules (each one learned the hard way):
  - Only SKUs IN the report are touched. Products missing from it (e.g. the
    discontinued items Guidecraft quoted separately) are left alone, never zeroed.
  - qty = "Qty Available" (not "On hand": part of that is already promised).
  - "Active - Do Not Order" = Guidecraft won't take orders: out of stock.
  - Multi-box kits (G6110-1 / G6110-2) become one SKU (G6110) with the smallest
    box count, since a kit ships only when every box is there.
  - Report SKUs with a country suffix (G16836US) map to the store SKU (G16836).
Rows for SKUs your store doesn't have are kept; Magento's "Skip error entries"
skips them, and the script lists them so nothing is a surprise.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

SUFFIX = re.compile(r"^(G\w+?)(US)$")
BOX = re.compile(r"^(G\w+)-(\d)$")


def read_report(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.DictReader(text.splitlines()))


def to_int(v):
    try:
        return max(0, int(float(str(v).replace(",", "").strip() or 0)))
    except ValueError:
        return 0


def build(rows):
    """-> (stock rows [{sku, qty, is_in_stock, note}], notes)."""
    out, boxes = {}, {}
    for r in rows:
        sku = (r.get("Name") or "").strip()
        if not sku:
            continue
        qty = to_int(r.get("Qty Available"))
        status = (r.get("Item Status") or "").strip()
        back = (r.get("Minnesota Back In Stock Date") or r.get("North Carolina Back In Stock Date") or "").strip()
        note = ""
        if "do not order" in status.lower():
            qty, note = 0, "Guidecraft: Do Not Order"
        elif qty == 0 and back:
            note = f"back {back}"
        m = BOX.match(sku)
        if m:
            boxes.setdefault(m.group(1), []).append((qty, note))
            continue
        m = SUFFIX.match(sku)
        if m:
            sku = m.group(1)
        out[sku] = {"sku": sku, "qty": qty, "is_in_stock": 1 if qty > 0 else 0, "note": note}
    for sku, parts in boxes.items():
        qty = min(q for q, _ in parts)
        out[sku] = {"sku": sku, "qty": qty, "is_in_stock": 1 if qty > 0 else 0,
                    "note": f"kit of {len(parts)} boxes, min box qty"}
    return list(out.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    rows = build(read_report(args.report))
    out = args.out or "guidecraft_inventory_import.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "qty", "is_in_stock"])
        for r in rows:
            w.writerow([r["sku"], r["qty"], r["is_in_stock"]])
    print(f"{len(rows)} SKUs -> {out}")
    print(f"  in stock: {sum(r['is_in_stock'] for r in rows)}   out of stock: "
          f"{sum(1 - r['is_in_stock'] for r in rows)}")
    for r in rows:
        if r["note"]:
            print(f"  {r['sku']:<10} qty {r['qty']:<5} {r['note']}")


if __name__ == "__main__":
    sys.exit(main())
