#!/usr/bin/env python3
"""Turn Whitney Brothers' "Inventory Update - No Parts" report (CSV from their email)
into a Magento stock import file: sku, qty, is_in_stock.

    python3 scripts/whitney_inventory.py Whitney_Brothers_inventory_update.csv
    ... --out docs/imports/whitney_inventory_YYYY-MM-DD.csv

Rules:
  - Only SKUs IN the report are touched; anything missing is left alone.
  - qty = "Available Inventory". 0 -> out of stock.
  - "*OB" / "ob*" in the description = obsolete at Whitney: out of stock.
  - A 0-qty item with an Availability Date is noted ("back 12/21/2026");
    1/0/1900 means "no date".
  - The report's title line and "Page -1 of 1" footer are ignored.
The report lists Whitney's whole line (about 500 SKUs); your store carries only
part of it. Import with Validation Strategy "Skip error entries" so Magento
skips the SKUs you don't carry instead of blocking the import.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

NO_DATE = "1/0/1900"


def read_report(path):
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lower().startswith("item,")), 0)
    return list(csv.DictReader(lines[start:]))


def build(rows):
    """-> [{sku, qty, is_in_stock, note}]. Pure."""
    out = {}
    for r in rows:
        sku = (r.get("item") or "").strip()
        if not sku or " " in sku or not re.match(r"^[A-Za-z0-9-]+$", sku):
            continue   # title, footer, junk
        try:
            qty = max(0, int(float((r.get("Available Inventory") or "0").replace(",", ""))))
        except ValueError:
            continue
        desc = (r.get("description") or "").strip()
        avail = (r.get("Availabilty Date") or r.get("Availability Date") or "").strip()
        disc = (r.get("Discontinue Date") or "").strip()
        note = ""
        if re.match(r"(?i)^\s*\*?ob\*?", desc):
            qty, note = 0, "obsolete at Whitney"
        elif qty == 0 and avail and avail != NO_DATE:
            note = f"back {avail}"
        if disc and disc != NO_DATE and qty > 0:
            note = (note + "; " if note else "") + f"discontinued after {disc}"
        out[sku] = {"sku": sku, "qty": qty, "is_in_stock": 1 if qty > 0 else 0, "note": note}
    return list(out.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--out", default="whitney_inventory_import.csv")
    args = ap.parse_args()
    rows = build(read_report(args.report))
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "qty", "is_in_stock"])
        for r in rows:
            w.writerow([r["sku"], r["qty"], r["is_in_stock"]])
    print(f"{len(rows)} SKUs -> {args.out}")
    print(f"  in stock: {sum(r['is_in_stock'] for r in rows)}   out of stock: "
          f"{sum(1 - r['is_in_stock'] for r in rows)}")
    print(f"  obsolete: {sum(1 for r in rows if 'obsolete' in r['note'])}   "
          f"in stock but discontinued after 12/31: {sum(1 for r in rows if 'discontinued' in r['note'])}")


if __name__ == "__main__":
    sys.exit(main())
