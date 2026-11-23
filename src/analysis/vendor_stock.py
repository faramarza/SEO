"""Vendor stock files -> what changes in the store. Used by Governor's Stock Update
page: upload the file as the vendor sent it, preview only what changes, apply with
one click, undo.

Recognizes:
  Guidecraft      CSV with "Qty Available" and "Item Status" columns
  Whitney Bros    CSV "Inventory Update - No Parts" report ("Available Inventory")
  Carpets for Kids  .xlsx stock status report (item, description, class, qty, note)
The per-vendor rules live in scripts/{guidecraft,whitney,carpets}_inventory.py and
are reused here unchanged, so the page and the import files always agree.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class VendorFileError(ValueError):
    pass


def detect_vendor(filename: str, head: bytes) -> str:
    """'guidecraft' | 'whitney' | 'carpets' from the file name and its first bytes. Pure."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or head[:2] == b"PK":
        return "carpets"
    text = head.decode("latin-1", errors="replace").lower()
    if "inventory update" in text or "available inventory" in text:
        return "whitney"
    if "qty available" in text and "item status" in text:
        return "guidecraft"
    raise VendorFileError("Not a vendor stock file I know (Guidecraft CSV, Whitney Brothers CSV, "
                          "or Carpets for Kids Excel).")


def parse(path: str, filename: str) -> tuple[str, list]:
    """(vendor, [{sku, qty, is_in_stock, note}]) for an uploaded file."""
    head = Path(path).read_bytes()[:4000]
    vendor = detect_vendor(filename, head)
    if vendor == "carpets":
        from scripts.carpets_inventory import build, read_xlsx
        rows = build(read_xlsx(path))
    elif vendor == "whitney":
        from scripts.whitney_inventory import build, read_report
        rows = build(read_report(path))
    else:
        from scripts.guidecraft_inventory import build, read_report
        rows = build(read_report(path))
    if not rows:
        raise VendorFileError("The file was recognized but has no stock rows.")
    return vendor, rows


def diff(rows: list, current: dict) -> dict:
    """Compare the vendor rows with the store. current: {sku: {"qty", "in_stock",
    "name"}} for SKUs that exist in the store. Returns only real changes. Pure."""
    changes, not_in_store, same = [], [], 0
    for r in rows:
        cur = current.get(r["sku"])
        if cur is None:
            not_in_store.append(r["sku"])
            continue
        new_qty, new_in = float(r["qty"]), bool(r["is_in_stock"])
        if cur.get("qty") is None:          # stock unreadable: write it, but don't guess the old value
            changes.append({"sku": r["sku"], "name": cur.get("name", ""), "old_qty": None, "new_qty": new_qty,
                            "old_in_stock": None, "new_in_stock": new_in, "change": "quantity only",
                            "note": (r.get("note", "") + " (current stock unreadable)").strip()})
            continue
        old_qty, old_in = float(cur.get("qty") or 0), bool(cur.get("in_stock"))
        if new_qty == old_qty and new_in == old_in:
            same += 1
            continue
        kind = ("goes OUT of stock" if old_in and not new_in else
                "comes BACK in stock" if new_in and not old_in else "quantity only")
        changes.append({"sku": r["sku"], "name": cur.get("name", ""), "old_qty": old_qty,
                        "new_qty": new_qty, "old_in_stock": old_in, "new_in_stock": new_in,
                        "change": kind, "note": r.get("note", "")})
    order = {"goes OUT of stock": 0, "comes BACK in stock": 1, "quantity only": 2}
    changes.sort(key=lambda c: (order[c["change"]], c["sku"]))
    return {"changes": changes, "not_in_store": not_in_store, "unchanged": same,
            "out": sum(1 for c in changes if c["change"] == "goes OUT of stock"),
            "back": sum(1 for c in changes if c["change"] == "comes BACK in stock"),
            "qty_only": sum(1 for c in changes if c["change"] == "quantity only")}
