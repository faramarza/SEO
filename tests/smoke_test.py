"""
Smoke Test — Data Contract Validator and Governor Decision Engine.

Per doctrine:
- Confirms GSC/GA4 credentials work (or skips if not configured)
- Verifies page inventory exists
- Computes a single PageAsset for sample URLs
- Demonstrates Governor decision-making

This is fully reversible and doctrine-compliant.
"""

import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.page_asset import PageAsset, GSCMetrics, GA4Metrics, TopQuery, AssetType
from src.models.cost_model import ActionCostModel
from src.models.profit_model import ProfitModel
from src.models.governance_state import GovernanceState
from src.validators.data_contract_validator import DataContractValidator
from src.governor.capital_governor import CapitalGovernor, GovernorConfig


def create_sample_assets() -> list[dict]:
    """
    Create sample PageAsset data for smoke testing.

    In production, this would come from crawl + GSC + GA4.
    """
    return [
        {
            "url": "https://alphabet-trains.com/products/wooden-letter-train-set",
            "asset_type": "product",
            "canonical_url": "https://alphabet-trains.com/products/wooden-letter-train-set",
            "http_status": 200,
            "indexable": True,
            "title": "Wooden Alphabet Train Set | Educational Toy",
            "h1": "Wooden Alphabet Train Set",
            "word_count": 850,
            "inlinks": 45,
            "outlinks": 12,
            "link_authority_score": 0.72,
            "gsc": {
                "impressions_28d": 12500,
                "clicks_28d": 625,
                "ctr_28d": 0.05,
                "avg_position_28d": 8.3,
                "query_dispersion": 0.65,
                "top_queries": [
                    {"query": "wooden alphabet train", "clicks": 180, "impressions": 3200, "ctr": 0.056, "position": 5.2},
                    {"query": "letter train set", "clicks": 95, "impressions": 2100, "ctr": 0.045, "position": 7.8},
                    {"query": "alphabet train toy", "clicks": 78, "impressions": 1800, "ctr": 0.043, "position": 9.1},
                ]
            },
            "ga4": {
                "sessions_28d": 580,
                "engaged_sessions_28d": 412,
                "engagement_rate_28d": 0.71,
                "purchases_28d": 23,
                "revenue_28d": 1223.37,
                "purchase_rate_28d": 0.0397
            }
        },
        {
            "url": "https://alphabet-trains.com/products/number-train-1-10",
            "asset_type": "product",
            "canonical_url": "https://alphabet-trains.com/products/number-train-1-10",
            "http_status": 200,
            "indexable": True,
            "title": "Number Train 1-10 | Learning Numbers Toy",
            "h1": "Number Train 1-10",
            "word_count": 720,
            "inlinks": 22,
            "outlinks": 8,
            "link_authority_score": 0.45,
            "gsc": {
                "impressions_28d": 4200,
                "clicks_28d": 168,
                "ctr_28d": 0.04,
                "avg_position_28d": 12.5,
                "query_dispersion": 0.42,
                "top_queries": [
                    {"query": "number train toy", "clicks": 52, "impressions": 1100, "ctr": 0.047, "position": 10.2},
                    {"query": "counting train", "clicks": 38, "impressions": 980, "ctr": 0.039, "position": 14.3},
                ]
            },
            "ga4": {
                "sessions_28d": 155,
                "engaged_sessions_28d": 98,
                "engagement_rate_28d": 0.63,
                "purchases_28d": 5,
                "revenue_28d": 265.95,
                "purchase_rate_28d": 0.0323
            }
        },
        {
            "url": "https://alphabet-trains.com/blog/best-educational-toys-2026",
            "asset_type": "blog",
            "canonical_url": "https://alphabet-trains.com/blog/best-educational-toys-2026",
            "http_status": 200,
            "indexable": True,
            "title": "Best Educational Toys for 2026 | Parent's Guide",
            "h1": "Best Educational Toys for 2026",
            "word_count": 2450,
            "inlinks": 8,
            "outlinks": 35,
            "link_authority_score": 0.85,
            "gsc": {
                "impressions_28d": 28000,
                "clicks_28d": 1680,
                "ctr_28d": 0.06,
                "avg_position_28d": 6.2,
                "query_dispersion": 0.78,
                "top_queries": [
                    {"query": "best educational toys 2026", "clicks": 420, "impressions": 5500, "ctr": 0.076, "position": 4.1},
                    {"query": "educational toys for toddlers", "clicks": 280, "impressions": 4200, "ctr": 0.067, "position": 5.8},
                ]
            },
            "ga4": {
                "sessions_28d": 1520,
                "engaged_sessions_28d": 1064,
                "engagement_rate_28d": 0.70,
                "purchases_28d": 12,
                "revenue_28d": 638.28,
                "purchase_rate_28d": 0.0079
            }
        },
        {
            "url": "https://alphabet-trains.com/category/wooden-trains",
            "asset_type": "category",
            "canonical_url": "https://alphabet-trains.com/category/wooden-trains",
            "http_status": 200,
            "indexable": True,
            "title": "Wooden Trains | Shop Educational Train Sets",
            "h1": "Wooden Train Collection",
            "word_count": 320,
            "inlinks": 65,
            "outlinks": 28,
            "link_authority_score": 0.68,
            "gsc": {
                "impressions_28d": 8500,
                "clicks_28d": 340,
                "ctr_28d": 0.04,
                "avg_position_28d": 11.2,
                "query_dispersion": 0.55,
                "top_queries": [
                    {"query": "wooden trains", "clicks": 85, "impressions": 2100, "ctr": 0.040, "position": 9.5},
                    {"query": "educational train sets", "clicks": 62, "impressions": 1450, "ctr": 0.043, "position": 12.1},
                ]
            },
            "ga4": {
                "sessions_28d": 310,
                "engaged_sessions_28d": 186,
                "engagement_rate_28d": 0.60,
                "purchases_28d": 8,
                "revenue_28d": 425.52,
                "purchase_rate_28d": 0.0258
            }
        },
        {
            "url": "https://alphabet-trains.com/products/discontinued-model-x",
            "asset_type": "product",
            "canonical_url": "https://alphabet-trains.com/products/discontinued-model-x",
            "http_status": 200,
            "indexable": True,
            "title": "Model X Train (Discontinued)",
            "h1": "Model X Train",
            "word_count": 450,
            "inlinks": 3,
            "outlinks": 5,
            "link_authority_score": 0.12,
            "gsc": {
                "impressions_28d": 120,
                "clicks_28d": 4,
                "ctr_28d": 0.033,
                "avg_position_28d": 45.2,
                "query_dispersion": 0.15,
                "top_queries": [
                    {"query": "model x train discontinued", "clicks": 2, "impressions": 45, "ctr": 0.044, "position": 38.0},
                ]
            },
            "ga4": {
                "sessions_28d": 3,
                "engaged_sessions_28d": 1,
                "engagement_rate_28d": 0.33,
                "purchases_28d": 0,
                "revenue_28d": 0.0,
                "purchase_rate_28d": 0.0
            }
        },
    ]


