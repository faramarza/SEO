#!/usr/bin/env python3
"""Where do a product line's sales come from? Every Magento order for the line in
the last N days, matched (by order number) to the GA4 visit that placed it, so
each order shows its source: Google Ads, free Google search, direct, email, ...
Compared with the N days before.

    /var/www/seo/venv/bin/python scripts/sales_source.py                 (name trains, 7 days)
    ... --line "Rugs" --days 14
    ... --list            (also print every order with its source)

"not seen by GA4" = an order with no matching GA4 purchase (ad/cookie blockers,
declined consent, phone or admin orders). Read-only.
"""
import argparse
import collections
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.sales_by_line import _load_dotenv, line_of  # noqa: E402

_load_dotenv()

NOT_SEEN = "not seen by GA4"


def label(source_medium):
    """Plain name for a GA4 source / medium. Pure."""
    s = (source_medium or "").lower()
    if not s:
        return NOT_SEEN
    if s == "google / cpc":
        return "Google Ads (google / cpc)"
    if s == "google / organic":
        return "Free Google search (google / organic)"
    if s.startswith("(direct)"):
        return "Direct (typed the address, bookmark, or untracked app/email)"
    if "klaviyo" in s or "email" in s:
        return f"Email ({source_medium})"
    if "bing" in s or "duckduckgo" in s or "yahoo" in s:
        return f"Other search ({source_medium})"
    return source_medium


def by_source(orders, ga4):
    """orders: [{number, revenue}]; ga4: {transaction id: source/medium}.
    -> [(source label, orders, revenue)] biggest first. Pure."""
    agg = collections.defaultdict(lambda: [0, 0.0])
    for o in orders:
        a = agg[label(ga4.get(str(o["number"])))]
        a[0] += 1
        a[1] += o["revenue"]
    return sorted(((k, v[0], round(v[1], 2)) for k, v in agg.items()), key=lambda x: (-x[1], -x[2]))


def fetch_line_orders(mc, start, end_excl, line):
    """[{number, date, revenue}] of orders holding at least one item of the line."""
    page, out = 1, []
    while True:
        q = mc._orders_date_query(start, end_excl, exclude_canceled=True, page_size=100, current_page=page)
        data = mc._req("GET", q) or {}
        orders = data.get("items") or []
        for o in orders:
            rev = sum(float(it.get("base_row_total") or it.get("row_total") or 0)
                      - float(it.get("base_discount_amount") or 0)
                      for it in o.get("items") or []
                      if not it.get("parent_item_id") and line_of(it.get("name")) == line)
            if rev > 0:
                out.append({"number": o.get("increment_id"), "date": (o.get("created_at") or "")[:10],
                            "revenue": round(rev, 2)})
        if not orders or page * 100 >= int(data.get("total_count") or 0):
            return out
        page += 1


def ga4_sources(start, end):
    """{transactionId: sessionSourceMedium} for purchases in the window."""
    from src.data_sources.ga4_client import GA4Client
    from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Dimension, Metric
    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())["data_sources"]["ga4"]
    client = GA4Client(property_id=str(cfg["property_id"]),
                       credentials_path=str(ROOT / cfg["credentials_path"]))._get_client()
    if client is None:
        sys.exit("GA4 not available (credentials).")
    out, offset = {}, 0
    while True:
        resp = client.run_report(RunReportRequest(
            property=f"properties/{cfg['property_id']}",
            date_ranges=[DateRange(start_date=start.isoformat(), end_date=end.isoformat())],
            dimensions=[Dimension(name="transactionId"), Dimension(name="sessionSourceMedium")],
            metrics=[Metric(name="purchaseRevenue")], limit=10000, offset=offset))
        for r in resp.rows:
            tid, sm = r.dimension_values[0].value, r.dimension_values[1].value
            if tid and tid != "(not set)":
                out.setdefault(tid, sm)
        offset += len(resp.rows)
        if not resp.rows or offset >= resp.row_count:
            return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default="Name trains")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured")
    today = date.today()
    cur_start, prev_start = today - timedelta(days=args.days), today - timedelta(days=2 * args.days)
    ga4 = ga4_sources(prev_start, today)
    for name, a, b in (("LAST", cur_start, today + timedelta(days=1)), ("BEFORE THAT", prev_start, cur_start)):
        orders = fetch_line_orders(mc, a, b, args.line)
        total = sum(o["revenue"] for o in orders)
        print(f"\n{args.line}, {name} {args.days} days ({a} to {b - timedelta(days=1)}): "
              f"{len(orders)} orders, ${total:,.2f}")
        for src, n, rev in by_source(orders, ga4):
            print(f"   {n:>4} orders  ${rev:>9,.2f}   {src}")
        if args.list:
            for o in orders:
                print(f"        #{o['number']}  {o['date']}  ${o['revenue']:,.2f}  {label(ga4.get(str(o['number'])))}")
    allo = fetch_line_orders(mc, prev_start, today + timedelta(days=1), args.line)
    unmatched = [o for o in allo if str(o["number"]) not in ga4]
    if allo and ga4 and len(unmatched) > 0.8 * len(allo):
        print("\nWARNING: almost no orders matched a GA4 purchase. GA4's transaction IDs may not be "
              f"the order numbers. Magento: {[o['number'] for o in allo[:3]]}, GA4: {list(ga4)[:3]}")
    print("\nGA4 needs a day to finish processing, so today's and yesterday's orders may show as "
          "'not seen by GA4' for now.")


if __name__ == "__main__":
    main()
