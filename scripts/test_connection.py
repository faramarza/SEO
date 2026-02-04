#!/usr/bin/env python3
"""
Connection Test — Verify GSC and GA4 API credentials work.

Run this after setting up credentials.json to confirm everything is connected.

Usage:
    python scripts/test_connection.py
"""

import json
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config():
    """Load configuration from defaults.json."""
    config_path = PROJECT_ROOT / "config" / "defaults.json"
    with open(config_path) as f:
        return json.load(f)


def test_gsc_connection(config: dict) -> bool:
    """Test Google Search Console API connection."""
    print("\n" + "=" * 50)
    print("TESTING GOOGLE SEARCH CONSOLE CONNECTION")
    print("=" * 50)

    gsc_config = config.get("data_sources", {}).get("gsc", {})
    property_url = gsc_config.get("property_url")
    credentials_path = PROJECT_ROOT / gsc_config.get("credentials_path", "credentials.json")

    print(f"Property: {property_url}")
    print(f"Credentials: {credentials_path}")

    if not credentials_path.exists():
        print("✗ FAIL: credentials.json not found")
        print(f"  Expected at: {credentials_path}")
        return False

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path),
            scopes=['https://www.googleapis.com/auth/webmasters.readonly']
        )

        service = build('searchconsole', 'v1', credentials=credentials)

        # Test: list sites accessible to this service account
        site_list = service.sites().list().execute()
        sites = site_list.get('siteEntry', [])

        print(f"\nAccessible sites ({len(sites)}):")
        target_found = False
        for site in sites:
            url = site.get('siteUrl', '')
            level = site.get('permissionLevel', 'unknown')
            marker = ""
            if property_url in url or url in property_url:
                marker = " ← TARGET"
                target_found = True
            print(f"  • {url} ({level}){marker}")

        if not sites:
            print("✗ FAIL: No sites accessible")
            print("  Did you add the service account email to GSC?")
            return False

        if not target_found:
            print(f"\n⚠ WARNING: Target property '{property_url}' not found in list")
            print("  The service account may not have access to this property.")
            return False

        # Test: fetch a small amount of data
        print("\nFetching sample data (last 7 days)...")
        request = {
            'startDate': '2024-01-01',
            'endDate': '2024-01-07',
            'dimensions': ['page'],
            'rowLimit': 5
        }

        response = service.searchanalytics().query(
            siteUrl=property_url,
            body=request
        ).execute()

        rows = response.get('rows', [])
        print(f"Sample rows returned: {len(rows)}")

        if rows:
            print("\nTop pages (sample):")
            for row in rows[:3]:
                page = row.get('keys', [''])[0]
                clicks = row.get('clicks', 0)
                impressions = row.get('impressions', 0)
                print(f"  • {page[:60]}...")
                print(f"    Clicks: {clicks}, Impressions: {impressions}")

        print("\n✓ PASS: GSC connection successful")
        return True

    except Exception as e:
        print(f"\n✗ FAIL: {type(e).__name__}: {e}")
        return False


def test_ga4_connection(config: dict) -> bool:
    """Test Google Analytics 4 Data API connection."""
    print("\n" + "=" * 50)
    print("TESTING GOOGLE ANALYTICS 4 CONNECTION")
    print("=" * 50)

    ga4_config = config.get("data_sources", {}).get("ga4", {})
    property_id = ga4_config.get("property_id")
    credentials_path = PROJECT_ROOT / ga4_config.get("credentials_path", "credentials.json")

    print(f"Property ID: {property_id}")
    print(f"Credentials: {credentials_path}")

    if not credentials_path.exists():
        print("✗ FAIL: credentials.json not found")
        return False

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import (
            RunReportRequest,
            DateRange,
            Dimension,
            Metric,
        )
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(credentials_path),
            scopes=['https://www.googleapis.com/auth/analytics.readonly']
        )

        client = BetaAnalyticsDataClient(credentials=credentials)

        # Test: run a simple report
        print("\nFetching sample data (last 7 days)...")
        request = RunReportRequest(
            property=f"properties/{property_id}",
            date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
            dimensions=[Dimension(name="landingPage")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="totalRevenue"),
            ],
            limit=5,
        )

        response = client.run_report(request)

        print(f"Sample rows returned: {len(response.rows)}")

        if response.rows:
            print("\nTop landing pages (sample):")
            for row in response.rows[:3]:
                page = row.dimension_values[0].value
                sessions = row.metric_values[0].value
                revenue = row.metric_values[1].value
                print(f"  • {page[:60]}...")
                print(f"    Sessions: {sessions}, Revenue: ${float(revenue):.2f}")

        print("\n✓ PASS: GA4 connection successful")
        return True

    except Exception as e:
        print(f"\n✗ FAIL: {type(e).__name__}: {e}")
        if "403" in str(e) or "permission" in str(e).lower():
            print("  Did you add the service account email to GA4?")
        return False


def main():
    """Run all connection tests."""
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  CAPITAL GOVERNOR — CONNECTION TEST                      ║")
    print("╚══════════════════════════════════════════════════════════╝")

    config = load_config()

    gsc_ok = test_gsc_connection(config)
    ga4_ok = test_ga4_connection(config)

    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"GSC: {'✓ PASS' if gsc_ok else '✗ FAIL'}")
    print(f"GA4: {'✓ PASS' if ga4_ok else '✗ FAIL'}")

    if gsc_ok and ga4_ok:
        print("\n✓ All connections successful. Ready to run evaluations.")
        print("\nNext step:")
        print("  python scripts/run_evaluation.py")
        return 0
    else:
        print("\n✗ Fix connection issues before running evaluations.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
