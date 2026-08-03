#!/usr/bin/env python3
"""Confirm the root cause of the '(not set)' order-attribution gap.

Run on the server (venv Python):
    venv/bin/python scripts/diagnose_attribution.py            # last 90 days
    venv/bin/python scripts/diagnose_attribution.py --days 7   # post-fix check

92% of orders were landing in source '(not set)'. This profiles that segment to
tell us WHY, so we know whether the checkout-tag fix is sufficient or a GA4 config
change (referral exclusions / cross-domain) is still needed:

  • If the '(not set)' ORDERS start (land) on a checkout/success/payment-return URL
    → the payment redirect is breaking the session (shopper returns from the gateway
    as a fresh, source-less session). A tag fix does NOT fix this — you need to add
    the gateway domain to GA4 referral exclusions + turn on cross-domain measurement.
  • If they land on normal product/category pages → it's a tag/consent firing issue
    (which your checkout fix may already have solved — re-run with --days 7 to see).

Read-only. Pulls from GA4. Modifies nothing.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "defaults.json") as f:
        return json.load(f)


def _client(config):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account
    g = config["data_sources"]["ga4"]
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / g["credentials_path"]),
        scopes=['https://www.googleapis.com/auth/analytics.readonly'])
    return BetaAnalyticsDataClient(credentials=creds), g["property_id"]


def report(client, prop, days, dims, metrics, src_filter=None, limit=25):
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, OrderBy,
        Filter, FilterExpression)
    kw = {}
    if src_filter is not None:
        kw["dimension_filter"] = FilterExpression(filter=Filter(
            field_name="sessionSourceMedium",
            string_filter=Filter.StringFilter(value=src_filter,
                match_type=Filter.StringFilter.MatchType.EXACT)))
    req = RunReportRequest(
        property=f"properties/{prop}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in metrics],
        limit=limit,
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name=metrics[-1]), desc=True)],
        **kw)
    resp = client.run_report(req)
    out = []
    for r in resp.rows:
        out.append({
            **{d: r.dimension_values[i].value for i, d in enumerate(dims)},
            **{m: float(r.metric_values[i].value or 0) for i, m in enumerate(metrics)}})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    config = load_config()
    try:
        client, prop = _client(config)
    except Exception as e:
        print(f"GA4 auth failed: {type(e).__name__}: {e}"); sys.exit(1)

    NS = "(not set)"
    print(f"Window: last {args.days} days\n")

    # 1. Where do the UNATTRIBUTED ORDERS start? (the smoking gun)
    print("=" * 78)
    print("'(not set)' ORDERS — where the session LANDS (its first page)")
    print("=" * 78)
    try:
        rows = report(client, prop, args.days,
                      ["landingPagePlusQueryString"],
                      ["sessions", "ecommercePurchases", "purchaseRevenue"],
                      src_filter=NS, limit=25)
    except Exception as e:
        print(f"  query failed: {type(e).__name__}: {e}")
        rows = []
    order_rows = [r for r in rows if r["ecommercePurchases"] > 0]
    if order_rows:
        print(f"  {'orders':>6} {'sess':>7} {'revenue':>10}  landing page")
        for r in order_rows[:20]:
            print(f"  {r['ecommercePurchases']:>6,.0f} {r['sessions']:>7,.0f} "
                  f"${r['purchaseRevenue']:>9,.0f}  {r['landingPagePlusQueryString'][:52]}")
        # verdict hint
        checkoutish = sum(r["ecommercePurchases"] for r in order_rows
                          if any(k in r["landingPagePlusQueryString"].lower()
                                 for k in ("checkout", "success", "onepage", "payment",
                                           "thank", "order", "paypal", "stripe", "return")))
        total = sum(r["ecommercePurchases"] for r in order_rows)
        if total:
            print(f"\n  → {100*checkoutish/total:.0f}% of these orders LAND on a "
                  f"checkout/success/payment-return page.")
            if checkoutish / total >= 0.4:
                print("    That's the PAYMENT-REDIRECT SESSION BREAK signature. A tag fix alone")
                print("    won't fix it — add your payment gateway domain to GA4 Admin →")
                print("    Data Streams → Configure tag settings → List unwanted referrals,")
                print("    and enable cross-domain measurement for the checkout/gateway domains.")
            else:
                print("    Orders mostly start on normal pages — this looks more like a tag/")
                print("    consent firing gap (which your checkout fix may already address).")
                print("    Re-run with --days 7 in a few days to confirm '(not set)' is shrinking.")
    else:
        print("  No '(not set)' orders in this window — if that's a SHORT window right")
        print("  after your fix, that's a GOOD sign attribution is recovering.")

    # 2. device split of the segment (rule out app/bot)
    print("\n" + "=" * 78)
    print("'(not set)' sessions by device (rule out app/bot artifacts)")
    print("=" * 78)
    try:
        for r in report(client, prop, args.days, ["deviceCategory"],
                        ["sessions", "ecommercePurchases"], src_filter=NS, limit=10):
            print(f"  {r['deviceCategory']:10} {r['sessions']:>8,.0f} sess | "
                  f"{r['ecommercePurchases']:>5,.0f} orders")
    except Exception as e:
        print(f"  query failed: {type(e).__name__}: {e}")

    print("\nRead-only — nothing modified.")


if __name__ == "__main__":
    main()
