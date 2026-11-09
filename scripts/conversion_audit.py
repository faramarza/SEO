#!/usr/bin/env python3
"""Conversion audit: where buyers drop out, on which devices and products, and
what each leak is worth in dollars. Site-wide, all channels.

Run on the server:

    /var/www/seo/venv/bin/python scripts/conversion_audit.py
    ... --days 90            (window, default 90)
    ... --top 20             (products to list, default 20)

METHOD (all from GA4, read-only):
  1. FUNNEL by distinct users: visit -> reached a product (view_item, select_item
     or add_to_cart, so carousel add-to-carts count) -> add_to_cart ->
     begin_checkout -> add_shipping_info -> add_payment_info -> purchase.
     Steps your store does not send to GA4 are shown as "not tracked".
  2. The same funnel per DEVICE (desktop, mobile, tablet).
  3. Each step rate is shown next to a published reference range (Baymard
     Institute cart-abandonment research, ~70% average; typical ecommerce
     add-to-cart and checkout-completion ranges). References are context, not
     targets: the ranking uses dollars.
  4. VALUE OF A FIX: if one step reached the reference midpoint while every
     other step stayed the same, extra orders = orders x (target / current - 1).
     Profit = extra orders x your average order x blended net margin.
  5. PRODUCTS: GA4 item views vs add-to-carts. Products with real traffic whose
     add-to-cart rate is far below the site median are where shoppers look and
     leave. Lost profit = views x (median rate - product rate) x cart-to-purchase
     rate x product price x margin.
"""
import argparse
import json
import os
import statistics
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

STEPS = [  # (key, label, reference low, reference high) rate vs the previous step
    ("visit", "Visit", None, None),
    ("reached", "Reached a product", 0.40, 0.60),
    ("add_to_cart", "Added to cart", 0.10, 0.20),
    ("begin_checkout", "Started checkout", 0.40, 0.60),
    ("add_shipping_info", "Entered shipping", 0.70, 0.90),
    ("add_payment_info", "Entered payment", 0.70, 0.90),
    ("purchase", "Purchased", 0.40, 0.60),
]
EVENTS = ["view_item", "select_item", "add_to_cart", "begin_checkout",
          "add_shipping_info", "add_payment_info", "purchase"]
MIN_ITEM_VIEWS = 40


# ---------------------------------------------------------------- pure logic
def step_rates(counts):
    """counts: {key: users or None}. Returns [(key, label, users, rate_vs_prev,
    ref_lo, ref_hi)]. Untracked steps (None) are skipped when computing the next
    rate, so a missing event never shows as a 0% leak."""
    out, prev = [], None
    for key, label, lo, hi in STEPS:
        n = counts.get(key)
        if n is None:
            out.append((key, label, None, None, lo, hi))
            continue
        rate = (n / prev) if (prev and prev > 0) else None
        out.append((key, label, n, rate, lo, hi))
        prev = n
    return out


def fix_value(rates, orders, aov, margin):
    """Per step below its reference midpoint: extra orders and profit if that
    one step reached the midpoint. Returns [(label, rate, target, extra_orders,
    extra_profit)] sorted by profit."""
    res = []
    for key, label, n, rate, lo, hi in rates:
        if rate is None or lo is None:
            continue
        target = (lo + hi) / 2
        if rate >= target or rate <= 0:
            continue
        extra = orders * (target / rate - 1)
        res.append((label, rate, target, extra, extra * aov * margin))
    return sorted(res, key=lambda x: -x[4])


def product_leaks(items, margin, min_views=MIN_ITEM_VIEWS):
    """items: {name: {viewed, added_to_cart, purchased, revenue}}. Returns
    (median_cart_rate, cart_to_purchase, [rows]) with rows sorted by lost profit."""
    tracked = {k: v for k, v in items.items() if v.get("viewed", 0) >= min_views}
    if not tracked:
        return None, None, []
    med = statistics.median(v["added_to_cart"] / v["viewed"] for v in tracked.values())
    added = sum(v.get("added_to_cart", 0) for v in items.values())
    bought = sum(v.get("purchased", 0) for v in items.values())
    c2p = (bought / added) if added else 0.0
    rows = []
    for name, v in tracked.items():
        rate = v["added_to_cart"] / v["viewed"]
        price = (v["revenue"] / v["purchased"]) if v.get("purchased") else None
        lost_carts = max(0.0, v["viewed"] * (med - rate))
        lost_profit = lost_carts * c2p * (price or 0) * margin
        rows.append({"name": name, "viewed": v["viewed"], "added": v["added_to_cart"],
                     "purchased": v.get("purchased", 0), "rate": rate, "price": price,
                     "lost_carts": lost_carts, "lost_profit": lost_profit})
    rows.sort(key=lambda r: (-r["lost_profit"], -r["lost_carts"]))
    return med, c2p, rows


