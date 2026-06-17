"""
Agentic Organic Growth Governor — Enhanced decision engine.

Implements the revised doctrine with three operating modes:
- MODE 1: PRESERVATION (protect existing revenue, require positive RAIP)
- MODE 2: OPPORTUNITY DISCOVERY (identify suppressed demand via EVUV)
- MODE 3: FUNNEL ALIGNMENT (value blogs via Assist Value)

Uses:
- Click-upside table for position-aware CTR estimation
- Intent classification for demand scoring
- Conversion proxy for non-converting pages
- Action ledger for learning
- Priority scoring for ranking
"""

from dataclasses import dataclass
from typing import Any, Optional
from datetime import datetime
from enum import Enum


class GovernorMode(str, Enum):
    """Operating modes for the Governor."""
    PRESERVATION = "preservation"
    OPPORTUNITY_DISCOVERY = "opportunity"
    FUNNEL_ALIGNMENT = "funnel"

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType
from src.models.cost_model import ActionCostModel
from src.models.profit_model import ProfitModel
from src.models.governance_state import GovernanceState
from src.models.decision_envelope import (
    DecisionEnvelope,
    DecisionType,
    RAIPEstimate,
    Evidence,
    RiskAssessment,
    Reversibility,
    ActionPlan,
)
from src.metrics.opportunity_metrics import (
    calculate_evuv,
    calculate_assist_value,
    calculate_demand_score,
    calculate_visibility_gap,
    calculate_click_upside,
    calculate_conversion_proxy,
    QueryIntent,
    EVUVResult,
    AssistValueResult,
)
from src.governor.priority_scoring import (
    calculate_priority,
    ActionType,
    PriorityScore,
    Reversibility as PriorityReversibility,
)
from src.ledger.action_ledger import (
    ActionLedger,
    ActionRecord,
    ActionFingerprint,
    ActionStatus,
    LearningInsight,
)


@dataclass
class GovernorConfig:
    """Configuration for the Agentic Governor."""
    # Confidence thresholds
    min_confidence_threshold: float = 0.55
    min_evuv_confidence: float = 0.50  # Lower bar for opportunity discovery
    min_av_confidence: float = 0.40    # Lower bar for assist value

    # Gate thresholds
    profit_to_cost_ratio_gate: float = 5.0
    min_evuv_value: float = 10.0  # Minimum EVUV to propose action
    min_av_value: float = 5.0     # Minimum AV to propose action

    # Risk parameters
    defensive_action_weight: float = 2.0
    exploratory_action_penalty: float = 3.0
    hostility_discount: float = 0.70  # 30% haircut for search engine hostility

    # Business parameters
    aov: float = 53.19
    gross_margin: float = 0.27
    site_avg_purchase_rate: float = 0.02
    hourly_rate: float = 50.0


@dataclass
class EvaluationResult:
    """Complete evaluation result for a page."""
    url: str
    asset_type: AssetType
    mode: str  # "preservation", "opportunity", "funnel"
    decision: DecisionType
    score_type: str  # "RAIP", "EVUV", "AV"
    score_value: float
    confidence: float
    priority: PriorityScore
    evidence: list[Evidence]
    action_plan: Optional[ActionPlan]
    learning_insight: Optional[LearningInsight]
    why_no_action: str
    raw_metrics: dict


