#!/usr/bin/env python3
"""
Run Tracking Sanity Diagnostics — Determine data trustworthiness.

This script:
1. Pulls GSC data (organic clicks)
2. Pulls GA4 data (organic sessions only)
3. Runs all 6 diagnostic checks on each page
4. Reports which pages are eligible for Governor evaluation

Usage:
    python scripts/run_diagnostics.py
    python scripts/run_diagnostics.py --top 100
    python scripts/run_diagnostics.py --output diagnostics.json
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.page_asset import PageAsset, GSCMetrics, GA4Metrics, TopQuery, AssetType
from src.diagnostics.tracking_sanity import (
    TrackingSanityDiagnostics,
    Severity,
)


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "defaults.json"
    with open(config_path) as f:
        return json.load(f)


def pull_gsc_data(config: dict, days: int = 28) -> dict[str, dict]:
    """Pull GSC data (same as run_evaluation.py)."""
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

    print(f"Pulling GSC data ({start_date} to {end_date})...")

    page_request = {
        'startDate': start_date.isoformat(),
        'endDate': end_date.isoformat(),
        'dimensions': ['page'],
        'rowLimit': 5000,
    }

    page_response = service.searchanalytics().query(
        siteUrl=property_url,
        body=page_request
    ).execute()

    pages_data = {}
    for row in page_response.get('rows', []):
        url = row['keys'][0]
        pages_data[url] = {
            'clicks': row.get('clicks', 0),
            'impressions': row.get('impressions', 0),
            'ctr': row.get('ctr', 0.0),
            'position': row.get('position', 0.0),
            'top_queries': [],
        }

    print(f"  Found {len(pages_data)} pages")

    # Pull query data for top pages
    print("Pulling query data for top pages...")
    top_pages = sorted(pages_data.keys(), key=lambda u: pages_data[u]['clicks'], reverse=True)[:100]

    for page_url in top_pages:
        query_request = {
            'startDate': start_date.isoformat(),
            'endDate': end_date.isoformat(),
            'dimensions': ['query'],
            'dimensionFilterGroups': [{
                'filters': [{
                    'dimension': 'page',
                    'operator': 'equals',
                    'expression': page_url
                }]
            }],
            'rowLimit': 20,
        }

        try:
            query_response = service.searchanalytics().query(
                siteUrl=property_url,
                body=query_request
            ).execute()

            for row in query_response.get('rows', []):
                pages_data[page_url]['top_queries'].append({
                    'query': row['keys'][0],
                    'clicks': row.get('clicks', 0),
                    'impressions': row.get('impressions', 0),
                    'ctr': row.get('ctr', 0.0),
                    'position': row.get('position', 0.0),
                })
        except Exception:
            pass

    return pages_data


def pull_ga4_organic_data(config: dict, days: int = 28) -> dict[str, dict]:
    """Pull GA4 organic search sessions only."""
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest,
        DateRange,
        Dimension,
        Metric,
        OrderBy,
        FilterExpression,
        Filter,
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

    print(f"Pulling GA4 organic search data (last {days} days)...")

    # Filter to Organic Search only
    dimension_filter = FilterExpression(
        filter=Filter(
            field_name="sessionDefaultChannelGroup",
            string_filter=Filter.StringFilter(
                value="Organic Search",
                match_type=Filter.StringFilter.MatchType.EXACT,
            ),
        ),
    )

    request = RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
        dimensions=[Dimension(name="landingPage")],
        metrics=[
            Metric(name="sessions"),
            Metric(name="engagedSessions"),
            Metric(name="ecommercePurchases"),
            Metric(name="totalRevenue"),
        ],
        dimension_filter=dimension_filter,
        order_bys=[OrderBy(
            metric=OrderBy.MetricOrderBy(metric_name="sessions"),
            desc=True,
        )],
        limit=5000,
    )

    response = client.run_report(request)

    ga4_data = {}
    for row in response.rows:
        page_path = row.dimension_values[0].value
        sessions = int(row.metric_values[0].value)
        engaged = int(row.metric_values[1].value)
        purchases = int(row.metric_values[2].value)
        revenue = float(row.metric_values[3].value)

        ga4_data[page_path] = {
            'organic_sessions': sessions,
            'engaged_sessions': engaged,
            'purchases': purchases,
            'revenue': revenue,
        }

    print(f"  Found {len(ga4_data)} landing pages with organic sessions")

    return ga4_data


def normalize_url(url: str, base_url: str = "https://alphabet-trains.com") -> str:
    """Normalize URL to path for matching."""
    if url.startswith(base_url):
        path = url[len(base_url):]
    else:
        path = url

    path = path.lower().split("?")[0].rstrip("/")
    if not path:
        path = "/"
    return path


def infer_asset_type(url: str) -> AssetType:
    """Infer asset type from URL pattern."""
    url_lower = url.lower()

    if '/product' in url_lower and '/products' not in url_lower:
        return AssetType.PRODUCT
    elif '/category' in url_lower or '/collection' in url_lower:
        return AssetType.CATEGORY
    elif '/blog' in url_lower or '/article' in url_lower or '/post' in url_lower:
        return AssetType.BLOG
    else:
        return AssetType.OTHER


def build_page_assets(
    gsc_data: dict,
    ga4_data: dict,
    base_url: str,
) -> tuple[list[PageAsset], dict[str, int]]:
    """
    Build PageAsset objects and organic sessions map.

    Returns:
        - List of PageAsset objects
        - Dict mapping normalized path -> organic sessions
    """
    assets = []
    organic_sessions_map = {}

    # Build organic sessions map from GA4
    for path, data in ga4_data.items():
        norm_path = path.lower().split("?")[0].rstrip("/")
        if not norm_path:
            norm_path = "/"
        organic_sessions_map[norm_path] = data['organic_sessions']

    # Build PageAssets from GSC data
    for url, gsc in gsc_data.items():
        path = normalize_url(url, base_url)
        ga4 = ga4_data.get(path, {})

        # Build top queries
        top_queries = [
            TopQuery(
                query=q['query'],
                clicks=q['clicks'],
                impressions=q['impressions'],
                ctr=q['ctr'],
                position=q['position'],
            )
            for q in gsc.get('top_queries', [])
        ]

        asset = PageAsset(
            url=url,
            asset_type=infer_asset_type(url),
            canonical_url=url,
            http_status=200,
            indexable=True,
            title="",
            h1="",
            word_count=0,
            inlinks=0,
            outlinks=0,
            link_authority_score=0.5,
            gsc=GSCMetrics(
                impressions_28d=gsc.get('impressions', 0),
                clicks_28d=gsc.get('clicks', 0),
                ctr_28d=gsc.get('ctr', 0.0),
                avg_position_28d=gsc.get('position', 0.0),
                query_dispersion=0.0,
                top_queries=top_queries,
            ),
            ga4=GA4Metrics(
                sessions_28d=ga4.get('organic_sessions', 0),
                engaged_sessions_28d=ga4.get('engaged_sessions', 0),
                engagement_rate_28d=0.0,
                purchases_28d=ga4.get('purchases', 0),
                revenue_28d=ga4.get('revenue', 0.0),
                purchase_rate_28d=0.0,
            ),
        )

        assets.append(asset)

    return assets, organic_sessions_map


def print_diagnostic_report(diagnostics, summary):
    """Print human-readable diagnostic report."""

    print("\n" + "=" * 80)
    print("TRACKING SANITY DIAGNOSTICS REPORT")
    print("=" * 80)

    # Summary
    print("\n## SUMMARY")
    print("-" * 40)
    print(f"Total pages analyzed: {summary['total_pages']}")
    print(f"  PASS:  {summary['passed']} ({summary['passed']/summary['total_pages']*100:.0f}%)")
    print(f"  WARN:  {summary['warned']} ({summary['warned']/summary['total_pages']*100:.0f}%)")
    print(f"  FAIL:  {summary['failed']} ({summary['failed']/summary['total_pages']*100:.0f}%)")
    print(f"\nEligible for RAIP evaluation: {summary['eligible_for_raip']} pages")

    # Failure breakdown
    if summary['failure_counts']:
        print("\n## FAILURES BY TYPE")
        print("-" * 40)
        for code, count in sorted(summary['failure_counts'].items(), key=lambda x: -x[1]):
            print(f"  {code}: {count}")

    # Failed pages (blocking)
    failed = [d for d in diagnostics if d.status == "FAIL"]
    if failed:
        print("\n## FAILED PAGES (BLOCKED FROM RAIP)")
        print("-" * 40)
        for diag in sorted(failed, key=lambda d: -len(d.failures))[:10]:
            print(f"\n✗ {diag.url}")
            for f in diag.failures:
                if f.severity == Severity.HIGH:
                    print(f"  [{f.code.value}] {f.interpretation[:80]}...")

    # Warned pages
    warned = [d for d in diagnostics if d.status == "WARN"]
    if warned:
        print("\n## WARNED PAGES (PROCEED WITH CAUTION)")
        print("-" * 40)
        for diag in sorted(warned, key=lambda d: -len(d.failures))[:10]:
            print(f"\n⚠ {diag.url}")
            if diag.special_classification:
                print(f"  [SPECIAL: {diag.special_classification}]")
            for f in diag.failures:
                if f.severity == Severity.MEDIUM:
                    print(f"  [{f.code.value}] {f.interpretation[:70]}...")

    # Eligible pages
    eligible = [d for d in diagnostics if d.eligible_for_raip]
    if eligible:
        print("\n## ELIGIBLE FOR RAIP EVALUATION")
        print("-" * 40)
        print(f"Total: {len(eligible)} pages")
        print("\nTop 10 by GSC clicks:")
        # Need to get clicks from somewhere - use the diagnostic URL to find asset
        for diag in eligible[:10]:
            print(f"  ✓ {diag.url}")


def main():
    parser = argparse.ArgumentParser(description="Run tracking sanity diagnostics")
    parser.add_argument('--top', type=int, default=None, help='Analyze top N pages by clicks')
    parser.add_argument('--output', type=str, help='Save results to JSON file')
    parser.add_argument('--days', type=int, default=28, help='Lookback period in days')
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════╗")
    print("║  TRACKING SANITY DIAGNOSTICS                             ║")
    print("║  \"Can I trust this page's data?\"                         ║")
    print("╚══════════════════════════════════════════════════════════╝")

    config = load_config()

    # Pull data
    try:
        gsc_data = pull_gsc_data(config, days=args.days)
        ga4_data = pull_ga4_organic_data(config, days=args.days)
    except Exception as e:
        print(f"\n✗ Failed to pull data: {e}")
        return 1

    # Build assets
    base_url = "https://alphabet-trains.com"
    assets, organic_sessions_map = build_page_assets(gsc_data, ga4_data, base_url)

    # Filter to top N if specified
    if args.top:
        assets = sorted(assets, key=lambda a: a.gsc.clicks_28d, reverse=True)[:args.top]

    print(f"\nAnalyzing {len(assets)} pages...")

    # Run diagnostics
    site_platform = config.get("data_sources", {}).get("site_platform", "magento")
    diagnostics_engine = TrackingSanityDiagnostics(base_url=base_url, site_platform=site_platform)
    diagnostics = diagnostics_engine.diagnose_all(assets, organic_sessions_map)
    summary = diagnostics_engine.summary(diagnostics)

    # Print report
    print_diagnostic_report(diagnostics, summary)

    # Save to file if requested
    if args.output:
        output_path = PROJECT_ROOT / args.output
        output_data = {
            "summary": summary,
            "diagnostics": [d.to_dict() for d in diagnostics],
        }
        with open(output_path, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"\n✓ Results saved to: {output_path}")

    print("\n" + "=" * 80)
    print("Diagnostics complete.")

    if summary['eligible_for_raip'] > 0:
        print(f"\nNext step: Run evaluation on {summary['eligible_for_raip']} eligible pages:")
        print("  python scripts/run_evaluation.py")
    else:
        print("\n⚠ No pages currently eligible for RAIP evaluation.")
        print("  Fix tracking issues before proceeding.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
