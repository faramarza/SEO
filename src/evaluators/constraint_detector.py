"""
Constraint Detection Evaluator

Detects what is BLOCKING demand capture, not what is performing well.

Core principle: Absence of sales is never evidence of absence of demand.
Impressions imply demand. Low clicks imply visibility constraints, not lack of interest.

Evaluation Dimensions (all independent):
A. Demand Existence - GSC impressions, stability, query clarity
B. Commercial Intent - Transactional queries, PDP behavior, funnel progression
C. Discoverability Constraints - Position, CTR vs expected, suppression
D. Economic Plausibility - AOV, margin, fulfillment reality
E. Channel Coverage Gaps - Organic carrying all discovery?
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType


class ConstraintType(str, Enum):
    """Types of constraints blocking demand capture."""
    VISIBILITY_BLOCKED = "visibility_blocked"  # High impressions, poor position
    CTR_SUPPRESSED = "ctr_suppressed"  # Good position but low CTR (title/snippet issue)
    INTENT_MISMATCH = "intent_mismatch"  # Traffic but no engagement
    COVERAGE_GAP = "coverage_gap"  # Demand exists, no monetization path
    CANNIBALIZATION = "cannibalization"  # Multiple pages competing
    ECONOMIC_INVALID = "economic_invalid"  # Economics don't work
    NO_CONSTRAINT = "no_constraint"  # Page is performing as expected


class CaptureClass(str, Enum):
    """Classification per the doctrine."""
    CAPTURE_EXPANSION = "capture_expansion"  # Demand + monetization exists, coverage incomplete
    CAPTURE_VALIDATION = "capture_validation"  # Demand + intent exists, monetization blocked
    CAPTURE_DEFERRED = "capture_deferred"  # Demand weak OR economics invalid
    NO_ACTION = "no_action"  # No constraint detected


@dataclass
class ConstraintSignal:
    """A single constraint signal with evidence."""
    constraint_type: ConstraintType
    severity: str  # "critical", "high", "medium", "low"
    description: str
    evidence: dict  # Raw data supporting the signal
    recommended_action: str
    reversibility: str  # "immediate", "slow", "irreversible"


@dataclass
class ConstraintResult:
    """Result of constraint detection for a page."""
    url: str
    capture_class: CaptureClass
    constraints: list[ConstraintSignal]
    demand_score: float  # 0-1, based on impressions/stability
    intent_score: float  # 0-1, based on commercial signals
    visibility_score: float  # 0-1, based on position/CTR
    economic_valid: bool
    primary_constraint: Optional[ConstraintType]
    recommended_actions: list[str]
    confidence: float
    extracted_queries: list[str]  # Verbatim queries only


class ConstraintDetector:
    """
    Detects constraints blocking demand capture.

    Does NOT judge products by performance.
    Does NOT infer lack of demand from lack of sales.
    """

    def __init__(
        self,
        aov: float = 53.19,
        margin: float = 0.27,
        min_economic_profit: float = 5.0,  # Minimum profit per order to be viable
    ):
        self.aov = aov
        self.margin = margin
        self.min_economic_profit = min_economic_profit

        # Expected CTR by position (approximate industry benchmarks)
        self.expected_ctr_by_position = {
            1: 0.30, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05,
            6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.015,
        }

    def evaluate(self, asset: PageAsset, all_assets: list[PageAsset] = None) -> ConstraintResult:
        """
        Detect constraints for a single page.

        Evaluates all dimensions independently - no single dimension nullifies others.
        """
        constraints = []

        # A. Demand Existence
        demand_score = self._evaluate_demand(asset)

        # B. Commercial Intent
        intent_score = self._evaluate_intent(asset)

        # C. Discoverability Constraints
        visibility_score, visibility_constraints = self._evaluate_visibility(asset)
        constraints.extend(visibility_constraints)

        # D. Economic Plausibility
        economic_valid = self._evaluate_economics(asset)

        # E. Coverage/Cannibalization
        if all_assets:
            coverage_constraints = self._evaluate_coverage(asset, all_assets)
            constraints.extend(coverage_constraints)

        # Classify
        capture_class = self._classify(
            demand_score=demand_score,
            intent_score=intent_score,
            visibility_score=visibility_score,
            economic_valid=economic_valid,
            has_constraints=len(constraints) > 0,
        )

        # Determine primary constraint
        primary_constraint = None
        if constraints:
            # Sort by severity
            severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            constraints.sort(key=lambda c: severity_order.get(c.severity, 4))
            primary_constraint = constraints[0].constraint_type

        # Build recommended actions
        recommended_actions = self._build_recommendations(
            capture_class=capture_class,
            constraints=constraints,
            asset=asset,
        )

        # Extract verbatim queries (never generated, only observed)
        extracted_queries = [q.query for q in asset.gsc.top_queries] if asset.gsc.top_queries else []

        # Confidence based on data quality
        confidence = self._calculate_confidence(asset, demand_score, intent_score)

        return ConstraintResult(
            url=asset.url,
            capture_class=capture_class,
            constraints=constraints,
            demand_score=demand_score,
            intent_score=intent_score,
            visibility_score=visibility_score,
            economic_valid=economic_valid,
            primary_constraint=primary_constraint,
            recommended_actions=recommended_actions,
            confidence=confidence,
            extracted_queries=extracted_queries,
        )

    def _evaluate_demand(self, asset: PageAsset) -> float:
        """
        Evaluate demand existence based on impressions.

        Impressions = demand signal. Period.
        """
        impressions = asset.gsc.impressions_28d

        if impressions >= 10000:
            return 1.0  # Strong demand
        elif impressions >= 5000:
            return 0.8
        elif impressions >= 1000:
            return 0.6
        elif impressions >= 500:
            return 0.4
        elif impressions >= 100:
            return 0.2
        else:
            return 0.1  # Weak but not zero

    def _evaluate_intent(self, asset: PageAsset) -> float:
        """
        Evaluate commercial intent from behavior signals.

        Intent is semantic and behavioral, not financial.
        """
        score = 0.0

        # Asset type implies intent
        if asset.asset_type == AssetType.PRODUCT:
            score += 0.4  # Product pages have inherent commercial intent
        elif asset.asset_type == AssetType.CATEGORY:
            score += 0.3

        # GA4 engagement signals
        if asset.ga4.sessions_28d > 0:
            engagement_rate = asset.ga4.engaged_sessions_28d / asset.ga4.sessions_28d
            score += engagement_rate * 0.3

        # Add-to-cart behavior (strongest intent signal)
        if asset.ga4.add_to_carts_28d > 0:
            score += 0.3

        # Revenue existence (but don't penalize absence)
        if asset.ga4.revenue_28d > 0:
            score += 0.1

        return min(score, 1.0)

    def _evaluate_visibility(self, asset: PageAsset) -> tuple[float, list[ConstraintSignal]]:
        """
        Evaluate discoverability constraints.

        Low visibility + impressions = BLOCKED opportunity, not failed product.
        """
        constraints = []
        position = asset.gsc.avg_position_28d
        impressions = asset.gsc.impressions_28d
        clicks = asset.gsc.clicks_28d
        actual_ctr = asset.gsc.avg_ctr_28d

        # Calculate visibility score (inverse of position, normalized)
        if position <= 3:
            visibility_score = 1.0
        elif position <= 10:
            visibility_score = 0.7
        elif position <= 20:
            visibility_score = 0.4
        elif position <= 50:
            visibility_score = 0.2
        else:
            visibility_score = 0.1

        # CONSTRAINT: Visibility blocked (high impressions, poor position)
        if impressions >= 500 and position > 10:
            severity = "critical" if impressions >= 5000 else "high" if impressions >= 1000 else "medium"
            constraints.append(ConstraintSignal(
                constraint_type=ConstraintType.VISIBILITY_BLOCKED,
                severity=severity,
                description=f"Page has {impressions:,} impressions but avg position {position:.1f} (page {int(position/10)+1})",
                evidence={
                    "impressions": impressions,
                    "position": position,
                    "clicks": clicks,
                    "potential_clicks_at_pos_5": int(impressions * 0.05),  # ~5% CTR at pos 5
                },
                recommended_action="Improve on-page SEO and internal linking to boost ranking",
                reversibility="slow",
            ))

        # CONSTRAINT: CTR suppressed (good position but low CTR)
        if position <= 10 and impressions >= 500:
            expected_ctr = self.expected_ctr_by_position.get(int(position), 0.01)
            if actual_ctr < expected_ctr * 0.5:  # Less than half expected
                constraints.append(ConstraintSignal(
                    constraint_type=ConstraintType.CTR_SUPPRESSED,
                    severity="high",
                    description=f"Position {position:.1f} should yield ~{expected_ctr*100:.1f}% CTR but actual is {actual_ctr*100:.2f}%",
                    evidence={
                        "position": position,
                        "expected_ctr": expected_ctr,
                        "actual_ctr": actual_ctr,
                        "ctr_gap": expected_ctr - actual_ctr,
                        "missed_clicks": int(impressions * (expected_ctr - actual_ctr)),
                    },
                    recommended_action="Test title tag and meta description to improve CTR",
                    reversibility="immediate",
                ))

        return visibility_score, constraints

    def _evaluate_economics(self, asset: PageAsset) -> bool:
        """
        Evaluate economic plausibility.

        If economics fundamentally fail, halt paid recommendations.
        But never let economics nullify organic opportunities.
        """
        profit_per_order = self.aov * self.margin
        return profit_per_order >= self.min_economic_profit

    def _evaluate_coverage(
        self,
        asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> list[ConstraintSignal]:
        """
        Evaluate coverage gaps and cannibalization.
        """
        constraints = []

        # Check for potential cannibalization (multiple pages targeting similar queries)
        if asset.gsc.top_queries:
            top_query = asset.gsc.top_queries[0].query if asset.gsc.top_queries else None
            if top_query:
                competing_pages = []
                for other in all_assets:
                    if other.url == asset.url:
                        continue
                    if other.gsc.top_queries:
                        other_queries = [q.query.lower() for q in other.gsc.top_queries]
                        if top_query.lower() in other_queries:
                            competing_pages.append(other.url)

                if len(competing_pages) > 0:
                    constraints.append(ConstraintSignal(
                        constraint_type=ConstraintType.CANNIBALIZATION,
                        severity="medium",
                        description=f"Query '{top_query}' also targets {len(competing_pages)} other page(s)",
                        evidence={
                            "query": top_query,
                            "competing_pages": competing_pages[:3],  # Limit for display
                        },
                        recommended_action="Consolidate content or differentiate targeting",
                        reversibility="slow",
                    ))

        return constraints

    def _classify(
        self,
        demand_score: float,
        intent_score: float,
        visibility_score: float,
        economic_valid: bool,
        has_constraints: bool,
    ) -> CaptureClass:
        """
        Classify into capture class per doctrine.

        Never classify based on sales alone.
        """
        # Capture Deferred: Demand weak OR economics invalid
        if demand_score < 0.2:
            return CaptureClass.CAPTURE_DEFERRED
        if not economic_valid:
            return CaptureClass.CAPTURE_DEFERRED

        # Capture Expansion: Demand exists, some monetization, coverage incomplete
        if demand_score >= 0.4 and intent_score >= 0.3 and visibility_score >= 0.4:
            if has_constraints:
                return CaptureClass.CAPTURE_EXPANSION

        # Capture Validation: Demand + intent exists, monetization blocked
        if demand_score >= 0.4 and intent_score >= 0.3 and visibility_score < 0.4:
            return CaptureClass.CAPTURE_VALIDATION

        # If demand exists but intent unclear
        if demand_score >= 0.4:
            return CaptureClass.CAPTURE_VALIDATION

        return CaptureClass.NO_ACTION

    def _build_recommendations(
        self,
        capture_class: CaptureClass,
        constraints: list[ConstraintSignal],
        asset: PageAsset,
    ) -> list[str]:
        """
        Build specific recommendations based on constraints.

        Only recommend what the evidence supports.
        """
        recommendations = []

        if capture_class == CaptureClass.CAPTURE_DEFERRED:
            if not self._evaluate_economics(asset):
                recommendations.append("NO PAID ACTION - Economics do not support investment")
            else:
                recommendations.append("NO ACTION - Insufficient demand signals")
            return recommendations

        for constraint in constraints:
            if constraint.constraint_type == ConstraintType.VISIBILITY_BLOCKED:
                recommendations.append(f"SEO: {constraint.recommended_action}")
                if asset.gsc.impressions_28d >= 2000:
                    recommendations.append("Consider internal linking from high-authority pages")

            elif constraint.constraint_type == ConstraintType.CTR_SUPPRESSED:
                recommendations.append(f"TITLE TEST: {constraint.recommended_action}")

            elif constraint.constraint_type == ConstraintType.CANNIBALIZATION:
                recommendations.append(f"CONTENT: {constraint.recommended_action}")

        if not recommendations:
            recommendations.append("MONITOR - No actionable constraints detected")

        return recommendations

    def _calculate_confidence(
        self,
        asset: PageAsset,
        demand_score: float,
        intent_score: float,
    ) -> float:
        """
        Calculate confidence in the assessment.

        More data = higher confidence.
        """
        confidence = 0.5  # Base

        # More impressions = more confident about demand
        if asset.gsc.impressions_28d >= 5000:
            confidence += 0.2
        elif asset.gsc.impressions_28d >= 1000:
            confidence += 0.1

        # More clicks = more confident about behavior
        if asset.gsc.clicks_28d >= 100:
            confidence += 0.15
        elif asset.gsc.clicks_28d >= 20:
            confidence += 0.1

        # GA4 data present
        if asset.ga4.sessions_28d > 0:
            confidence += 0.1

        # Query data present
        if asset.gsc.top_queries and len(asset.gsc.top_queries) >= 3:
            confidence += 0.05

        return min(confidence, 0.95)
