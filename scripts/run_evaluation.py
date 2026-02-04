#!/usr/bin/env python3
"""
Run Evaluation — Pull real data and evaluate pages with the Capital Governor.

This script:
1. Pulls page-level data from GSC (impressions, clicks, positions, queries)
2. Pulls landing page data from GA4 (sessions, revenue, conversions)
3. Merges into PageAsset objects
4. Runs the Governor evaluation on each asset
5. Outputs decisions

Usage:
    python scripts/run_evaluation.py
    python scripts/run_evaluation.py --top 50  # Evaluate top 50 pages by sessions
    python scripts/run_evaluation.py --url "https://alphabet-trains.com/products/..."
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.page_asset import PageAsset, GSCMetrics, GA4Metrics, TopQuery, AssetType
from src.models.cost_model import ActionCostModel
from src.models.profit_model import ProfitModel
from src.models.governance_state import GovernanceState
from src.models.decision_envelope import DecisionType
from src.governor.capital_governor import CapitalGovernor, GovernorConfig


def load_config() -> dict:
    """Load configuration from defaults.json."""
    config_path = PROJECT_ROOT / "config" / "defaults.json"
    with open(config_path) as f:
        return json.load(f)


def load_governance_state() -> GovernanceState:
    """Load or create governance state."""
    state_path = PROJECT_ROOT / "data" / "governance_state.json"

    if state_path.exists():
        with open(state_path) as f:
            data = json.load(f)
            return GovernanceState.model_validate(data)

    # Create default state
    return GovernanceState(
        regret_budget_year=2,
        regret_budget_used=0,
        observe_only_mode=False,
        past_actions=[],
    )


def save_governance_state(state: GovernanceState) -> None:
    """Save governance state to disk."""
    state_path = PROJECT_ROOT / "data" / "governance_state.json"
    state_path.parent.mkdir(exist_ok=True)

    with open(state_path, 'w') as f:
        json.dump(state.model_dump(), f, indent=2, default=str)


def pull_gsc_data(config: dict, days: int = 28) -> dict[str, dict]:
    """
    Pull page-level data from Google Search Console.

    Returns dict mapping URL -> GSC metrics.
    """
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

    end_date = date.today() - timedelta(days=3)  # GSC has ~3 day lag
    start_date = end_date - timedelta(days=days)

    # Pull page-level metrics
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

    print(f"  Found {len(pages_data)} pages with GSC data")

    # Pull query-level data for top pages
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
            pass  # Skip query data for this page

    return pages_data


def pull_ga4_data(config: dict, days: int = 28, organic_only: bool = True) -> dict[str, dict]:
    """
    Pull landing page data from Google Analytics 4.

    Args:
        config: Configuration dict
        days: Lookback period in days
        organic_only: If True, filter to Organic Search channel only (required for GSC comparison)

    Returns dict mapping page path -> GA4 metrics.
    """
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

    channel_label = "ORGANIC ONLY" if organic_only else "ALL CHANNELS"
    print(f"Pulling GA4 data (last {days} days, {channel_label})...")

    # Build dimension filter for organic search only
    dimension_filter = None
    if organic_only:
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
            Metric(name="engagementRate"),
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
        engagement_rate = float(row.metric_values[2].value)
        purchases = int(row.metric_values[3].value)
        revenue = float(row.metric_values[4].value)

        ga4_data[page_path] = {
            'sessions': sessions,
            'organic_sessions': sessions,  # Explicitly mark as organic
            'engaged_sessions': engaged,
            'engagement_rate': engagement_rate,
            'purchases': purchases,
            'revenue': revenue,
            'purchase_rate': purchases / sessions if sessions > 0 else 0.0,
        }

    print(f"  Found {len(ga4_data)} landing pages with GA4 organic data")

    return ga4_data


def infer_asset_type(url: str) -> AssetType:
    """Infer asset type from URL pattern."""
    url_lower = url.lower()

    if '/product' in url_lower:
        return AssetType.PRODUCT
    elif '/category' in url_lower or '/collection' in url_lower:
        return AssetType.CATEGORY
    elif '/blog' in url_lower or '/article' in url_lower or '/post' in url_lower:
        return AssetType.BLOG
    else:
        return AssetType.OTHER


def merge_data(gsc_data: dict, ga4_data: dict, base_url: str) -> list[PageAsset]:
    """
    Merge GSC and GA4 data into PageAsset objects.

    GSC uses full URLs, GA4 uses paths. We need to match them.
    """
    print("Merging GSC and GA4 data...")

    assets = []
    matched = 0

    for url, gsc in gsc_data.items():
        # Extract path from URL for GA4 matching
        if base_url in url:
            path = url.replace(base_url, '')
            if not path.startswith('/'):
                path = '/' + path
        else:
            path = url

        # Try to find matching GA4 data
        ga4 = ga4_data.get(path, {})
        if ga4:
            matched += 1

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

        # Calculate query dispersion (entropy-based)
        total_clicks = sum(q.clicks for q in top_queries) if top_queries else 0
        if total_clicks > 0 and len(top_queries) > 1:
            # Simple dispersion: 1 - (top query share)
            top_share = top_queries[0].clicks / total_clicks if top_queries else 1.0
            dispersion = 1 - top_share
        else:
            dispersion = 0.0

        asset = PageAsset(
            url=url,
            asset_type=infer_asset_type(url),
            canonical_url=url,
            http_status=200,
            indexable=True,
            title="",  # Would need crawl data
            h1="",
            word_count=0,
            inlinks=0,  # Would need crawl data
            outlinks=0,
            link_authority_score=0.5,  # Default; would need link graph
            gsc=GSCMetrics(
                impressions_28d=gsc.get('impressions', 0),
                clicks_28d=gsc.get('clicks', 0),
                ctr_28d=gsc.get('ctr', 0.0),
                avg_position_28d=gsc.get('position', 0.0),
                query_dispersion=dispersion,
                top_queries=top_queries,
            ),
            ga4=GA4Metrics(
                sessions_28d=ga4.get('sessions', 0),
                engaged_sessions_28d=ga4.get('engaged_sessions', 0),
                engagement_rate_28d=ga4.get('engagement_rate', 0.0),
                purchases_28d=ga4.get('purchases', 0),
                revenue_28d=ga4.get('revenue', 0.0),
                purchase_rate_28d=ga4.get('purchase_rate', 0.0),
            ),
        )

        assets.append(asset)

    print(f"  Built {len(assets)} PageAssets ({matched} with GA4 match)")

    return assets


def run_evaluation(
    assets: list[PageAsset],
    config: dict,
    governance: GovernanceState,
    top_n: Optional[int] = None,
    target_url: Optional[str] = None,
) -> list[tuple[PageAsset, Any]]:
    """
    Run Governor evaluation on assets.

    Returns list of (asset, decision) tuples.
    """
    # Initialize models
    cost_model = ActionCostModel(
        cost_units=config["action_cost_model"]["cost_units"],
        page_reinvestment=config["action_cost_model"]["defaults"]["page_reinvestment"],
        internal_link_change=config["action_cost_model"]["defaults"]["internal_link_change"],
        new_page_creation=config["action_cost_model"]["defaults"]["new_page_creation"],
    )

    profit_model = ProfitModel(
        aov=config["profit_model"]["aov"],
        gross_margin_low=config["profit_model"]["gross_margin_low"],
        gross_margin_high=config["profit_model"]["gross_margin_high"],
        evaluation_window_days=config["profit_model"]["evaluation_window_days"],
    )

    governor_config = GovernorConfig(
        min_confidence_threshold=config["governance"]["min_confidence_threshold"],
        profit_to_cost_ratio_gate=config["governance"]["profit_to_cost_ratio_gate"],
        defensive_action_weight=config["governance"]["defensive_action_weight"],
        exploratory_action_penalty=config["governance"]["exploratory_action_penalty"],
    )

    governor = CapitalGovernor(
        cost_model=cost_model,
        profit_model=profit_model,
        governance_state=governance,
        config=governor_config,
    )

    # Filter assets
    if target_url:
        assets = [a for a in assets if target_url in a.url]

    # Sort by revenue (highest first)
    assets = sorted(assets, key=lambda a: a.ga4.revenue_28d, reverse=True)

    if top_n:
        assets = assets[:top_n]

    print(f"\nEvaluating {len(assets)} pages...")
    print("-" * 60)

    results = []

    for asset in assets:
        # Evaluate page reinvestment potential
        decision = governor.evaluate_page_reinvestment(
            asset=asset,
            expected_traffic_lift=0.15,  # Conservative 15% lift assumption
            base_confidence=0.70,
            is_exploratory=False,
        )

        results.append((asset, decision))

    return results


def print_results(results: list[tuple[PageAsset, Any]]) -> None:
    """Print evaluation results."""
    print("\n" + "=" * 60)
    print("EVALUATION RESULTS")
    print("=" * 60)

    # Categorize by decision type
    by_type = {}
    for asset, decision in results:
        dtype = decision.decision.value
        if dtype not in by_type:
            by_type[dtype] = []
        by_type[dtype].append((asset, decision))

    # Summary
    print(f"\nTotal evaluated: {len(results)}")
    for dtype, items in sorted(by_type.items()):
        pct = len(items) / len(results) * 100
        print(f"  {dtype}: {len(items)} ({pct:.0f}%)")

    # Doctrine compliance check
    no_action_count = len(by_type.get('NO_ACTION', [])) + len(by_type.get('OBSERVE_ONLY', []))
    no_action_pct = no_action_count / len(results) * 100 if results else 0

    print(f"\nDoctrine compliance: ", end="")
    if no_action_pct >= 60:
        print(f"PASS ({no_action_pct:.0f}% NO_ACTION)")
    else:
        print(f"REVIEW ({no_action_pct:.0f}% NO_ACTION, target ≥60%)")

    # Show action decisions (if any)
    action_decisions = [
        (a, d) for a, d in results
        if d.decision not in (DecisionType.NO_ACTION, DecisionType.OBSERVE_ONLY)
    ]

    if action_decisions:
        print("\n" + "-" * 60)
        print("PROPOSED ACTIONS")
        print("-" * 60)

        for asset, decision in action_decisions:
            print(f"\n{decision.decision.value}: {asset.url}")
            print(f"  Revenue (28d): ${asset.ga4.revenue_28d:.2f}")
            print(f"  RAIP: ${decision.raip_estimate.raip:.2f}")
            print(f"  Confidence: {decision.confidence:.2f}")
            print(f"  Risk: {decision.risk.reversibility.value}")

    # Show top NO_ACTION reasons
    no_action = by_type.get('NO_ACTION', [])
    if no_action:
        print("\n" + "-" * 60)
        print("SAMPLE NO_ACTION REASONS (top 5 by revenue)")
        print("-" * 60)

        no_action_sorted = sorted(no_action, key=lambda x: x[0].ga4.revenue_28d, reverse=True)
        for asset, decision in no_action_sorted[:5]:
            print(f"\n{asset.url[:70]}...")
            print(f"  Revenue: ${asset.ga4.revenue_28d:.2f}")
            print(f"  Reason: {decision.why_no_action_may_be_better[:100]}...")


def normalize_path(url: str, base_url: str = "https://alphabet-trains.com") -> str:
    """Normalize URL to path for organic sessions matching."""
    if url.startswith(base_url):
        path = url[len(base_url):]
    else:
        path = url
    path = path.lower().split("?")[0].rstrip("/")
    if not path:
        path = "/"
    return path


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Run Capital Governor evaluation")
    parser.add_argument('--top', type=int, default=50, help='Evaluate top N pages by revenue')
    parser.add_argument('--url', type=str, help='Evaluate specific URL only')
    parser.add_argument('--days', type=int, default=28, help='Lookback period in days')
    parser.add_argument('--skip-diagnostics', action='store_true', help='Skip tracking sanity diagnostics')
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════╗")
    print("║  CAPITAL GOVERNOR — LIVE EVALUATION                      ║")
    print("╚══════════════════════════════════════════════════════════╝")

    # Load config
    config = load_config()
    governance = load_governance_state()

    # Check observe-only mode
    if governance.observe_only_mode:
        print("\n⚠ WARNING: System is in OBSERVE-ONLY mode")
        print(f"  Regret budget: {governance.regret_budget_used}/{governance.regret_budget_year}")
        print("  No actions will be proposed.")

    # Pull data (organic only for proper comparison)
    try:
        gsc_data = pull_gsc_data(config, days=args.days)
        ga4_data = pull_ga4_data(config, days=args.days, organic_only=True)
    except Exception as e:
        print(f"\n✗ Failed to pull data: {e}")
        print("  Run 'python scripts/test_connection.py' to diagnose.")
        return 1

    # Merge into PageAssets
    base_url = "https://alphabet-trains.com"
    assets = merge_data(gsc_data, ga4_data, base_url)

    if not assets:
        print("\n✗ No pages to evaluate. Check data sources.")
        return 1

    # === TRACKING SANITY DIAGNOSTICS (Pre-filter Gate) ===
    if not args.skip_diagnostics:
        print("\n" + "-" * 60)
        print("PHASE 1: TRACKING SANITY DIAGNOSTICS")
        print("-" * 60)

        from src.diagnostics.tracking_sanity import TrackingSanityDiagnostics

        # Build organic sessions map for diagnostics
        organic_sessions_map = {}
        for path, data in ga4_data.items():
            norm_path = path.lower().split("?")[0].rstrip("/")
            if not norm_path:
                norm_path = "/"
            organic_sessions_map[norm_path] = data.get('organic_sessions', data.get('sessions', 0))

        diagnostics_engine = TrackingSanityDiagnostics(base_url=base_url)
        diagnostics = diagnostics_engine.diagnose_all(assets, organic_sessions_map)
        summary = diagnostics_engine.summary(diagnostics)

        print(f"\nDiagnostics complete:")
        print(f"  Total pages: {summary['total_pages']}")
        print(f"  PASS: {summary['passed']}")
        print(f"  WARN: {summary['warned']}")
        print(f"  FAIL: {summary['failed']}")
        print(f"  Eligible for RAIP: {summary['eligible_for_raip']}")

        # Filter to eligible pages only
        eligible_urls = {d.url for d in diagnostics if d.eligible_for_raip}
        original_count = len(assets)
        assets = [a for a in assets if a.url in eligible_urls]

        print(f"\nFiltered: {original_count} → {len(assets)} pages eligible for evaluation")

        if not assets:
            print("\n⚠ No pages passed tracking sanity diagnostics.")
            print("  Run 'python scripts/run_diagnostics.py' for detailed report.")
            print("  Fix tracking issues before Governor can propose actions.")
            return 0

    print("\n" + "-" * 60)
    print("PHASE 2: GOVERNOR EVALUATION")
    print("-" * 60)

    # Run evaluation on eligible pages only
    results = run_evaluation(
        assets=assets,
        config=config,
        governance=governance,
        top_n=args.top,
        target_url=args.url,
    )

    # Print results
    print_results(results)

    # Save governance state
    save_governance_state(governance)

    print("\n" + "=" * 60)
    print("Evaluation complete.")
    print("Governance state saved to: data/governance_state.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
