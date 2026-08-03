#!/usr/bin/env python3
"""Pinterest (and all social) → site → conversion, straight from GA4.

Run on the server (venv Python — needs the Google Analytics libs):

    venv/bin/python scripts/diagnose_pinterest.py            # last 90 days
    venv/bin/python scripts/diagnose_pinterest.py --days 28

The question this answers: your Pinterest profile shows ~18.7k monthly VIEWS — but
do those views become site sessions and SALES? GA4 is the only source that knows.
It pulls sessions / engaged sessions / add-to-carts / purchases / revenue by
source-medium, isolates Pinterest, and lines it up against your other channels so
the number has context (is Pinterest a live channel you're under-using, or reach
that never reaches your site?).

Read-only. Pulls from GA4. Modifies nothing.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

METRICS = ["sessions", "engagedSessions", "addToCarts", "ecommercePurchases", "purchaseRevenue"]
SOCIAL_HINTS = ("pinterest", "pin.it", "instagram", "facebook", "fb.", "reddit",
                "t.co", "twitter", "x.com", "tiktok", "youtube", "linkedin")


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "defaults.json") as f:
        return json.load(f)


def pull(config: dict, days: int):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, OrderBy)
    from google.oauth2 import service_account

    g = config["data_sources"]["ga4"]
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / g["credentials_path"]),
        scopes=['https://www.googleapis.com/auth/analytics.readonly'])
    client = BetaAnalyticsDataClient(credentials=creds)
    req = RunReportRequest(
        property=f"properties/{g['property_id']}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="sessionSourceMedium")],
        metrics=[Metric(name=m) for m in METRICS],
        limit=250,
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
    )
    resp = client.run_report(req)
    rows = []
    for r in resp.rows:
        vals = [float(v.value or 0) for v in r.metric_values]
        rows.append({"src": r.dimension_values[0].value, **dict(zip(METRICS, vals))})
    return rows


def _fmt(r: dict) -> str:
    s = r["sessions"]
    cvr = (r["ecommercePurchases"] / s * 100) if s else 0
    eng = (r["engagedSessions"] / s * 100) if s else 0
    return (f"{s:>7,.0f} sess | {eng:>4.0f}% eng | {r['addToCarts']:>5,.0f} ATC | "
            f"{r['ecommercePurchases']:>4,.0f} orders ({cvr:>4.2f}% CVR) | "
            f"${r['purchaseRevenue']:>8,.0f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    config = load_config()

    try:
        rows = pull(config, args.days)
    except Exception as e:
        print(f"GA4 pull failed: {type(e).__name__}: {e}")
        sys.exit(1)
    if not rows:
        print("GA4 returned no rows — check the property/credentials.")
        return

    tot = {m: sum(r[m] for r in rows) for m in METRICS}
    print(f"Window: last {args.days} days\n")
    print("SITE TOTAL (all channels):")
    print("  " + _fmt(tot))
    print()

    pin = [r for r in rows if "pinterest" in r["src"].lower() or "pin.it" in r["src"].lower()]
    print("=" * 78)
    print("PINTEREST → SITE → SALES")
    print("=" * 78)
    if pin:
        pt = {m: sum(r[m] for r in pin) for m in METRICS}
        print("  " + _fmt(pt))
        print(f"  = {100*pt['sessions']/tot['sessions']:.1f}% of all site sessions, "
              f"{100*pt['ecommercePurchases']/tot['ecommercePurchases'] if tot['ecommercePurchases'] else 0:.1f}% of orders")
        print("  breakdown by exact source/medium:")
        for r in sorted(pin, key=lambda x: -x["sessions"]):
            print(f"    {r['src'][:34]:34} {_fmt(r)}")
    else:
        print("  ZERO Pinterest sessions in this window.")
        print("  → Your 18.7k Pinterest views are NOT reaching the site at all. That's a")
        print("    content/link problem (or the Pinterest Tag / UTMs aren't set), not a")
        print("    'Pinterest doesn't work' problem — the views exist, the clicks don't.")

    # all social for context
    social = [r for r in rows if any(h in r["src"].lower() for h in SOCIAL_HINTS)]
    if social:
        print("\n" + "=" * 78)
        print("ALL SOCIAL SOURCES (context)")
        print("=" * 78)
        for r in sorted(social, key=lambda x: -x["sessions"]):
            print(f"  {r['src'][:34]:34} {_fmt(r)}")

    print("\n" + "=" * 78)
    print("TOP 15 CHANNELS OVERALL (what actually drives your sessions)")
    print("=" * 78)
    for r in sorted(rows, key=lambda x: -x["sessions"])[:15]:
        print(f"  {r['src'][:34]:34} {_fmt(r)}")

    print("\nRead-only — nothing modified.")


if __name__ == "__main__":
    main()
