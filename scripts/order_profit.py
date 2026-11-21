#!/usr/bin/env python3
"""Real profit per order (before advertising), by product line and by where the
customer came from. Run on the Governor server:

    /var/www/seo/venv/bin/python scripts/order_profit.py                 (last 14 days)
    ... --days 30
    ... --awin transactions.csv     (Awin export: subtracts each order's commission)
    ... --list                      (every order: lines, cost basis, profit)

Profit = what the customer paid before tax (after discounts and refunds)
         - product cost and vendor shipping - Guidecraft $5 drop-ship fee
         - card fee - affiliate commission.
Each order says whether its cost is EXACT (vendor price data) or ESTIMATED (the
product line's margin). Ad spend is not per order: compare the "Google Ads" row's
profit with that campaign's spend for the same days. Read-only.
"""
import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.sales_by_line import _load_dotenv, line_of  # noqa: E402

_load_dotenv()

from src.analysis.order_profit import order_profit, summarize  # noqa: E402


def load_awin(path):
    """{order number: commission} for open and approved transactions (declined = 0)."""
    out = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for r in csv.DictReader(f):
            if (r.get("commission_status") or "").strip() == "2":
                continue
            n = (r.get("extra") or "").strip()
            try:
                out[n] = out.get(n, 0.0) + float(r.get("commission") or 0)
            except ValueError:
                pass
    return out


def fetch_orders(mc, start, end_excl):
    page, out = 1, []
    while True:
        data = mc._req("GET", mc._orders_date_query(start, end_excl, exclude_canceled=False,
                                                     page_size=100, current_page=page)) or {}
        for o in data.get("items") or []:
            items = [{"sku": it.get("sku", ""), "name": it.get("name", ""),
                      "qty": float(it.get("qty_ordered") or 0),
                      "revenue": float(it.get("base_row_total") or 0) - float(it.get("base_discount_amount") or 0)}
                     for it in o.get("items") or [] if not it.get("parent_item_id")]
            out.append({"number": o.get("increment_id"), "date": (o.get("created_at") or "")[:10],
                        "status": o.get("status") or "", "items": items,
                        "shipping_charged": float(o.get("base_shipping_amount") or 0),
                        "tax": float(o.get("base_tax_amount") or 0),
                        "refunded": float(o.get("base_total_refunded") or 0),
                        "charged": float(o.get("base_grand_total") or 0)})
        if not data.get("items") or page * 100 >= int(data.get("total_count") or 0):
            return out
        page += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--awin", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-ga4", action="store_true")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    try:
        carpets = json.loads((ROOT / "data" / "product_margins.json").read_text())["lines"]["carpets"]["skus"]
    except Exception:
        carpets = {}
        print("(no Carpets for Kids price data: rugs use the line margin)")
    commissions = load_awin(args.awin) if args.awin else {}
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured")
    today = date.today()
    start = today - timedelta(days=args.days)
    orders = fetch_orders(mc, start.isoformat(), (today + timedelta(days=1)).isoformat())
    results = [dict(order_profit(o, cfg, carpets, commissions), date=o["date"], items=o["items"]) for o in orders]

    sources = {}
    if not args.no_ga4:
        try:
            from scripts.sales_source import ga4_sources, label
            sources = {k: label(v) for k, v in ga4_sources(start, today).items()}
        except Exception as e:
            print(f"(GA4 sources unavailable: {e})")

    live = [r for r in results if r["basis"] != "canceled"]
    tot_rev = sum(r["revenue"] for r in live)
    tot_c = sum(r["contribution"] for r in live)
    print(f"\nORDERS {start} to {today}: {len(live)} (plus {len(results) - len(live)} canceled)")
    print(f"  Paid before tax ${tot_rev:,.2f}   PROFIT before ads ${tot_c:,.2f}"
          + (f" ({tot_c / tot_rev:.0%})" if tot_rev else ""))
    print(f"  Cost basis: {sum(1 for r in live if r['basis'] == 'exact')} exact, "
          f"{sum(1 for r in live if r['basis'] == 'estimated')} estimated from line margins, "
          f"{sum(1 for r in live if r['basis'] == 'assumed')} assumed (no margin on file), "
          f"{sum(1 for r in live if r['basis'] == 'refunded')} refunded")
    if not args.awin:
        print("  Affiliate commission NOT subtracted (add --awin with your Awin export).")

    def row(t):
        k, n, rv, c, m = t
        print(f"   {n:>4} orders  ${rv:>10,.2f} paid   ${c:>9,.2f} profit  "
              + (f"{m:>4.0f}%" if m is not None else "   - ") + f"   {k}")

    print("\nBY PRODUCT LINE (by the order's largest item)")
    for t in summarize(live, lambda r: line_of(max(r["items"], key=lambda i: i["revenue"])["name"])
                       if r["items"] else "Other"):
        row(t)
    if sources:
        print("\nBY WHERE THE CUSTOMER CAME FROM (GA4; compare 'Google Ads' with your ad spend)")
        for t in summarize(live, lambda r: sources.get(str(r["number"]), "not seen by GA4")):
            row(t)
    if args.list:
        print("\nEVERY ORDER")
        for r in sorted(live, key=lambda x: x["date"]):
            print(f"  #{r['number']} {r['date']} {r['status']:<10} paid ${r['revenue']:>8,.2f}  "
                  f"profit ${r['contribution']:>8,.2f}  [{r['basis']}] {r.get('note', '')}")
            for l in r["lines"]:
                print(f"        {l['name'][:45]:<46} ${l['revenue']:>8,.2f}  cost ${l['cost']:>8,.2f}  {l['basis']}")
    print("\nNot included: advertising (compare per source above), returns not yet refunded in Magento.")


if __name__ == "__main__":
    main()
