#!/usr/bin/env python3
"""Traffic quality: how much of your traffic is real people vs bots, and what the
REAL human conversion rate is. Answers "is it a ranking problem or a CRO problem?"
with numbers instead of a guess.

Run on the server (venv Python, needs the Google libs + credentials.json):

    /var/www/seo/venv/bin/python scripts/traffic_quality.py
    ... --days 90            (window length, default 90)
    ... --pages 25           (landing pages to list, default 25)

HOW IT SEPARATES BOTS FROM PEOPLE (three independent checks, no single guess):
  1. GSC clicks are Google-verified human clicks from Google Search. Google
     filters invalid/bot clicks before reporting. So GSC clicks are a FLOOR on
     real organic humans from Google. If GA4 "Organic Search" sessions are far
     above GSC clicks (+ a Bing allowance), the excess is not Google humans.
  2. GA4 engaged sessions (10s+ on site, or 2+ pages, or a key event). Bots and
     scrapers almost never engage. Engaged sessions = the human estimate; the
     unengaged remainder is mostly bots, accidental clicks and instant bounces.
  3. Segment red flags: countries you don't ship to, and country/device/browser
     segments with near-zero engagement and many sessions (classic bot farms,
     e.g. "(not set)" browser, Linux desktop from data centers, China/Singapore
     spikes). These are listed so you can see them, and excluded from the
     cleaned estimate.
Then it compares GA4 purchases with REAL Magento orders for the same window, so
a tracking leak is not mistaken for a conversion problem.

Read-only. Changes nothing in GA4, GSC, Magento or the site.
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Countries you can actually sell to. Traffic outside these cannot buy, so it is
# reported separately (real or not, it is not your market).
SELL_COUNTRIES = {"United States", "Canada"}
# A segment is flagged as bot-like when it has real volume but almost nobody
# engages. Thresholds are deliberately conservative so humans are not discarded.
FLAG_MIN_SESSIONS = 30
FLAG_MAX_ENGAGEMENT = 0.15
FLAG_MAX_AVG_ENGAGED_SEC = 3.0


def _cfg():
    return json.loads((ROOT / "config" / "defaults.json").read_text())


def _ga4(cfg):
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account
    g = cfg["data_sources"]["ga4"]
    creds = service_account.Credentials.from_service_account_file(
        str(ROOT / g["credentials_path"]),
        scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    return BetaAnalyticsDataClient(credentials=creds), f"properties/{g['property_id']}"


def _gsc(cfg):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    g = cfg["data_sources"]["gsc"]
    creds = service_account.Credentials.from_service_account_file(
        str(ROOT / g["credentials_path"]),
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    return build("searchconsole", "v1", credentials=creds), g["property_url"]


def ga4_report(client, prop, start, end, dims, organic_only=False, limit=1000):
    """Rows of {dim..., sessions, engaged, eng_rate, eng_sec, purchases, revenue}."""
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric, Filter, FilterExpression,
        OrderBy,
    )
    flt = None
    if organic_only:
        flt = FilterExpression(filter=Filter(
            field_name="sessionDefaultChannelGroup",
            string_filter=Filter.StringFilter(
                value="Organic Search", match_type=Filter.StringFilter.MatchType.EXACT)))
    req = RunReportRequest(
        property=prop,
        date_ranges=[DateRange(start_date=start, end_date=end)],
        dimensions=[Dimension(name=d) for d in dims],
        metrics=[Metric(name=m) for m in (
            "sessions", "engagedSessions", "userEngagementDuration",
            "ecommercePurchases", "purchaseRevenue")],
        dimension_filter=flt,
        order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(req)
    out = []
    for row in resp.rows:
        r = {d: row.dimension_values[i].value or "(not set)" for i, d in enumerate(dims)}
        m = {h.name: float(row.metric_values[i].value or 0)
             for i, h in enumerate(resp.metric_headers)}
        s = int(m["sessions"])
        e = int(m["engagedSessions"])
        r.update(sessions=s, engaged=e,
                 eng_rate=(e / s) if s else 0.0,
                 eng_sec=(m["userEngagementDuration"] / s) if s else 0.0,
                 purchases=int(m["ecommercePurchases"]),
                 revenue=round(m["purchaseRevenue"], 2))
        out.append(r)
    return out


def is_bot_like(r):
    return (r["sessions"] >= FLAG_MIN_SESSIONS
            and r["eng_rate"] <= FLAG_MAX_ENGAGEMENT
            and r["eng_sec"] <= FLAG_MAX_AVG_ENGAGED_SEC)


def classify(rows):
    """Split organic country x device x browser rows into buckets. Pure function
    so it can be unit tested without Google credentials."""
    b = {"total": 0, "engaged": 0, "foreign": 0, "foreign_engaged": 0,
         "botlike": 0, "botlike_segments": [], "clean": 0, "clean_engaged": 0,
         "purchases": 0}
    for r in rows:
        b["total"] += r["sessions"]
        b["engaged"] += r["engaged"]
        b["purchases"] += r["purchases"]
        if r.get("country") not in SELL_COUNTRIES:
            b["foreign"] += r["sessions"]
            b["foreign_engaged"] += r["engaged"]
            continue
        if is_bot_like(r):
            b["botlike"] += r["sessions"]
            b["botlike_segments"].append(r)
            continue
        b["clean"] += r["sessions"]
        b["clean_engaged"] += r["engaged"]
    return b


def pct(a, b):
    return f"{(100.0 * a / b):.1f}%" if b else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pages", type=int, default=25)
    args = ap.parse_args()

    cfg = _cfg()
    # GSC lags ~3 days; use the same window for every source so numbers line up.
    end_d = date.today() - timedelta(days=3)
    start_d = end_d - timedelta(days=args.days - 1)
    start, end = start_d.isoformat(), end_d.isoformat()

    try:
        ga, prop = _ga4(cfg)
    except Exception as e:
        sys.exit(f"GA4 not available ({e}). Run on the server with credentials.json.")

    print("=" * 76)
    print(f"TRAFFIC QUALITY  {start} -> {end}  ({args.days} days)")
    print("=" * 76)

    # ---- 1. Channels: where is the unengaged traffic? -------------------------
    ch = ga4_report(ga, prop, start, end, ["sessionDefaultChannelGroup"])
    print("\n1) ALL CHANNELS  (engaged = 10s+, or 2+ pages, or a key event)")
    print(f"   {'channel':<22}{'sessions':>9}{'engaged':>9}{'eng%':>7}{'sec/sess':>9}{'GA4 buys':>9}")
    for r in ch:
        print(f"   {r['sessionDefaultChannelGroup'][:21]:<22}{r['sessions']:>9}{r['engaged']:>9}"
              f"{r['eng_rate']*100:>6.0f}%{r['eng_sec']:>9.1f}{r['purchases']:>9}")
    all_sessions = sum(r["sessions"] for r in ch)
    all_engaged = sum(r["engaged"] for r in ch)
    ga4_buys = sum(r["purchases"] for r in ch)
    ga4_rev = sum(r["revenue"] for r in ch)

    # ---- 2. Organic segments: country x device x browser -----------------------
    seg = ga4_report(ga, prop, start, end, ["country", "deviceCategory", "browser"],
                     organic_only=True, limit=5000)
    b = classify(seg)
    print("\n2) ORGANIC SEARCH, split into real-market humans vs the rest")
    print(f"   GA4 organic sessions ............... {b['total']:>7}")
    print(f"   - outside US/Canada (can't buy) .... {b['foreign']:>7}  ({pct(b['foreign'], b['total'])})"
          f", of which engaged {b['foreign_engaged']}")
    print(f"   - bot-like US/CA segments .......... {b['botlike']:>7}  ({pct(b['botlike'], b['total'])})")
    print(f"   = clean US/CA sessions ............. {b['clean']:>7}")
    print(f"     of which ENGAGED (best human est.) {b['clean_engaged']:>7}  ({pct(b['clean_engaged'], b['total'])} of all organic)")
    if b["botlike_segments"]:
        print("\n   Bot-like segments (>= {0} sessions, <= {1:.0f}% engaged, <= {2:.0f}s avg):"
              .format(FLAG_MIN_SESSIONS, FLAG_MAX_ENGAGEMENT * 100, FLAG_MAX_AVG_ENGAGED_SEC))
        for r in sorted(b["botlike_segments"], key=lambda x: -x["sessions"])[:12]:
            print(f"     {r['country'][:16]:<17}{r['deviceCategory']:<9}{r['browser'][:18]:<19}"
                  f"{r['sessions']:>6} sess  {r['eng_rate']*100:>3.0f}% eng  {r['eng_sec']:.1f}s")
    foreign = {}
    for r in seg:
        if r["country"] not in SELL_COUNTRIES:
            foreign[r["country"]] = foreign.get(r["country"], 0) + r["sessions"]
    if foreign:
        top = sorted(foreign.items(), key=lambda x: -x[1])[:8]
        print("   Top non-US/CA countries: " + ", ".join(f"{c} {n}" for c, n in top))

    # ---- 3. GSC cross-check: Google-verified human clicks ---------------------
    gsc_clicks = None
    try:
        svc, gprop = _gsc(cfg)
        rows = svc.searchanalytics().query(siteUrl=gprop, body={
            "startDate": start, "endDate": end, "type": "web"}).execute().get("rows", [])
        gsc_clicks = int(rows[0]["clicks"]) if rows else 0
        gsc_us = svc.searchanalytics().query(siteUrl=gprop, body={
            "startDate": start, "endDate": end, "type": "web", "dimensions": ["country"],
            "rowLimit": 300}).execute().get("rows", [])
        us_ca = sum(int(r["clicks"]) for r in gsc_us if r["keys"][0] in ("usa", "can"))
    except Exception as e:
        print(f"\n3) GSC cross-check skipped ({type(e).__name__}: {e})")
    if gsc_clicks is not None:
        print("\n3) GSC CROSS-CHECK  (Google filters bot clicks before reporting)")
        print(f"   Google web-search clicks ........... {gsc_clicks:>7}   (US+Canada: {us_ca})")
        print(f"   GA4 organic sessions ............... {b['total']:>7}   (includes Bing, DuckDuckGo, etc.)")
        if gsc_clicks:
            ratio = b["total"] / gsc_clicks
            print(f"   GA4 organic / GSC clicks ........... {ratio:>7.2f}x")
            if ratio > 1.5:
                print("   -> GA4 shows much MORE organic traffic than Google sent. The excess is "
                      "other engines,\n      bots, or session splitting. Trust GSC clicks as "
                      "the human floor.")
            elif ratio < 0.7:
                print("   -> GA4 shows much LESS than Google sent: consent banner / tag not firing "
                      "on some pages.\n      Organic humans are UNDER-counted, not over-counted.")
            else:
                print("   -> GA4 and GSC broadly agree: organic traffic is mostly real people.")

    # ---- 4. Tracking: GA4 purchases vs real Magento orders --------------------
    print("\n4) PURCHASE TRACKING  (GA4 vs real Magento orders, same window)")
    mag = None
    try:
        from src.data_sources.magento_client import MagentoClient
        mc = MagentoClient()
        if mc.configured:
            mag = mc.order_revenue(start, (end_d + timedelta(days=1)).isoformat())
    except Exception as e:
        print(f"   Magento read failed ({type(e).__name__}: {e})")
    print(f"   GA4 purchases (all channels) ....... {ga4_buys:>7}   ${ga4_rev:,.0f}")
    if mag:
        print(f"   Magento orders (not canceled) ...... {mag['count']:>7}   ${mag['base_revenue']:,.0f}")
        cap = (ga4_buys / mag["count"]) if mag["count"] else 0
        print(f"   GA4 captures ....................... {cap*100:>6.0f}% of real orders")
        if cap < 0.8:
            print("   -> GA4 is MISSING orders. Every GA4 conversion rate below is too low by "
                  "about this factor.")
    else:
        print("   Magento not configured here (MAGENTO_BASE_URL/MAGENTO_TOKEN); "
              "capture rate unknown.")

    # ---- 5. The verdict numbers ----------------------------------------------
    print("\n5) REAL CONVERSION RATES")
    print(f"   Site, raw GA4:  {pct(ga4_buys, all_sessions)} of all sessions"
          f"   ({ga4_buys} / {all_sessions})")
    print(f"   Site, engaged:  {pct(ga4_buys, all_engaged)} of engaged sessions")
    if mag:
        print(f"   Site, REAL:     {pct(mag['count'], all_engaged)} of engaged sessions using "
              f"Magento orders  ({mag['count']} / {all_engaged})")
    print(f"   Organic, GA4:   {pct(b['purchases'], b['clean_engaged'])} of clean engaged "
          f"organic sessions ({b['purchases']} / {b['clean_engaged']})")
    print("   Benchmark: small ecommerce gift/toy stores typically convert 1-3% of engaged "
          "sessions.\n   Below that on REAL orders = CRO problem. In range = the problem "
          "is not enough visitors\n   (rankings), not the pages.")

    # ---- 6. Organic landing pages, human-only view ---------------------------
    lp = ga4_report(ga, prop, start, end, ["landingPage"], organic_only=True, limit=args.pages)
    print(f"\n6) TOP {args.pages} ORGANIC LANDING PAGES  (engaged = human estimate)")
    print(f"   {'page':<52}{'sess':>6}{'human':>7}{'eng%':>6}{'buys':>5}")
    for r in lp:
        print(f"   {r['landingPage'][:51]:<52}{r['sessions']:>6}{r['engaged']:>7}"
              f"{r['eng_rate']*100:>5.0f}%{r['purchases']:>5}")

    print("\nNext: scripts/winnable_action_sheet.py --min-pos 8 --max-pos 30 lists the "
          "page-2/3 queries\nworth pushing to page 1, per page, with the fix for each.")


if __name__ == "__main__":
    main()
