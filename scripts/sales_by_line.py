#!/usr/bin/env python3
"""Real sales by product, brand and line, from Magento ORDERS (not GA4). Catches
orders GA4 never sees (phone, email, purchase orders entered in Magento) and
gives the true best sellers for every category page.

Run on the server:

    /var/www/seo/venv/bin/python scripts/sales_by_line.py
    ... --days 365          (window, default 365)
    ... --top 25            (products to list per section, default 25)
    ... --match "carpets"   (only products whose name or SKU contains this text)

Read-only. Canceled orders excluded.
"""
import argparse
import collections
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

LINES = [  # (line, words in the product name) - first match wins
    ("Name trains", ("name train", "nametrain", "letter train", "alphabet train")),
    ("Name puzzle stools", ("stool",)),
    ("Name puzzles", ("name puzzle",)),
    ("Personalized books", ("personalized book", "story book", "storybook", "board book",
                            "personalized story", "book and ")),
    ("Blankets", ("blanket",)),
    ("Rugs", ("rug", "carpet")),
    ("Blocks", ("block",)),
    ("Tables", ("table",)),
    ("Storage and cabinets", ("cabinet", "cubby", "storage", "locker", "shelf", "cart")),
]


def line_of(name):
    n = (name or "").lower()
    for line, words in LINES:
        if any(w in n for w in words):
            return line
    return "Other"


def summarize(items):
    """items: [{sku, name, qty, revenue, order_id}] -> per product and per line."""
    prod = collections.defaultdict(lambda: {"name": "", "qty": 0.0, "revenue": 0.0, "orders": set()})
    for it in items:
        p = prod[it["sku"]]
        p["name"] = it["name"]
        p["qty"] += it["qty"]
        p["revenue"] += it["revenue"]
        p["orders"].add(it["order_id"])
    lines = collections.defaultdict(lambda: {"qty": 0.0, "revenue": 0.0, "orders": set(), "skus": 0})
    for sku, p in prod.items():
        l = lines[line_of(p["name"])]
        l["qty"] += p["qty"]
        l["revenue"] += p["revenue"]
        l["orders"] |= p["orders"]
        l["skus"] += 1
    return prod, lines


def fetch_items(mc, start, end_excl, max_orders=20000):
    page, out, seen = 1, [], 0
    while True:
        q = mc._orders_date_query(start, end_excl, exclude_canceled=True, page_size=100,
                                  current_page=page)
        data = mc._req("GET", q) or {}
        orders = data.get("items") or []
        for o in orders:
            seen += 1
            for it in o.get("items") or []:
                if it.get("parent_item_id"):
                    continue  # child of a configurable/bundle: counted on the parent
                out.append({"sku": it.get("sku", ""), "name": it.get("name", ""),
                            "qty": float(it.get("qty_ordered") or 0),
                            "revenue": float(it.get("base_row_total") or it.get("row_total") or 0)
                            - float(it.get("base_discount_amount") or 0),
                            "order_id": o.get("entity_id")})
        total = int(data.get("total_count") or 0)
        if not orders or page * 100 >= total or seen >= max_orders:
            break
        page += 1
    return out, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--match", default=None)
    args = ap.parse_args()

    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured (MAGENTO_BASE_URL / MAGENTO_TOKEN in .env).")
    end = date.today()
    start = end - timedelta(days=args.days)
    items, n_orders = fetch_items(mc, start.isoformat(), (end + timedelta(days=1)).isoformat())
    if args.match:
        m = args.match.lower()
        items = [i for i in items if m in f"{i['name']} {i['sku']}".lower()]
    prod, lines = summarize(items)

    print("=" * 96)
    print(f"REAL SALES FROM MAGENTO ORDERS  {start} -> {end}  ({n_orders} orders, canceled excluded)"
          + (f"  filter: '{args.match}'" if args.match else ""))
    print("=" * 96)
    print(f"\nBY PRODUCT LINE   {'line':<24}{'orders':>8}{'units':>8}{'revenue':>12}{'products sold':>15}")
    for name, l in sorted(lines.items(), key=lambda x: -x[1]["revenue"]):
        print(f"                  {name:<24}{len(l['orders']):>8}{l['qty']:>8.0f}{l['revenue']:>12,.0f}"
              f"{l['skus']:>15}")
    print(f"\nTOP {args.top} PRODUCTS BY REVENUE")
    print(f"   {'sku':<18}{'product':<52}{'orders':>7}{'units':>7}{'revenue':>11}")
    for sku, p in sorted(prod.items(), key=lambda x: -x[1]["revenue"])[:args.top]:
        print(f"   {sku[:17]:<18}{p['name'][:51]:<52}{len(p['orders']):>7}{p['qty']:>7.0f}"
              f"{p['revenue']:>11,.0f}")


if __name__ == "__main__":
    main()