class AgenticGovernor:
    """
    Agentic Organic Growth Governor.

    Three operating modes (all active simultaneously):
    - MODE 1: PRESERVATION — Protect existing revenue via RAIP
    - MODE 2: OPPORTUNITY DISCOVERY — Find suppressed demand via EVUV
    - MODE 3: FUNNEL ALIGNMENT — Value content via Assist Value (AV)
    """

    def __init__(
        self,
        cost_model: ActionCostModel,
        profit_model: ProfitModel,
        governance_state: GovernanceState,
        config: Optional[GovernorConfig] = None,
        ledger: Optional[ActionLedger] = None,
    ):
        self.cost_model = cost_model
        self.profit_model = profit_model
        self.governance = governance_state
        self.config = config or GovernorConfig()
        self.ledger = ledger or ActionLedger()

        # Reset for new year if needed
        self.governance.reset_for_new_year()

    def _create_fingerprint(
        self,
        asset: PageAsset,
        action_type: str,
        intent: QueryIntent,
    ) -> ActionFingerprint:
        """Create fingerprint for action pattern matching."""
        return ActionFingerprint(
            page_type=asset.asset_type.value,
            intent_cluster=intent.value,
            action_surface="content",  # Default; could be more specific
            action_type=action_type,
        )

    def _build_evidence(self, asset: PageAsset, extra: dict = None) -> list[Evidence]:
        """Build evidence list for decision."""
        evidence = [
            Evidence(source="GSC", fact="Impressions (28d)", value=str(asset.gsc.impressions_28d)),
            Evidence(source="GSC", fact="Clicks (28d)", value=str(asset.gsc.clicks_28d)),
            Evidence(source="GSC", fact="Avg Position", value=f"{asset.gsc.avg_position_28d:.1f}"),
            Evidence(source="GA4", fact="Sessions (28d)", value=str(asset.ga4.sessions_28d)),
            Evidence(source="GA4", fact="Revenue (28d)", value=f"${asset.ga4.revenue_28d:.2f}"),
        ]

        if extra:
            for (source, fact), value in extra.items():
                evidence.append(Evidence(source=source, fact=fact, value=str(value)))

        return evidence

    # =========================================================================
    # MODE 1: PRESERVATION GOVERNOR (RAIP)
    # =========================================================================

    def evaluate_preservation(
        self,
        asset: PageAsset,
        expected_traffic_lift: float = 0.15,
    ) -> EvaluationResult:
        """
        MODE 1: Preservation evaluation using RAIP.

        For pages with existing revenue, protect and optimize.

        Asset-type constraints:
        - PRODUCT: Primary value source is direct revenue (RAIP)
        - CATEGORY: RAIP + demand visibility
        - BLOG/GUIDE: Must NOT use RAIP — blogs use Assist Value only
        - Traffic may increase confidence but NOT override negative RAIP
        """
        # BLOG pages must NOT use RAIP — enforce asset-type rule
        if asset.asset_type == AssetType.BLOG:
            return EvaluationResult(
                url=asset.url,
                asset_type=asset.asset_type,
                mode="preservation",
                decision=DecisionType.NO_ACTION,
                score_type="RAIP",
                score_value=0.0,
                confidence=0.0,
                priority=calculate_priority(0, 0, ActionType.NO_ACTION),
                evidence=self._build_evidence(asset),
                action_plan=None,
                learning_insight=None,
                why_no_action="Blog pages must not use RAIP. Use Assist Value (AV) via Funnel Alignment mode.",
                raw_metrics={},
            )

        # Check for revenue signal
        if not asset.ga4.revenue_28d > 0:
            return EvaluationResult(
                url=asset.url,
                asset_type=asset.asset_type,
                mode="preservation",
                decision=DecisionType.NO_ACTION,
                score_type="RAIP",
                score_value=0.0,
                confidence=0.0,
                priority=calculate_priority(0, 0, ActionType.NO_ACTION),
                evidence=self._build_evidence(asset),
                action_plan=None,
                learning_insight=None,
                why_no_action="No revenue signal. Page not eligible for preservation mode.",
                raw_metrics={},
            )

        # Calculate components
        demand_score, intent = calculate_demand_score(asset)
        conversion_proxy = calculate_conversion_proxy(
            asset,
            self.config.site_avg_purchase_rate,
        )
        click_upside = calculate_click_upside(asset.gsc.avg_position_28d)

        # Calculate incremental profit
        current_sessions = asset.ga4.sessions_28d
        incremental_sessions = current_sessions * expected_traffic_lift
        incremental_conversions = incremental_sessions * conversion_proxy.proxy_rate
        incremental_revenue = incremental_conversions * self.config.aov
        incremental_profit = incremental_revenue * self.config.gross_margin

        # Apply hostility discount
        incremental_profit *= self.config.hostility_discount

        # Calculate downside risk (15% of current profit)
        current_profit = asset.ga4.revenue_28d * self.config.gross_margin
        downside_risk = current_profit * 0.15

        # Base confidence
        base_confidence = 0.70 * demand_score * conversion_proxy.confidence

        # Apply learning rules
        fingerprint = self._create_fingerprint(asset, "page_reinvestment", intent)
        adjusted_confidence, learning_insight = self.ledger.apply_learning_rules(
            base_confidence, fingerprint, self.config.min_confidence_threshold
        )

        # Calculate RAIP
        raip = (incremental_profit * adjusted_confidence) - downside_risk

        # Calculate priority
        priority = calculate_priority(
            expected_value=raip,
            confidence=adjusted_confidence,
            action_type=ActionType.PAGE_REINVESTMENT,
            impressions=asset.gsc.impressions_28d,
            position=asset.gsc.avg_position_28d,
            has_revenue=True,
        )

        # Decision logic
        if raip <= 0:
            decision = DecisionType.NO_ACTION
            why_no_action = f"RAIP is negative (${raip:.2f}). Risk exceeds expected return."
            action_plan = None
        elif adjusted_confidence < self.config.min_confidence_threshold:
            decision = DecisionType.NO_ACTION
            why_no_action = f"Confidence {adjusted_confidence:.2f} below threshold {self.config.min_confidence_threshold}."
            action_plan = None
        else:
            decision = DecisionType.PAGE_REINVESTMENT
            why_no_action = "Action proposed. However, page is already generating revenue — changes carry risk."
            action_plan = ActionPlan(
                steps=[
                    "Backup current page state",
                    "Implement targeted optimizations",
                    "Monitor GSC/GA4 for 28 days",
                ],
                rollback=["Restore from backup"],
            )

        return EvaluationResult(
            url=asset.url,
            asset_type=asset.asset_type,
            mode="preservation",
            decision=decision,
            score_type="RAIP",
            score_value=raip,
            confidence=adjusted_confidence,
            priority=priority,
            evidence=self._build_evidence(asset),
            action_plan=action_plan,
            learning_insight=learning_insight,
            why_no_action=why_no_action,
            raw_metrics={
                "demand_score": demand_score,
                "intent": intent.value,
                "conversion_proxy": conversion_proxy.proxy_rate,
                "incremental_profit": incremental_profit,
                "downside_risk": downside_risk,
            },
        )

    # =========================================================================
    # MODE 2: OPPORTUNITY DISCOVERY (EVUV)
    # =========================================================================

    def evaluate_opportunity(self, asset: PageAsset) -> EvaluationResult:
        """
        MODE 2: Opportunity discovery using EVUV.

        For pages with suppressed demand (high impressions, low position/clicks).

        Asset-type constraints:
        - PRODUCT: EVUV fully applicable
        - CATEGORY: EVUV applicable, traffic relevant only with commercial intent
        - BLOG/GUIDE: Must NOT use EVUV — blogs use Assist Value only
        """
        # BLOG pages must NOT use EVUV — enforce asset-type rule
        if asset.asset_type == AssetType.BLOG:
            return EvaluationResult(
                url=asset.url,
                asset_type=asset.asset_type,
                mode="opportunity",
                decision=DecisionType.NO_ACTION,
                score_type="EVUV",
                score_value=0.0,
                confidence=0.0,
                priority=calculate_priority(0, 0, ActionType.NO_ACTION),
                evidence=self._build_evidence(asset),
                action_plan=None,
                learning_insight=None,
                why_no_action="Blog pages must not use EVUV. Use Assist Value (AV) via Funnel Alignment mode.",
                raw_metrics={},
            )

        # Calculate EVUV
        evuv_result = calculate_evuv(
            asset,
            aov=self.config.aov,
            gross_margin=self.config.gross_margin,
            site_avg_purchase_rate=self.config.site_avg_purchase_rate,
        )

        # Apply learning rules
        fingerprint = self._create_fingerprint(
            asset, "page_reinvestment", evuv_result.dominant_intent
        )
        base_confidence = evuv_result.conversion_proxy.confidence * evuv_result.demand_score
        adjusted_confidence, learning_insight = self.ledger.apply_learning_rules(
            base_confidence, fingerprint, self.config.min_evuv_confidence
        )

        # Recalculate with adjusted confidence
        adjusted_evuv = evuv_result.evuv * (adjusted_confidence / max(0.01, base_confidence))

        # Calculate priority
        priority = calculate_priority(
            expected_value=adjusted_evuv,
            confidence=adjusted_confidence,
            action_type=ActionType.PAGE_REINVESTMENT,
            impressions=asset.gsc.impressions_28d,
            position=asset.gsc.avg_position_28d,
            has_revenue=asset.ga4.revenue_28d > 0,
        )

        # Decision logic
        if adjusted_evuv < self.config.min_evuv_value:
            decision = DecisionType.NO_ACTION
            why_no_action = f"EVUV (${adjusted_evuv:.2f}) below minimum threshold (${self.config.min_evuv_value})."
            action_plan = None
        elif adjusted_confidence < self.config.min_evuv_confidence:
            decision = DecisionType.NO_ACTION
            why_no_action = f"Confidence {adjusted_confidence:.2f} too low for opportunity pursuit."
            action_plan = None
        elif evuv_result.visibility_gap < 0.3:
            decision = DecisionType.NO_ACTION
            why_no_action = "Visibility gap too small. Page already well-positioned."
            action_plan = None
        else:
            decision = DecisionType.PAGE_REINVESTMENT
            why_no_action = "Opportunity identified. But improvements may not materialize as projected."
            action_plan = ActionPlan(
                steps=[
                    f"Target position improvement from {asset.gsc.avg_position_28d:.1f} to top 5",
                    "Optimize for dominant intent: " + evuv_result.dominant_intent.value,
                    "Monitor visibility metrics for 28 days",
                ],
                rollback=["Revert changes if metrics decline >20%"],
            )

        return EvaluationResult(
            url=asset.url,
            asset_type=asset.asset_type,
            mode="opportunity",
            decision=decision,
            score_type="EVUV",
            score_value=adjusted_evuv,
            confidence=adjusted_confidence,
            priority=priority,
            evidence=self._build_evidence(asset, {
                ("Metrics", "Visibility Gap"): f"{evuv_result.visibility_gap:.2f}",
                ("Metrics", "Click Upside"): f"{evuv_result.click_upside:.1f}x",
                ("Metrics", "Demand Score"): f"{evuv_result.demand_score:.2f}",
            }),
            action_plan=action_plan,
            learning_insight=learning_insight,
            why_no_action=why_no_action,
            raw_metrics={
                "visibility_gap": evuv_result.visibility_gap,
                "click_upside": evuv_result.click_upside,
                "demand_score": evuv_result.demand_score,
                "conversion_proxy": evuv_result.conversion_proxy.proxy_rate,
                "breakdown": evuv_result.breakdown,
            },
        )

    # =========================================================================
    # MODE 3: FUNNEL ALIGNMENT (ASSIST VALUE)
    # =========================================================================

    def evaluate_funnel(
        self,
        asset: PageAsset,
        internal_links_to_products: int = 0,
        internal_links_to_categories: int = 0,
    ) -> EvaluationResult:
        """
        MODE 3: Funnel alignment using Assist Value.

        For blogs/guides that should route traffic to revenue pages.

        Decision enforcement:
        - If AV ≤ threshold AND no routing improvements possible:
          Value = LOW, Recommendation = NO_ACTION
        - Do not propose traffic or content expansion for blogs
        - Blog optimization must focus on routing quality, not traffic expansion
        """
        # Calculate Assist Value (uses impressions for demand exposure, not sessions)
        av_result = calculate_assist_value(
            asset,
            internal_links_to_products=internal_links_to_products,
            internal_links_to_categories=internal_links_to_categories,
            avg_product_conversion_rate=self.config.site_avg_purchase_rate * 1.5,
            avg_product_aov=self.config.aov,
            gross_margin=self.config.gross_margin,
        )

        # Apply learning rules
        demand_score, intent = calculate_demand_score(asset)
        fingerprint = self._create_fingerprint(asset, "internal_link_reallocation", intent)
        adjusted_confidence, learning_insight = self.ledger.apply_learning_rules(
            av_result.confidence, fingerprint, self.config.min_av_confidence
        )

        # Recalculate with adjusted confidence
        adjusted_av = av_result.assist_value * (adjusted_confidence / max(0.01, av_result.confidence))

        # Calculate priority (internal linking is lower effort)
        priority = calculate_priority(
            expected_value=adjusted_av,
            confidence=adjusted_confidence,
            action_type=ActionType.INTERNAL_LINK_REALLOCATION,
            impressions=asset.gsc.impressions_28d,
            position=asset.gsc.avg_position_28d,
            has_revenue=False,
        )

        # Decision logic with blog-specific enforcement
        has_routing = av_result.funnel_completion_prob >= 0.05
        can_improve_routing = internal_links_to_products == 0 or internal_links_to_categories == 0

        if adjusted_av < self.config.min_av_value and not can_improve_routing:
            # AV below threshold AND no routing improvements possible → NO ACTION
            decision = DecisionType.NO_ACTION
            why_no_action = (
                f"Assist Value (${adjusted_av:.2f}) below threshold and no routing improvements possible. "
                f"Do not propose traffic or content expansion."
            )
            action_plan = None
        elif adjusted_av < self.config.min_av_value:
            decision = DecisionType.NO_ACTION
            why_no_action = f"Assist Value (${adjusted_av:.2f}) below minimum threshold."
            action_plan = None
        elif not has_routing:
            decision = DecisionType.NO_ACTION
            why_no_action = (
                "Funnel completion probability too low. Blog needs internal links "
                "to product/category pages before it can generate value."
            )
            action_plan = ActionPlan(
                steps=["Add internal links to product/category pages before re-evaluation"],
                rollback=[],
            )
        else:
            decision = DecisionType.INTERNAL_LINK_REALLOCATION
            why_no_action = "Action proposed. Blog attribution is inherently uncertain."
            action_plan = ActionPlan(
                steps=[
                    "Improve internal linking to revenue pages (focus on routing quality)",
                    "Add 'next step' block directing users to relevant product/category",
                    "Monitor funnel flow for 28 days",
                ],
                rollback=["Remove added links if no improvement"],
            )

        return EvaluationResult(
            url=asset.url,
            asset_type=asset.asset_type,
            mode="funnel",
            decision=decision,
            score_type="AV",
            score_value=adjusted_av,
            confidence=adjusted_confidence,
            priority=priority,
            evidence=self._build_evidence(asset, {
                ("Funnel", "FCP"): f"{av_result.funnel_completion_prob:.2%}",
                ("Funnel", "RCC"): f"{av_result.revenue_contribution_coef:.2f}",
                ("Funnel", "Links to Products"): str(internal_links_to_products),
            }),
            action_plan=action_plan,
            learning_insight=learning_insight,
            why_no_action=why_no_action,
            raw_metrics={
                "demand_exposure": av_result.demand_exposure,
                "fcp": av_result.funnel_completion_prob,
                "rcc": av_result.revenue_contribution_coef,
                "breakdown": av_result.breakdown,
            },
        )

    # =========================================================================
    # UNIFIED EVALUATION
    # =========================================================================

    def evaluate(
        self,
        asset: PageAsset,
        internal_links_to_products: int = 0,
        internal_links_to_categories: int = 0,
    ) -> EvaluationResult:
        """
        Unified evaluation that selects the best mode for the asset.

        Selection logic:
        1. If page has revenue → MODE 1 (Preservation)
        2. If page is blog/guide → MODE 3 (Funnel)
        3. Otherwise → MODE 2 (Opportunity)

        Returns the evaluation result from the selected mode.
        """
        # Check observe-only mode
        if self.governance.observe_only_mode:
            return EvaluationResult(
                url=asset.url,
                asset_type=asset.asset_type,
                mode="observe_only",
                decision=DecisionType.OBSERVE_ONLY,
                score_type="N/A",
                score_value=0.0,
                confidence=0.0,
                priority=calculate_priority(0, 0, ActionType.OBSERVE_ONLY),
                evidence=[],
                action_plan=None,
                learning_insight=None,
                why_no_action="System in OBSERVE-ONLY mode. Regret budget exhausted.",
                raw_metrics={},
            )

        # Select mode
        if asset.ga4.revenue_28d > 0:
            # Has revenue → Preservation mode
            return self.evaluate_preservation(asset)
        elif asset.asset_type == AssetType.BLOG:
            # Blog → Funnel mode
            return self.evaluate_funnel(
                asset,
                internal_links_to_products,
                internal_links_to_categories,
            )
        else:
            # Everything else → Opportunity mode
            return self.evaluate_opportunity(asset)

    def evaluate_all(
        self,
        assets: list[PageAsset],
        link_data: Optional[dict] = None,
    ) -> list[EvaluationResult]:
        """
        Evaluate all assets and return results sorted by priority.

        Args:
            assets: List of PageAssets to evaluate
            link_data: Optional dict mapping URL -> (product_links, category_links)
        """
        link_data = link_data or {}
        results = []

        for asset in assets:
            product_links, category_links = link_data.get(asset.url, (0, 0))
            result = self.evaluate(
                asset,
                internal_links_to_products=product_links,
                internal_links_to_categories=category_links,
            )
            results.append(result)

        # Sort by priority
        results.sort(key=lambda r: r.priority.priority, reverse=True)

        return results

    def create_action_record(self, result: EvaluationResult) -> Optional[ActionRecord]:
        """
        Create action record for non-NO_ACTION results.

        Returns None for NO_ACTION decisions.
        """
        if result.decision in (DecisionType.NO_ACTION, DecisionType.OBSERVE_ONLY):
            return None

        demand_score, intent = calculate_demand_score(
            PageAsset(url=result.url, asset_type=result.asset_type)
        ) if hasattr(result, 'raw_metrics') else (0.5, QueryIntent.UNKNOWN)

        fingerprint = ActionFingerprint(
            page_type=result.asset_type.value,
            intent_cluster=result.raw_metrics.get("intent", "unknown"),
            action_surface="content",
            action_type=result.decision.value,
        )

        record = ActionRecord(
            action_id=self.ledger.generate_action_id(),
            url=result.url,
            action_type=result.decision.value,
            score_type=result.score_type,
            score_value=result.score_value,
            confidence=result.confidence,
            fingerprint=fingerprint,
            recommendation_json={
                "action_plan": result.action_plan.__dict__ if result.action_plan else {},
                "evidence": [e.__dict__ for e in result.evidence],
                "why_no_action": result.why_no_action,
            },
            status=ActionStatus.PROPOSED,
            prior_action_refs=(
                result.learning_insight.action_ids[:5]
                if result.learning_insight else []
            ),
        )

        return record