# ---------------------------------------------------------------- GA4
def _ga4(cfg):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account
    g = cfg["data_sources"]["ga4"]
    creds = service_account.Credentials.from_service_account_file(
        str(ROOT / g["credentials_path"]),
        scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    return BetaAnalyticsDataClient(credentials=creds), f"properties/{g['property_id']}"


def funnel_by_device(client, prop, start, end):
    """{device: {step_key: users or None}} plus '_all'."""
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, Filter, FilterExpression,
    )
    dr = [DateRange(start_date=start, end_date=end)]

    def users(dims, flt=None):
        req = RunReportRequest(property=prop, date_ranges=dr,
                               dimensions=[Dimension(name=d) for d in dims],
                               metrics=[Metric(name="totalUsers")],
                               dimension_filter=flt, limit=1000)
        return client.run_report(req).rows

    def in_list(vals):
        return FilterExpression(filter=Filter(field_name="eventName",
                                              in_list_filter=Filter.InListFilter(values=vals)))

    out = {}
    for row in users(["deviceCategory"]):
        out.setdefault(row.dimension_values[0].value, {})["visit"] = int(row.metric_values[0].value)
    for row in users(["deviceCategory"], in_list(["view_item", "select_item", "add_to_cart"])):
        out.setdefault(row.dimension_values[0].value, {})["reached"] = int(row.metric_values[0].value)
    seen = set()
    for row in users(["deviceCategory", "eventName"], in_list(EVENTS)):
        dev, ev = row.dimension_values[0].value, row.dimension_values[1].value
        seen.add(ev)
        out.setdefault(dev, {})[ev] = int(row.metric_values[0].value)
    all_ = {"visit": 0, "reached": 0}
    for dev, d in out.items():
        for k, v in d.items():
            all_[k] = all_.get(k, 0) + v
    out["_all"] = all_
    for d in out.values():  # events the store never sends = not tracked
        for ev in EVENTS:
            if ev not in seen:
                d[ev] = None
            else:
                d.setdefault(ev, 0)
    return out


def items(cfg, days):
    from src.data_sources.ga4_client import GA4Client
    g = cfg["data_sources"]["ga4"]
    return GA4Client(property_id=str(g["property_id"]),
                     credentials_path=str(ROOT / g["credentials_path"])).get_item_data(days=days)


def item_breakdown(client, prop, start, end, name_contains):
    """Views / add-to-carts / purchases of matching items by channel and device.
    Item-scoped metrics with session dimensions; prints why if GA4 refuses."""
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, Filter, FilterExpression,
    )
    flt = FilterExpression(filter=Filter(field_name="itemName", string_filter=Filter.StringFilter(
        value=name_contains, match_type=Filter.StringFilter.MatchType.CONTAINS,
        case_sensitive=False)))
    for dim in ("sessionDefaultChannelGroup", "sessionSourceMedium", "deviceCategory"):
        try:
            req = RunReportRequest(
                property=prop, date_ranges=[DateRange(start_date=start, end_date=end)],
                dimensions=[Dimension(name="itemName"), Dimension(name=dim)],
                metrics=[Metric(name=m) for m in ("itemsViewed", "itemsAddedToCart",
                                                  "itemsPurchased", "itemRevenue")],
                dimension_filter=flt, limit=200)
            rows = client.run_report(req).rows
        except Exception as e:
            print(f"   ({dim}: GA4 refused this combination: {str(e)[:120]})")
            continue
        print(f"\n   by {dim}:")
        print(f"   {'item':<40}{dim[:22]:<24}{'views':>7}{'carts':>7}{'sold':>6}{'revenue':>9}")
        data = sorted(rows, key=lambda r: -float(r.metric_values[0].value))
        for r in data[:25]:
            m = [float(x.value) for x in r.metric_values]
            print(f"   {r.dimension_values[0].value[:39]:<40}{r.dimension_values[1].value[:23]:<24}"
                  f"{m[0]:>7.0f}{m[1]:>7.0f}{m[2]:>6.0f}{m[3]:>9.0f}")


