#!/usr/bin/env python3
"""
Run Agentic Governor Evaluation — Full three-mode evaluation.

This script implements the revised doctrine with:
- MODE 1: PRESERVATION (RAIP for revenue pages)
- MODE 2: OPPORTUNITY DISCOVERY (EVUV for suppressed demand)
- MODE 3: FUNNEL ALIGNMENT (AV for blogs/guides)

Usage:
    python scripts/run_agentic_evaluation.py
    python scripts/run_agentic_evaluation.py --top 50
    python scripts/run_agentic_evaluation.py --mode preservation
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.page_asset import PageAsset, GSCMetrics, GA4Metrics, TopQuery, AssetType
from src.models.cost_model import ActionCostModel
from src.models.profit_model import ProfitModel
from src.models.governance_state import GovernanceState
from src.models.decision_envelope import DecisionType
from src.governor.agentic_governor import AgenticGovernor, GovernorConfig, EvaluationResult
from src.ledger.action_ledger import ActionLedger
from src.diagnostics.tracking_sanity import TrackingSanityDiagnostics


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "defaults.json"
    with open(config_path) as f:
        return json.load(f)


def load_governance_state() -> GovernanceState:
    state_path = PROJECT_ROOT / "data" / "governance_state.json"
    if state_path.exists():
        with open(state_path) as f:
            data = json.load(f)
            return GovernanceState.model_validate(data)
    return GovernanceState()


def save_governance_state(state: GovernanceState) -> None:
    state_path = PROJECT_ROOT / "data" / "governance_state.json"
    state_path.parent.mkdir(exist_ok=True)
    with open(state_path, 'w') as f:
        json.dump(state.model_dump(), f, indent=2, default=str)


def pull_gsc_data(config: dict, days: int = 28) -> dict[str, dict]:
    """Pull GSC page and query data."""
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
    print("Pulling query data for top 100 pages...")
    top_pages = sorted(pages_data.keys(), key=lambda u: pages_data[u]['impressions'], reverse=True)[:100]

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
    """Pull GA4 organic-only data."""
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

    print(f"Pulling GA4 organic data (last {days} days)...")

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
            'organic_sessions': sessions,
            'engaged_sessions': engaged,
            'engagement_rate': engagement_rate,
            'purchases': purchases,
            'revenue': revenue,
            'purchase_rate': purchases / sessions if sessions > 0 else 0.0,
        }

    print(f"  Found {len(ga4_data)} landing pages")

    return ga4_data


def infer_asset_type(url: str) -> AssetType:
    """Infer asset type from URL."""
    url_lower = url.lower()
    if '/blog' in url_lower or '/post' in url_lower:
        return AssetType.BLOG
    elif '/category' in url_lower or '/collection' in url_lower:
        return AssetType.CATEGORY
    elif '-train' in url_lower or 'product' in url_lower:
        return AssetType.PRODUCT
    return AssetType.OTHER


def build_page_assets(gsc_data: dict, ga4_data: dict, base_url: str) -> list[PageAsset]:
    """Build PageAsset objects from GSC and GA4 data."""
    assets = []

    for url, gsc in gsc_data.items():
        # Normalize path for GA4 matching
        if url.startswith(base_url):
            path = url[len(base_url):]
        else:
            path = url
        path = path.lower().split("?")[0].rstrip("/")
        if not path:
            path = "/"

        ga4 = ga4_data.get(path, {})

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
                sessions_28d=ga4.get('sessions', 0),
                engaged_sessions_28d=ga4.get('engaged_sessions', 0),
                engagement_rate_28d=ga4.get('engagement_rate', 0.0),
                purchases_28d=ga4.get('purchases', 0),
                revenue_28d=ga4.get('revenue', 0.0),
                purchase_rate_28d=ga4.get('purchase_rate', 0.0),
            ),
        )

        assets.append(asset)

    return assets


def print_results(results: list[EvaluationResult]) -> None:
    """Print evaluation results in structured format."""

    print("\n" + "=" * 80)
    print("AGENTIC GOVERNOR EVALUATION RESULTS")
    print("=" * 80)

    # 1. Executive Decision Summary
    print("\n## EXECUTIVE SUMMARY")
    print("-" * 40)

    by_decision = {}
    by_mode = {}
    for r in results:
        d = r.decision.value
        m = r.mode
        by_decision[d] = by_decision.get(d, 0) + 1
        by_mode[m] = by_mode.get(m, 0) + 1

    print(f"Total evaluated: {len(results)}")
    print("\nBy Decision:")
    for decision, count in sorted(by_decision.items(), key=lambda x: -x[1]):
        pct = count / len(results) * 100
        print(f"  {decision}: {count} ({pct:.0f}%)")

    print("\nBy Mode:")
    for mode, count in sorted(by_mode.items(), key=lambda x: -x[1]):
        pct = count / len(results) * 100
        print(f"  {mode}: {count} ({pct:.0f}%)")

    # Doctrine compliance
    no_action_count = by_decision.get("NO_ACTION", 0) + by_decision.get("OBSERVE_ONLY", 0)
    no_action_pct = no_action_count / len(results) * 100 if results else 0
    print(f"\nDoctrine compliance: ", end="")
    if no_action_pct >= 60:
        print(f"PASS ({no_action_pct:.0f}% NO_ACTION)")
    else:
        print(f"REVIEW ({no_action_pct:.0f}% NO_ACTION, target ≥60%)")

    # 2. Ranked Opportunity Queue
    action_results = [r for r in results if r.decision not in (DecisionType.NO_ACTION, DecisionType.OBSERVE_ONLY)]

    if action_results:
        print("\n## RANKED OPPORTUNITY QUEUE")
        print("-" * 40)
        print(f"{'Rank':<5} {'URL':<50} {'Mode':<12} {'Score':<10} {'Conf':<6} {'Priority':<10}")
        print("-" * 95)

        for i, r in enumerate(action_results[:20], 1):
            url_short = r.url[-48:] if len(r.url) > 48 else r.url
            print(f"{i:<5} {url_short:<50} {r.mode:<12} ${r.score_value:<9.2f} {r.confidence:<6.2f} {r.priority.priority:<10.2f}")

    # 3. Top NO_ACTION by potential
    no_action_results = [r for r in results if r.decision == DecisionType.NO_ACTION]
    no_action_sorted = sorted(no_action_results, key=lambda r: r.score_value, reverse=True)

    print("\n## TOP NO_ACTION PAGES (by potential)")
    print("-" * 40)
    for r in no_action_sorted[:10]:
        url_short = r.url.split("/")[-1][:40] if "/" in r.url else r.url[:40]
        print(f"\n{r.url}")
        print(f"  Mode: {r.mode} | {r.score_type}: ${r.score_value:.2f} | Confidence: {r.confidence:.2f}")
        print(f"  Reason: {r.why_no_action[:80]}...")

    # 4. Detailed Recommendations (for actions)
    if action_results:
        print("\n## DETAILED RECOMMENDATIONS")
        print("-" * 40)

        for r in action_results[:5]:
            print(f"\n{'='*60}")
            print(f"URL: {r.url}")
            print(f"Decision: {r.decision.value}")
            print(f"Mode: {r.mode}")
            print(f"Score ({r.score_type}): ${r.score_value:.2f}")
            print(f"Confidence: {r.confidence:.2f}")
            print(f"Priority: {r.priority.priority:.2f}")

            print("\nEvidence:")
            for e in r.evidence[:5]:
                print(f"  [{e.source}] {e.fact}: {e.value}")

            if r.action_plan:
                print("\nAction Plan:")
                for i, step in enumerate(r.action_plan.steps, 1):
                    print(f"  {i}. {step}")

                if r.action_plan.rollback:
                    print("\nRollback:")
                    for step in r.action_plan.rollback:
                        print(f"  - {step}")

            if r.learning_insight:
                print(f"\nLearning: {r.learning_insight.recommendation}")
                if r.learning_insight.action_ids:
                    print(f"  Prior actions: {', '.join(r.learning_insight.action_ids[:3])}")

            print(f"\nWhy NO_ACTION may be better: {r.why_no_action}")


def main():
    parser = argparse.ArgumentParser(description="Run Agentic Governor evaluation")
    parser.add_argument('--top', type=int, default=100, help='Evaluate top N pages by impressions')
    parser.add_argument('--days', type=int, default=28, help='Lookback period in days')
    parser.add_argument('--mode', type=str, choices=['preservation', 'opportunity', 'funnel', 'all'],
                        default='all', help='Evaluation mode')
    parser.add_argument('--skip-diagnostics', action='store_true', help='Skip tracking sanity diagnostics')
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════╗")
    print("║  AGENTIC ORGANIC GROWTH GOVERNOR                         ║")
    print("║  Three-Mode Evaluation                                   ║")
    print("╚══════════════════════════════════════════════════════════╝")

    config = load_config()
    governance = load_governance_state()

    if governance.observe_only_mode:
        print("\n⚠ WARNING: System in OBSERVE-ONLY mode")

    # Pull data
    try:
        gsc_data = pull_gsc_data(config, days=args.days)
        ga4_data = pull_ga4_organic_data(config, days=args.days)
    except Exception as e:
        print(f"\n✗ Failed to pull data: {e}")
        return 1

    # Build assets
    base_url = "https://alphabet-trains.com"
    assets = build_page_assets(gsc_data, ga4_data, base_url)

    if not assets:
        print("\n✗ No pages to evaluate.")
        return 1

    # Run diagnostics
    if not args.skip_diagnostics:
        print("\n" + "-" * 60)
        print("PHASE 1: TRACKING SANITY DIAGNOSTICS")
        print("-" * 60)

        organic_sessions_map = {
            p.lower().split("?")[0].rstrip("/") or "/": d.get('organic_sessions', 0)
            for p, d in ga4_data.items()
        }

        diagnostics_engine = TrackingSanityDiagnostics(base_url=base_url)
        diagnostics = diagnostics_engine.diagnose_all(assets, organic_sessions_map)
        summary = diagnostics_engine.summary(diagnostics)

        print(f"  PASS: {summary['passed']} | WARN: {summary['warned']} | FAIL: {summary['failed']}")
        print(f"  Eligible: {summary['eligible_for_raip']} pages")

        eligible_urls = {d.url for d in diagnostics if d.eligible_for_raip}
        assets = [a for a in assets if a.url in eligible_urls]

    # Sort by impressions and limit
    assets = sorted(assets, key=lambda a: a.gsc.impressions_28d, reverse=True)[:args.top]

    print(f"\n" + "-" * 60)
    print(f"PHASE 2: AGENTIC EVALUATION ({len(assets)} pages)")
    print("-" * 60)

    # Initialize Governor
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
        aov=config["profit_model"]["aov"],
        gross_margin=config["profit_model"]["gross_margin_low"],
    )

    ledger = ActionLedger()

    governor = AgenticGovernor(
        cost_model=cost_model,
        profit_model=profit_model,
        governance_state=governance,
        config=governor_config,
        ledger=ledger,
    )

    # Run evaluation
    results = governor.evaluate_all(assets)

    # Print results
    print_results(results)

    # Save state
    save_governance_state(governance)

    # Save action records for non-NO_ACTION
    action_count = 0
    for result in results:
        record = governor.create_action_record(result)
        if record:
            ledger.add_action(record)
            action_count += 1

    if action_count > 0:
        print(f"\n✓ {action_count} action records saved to ledger")

    print("\n" + "=" * 80)
    print("Evaluation complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
