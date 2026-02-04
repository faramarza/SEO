#!/usr/bin/env python3
"""
Tracking Sanity Diagnostic — Show GSC clicks vs GA4 sessions mismatch.

Usage:
    python scripts/diagnose_tracking.py
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "defaults.json"
    with open(config_path) as f:
        return json.load(f)


def pull_gsc_clicks(config: dict, days: int = 28) -> dict[str, int]:
    """Pull clicks per page from GSC."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    gsc_config = config["data_sources"]["gsc"]
    credentials_path = PROJECT_ROOT / gsc_config["credentials_path"]
    property_url = gsc_config["property_url"]

    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=['https://www.googleapis.com/auth/webmasters.readonly']
    )

    service = build('searchconsole', 'v1', credentials=credentials)

    end_date = date.today() - timedelta(days=3)
    start_date = end_date - timedelta(days=days)

    request = {
        'startDate': start_date.isoformat(),
        'endDate': end_date.isoformat(),
        'dimensions': ['page'],
        'rowLimit': 1000,
    }

    response = service.searchanalytics().query(
        siteUrl=property_url,
        body=request
    ).execute()

    return {
        row['keys'][0]: row.get('clicks', 0)
        for row in response.get('rows', [])
    }


def pull_ga4_sessions(config: dict, days: int = 28) -> dict[str, int]:
    """Pull sessions per landing page from GA4."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest,
        DateRange,
        Dimension,
        Metric,
    )
    from google.oauth2 import service_account

    ga4_config = config["data_sources"]["ga4"]
    credentials_path = PROJECT_ROOT / ga4_config["credentials_path"]
    property_id = ga4_config["property_id"]

    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=['https://www.googleapis.com/auth/analytics.readonly']
    )

    client = BetaAnalyticsDataClient(credentials=credentials)

    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="landingPage")],
        metrics=[Metric(name="sessions")],
        limit=1000,
    )

    response = client.run_report(request)

    return {
        row.dimension_values[0].value: int(row.metric_values[0].value)
        for row in response.rows
    }


def normalize_url_to_path(url: str, base: str = "https://alphabet-trains.com") -> str:
    """Convert full URL to path for matching."""
    if url.startswith(base):
        path = url[len(base):]
        if not path:
            path = "/"
        return path
    return url


def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  TRACKING SANITY DIAGNOSTIC                              ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    config = load_config()

    print("Pulling GSC clicks (28d)...")
    gsc_clicks = pull_gsc_clicks(config)
    print(f"  Found {len(gsc_clicks)} pages")

    print("Pulling GA4 sessions (28d)...")
    ga4_sessions = pull_ga4_sessions(config)
    print(f"  Found {len(ga4_sessions)} landing pages")

    # Match and compare
    print("\n" + "=" * 80)
    print("TRACKING COMPARISON: GSC Clicks vs GA4 Sessions")
    print("=" * 80)
    print()
    print("Healthy ratio: 0.5 to 2.0 (sessions/clicks)")
    print("Outside this range = tracking mismatch")
    print()

    comparisons = []

    for url, clicks in gsc_clicks.items():
        path = normalize_url_to_path(url)
        sessions = ga4_sessions.get(path, 0)

        if clicks > 0:
            ratio = sessions / clicks
        else:
            ratio = None

        comparisons.append({
            'url': url,
            'path': path,
            'gsc_clicks': clicks,
            'ga4_sessions': sessions,
            'ratio': ratio,
            'status': 'OK' if ratio and 0.5 <= ratio <= 2.0 else 'MISMATCH'
        })

    # Sort by clicks (highest first)
    comparisons.sort(key=lambda x: x['gsc_clicks'], reverse=True)

    # Print table
    print(f"{'Page (truncated)':<50} {'GSC Clicks':>12} {'GA4 Sessions':>14} {'Ratio':>8} {'Status':>10}")
    print("-" * 100)

    mismatches = []
    for c in comparisons[:50]:  # Top 50
        page_display = c['path'][:48] + ".." if len(c['path']) > 50 else c['path']
        ratio_str = f"{c['ratio']:.2f}" if c['ratio'] else "N/A"
        status_marker = "⚠ MISMATCH" if c['status'] == 'MISMATCH' else "✓ OK"

        print(f"{page_display:<50} {c['gsc_clicks']:>12} {c['ga4_sessions']:>14} {ratio_str:>8} {status_marker:>10}")

        if c['status'] == 'MISMATCH' and c['gsc_clicks'] >= 10:
            mismatches.append(c)

    # Summary
    total = len(comparisons)
    ok_count = sum(1 for c in comparisons if c['status'] == 'OK')
    mismatch_count = total - ok_count

    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total pages compared: {total}")
    print(f"Tracking OK: {ok_count} ({ok_count/total*100:.0f}%)")
    print(f"Tracking Mismatch: {mismatch_count} ({mismatch_count/total*100:.0f}%)")

    if mismatches:
        print()
        print("SIGNIFICANT MISMATCHES (clicks ≥ 10):")
        print("-" * 80)

        for c in mismatches[:10]:
            print(f"\n{c['url']}")
            print(f"  GSC Clicks: {c['gsc_clicks']}")
            print(f"  GA4 Sessions: {c['ga4_sessions']}")
            if c['ratio']:
                if c['ratio'] < 0.5:
                    print(f"  Ratio: {c['ratio']:.2f} — GA4 under-reporting (missing sessions)")
                else:
                    print(f"  Ratio: {c['ratio']:.2f} — GA4 over-reporting (extra sessions)")

        print()
        print("POSSIBLE CAUSES:")
        print("  • Ratio < 0.5: GA4 tracking missing on some pages, bot filtering, or sampled data")
        print("  • Ratio > 2.0: Non-organic traffic counted, internal traffic, or GA4 counting refreshes")
        print()
        print("RECOMMENDED ACTIONS:")
        print("  1. Check GA4 real-time for these pages — is tracking firing?")
        print("  2. Check if these pages have different templates (missing GA4 tag?)")
        print("  3. Check GA4 filters — is internal traffic excluded?")


if __name__ == "__main__":
    main()