# ---------------------------------------------------------------- main
def pct(x):
    return "   -  " if x is None else f"{x * 100:5.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--item", default=None,
                    help="only show where views/carts/sales of items whose name contains "
                         "this text come from (channel, source, device)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    ps = cfg.get("paid_search", {})
    margins = [float(v["margin"]) for v in ps.get("product_lines", {}).values() if "margin" in v]
    margin = (statistics.median(margins) if margins else 0.30) - float(ps.get("card_fee", 0.03))
    months = args.days / 30.0

    end_d = date.today() - timedelta(days=1)
    start_d = end_d - timedelta(days=args.days - 1)
    client, prop = _ga4(cfg)
    if args.item:
        print(f"ITEM BREAKDOWN  '{args.item}'  {start_d} -> {end_d}")
        item_breakdown(client, prop, start_d.isoformat(), end_d.isoformat(), args.item)
        return
    fb = funnel_by_device(client, prop, start_d.isoformat(), end_d.isoformat())
    it = items(cfg, args.days)

    orders = (fb["_all"].get("purchase") or 0)  # purchasing users, ~= orders
    aov = float(cfg.get("profit_model", {}).get("aov", 53.19))

    print("=" * 92)
    print(f"CONVERSION AUDIT  {start_d} -> {end_d}  ({args.days} days, all channels, GA4 users)")
    print(f"blended net margin used: {margin*100:.1f}%   average order: ${aov:.2f}")
    print("=" * 92)

    def show(name, counts):
        rates = step_rates(counts)
        print(f"\n{name}")
        print(f"   {'step':<20}{'users':>8}{'of prev':>9}{'reference':>14}")
        for key, label, n, rate, lo, hi in rates:
            ref = f"{lo*100:.0f}-{hi*100:.0f}%" if lo is not None else ""
            flag = ""
            if rate is not None and lo is not None and rate < lo:
                flag = "  <-- below range"
            nn = "not tracked" if n is None else f"{n:,}"
            print(f"   {label:<20}{nn:>8}{pct(rate):>9}{ref:>14}{flag}")
        if counts.get("visit") and counts.get("purchase") is not None:
            print(f"   overall: {counts['purchase'] / counts['visit'] * 100:.2f}% of visitors bought")
        return rates

    all_rates = show("WHOLE STORE", fb["_all"])
    for dev in ("desktop", "mobile", "tablet"):
        if dev in fb and fb[dev].get("visit", 0) >= 200:
            show(dev.upper(), fb[dev])

    print("\nWHAT FIXING EACH STEP IS WORTH (one step at a time, to its reference midpoint)")
    fv = fix_value(all_rates, orders / months, aov, margin)
    if not fv:
        print("   every tracked step is at or above its reference midpoint")
    for label, rate, target, extra, profit in fv:
        print(f"   {label:<20} {rate*100:5.1f}% -> {target*100:4.0f}%:  +{extra:,.1f} orders/month, "
              f"+${profit:,.0f} gross profit/month")

    med, c2p, rows = product_leaks(it, margin)
    print(f"\nPRODUCTS SHOPPERS LOOK AT BUT DON'T ADD TO CART  (>= {MIN_ITEM_VIEWS} views)")
    if med is None:
        print("   not enough item-level data in GA4 (view_item / add_to_cart by item).")
    else:
        print(f"   site median add-to-cart rate {med*100:.1f}% of views; "
              f"cart-to-purchase {c2p*100:.1f}%")
        print(f"   {'product':<46}{'views':>7}{'carts':>7}{'rate':>7}{'sold':>6}{'price':>8}{'lost $/mo':>11}")
        for r in rows[:args.top]:
            price = f"{r['price']:.0f}" if r["price"] else "-"
            print(f"   {r['name'][:45]:<46}{r['viewed']:>7}{r['added']:>7}{r['rate']*100:>6.1f}%"
                  f"{r['purchased']:>6}{price:>8}{r['lost_profit']/months:>11,.0f}")
    print("\n'reference' = rough ecommerce rules of thumb (e.g. Baymard: ~70% of carts are "
          "abandoned on average).\nStronger evidence is internal: desktop vs mobile, and "
          "each product vs your own median. Decide by the dollar columns.")


if __name__ == "__main__":
    main()
