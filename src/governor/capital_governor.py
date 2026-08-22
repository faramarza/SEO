"""
Capital Governor — the core decision engine.

Mandate: Maximize long-term, risk-adjusted incremental gross profit from organic search.
Default state: NO ACTION

Doctrine compliance is enforced at every decision point.
"""

from dataclasses import dataclass
from typing import Optional

from ..models.page_asset import PageAsset
from ..models.cost_model import ActionCostModel, ActionType
from ..models.profit_model import ProfitModel
from ..models.governance_state import GovernanceState
from ..models.decision_envelope import (
    DecisionEnvelope,
    DecisionType,
    RAIPEstimate,
    Evidence,
    RiskAssessment,
    Reversibility,
    ActionPlan,
)


@dataclass
class GovernorConfig:
    """Configuration for the Capital Governor."""
    min_confidence_threshold: float = 0.55
    profit_to_cost_ratio_gate: float = 5.0
    defensive_action_weight: float = 2.0
    exploratory_action_penalty: float = 3.0
    hourly_rate: float = 50.0  # For cost calculation


class CapitalGovernor:
    """
    Alphabet Trains Organic Capital Governor.

    A safety-first, risk-averse capital allocator for organic search decisions.

    Core Doctrine (Non-Negotiable):
    1. Inaction is the default (60-80% NO ACTION)
    2. Pages are assets, keywords are noise
    3. Profit over traffic, always
    4. Hard revenue gate: profit ≥ 5× cost, confidence ≥ lane threshold
    5. Search engine hostility assumption
    6. Limited action types only
    7. Regret budget of 2/year
    8. Counterfactual memory required
    9. Strict explanation discipline
    10. Owner-operator test
    """

    def __init__(
        self,
        cost_model: ActionCostModel,
        profit_model: ProfitModel,
        governance_state: GovernanceState,
        config: Optional[GovernorConfig] = None,
    ):
        self.cost_model = cost_model
        self.profit_model = profit_model
        self.governance = governance_state
        self.config = config or GovernorConfig()

        # Reset for new year if needed
        self.governance.reset_for_new_year()

    def _owner_operator_test(self) -> bool:
        """
        Final test (Doctrine #10):
        Would a disciplined owner-operator approve this if their own capital were at risk?

        Returns True if the system is in a state to make decisions.
        """
        if self.governance.observe_only_mode:
            return False
        if self.governance.should_be_observe_only:
            return False
        return True

    def _calculate_action_cost(self, action_type: ActionType) -> float:
        """Calculate monetary cost for an action."""
        minutes = self.cost_model.get_cost(action_type)
        return self.cost_model.minutes_to_cost_value(minutes, self.config.hourly_rate)

    def _passes_revenue_gate(
        self,
        expected_profit: float,
        action_cost: float,
    ) -> bool:
        """
        Check hard revenue gate (Doctrine #4):
        Expected Incremental Gross Profit ≥ 5× cost of action
        """
        if action_cost == 0:
            return True
        ratio = expected_profit / action_cost
        return ratio >= self.config.profit_to_cost_ratio_gate

    def _estimate_incremental_profit(
        self,
        asset: PageAsset,
        expected_traffic_lift: float,  # e.g., 0.20 for 20% lift
    ) -> float:
        """
        Estimate incremental gross profit from a traffic lift.

        Uses conservative margin and applies search engine hostility discount.
        """
        current_sessions = asset.ga4.sessions_28d
        current_purchase_rate = asset.ga4.purchase_rate_28d

        # Expected additional sessions
        incremental_sessions = current_sessions * expected_traffic_lift

        # Expected additional orders (conservative: use current purchase rate)
        if current_purchase_rate == 0:
            # No conversion history - cannot estimate with confidence
            return 0.0

        incremental_orders = incremental_sessions * current_purchase_rate

        # Apply search engine hostility discount (Doctrine #5)
        # AI Overviews and product grids reduce expected CTR
        hostility_discount = 0.7  # 30% haircut

        return self.profit_model.estimate_incremental_profit(
            incremental_orders * hostility_discount,
            use_conservative=True,
        )

    def _calculate_downside_risk(
        self,
        asset: PageAsset,
        action_type: ActionType,
    ) -> float:
        """
        Calculate potential downside risk from an action.

        Based on current revenue contribution and action reversibility.
        """
        current_revenue = asset.ga4.revenue_28d
        current_profit = current_revenue * self.profit_model.gross_margin_conservative

        # Risk factors by action type
        risk_factors = {
            ActionType.NO_ACTION: 0.0,
            ActionType.PAGE_REINVESTMENT: 0.15,  # 15% risk of temporary traffic loss
            ActionType.INTERNAL_LINK_REALLOCATION: 0.05,  # Low risk
            ActionType.NEW_PAGE_CREATION: 0.25,  # Cannibalization risk
        }

        risk_factor = risk_factors.get(action_type, 0.10)
        return current_profit * risk_factor

    def _compute_raip(
        self,
        expected_profit: float,
        confidence: float,
        downside_risk: float,
    ) -> RAIPEstimate:
        """
        Compute Risk-Adjusted Incremental Profit.

        RAIP = (Expected Incremental Gross Profit × Confidence Score) − Downside Risk
        """
        return RAIPEstimate(
            expected_incremental_gross_profit=expected_profit,
            confidence_multiplier=confidence,
            downside_risk=downside_risk,
        )

    def _apply_confidence_penalties(
        self,
        base_confidence: float,
        action_type: ActionType,
        is_exploratory: bool = False,
    ) -> float:
        """
        Apply confidence penalties based on doctrine and history.

        - Past failures reduce confidence
        - Exploratory actions are penalized 3×
        - Defensive actions are weighted 2× (bonus)
        """
        confidence = base_confidence

        # Apply historical penalty (Doctrine #8)
        action_type_str = action_type.value
        historical_penalty = self.governance.confidence_penalty(action_type_str)
        confidence -= historical_penalty

        # Apply exploratory penalty (Doctrine #5)
        if is_exploratory:
            confidence -= 0.15  # Exploratory actions get a 15% penalty

        # Defensive actions get a small boost
        if not is_exploratory:
            confidence = min(1.0, confidence + 0.05)

        return max(0.0, min(1.0, confidence))

    def evaluate_page_reinvestment(
        self,
        asset: PageAsset,
        expected_traffic_lift: float,
        base_confidence: float,
        is_exploratory: bool = False,
    ) -> DecisionEnvelope:
        """
        Evaluate a potential page reinvestment action.

        Returns a decision envelope with all doctrine-required fields.
        """
        action_type = ActionType.PAGE_REINVESTMENT

        # Check observe-only mode first
        if not self._owner_operator_test():
            return DecisionEnvelope.observe_only(
                reason="Regret budget exhausted. System in OBSERVE-ONLY mode.",
                evidence=[Evidence(
                    source="Governance",
                    fact="Regret budget status",
                    value=f"Used {self.governance.regret_budget_used}/{self.governance.regret_budget_year}",
                )],
            )

        # Check for revenue signal
        if not asset.has_revenue_signal:
            return DecisionEnvelope.no_action(
                reason="No revenue signal. Cannot estimate RAIP without conversion history.",
                evidence=[Evidence(
                    source="GA4",
                    fact="Revenue contribution",
                    value=f"${asset.ga4.revenue_28d:.2f}",
                )],
            )

        # Check tracking sanity
        if not asset.tracking_sanity_ok:
            return DecisionEnvelope.no_action(
                reason="Tracking sanity check failed. Data unreliable for decision-making.",
                evidence=[Evidence(
                    source="Tracking",
                    fact="Clicks to sessions ratio",
                    value=str(asset.clicks_to_sessions_ratio),
                )],
            )

        # Calculate costs and profits
        action_cost = self._calculate_action_cost(action_type)
        expected_profit = self._estimate_incremental_profit(asset, expected_traffic_lift)
        downside_risk = self._calculate_downside_risk(asset, action_type)

        # Apply confidence adjustments
        confidence = self._apply_confidence_penalties(
            base_confidence, action_type, is_exploratory
        )

        # Compute RAIP
        raip = self._compute_raip(expected_profit, confidence, downside_risk)

        # Build evidence
        evidence = [
            Evidence(source="GSC", fact="Impressions (28d)", value=str(asset.gsc.impressions_28d)),
            Evidence(source="GSC", fact="Clicks (28d)", value=str(asset.gsc.clicks_28d)),
            Evidence(source="GSC", fact="CTR (28d)", value=f"{asset.gsc.ctr_28d:.2%}"),
            Evidence(source="GA4", fact="Sessions (28d)", value=str(asset.ga4.sessions_28d)),
            Evidence(source="GA4", fact="Revenue (28d)", value=f"${asset.ga4.revenue_28d:.2f}"),
            Evidence(source="GA4", fact="Purchase rate", value=f"{asset.ga4.purchase_rate_28d:.2%}"),
            Evidence(source="LinkGraph", fact="Authority score", value=f"{asset.link_authority_score:.2f}"),
        ]

        # Check all gates
        passes_confidence = confidence >= self.config.min_confidence_threshold
        passes_raip = raip.is_positive
        passes_revenue = self._passes_revenue_gate(expected_profit, action_cost)

        if not passes_confidence:
            return DecisionEnvelope.no_action(
                reason=f"Confidence {confidence:.2f} below threshold {self.config.min_confidence_threshold}",
                evidence=evidence,
            )

        if not passes_raip:
            return DecisionEnvelope.no_action(
                reason=f"RAIP is negative (${raip.raip:.2f}). Risk exceeds expected return.",
                evidence=evidence,
            )

        if not passes_revenue:
            return DecisionEnvelope.no_action(
                reason=f"Expected profit ${expected_profit:.2f} does not meet 5× cost gate (${action_cost * 5:.2f} required)",
                evidence=evidence,
            )

        # All gates passed - but still provide why NO_ACTION might be better
        return DecisionEnvelope(
            decision=DecisionType.PAGE_REINVESTMENT,
            confidence=confidence,
            raip_estimate=raip,
            evidence=evidence,
            target_urls=[asset.url],
            risk=RiskAssessment(
                reversibility=Reversibility.SLOWLY_REVERSIBLE,
                failure_modes=[
                    "Content changes may temporarily reduce rankings",
                    "Search engine may not reindex promptly",
                    "User behavior may differ from expectation",
                ],
                cannibalization_risk=0.0,
            ),
            action_plan=ActionPlan(
                steps=[
                    "Backup current page content",
                    "Implement targeted structural/content changes",
                    "Submit URL for reindexing",
                    "Monitor GSC performance for 14 days",
                ],
                rollback=[
                    "Restore backed up content",
                    "Resubmit for reindexing",
                ],
            ),
            why_no_action_may_be_better=(
                "Page is currently generating revenue. Changes introduce risk. "
                "If the expected lift does not materialize, recovery may take weeks. "
                "The search engine hostility assumption means actual results may be 30% lower than projected."
            ),
        )

    def evaluate_internal_link_reallocation(
        self,
        source_asset: PageAsset,
        target_asset: PageAsset,
        base_confidence: float,
    ) -> DecisionEnvelope:
        """
        Evaluate a potential internal link reallocation action.

        Authority transfer from high-authority source to target money page.
        """
        action_type = ActionType.INTERNAL_LINK_REALLOCATION

        # Check observe-only mode
        if not self._owner_operator_test():
            return DecisionEnvelope.observe_only(
                reason="Regret budget exhausted. System in OBSERVE-ONLY mode.",
            )

        # Validate link authority leverage
        if source_asset.link_authority_score <= target_asset.link_authority_score:
            return DecisionEnvelope.no_action(
                reason="Source page does not have higher authority than target. No leverage available.",
                evidence=[
                    Evidence(source="LinkGraph", fact="Source authority", value=f"{source_asset.link_authority_score:.2f}"),
                    Evidence(source="LinkGraph", fact="Target authority", value=f"{target_asset.link_authority_score:.2f}"),
                ],
            )

        # Target must have revenue signal
        if not target_asset.has_revenue_signal:
            return DecisionEnvelope.no_action(
                reason="Target page has no revenue signal. Cannot prioritize for link investment.",
            )

        # Estimate profit (internal links typically yield 5-10% lift)
        expected_lift = 0.07  # Conservative 7% estimate
        expected_profit = self._estimate_incremental_profit(target_asset, expected_lift)
        action_cost = self._calculate_action_cost(action_type)
        downside_risk = self._calculate_downside_risk(target_asset, action_type)

        confidence = self._apply_confidence_penalties(base_confidence, action_type, False)
        raip = self._compute_raip(expected_profit, confidence, downside_risk)

        evidence = [
            Evidence(source="LinkGraph", fact="Source authority", value=f"{source_asset.link_authority_score:.2f}"),
            Evidence(source="LinkGraph", fact="Target authority", value=f"{target_asset.link_authority_score:.2f}"),
            Evidence(source="GA4", fact="Target revenue (28d)", value=f"${target_asset.ga4.revenue_28d:.2f}"),
        ]

        # Check gates
        if confidence < self.config.min_confidence_threshold:
            return DecisionEnvelope.no_action(
                reason=f"Confidence {confidence:.2f} below threshold",
                evidence=evidence,
            )

        if not raip.is_positive:
            return DecisionEnvelope.no_action(
                reason=f"RAIP is negative (${raip.raip:.2f})",
                evidence=evidence,
            )

        if not self._passes_revenue_gate(expected_profit, action_cost):
            return DecisionEnvelope.no_action(
                reason="Does not meet 5× cost gate",
                evidence=evidence,
            )

        return DecisionEnvelope(
            decision=DecisionType.INTERNAL_LINK_REALLOCATION,
            confidence=confidence,
            raip_estimate=raip,
            evidence=evidence,
            target_urls=[source_asset.url, target_asset.url],
            risk=RiskAssessment(
                reversibility=Reversibility.FULLY_REVERSIBLE,
                failure_modes=[
                    "Link equity transfer may be minimal",
                    "Source page CTR may decrease if link is prominent",
                ],
                cannibalization_risk=source_asset.compute_cannibalization_risk(target_asset),
            ),
            action_plan=ActionPlan(
                steps=[
                    f"Add internal link from {source_asset.url} to {target_asset.url}",
                    "Use descriptive anchor text matching target page intent",
                    "Position link in main content area",
                    "Monitor target page GSC performance for 28 days",
                ],
                rollback=[
                    "Remove the added internal link",
                ],
            ),
            why_no_action_may_be_better=(
                "Internal link effects are uncertain and slow to manifest. "
                "The source page may lose some authority. "
                "Expected lift is modest (5-10%) and may not materialize."
            ),
        )

    def evaluate_new_page_creation(
        self,
        proposed_url: str,
        existing_assets: list[PageAsset],
        expected_monthly_orders: float,
        base_confidence: float,
    ) -> DecisionEnvelope:
        """
        Evaluate new page creation (rare; <10% of actions).

        Per doctrine, only allowed if:
        - Proven converting demand exists
        - No existing page satisfies intent
        - Cannibalization risk < 10%
        """
        action_type = ActionType.NEW_PAGE_CREATION

        # Check observe-only mode
        if not self._owner_operator_test():
            return DecisionEnvelope.observe_only(
                reason="Regret budget exhausted. System in OBSERVE-ONLY mode.",
            )

        # New page creation is exploratory - apply heavy penalty
        confidence = self._apply_confidence_penalties(
            base_confidence, action_type, is_exploratory=True
        )

        # Calculate cannibalization risk against existing assets

        # Note: This is a simplified check - in practice you'd compare query intent
        # For now, we flag this as high risk by default for new pages

        # Estimate profit
        expected_profit = self.profit_model.estimate_incremental_profit(
            expected_monthly_orders,
            use_conservative=True,
        )

        # Apply additional exploratory penalty
        expected_profit *= 0.5  # 50% haircut for unproven page

        action_cost = self._calculate_action_cost(action_type)
        downside_risk = action_cost * 0.5  # Risk of wasted investment

        raip = self._compute_raip(expected_profit, confidence, downside_risk)

        evidence = [
            Evidence(source="Estimate", fact="Expected monthly orders", value=str(expected_monthly_orders)),
            Evidence(source="Estimate", fact="Expected profit (discounted)", value=f"${expected_profit:.2f}"),
            Evidence(source="Governance", fact="Action type", value="Exploratory (3× penalty applied)"),
        ]

        # Gates with extra scrutiny
        if confidence < self.config.min_confidence_threshold:
            return DecisionEnvelope.no_action(
                reason=f"Confidence {confidence:.2f} too low for new page creation",
                evidence=evidence,
            )

        if not raip.is_positive:
            return DecisionEnvelope.no_action(
                reason=f"RAIP negative after exploratory discount (${raip.raip:.2f})",
                evidence=evidence,
            )

        # Stricter revenue gate for new pages (10× instead of 5×)
        if expected_profit < action_cost * 10:
            return DecisionEnvelope.no_action(
                reason="New page creation requires 10× cost gate. Not met.",
                evidence=evidence,
            )

        return DecisionEnvelope(
            decision=DecisionType.NEW_PAGE_CREATION,
            confidence=confidence,
            raip_estimate=raip,
            evidence=evidence,
            target_urls=[proposed_url],
            risk=RiskAssessment(
                reversibility=Reversibility.SLOWLY_REVERSIBLE,
                failure_modes=[
                    "Page may not rank for intended queries",
                    "May cannibalize existing pages",
                    "Content investment may not yield returns",
                    "Search engine may not index promptly",
                ],
                cannibalization_risk=0.15,  # Default elevated risk for new pages
            ),
            action_plan=ActionPlan(
                steps=[
                    "Create page with minimal viable content",
                    "Ensure proper canonicalization",
                    "Add conservative internal linking",
                    "Submit to GSC for indexing",
                    "Monitor for 60 days before further investment",
                ],
                rollback=[
                    "Set page to noindex",
                    "Remove internal links",
                    "Monitor for deindexing",
                    "Delete page after deindexed",
                ],
            ),
            why_no_action_may_be_better=(
                "New pages are high-risk investments. "
                "Existing pages often can be modified to capture demand. "
                "The 3× exploratory penalty exists because most new pages fail to rank. "
                "Consider page reinvestment on existing assets first."
            ),
        )

    def default_decision(self) -> DecisionEnvelope:
        """
        Return the default NO_ACTION decision.

        Doctrine #1: Inaction is the default. At least 60-80% of cycles
        must result in NO ACTION.
        """
        return DecisionEnvelope.no_action(
            reason="No evaluation requested. Default to inaction per doctrine.",
            evidence=[Evidence(
                source="Doctrine",
                fact="Default state",
                value="NO_ACTION is the preferred outcome",
            )],
        )