def run_validation_smoke_test():
    """Run data contract validation on sample assets."""
    print("=" * 60)
    print("DATA CONTRACT VALIDATION SMOKE TEST")
    print("=" * 60)
    print()

    sample_inventory = create_sample_assets()

    validator = DataContractValidator(
        gsc_client=None,  # No credentials in smoke test
        ga4_client=None,
    )

    result = validator.run_full_validation(sample_inventory)

    print(result.to_markdown())
    print()
    print(f"Can proceed with Governor evaluation: {result.can_proceed}")
    print()

    return result


def run_governor_smoke_test():
    """Run Governor decision evaluation on sample assets."""
    print("=" * 60)
    print("CAPITAL GOVERNOR DECISION SMOKE TEST")
    print("=" * 60)
    print()

    # Load configuration
    config_path = Path(__file__).parent.parent / "config" / "defaults.json"
    with open(config_path) as f:
        config = json.load(f)

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

    governance_state = GovernanceState(
        regret_budget_year=config["governance"]["regret_budget_year"],
        regret_budget_used=0,
        observe_only_mode=False,
        past_actions=[],
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
        governance_state=governance_state,
        config=governor_config,
    )

    # Parse sample assets
    sample_data = create_sample_assets()
    assets = [PageAsset.model_validate(a) for a in sample_data]

    # Test 1: Default decision
    print("-" * 40)
    print("TEST 1: Default Decision")
    print("-" * 40)
    default = governor.default_decision()
    print(default.to_markdown())
    print()

    # Test 2: Evaluate page reinvestment on high-performing product
    print("-" * 40)
    print("TEST 2: Page Reinvestment Evaluation")
    print("Target: Wooden Letter Train Set (highest revenue)")
    print("-" * 40)
    product_asset = assets[0]  # Wooden alphabet train
    reinvest_decision = governor.evaluate_page_reinvestment(
        asset=product_asset,
        expected_traffic_lift=0.15,  # 15% expected lift
        base_confidence=0.70,
        is_exploratory=False,
    )
    print(reinvest_decision.to_markdown())
    print()

    # Test 3: Evaluate reinvestment on low-performing product (should be NO_ACTION)
    print("-" * 40)
    print("TEST 3: Page Reinvestment on Low Performer")
    print("Target: Discontinued Model X (no revenue)")
    print("-" * 40)
    low_asset = assets[4]  # Discontinued model
    low_decision = governor.evaluate_page_reinvestment(
        asset=low_asset,
        expected_traffic_lift=0.20,
        base_confidence=0.75,
        is_exploratory=False,
    )
    print(low_decision.to_markdown())
    print()

    # Test 4: Internal link reallocation
    print("-" * 40)
    print("TEST 4: Internal Link Reallocation")
    print("Source: Blog (high authority)")
    print("Target: Number Train (lower authority, has revenue)")
    print("-" * 40)
    blog_asset = assets[2]  # Blog (high authority)
    number_train = assets[1]  # Number train (lower authority)
    link_decision = governor.evaluate_internal_link_reallocation(
        source_asset=blog_asset,
        target_asset=number_train,
        base_confidence=0.72,
    )
    print(link_decision.to_markdown())
    print()

    # Test 5: New page creation (should be heavily scrutinized)
    print("-" * 40)
    print("TEST 5: New Page Creation (Exploratory)")
    print("Proposed: /products/personalized-name-train")
    print("-" * 40)
    new_page_decision = governor.evaluate_new_page_creation(
        proposed_url="https://alphabet-trains.com/products/personalized-name-train",
        existing_assets=assets,
        expected_monthly_orders=15,
        base_confidence=0.68,
    )
    print(new_page_decision.to_markdown())
    print()

    # Summary
    print("=" * 60)
    print("SMOKE TEST SUMMARY")
    print("=" * 60)
    decisions = [default, reinvest_decision, low_decision, link_decision, new_page_decision]
    no_action_count = sum(1 for d in decisions if d.decision.value in ("NO_ACTION", "OBSERVE_ONLY"))
    action_count = len(decisions) - no_action_count

    print(f"Total evaluations: {len(decisions)}")
    print(f"NO_ACTION decisions: {no_action_count} ({no_action_count/len(decisions)*100:.0f}%)")
    print(f"Action decisions: {action_count} ({action_count/len(decisions)*100:.0f}%)")
    print()
    print("Doctrine compliance: ", end="")
    if no_action_count >= len(decisions) * 0.6:
        print("PASS (≥60% NO_ACTION)")
    else:
        print("REVIEW (action rate higher than typical)")
    print()


def main():
    """Run all smoke tests."""
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  ALPHABET TRAINS ORGANIC CAPITAL GOVERNOR                ║")
    print("║  Smoke Test Suite                                        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    validation_result = run_validation_smoke_test()

    if validation_result.can_proceed:
        run_governor_smoke_test()
    else:
        print("CRITICAL: Validation failed. Cannot proceed with Governor evaluation.")
        print("Fix data contract issues before continuing.")

    print("Smoke test complete.")


if __name__ == "__main__":
    main()
