"""
Full Evaluation Workflow

Integrates all components into a complete evaluation pipeline:
1. Load data from GSC/GA4
2. Run tracking diagnostics
3. Build page inventory and link graph
4. Run all evaluators per mode
5. Apply learning rules
6. Generate structured output

This is the main entry point for running the Governor.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType, GSCMetrics, GA4Metrics, TopQuery
from src.models.governance_state import GovernanceState
from src.models.cost_model import ActionCostModel
from src.models.profit_model import ProfitModel
from src.governor.agentic_governor import GovernorConfig
from src.data_sources.gsc_client import GSCClient
from src.data_sources.ga4_client import GA4Client
from src.diagnostics.tracking_sanity import TrackingSanityDiagnostics
from src.governor.agentic_governor import AgenticGovernor, GovernorMode
from src.governor.priority_scoring import ActionType, calculate_priority
from src.evaluators.title_meta_evaluator import TitleMetaEvaluator
from src.evaluators.canonical_evaluator import CanonicalEvaluator
from src.evaluators.internal_link_evaluator import InternalLinkEvaluator
from src.evaluators.asset_creation_evaluator import AssetCreationEvaluator
from src.ledger.action_ledger import ActionLedger, ActionFingerprint, ActionRecord
from src.output.decision_formatter import DecisionFormatter, OutputFormat
from src.crawlers.page_inventory import PageInventory
from src.crawlers.link_graph import LinkGraph


@dataclass
class WorkflowConfig:
    """Configuration for the evaluation workflow."""
    # Data source credentials
    gsc_property: str
    ga4_property_id: str
    credentials_path: str

    # Business parameters
    aov: float = 53.19
    margin: float = 0.27

    # Governance
    min_confidence_threshold: float = 0.65
    regret_budget_year: int = 2
    profit_to_cost_ratio_gate: float = 5.0

    # Output
    output_format: OutputFormat = OutputFormat.CONSOLE
    output_path: Optional[Path] = None

    # Modes to evaluate
    modes: list[GovernorMode] = None

    # Diagnostic settings
    run_diagnostics: bool = True
    block_on_tier_a: bool = True

    @classmethod
    def from_json(cls, path: Path) -> "WorkflowConfig":
        """Load config from JSON file."""
        with open(path) as f:
            data = json.load(f)

        return cls(
            gsc_property=data["data_sources"]["gsc"]["property_url"],
            ga4_property_id=data["data_sources"]["ga4"]["property_id"],
            credentials_path=data["data_sources"]["gsc"]["credentials_path"],
            aov=data.get("profit_model", {}).get("aov", 53.19),
            margin=data.get("profit_model", {}).get("gross_margin_low", 0.27),
            min_confidence_threshold=data.get("governance", {}).get("min_confidence_threshold", 0.65),
            regret_budget_year=data.get("governance", {}).get("regret_budget_year", 2),
            profit_to_cost_ratio_gate=data.get("governance", {}).get("profit_to_cost_ratio_gate", 5.0),
        )


class FullEvaluationWorkflow:
    """
    Complete evaluation workflow integrating all Governor components.

    Pipeline:
    1. Data Loading: GSC + GA4 metrics
    2. Diagnostics: Tracking sanity checks
    3. Inventory: Page data + link graph
    4. Evaluation: All modes and action types
    5. Learning: Apply ledger rules
    6. Output: Structured decision report
    """

    def __init__(self, config: WorkflowConfig):
        """
        Initialize workflow with configuration.

        Args:
            config: Workflow configuration
        """
        self.config = config

        # Initialize clients
        self.gsc_client = GSCClient(
            site_url=config.gsc_property,
            credentials_path=config.credentials_path,
        )
        self.ga4_client = GA4Client(
            property_id=config.ga4_property_id,
            credentials_path=config.credentials_path,
        )

        # Initialize components
        self.diagnostics = TrackingSanityDiagnostics()
        self.ledger = ActionLedger()

        # Create Governor dependencies
        cost_model = ActionCostModel()
        profit_model = ProfitModel(
            aov=config.aov,
            gross_margin_low=config.margin,
            gross_margin_high=config.margin + 0.03,
        )
        governance_state = GovernanceState()
        governor_config = GovernorConfig(
            aov=config.aov,
            gross_margin=config.margin,
            min_confidence_threshold=config.min_confidence_threshold,
            profit_to_cost_ratio_gate=config.profit_to_cost_ratio_gate,
        )

        self.governor = AgenticGovernor(
            cost_model=cost_model,
            profit_model=profit_model,
            governance_state=governance_state,
            config=governor_config,
            ledger=self.ledger,
        )
        self.formatter = DecisionFormatter()

        # Evaluators
        self.title_evaluator = TitleMetaEvaluator()
        self.canonical_evaluator = CanonicalEvaluator()
        self.link_evaluator = InternalLinkEvaluator()
        self.asset_evaluator = AssetCreationEvaluator(
            aov=config.aov,
            margin=config.margin,
        )

        # Page inventory and link graph
        domain = config.gsc_property.replace("sc-domain:", "")
        self.page_inventory = PageInventory(base_domain=domain)
        self.link_graph = LinkGraph()

        # State
        self._assets: list[PageAsset] = []
        self._diagnostic_results: list = []
        self._evaluation_results: list = []

    def load_data(self, days: int = 28) -> list[PageAsset]:
        """
        Load data from GSC and GA4.

        Args:
            days: Number of days of data to load

        Returns:
            List of PageAsset objects
        """
        print(f"Loading data from GSC and GA4 ({days} days)...")

        # Get GSC data
        gsc_data = self.gsc_client.get_page_data(days=days)
        print(f"  GSC: {len(gsc_data)} URLs")

        # Get GA4 data (organic only)
        ga4_data = self.ga4_client.get_page_data(days=days, organic_only=True)
        print(f"  GA4: {len(ga4_data)} URLs")

        # Merge into PageAssets
        all_urls = set(gsc_data.keys()) | set(ga4_data.keys())
        assets = []

        for url in all_urls:
            gsc = gsc_data.get(url, {})
            ga4 = ga4_data.get(url, {})

            # Create GSC metrics
            gsc_metrics = GSCMetrics(
                clicks_28d=gsc.get("clicks", 0),
                impressions_28d=gsc.get("impressions", 0),
                avg_position_28d=gsc.get("position", 100),
                avg_ctr_28d=gsc.get("ctr", 0),
                top_queries=[
                    TopQuery(
                        query=q["query"],
                        clicks=q["clicks"],
                        impressions=q["impressions"],
                        position=q["position"],
                        ctr=q["ctr"],
                    )
                    for q in gsc.get("queries", [])
                ],
            )

            # Create GA4 metrics
            ga4_metrics = GA4Metrics(
                sessions_28d=ga4.get("sessions", 0),
                users_28d=ga4.get("users", 0),
                engaged_sessions_28d=ga4.get("engaged_sessions", 0),
                conversions_28d=ga4.get("conversions", 0),
                revenue_28d=ga4.get("revenue", 0),
                add_to_carts_28d=ga4.get("add_to_carts", 0),
                bounce_rate_28d=ga4.get("bounce_rate", 0),
            )

            # Determine asset type from URL pattern
            asset_type = self._classify_asset_type(url)

            asset = PageAsset(
                url=url,
                asset_type=asset_type,
                gsc=gsc_metrics,
                ga4=ga4_metrics,
            )
            assets.append(asset)

        self._assets = assets
        print(f"  Merged: {len(assets)} PageAssets")

        # Import URLs to page inventory
        self.page_inventory.import_from_gsc_urls([a.url for a in assets])

        return assets

    def _classify_asset_type(self, url: str) -> AssetType:
        """Classify URL into asset type."""
        url_lower = url.lower()

        if "/product" in url_lower or "/p/" in url_lower:
            return AssetType.PRODUCT
        elif "/category" in url_lower or "/c/" in url_lower or "/collections" in url_lower:
            return AssetType.CATEGORY
        elif "/blog" in url_lower or "/article" in url_lower or "/post" in url_lower:
            return AssetType.BLOG
        elif url_lower.endswith("/") and url_lower.count("/") <= 3:
            return AssetType.CATEGORY  # Likely homepage or main category
        else:
            return AssetType.OTHER

    def run_diagnostics(self) -> dict:
        """
        Run tracking sanity diagnostics.

        Returns:
            Diagnostic summary
        """
        print("Running tracking diagnostics...")

        # Build organic sessions map with normalized paths
        organic_sessions_map = {}
        for asset in self._assets:
            normalized_path = self.diagnostics.normalize_url(asset.url)
            # Use GA4 sessions as proxy for organic (GA4 client filters organic by default)
            organic_sessions_map[normalized_path] = asset.ga4.sessions_28d

        # Run diagnostics using the correct method
        results = self.diagnostics.diagnose_all(
            assets=self._assets,
            organic_sessions_map=organic_sessions_map,
        )
        self._diagnostic_results = results

        # Summarize using the diagnostics summary method
        summary = self.diagnostics.summary(results)

        print(f"  Total pages: {summary['total_pages']}")
        print(f"  Passed: {summary['passed']}")
        print(f"  Warned: {summary['warned']}")
        print(f"  Failed (blocking): {summary['failed']}")
        print(f"  Eligible for RAIP: {summary['eligible_for_raip']}")

        return summary

    def build_link_graph(self) -> dict:
        """
        Build internal link graph from page inventory.

        Returns:
            Link graph summary
        """
        print("Building link graph...")

        # Get pages from inventory
        pages = self.page_inventory.get_all_pages()

        if pages:
            self.link_graph.build_from_inventory(pages)

            # Update assets with link metrics
            for asset in self._assets:
                metrics = self.link_graph.get_page_metrics(asset.url)
                if metrics:
                    asset.inlinks = metrics.inlinks
                    asset.outlinks = metrics.outlinks
                    asset.link_authority_score = metrics.authority_score

        summary = self.link_graph.summary()
        print(f"  Nodes: {summary['total_nodes']}")
        print(f"  Edges: {summary['total_edges']}")
        print(f"  Orphans: {summary['orphan_pages']}")

        return summary

    def evaluate_all(self, modes: list[GovernorMode] = None) -> list[dict]:
        """
        Run all evaluations across all modes.

        Args:
            modes: Modes to evaluate. Default: all modes

        Returns:
            List of evaluation results
        """
        if modes is None:
            modes = self.config.modes or [
                GovernorMode.PRESERVATION,
                GovernorMode.OPPORTUNITY_DISCOVERY,
                GovernorMode.FUNNEL_ALIGNMENT,
            ]

        print(f"Running evaluations for modes: {[m.value for m in modes]}...")

        all_results = []

        for asset in self._assets:
            result = self._evaluate_asset(asset, modes)
            all_results.append(result)

        self._evaluation_results = all_results

        # Summary
        actions = [r for r in all_results if r.get("recommended_action") != "NO_ACTION"]
        print(f"  Total evaluated: {len(all_results)}")
        print(f"  With actions: {len(actions)}")

        return all_results

    def _evaluate_asset(
        self,
        asset: PageAsset,
        modes: list[GovernorMode],
    ) -> dict:
        """
        Evaluate a single asset across all modes and evaluators.

        Returns best recommendation after applying learning rules.
        """
        candidates = []

        # Run main Governor evaluation for each mode
        for mode in modes:
            result = self.governor.evaluate(asset, mode)
            if result.recommended_action != "NO_ACTION":
                candidates.append({
                    "mode": mode.value,
                    "action": result.recommended_action,
                    "expected_value": result.expected_value,
                    "confidence": result.confidence,
                    "risk_level": result.risk_level,
                    "implementation_steps": result.implementation_steps,
                    "source": "governor",
                })

        # Run specialized evaluators
        # Title/Meta evaluation
        if asset.gsc.impressions_28d >= 500:
            title_result = self.title_evaluator.evaluate(asset)
            if title_result.recommended_action != "NO_ACTION":
                candidates.append({
                    "mode": "OPPORTUNITY_DISCOVERY",
                    "action": title_result.recommended_action,
                    "expected_value": title_result.expected_lift * asset.ga4.sessions_28d * 0.5,
                    "confidence": title_result.confidence,
                    "risk_level": title_result.risk_level,
                    "implementation_steps": title_result.test_variants[:2] if title_result.test_variants else [],
                    "source": "title_evaluator",
                })

        # Canonical evaluation
        canonical_result = self.canonical_evaluator.evaluate(asset)
        if canonical_result.recommended_action != "NO_ACTION":
            candidates.append({
                "mode": "PRESERVATION",
                "action": canonical_result.recommended_action,
                "expected_value": canonical_result.expected_lift * asset.ga4.sessions_28d,
                "confidence": canonical_result.confidence,
                "risk_level": canonical_result.risk_level,
                "implementation_steps": canonical_result.implementation_steps,
                "source": "canonical_evaluator",
            })

        # Internal link evaluation
        link_result = self.link_evaluator.evaluate(asset, self._assets)
        if link_result.recommended_action != "NO_ACTION":
            candidates.append({
                "mode": "FUNNEL_ALIGNMENT",
                "action": link_result.recommended_action,
                "expected_value": link_result.expected_lift * asset.ga4.sessions_28d * 2,
                "confidence": link_result.confidence,
                "risk_level": link_result.risk_level,
                "implementation_steps": link_result.implementation_steps,
                "source": "link_evaluator",
            })

        # Apply learning rules to adjust confidence
        for candidate in candidates:
            fingerprint = ActionFingerprint(
                page_type=asset.asset_type.value,
                intent_cluster=self._get_intent_cluster(asset),
                action_surface=self._get_action_surface(candidate["action"]),
                action_type=candidate["action"],
            )

            adjusted_conf, insight = self.ledger.apply_learning_rules(
                base_confidence=candidate["confidence"],
                fingerprint=fingerprint,
                min_confidence_threshold=self.config.min_confidence_threshold,
            )

            candidate["original_confidence"] = candidate["confidence"]
            candidate["confidence"] = adjusted_conf
            candidate["learning_adjustment"] = insight.confidence_adjustment
            candidate["learning_reference"] = insight.recommendation if insight.matching_actions > 0 else None

        # Calculate priority for each candidate
        for candidate in candidates:
            try:
                action_type = ActionType(candidate["action"])
            except ValueError:
                action_type = ActionType.OBSERVE_ONLY

            priority = calculate_priority(
                expected_value=candidate["expected_value"],
                confidence=candidate["confidence"],
                action_type=action_type,
                impressions=asset.gsc.impressions_28d,
                position=asset.gsc.avg_position_28d,
                has_revenue=asset.ga4.revenue_28d > 0,
            )
            candidate["priority_score"] = priority.priority

        # Select best candidate by priority
        if candidates:
            candidates.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
            best = candidates[0]

            # Check confidence threshold
            if best["confidence"] < self.config.min_confidence_threshold:
                return {
                    "url": asset.url,
                    "recommended_action": "OBSERVE_ONLY",
                    "mode": best["mode"],
                    "expected_value": best["expected_value"],
                    "confidence": best["confidence"],
                    "priority_score": 0,
                    "risk_level": "low",
                    "implementation_steps": [],
                    "reason": f"Confidence {best['confidence']:.2f} below threshold {self.config.min_confidence_threshold}",
                }

            return {
                "url": asset.url,
                "recommended_action": best["action"],
                "mode": best["mode"],
                "expected_value": best["expected_value"],
                "confidence": best["confidence"],
                "priority_score": best.get("priority_score", 0),
                "risk_level": best["risk_level"],
                "implementation_steps": best["implementation_steps"],
                "implementation_summary": best["implementation_steps"][0] if best["implementation_steps"] else "",
                "learning_reference": best.get("learning_reference"),
                "source": best["source"],
            }

        # No action recommended
        return {
            "url": asset.url,
            "recommended_action": "NO_ACTION",
            "mode": "PRESERVATION",
            "expected_value": 0,
            "confidence": 1.0,
            "priority_score": 0,
            "risk_level": "none",
            "implementation_steps": [],
            "reason": "No actionable opportunities identified",
        }

    def _get_intent_cluster(self, asset: PageAsset) -> str:
        """Get dominant intent cluster for asset."""
        if asset.ga4.revenue_28d > 0:
            return "transactional"
        elif asset.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY):
            return "commercial"
        elif asset.asset_type == AssetType.BLOG:
            return "informational"
        return "unknown"

    def _get_action_surface(self, action: str) -> str:
        """Get action surface from action type."""
        surfaces = {
            "TITLE_META_TEST": "title",
            "CANONICAL_FIX": "canonical",
            "INTERNAL_LINK_REALLOCATION": "links",
            "PAGE_REINVESTMENT": "content",
            "NEW_ASSET_CREATION": "content",
        }
        return surfaces.get(action, "other")

    def save_results_for_dashboard(self):
        """Save evaluation and diagnostic results for the web dashboard."""
        data_dir = Path(__file__).parent.parent.parent / "data"
        data_dir.mkdir(exist_ok=True)

        # Save evaluation results
        eval_data = {
            "timestamp": datetime.now().isoformat(),
            "total_pages": len(self._assets),
            "pages_with_action": sum(
                1 for r in self._evaluation_results
                if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
            ),
            "action_rate": round(
                sum(1 for r in self._evaluation_results
                    if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None))
                / max(len(self._evaluation_results), 1) * 100, 1
            ),
            "total_expected_value": round(
                sum(r.get("expected_value", 0) for r in self._evaluation_results), 2
            ),
            "results": self._evaluation_results,
        }

        with open(data_dir / "latest_evaluation.json", "w") as f:
            json.dump(eval_data, f, indent=2, default=str)

        # Save diagnostic results
        if self._diagnostic_results:
            diag_data = {
                "timestamp": datetime.now().isoformat(),
                "results": [
                    {
                        "url": d.url,
                        "status": d.status,
                        "blocking": d.blocking,
                        "tier": "A" if d.blocking else ("B" if d.status == "WARN" else "PASS"),
                        "eligible_for_raip": d.eligible_for_raip,
                        "special_classification": d.special_classification,
                        "failures": [
                            {
                                "code": f.code.value,
                                "severity": f.severity.value,
                                "interpretation": f.interpretation,
                                "recommended_fix": f.recommended_fix,
                            }
                            for f in d.failures
                        ],
                    }
                    for d in self._diagnostic_results
                ],
            }

            with open(data_dir / "diagnostic_results.json", "w") as f:
                json.dump(diag_data, f, indent=2)

        print(f"  Results saved to {data_dir}")

    def generate_output(self) -> str:
        """
        Generate formatted output report.

        Returns:
            Formatted report string
        """
        print("Generating output report...")

        # Get learning insights for referenced patterns
        learning_insights = []
        for result in self._evaluation_results:
            if result.get("learning_reference"):
                # Could fetch from ledger, but for now just include in output
                pass

        # Format diagnostic summary
        diagnostic_summary = None
        if self._diagnostic_results:
            # PageDiagnostic objects have blocking (Tier A) and status attributes
            diagnostic_summary = {
                "tier_a_failures": sum(1 for r in self._diagnostic_results if r.blocking),
                "tier_b_warnings": sum(1 for r in self._diagnostic_results if r.status == "WARN"),
                "eligible_for_raip": sum(1 for r in self._diagnostic_results if r.eligible_for_raip),
            }

        # Get regret budget from ledger
        ledger_summary = self.ledger.summary()
        # Count irreversible actions in last year
        # (simplified - would need to track properly)
        regret_budget_remaining = self.config.regret_budget_year

        output = self.formatter.format_and_output(
            evaluation_results=self._evaluation_results,
            output_format=self.config.output_format,
            output_path=self.config.output_path,
            diagnostic_summary=diagnostic_summary,
            regret_budget_remaining=regret_budget_remaining,
        )

        return output

    def run(self, days: int = 28) -> str:
        """
        Run the complete evaluation workflow.

        Args:
            days: Days of data to analyze

        Returns:
            Formatted output report
        """
        print("=" * 60)
        print("AGENTIC ORGANIC GROWTH GOVERNOR")
        print("Full Evaluation Workflow")
        print("=" * 60)
        print()

        # Step 1: Load data
        self.load_data(days=days)
        print()

        # Step 2: Run diagnostics
        if self.config.run_diagnostics:
            diag_summary = self.run_diagnostics()
            print()

            # Check for blockers (failed = Tier A blocking failures)
            if self.config.block_on_tier_a and diag_summary["failed"] > 0:
                print("⚠️  BLOCKED: Tier A diagnostic failures detected.")
                print(f"   {diag_summary['failed']} pages have blocking issues.")
                print("   Resolve tracking issues before proceeding.")
                return "BLOCKED: Tier A diagnostic failures"

        # Step 3: Build link graph
        self.build_link_graph()
        print()

        # Step 4: Run evaluations
        self.evaluate_all()
        print()

        # Step 5: Save results for dashboard
        self.save_results_for_dashboard()
        print()

        # Step 6: Generate output
        output = self.generate_output()
        print()

        print("Workflow complete.")
        return output


def main():
    """Main entry point for running the workflow."""
    import argparse

    parser = argparse.ArgumentParser(description="Run Agentic Organic Growth Governor")
    parser.add_argument("--config", type=str, default="config/defaults.json",
                        help="Path to config file")
    parser.add_argument("--days", type=int, default=28,
                        help="Days of data to analyze")
    parser.add_argument("--format", type=str, default="console",
                        choices=["console", "json", "markdown"],
                        help="Output format")
    parser.add_argument("--output", type=str, help="Output file path")
    parser.add_argument("--skip-diagnostics", action="store_true",
                        help="Skip tracking diagnostics")
    parser.add_argument("--no-block", action="store_true",
                        help="Don't block on Tier A diagnostic failures")

    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        config = WorkflowConfig.from_json(config_path)
    else:
        print(f"Config file not found: {config_path}")
        print("Using default configuration...")
        config = WorkflowConfig(
            gsc_property="sc-domain:example.com",
            ga4_property_id="123456789",
            credentials_path="credentials.json",
        )

    # Override from command line
    config.output_format = OutputFormat(args.format)
    if args.output:
        config.output_path = Path(args.output)
    config.run_diagnostics = not args.skip_diagnostics
    if args.no_block:
        config.block_on_tier_a = False

    # Run workflow
    workflow = FullEvaluationWorkflow(config)
    output = workflow.run(days=args.days)

    print(output)


if __name__ == "__main__":
    main()
