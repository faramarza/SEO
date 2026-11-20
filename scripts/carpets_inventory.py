#!/usr/bin/env python3
"""Turn Carpets for Kids' stock report (IM_StockStatus .xlsx from their email) into a
Magento stock import file: sku, qty, is_in_stock.

    python3 scripts/carpets_inventory.py Carpets_for_Kids_Inventory.xlsx \
        --store-export export_catalog_product.csv --out docs/imports/carpets_inventory_YYYY-MM-DD.csv

Columns (no header row): vendor, item number, description, class, qty available, note.
Rules:
  - Item numbers are rebuilt from the cell's own number format: Excel keeps "36.10"
    as the number 36.1, and the store's SKU is "36.10".
  - qty = available quantity. 0 (always "ETA 3 Weeks" so far) -> out of stock.
  - Class DISC = discontinued, but whatever stock is left can still ship: kept for
    sale with its real quantity, so it sells out naturally.
  - Only SKUs in the report are touched; with --store-export, only SKUs the store has
    (Magento blocks an import with more than 10 unknown SKUs).
"""
import argparse
import csv
import sys


def sku_of(value, number_format="General"):
    """Item number as text, as the store spells it. Pure."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        f = number_format or "General"
        if f.startswith("0.") and set(f[2:]) == {"0"}:
            return f"{value:.{len(f) - 2}f}"
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip() or None


def build(cells):
    """cells: [(item_value, item_format, desc, cls, qty, note)] -> [{sku, qty, is_in_stock, note}]."""
    out = {}
    for item, fmt, desc, cls, qty, note in cells:
        sku = sku_of(item, fmt)
        try:
            q = max(0, int(float(qty)))
        except (TypeError, ValueError):
            continue
        if not sku:
            continue
        n = (note or "").strip() if isinstance(note, str) else ""
        if cls == "DISC":
            n = (n + "; " if n else "") + "discontinued, selling remaining stock"
        out[sku] = {"sku": sku, "qty": q, "is_in_stock": 1 if q > 0 else 0, "note": n}
    return list(out.values())


def read_xlsx(path):
    import openpyxl
    ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    rows = []
    for r in ws.iter_rows():
        if len(r) < 6:
            continue
        rows.append((r[1].value, r[1].number_format, r[2].value, r[3].value, r[4].value, r[5].value))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--store-export", default=None)
    ap.add_argument("--out", default="carpets_inventory_import.csv")
    args = ap.parse_args()
    rows = build(read_xlsx(args.report))
    if args.store_export:
        csv.field_size_limit(10 ** 9)
        with open(args.store_export, newline="", encoding="utf-8", errors="replace") as f:
            keep = {(r.get("sku") or "").strip() for r in csv.DictReader(f)}
        print(f"{sum(1 for r in rows if r['sku'] not in keep)} rugs you don't carry left out")
        rows = [r for r in rows if r["sku"] in keep]
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "qty", "is_in_stock"])
        for r in rows:
            w.writerow([r["sku"], r["qty"], r["is_in_stock"]])
    print(f"{len(rows)} SKUs -> {args.out}")
    print(f"  in stock: {sum(r['is_in_stock'] for r in rows)}   out of stock: "
          f"{sum(1 - r['is_in_stock'] for r in rows)}   discontinued but still selling: "
          f"{sum(1 for r in rows if 'discontinued' in r['note'] and r['is_in_stock'])}")


if __name__ == "__main__":
    sys.exit(main())
