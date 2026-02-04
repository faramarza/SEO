"""
New Asset Creation Evaluator

Implements the mandatory asset creation gate per doctrine:
- Requires proven demand signal (GSC impressions for related queries)
- Must have clear funnel role (awareness, consideration, conversion)
- Mandatory cost-benefit analysis
- Blog/guide pages require additional validation

Asset creation is SLOWLY_REVERSIBLE - once created, removing is costly.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType


class FunnelRole(str, Enum):
    """Funnel role for new asset."""
    AWARENESS = "awareness"       # Top of funnel - informational
    CONSIDERATION = "consideration"  # Middle - commercial comparison
    CONVERSION = "conversion"     # Bottom - transactional/purchase


class AssetCreationGateResult(str, Enum):
    """Result of the asset creation gate check."""
    APPROVED = "approved"
    REJECTED_NO_DEMAND = "rejected_no_demand"
    REJECTED_NO_FUNNEL_ROLE = "rejected_no_funnel_role"
    REJECTED_INSUFFICIENT_ROI = "rejected_insufficient_roi"
    REJECTED_CANNIBALIZATION = "rejected_cannibalization"
    REJECTED_BLOG_VALIDATION = "rejected_blog_validation"


@dataclass
class DemandSignal:
    """Evidence of search demand for proposed asset."""
    primary_query: str
    monthly_impressions: int
    avg_position: float  # Position of existing pages for this query
    click_through_rate: float
    competition_level: str  # low, medium, high
    related_queries: list[str]
    total_query_volume: int


@dataclass
class AssetProposal:
    """Proposal for a new asset."""
    proposed_url: str
    proposed_title: str
    asset_type: AssetType
    funnel_role: FunnelRole
    target_queries: list[str]
    estimated_word_count: int
    estimated_creation_cost: float  # In operator minutes or dollars
    rationale: str


@dataclass
class AssetCreationResult:
    """Result of asset creation evaluation."""
    proposal: AssetProposal
    gate_result: AssetCreationGateResult
    demand_signal: Optional[DemandSignal]
    expected_value: float
    confidence: float
    risk_level: str
    cannibalization_risk: list[str]  # URLs that might be cannibalized
    implementation_steps: list[str]
    rejection_reason: Optional[str]
    recommended_action: str


class AssetCreationEvaluator:
    """
    Evaluates new asset creation opportunities.

    Implements mandatory gate:
    1. Proven demand signal (impressions > threshold)
    2. Clear funnel role
    3. Positive cost-benefit ratio (5×)
    4. No significant cannibalization risk
    5. Blog/guide specific validation
    """

    # Thresholds
    MIN_IMPRESSIONS_FOR_DEMAND = 500  # Monthly impressions
    MIN_RELATED_QUERIES = 3  # Should have query cluster, not single query
    MIN_ROI_MULTIPLE = 5.0  # Expected value must be 5× cost
    MAX_CANNIBALIZATION_OVERLAP = 0.4  # Max query overlap with existing pages
    MIN_WORD_COUNT_BLOG = 1500  # Minimum for blog posts
    MIN_WORD_COUNT_GUIDE = 2500  # Minimum for guides

    def __init__(self, aov: float = 53.19, margin: float = 0.27):
        """
        Initialize evaluator.

        Args:
            aov: Average order value
            margin: Gross margin rate
        """
        self.aov = aov
        self.margin = margin

    def check_demand_signal(
        self,
        target_queries: list[str],
        existing_assets: list[PageAsset],
    ) -> Optional[DemandSignal]:
        """
        Check if there's proven demand for the proposed asset.

        Looks for:
        - GSC impressions for target queries
        - Query cluster (not just single query)
        - Current coverage gap
        """
        if not target_queries:
            return None

        # Find existing pages ranking for target queries
        total_impressions = 0
        total_clicks = 0
        positions = []
        found_queries = []

        for asset in existing_assets:
            if not asset.gsc.top_queries:
                continue

            for query_data in asset.gsc.top_queries:
                query_lower = query_data.query.lower()
                for target in target_queries:
                    if target.lower() in query_lower or query_lower in target.lower():
                        total_impressions += query_data.impressions
                        total_clicks += query_data.clicks
                        positions.append(query_data.avg_position)
                        if query_data.query not in found_queries:
                            found_queries.append(query_data.query)

        if total_impressions < self.MIN_IMPRESSIONS_FOR_DEMAND:
            return None

        # Calculate CTR
        ctr = total_clicks / total_impressions if total_impressions > 0 else 0

        # Determine competition level based on position
        avg_position = sum(positions) / len(positions) if positions else 50
        if avg_position <= 10:
            competition = "high"  # Already ranking well
        elif avg_position <= 30:
            competition = "medium"
        else:
            competition = "low"  # Not ranking = opportunity

        return DemandSignal(
            primary_query=target_queries[0],
            monthly_impressions=total_impressions,
            avg_position=avg_position,
            click_through_rate=ctr,
            competition_level=competition,
            related_queries=found_queries,
            total_query_volume=total_impressions,
        )

    def check_cannibalization_risk(
        self,
        proposal: AssetProposal,
        existing_assets: list[PageAsset],
    ) -> tuple[bool, list[str]]:
        """
        Check if new asset would cannibalize existing pages.

        Returns:
            (has_risk, list of potentially cannibalized URLs)
        """
        at_risk = []

        target_queries_lower = {q.lower() for q in proposal.target_queries}

        for asset in existing_assets:
            if not asset.gsc.top_queries:
                continue

            # Count query overlap
            asset_queries = {q.query.lower() for q in asset.gsc.top_queries}
            overlap = target_queries_lower & asset_queries

            if len(overlap) / len(target_queries_lower) > self.MAX_CANNIBALIZATION_OVERLAP:
                at_risk.append(asset.url)

        has_risk = len(at_risk) > 0
        return has_risk, at_risk

    def validate_blog_guide(
        self,
        proposal: AssetProposal,
    ) -> tuple[bool, Optional[str]]:
        """
        Apply additional validation rules for blog/guide pages.

        Rules:
        - Minimum word count
        - Must have clear funnel role
        - Should link to money pages
        """
        if proposal.asset_type not in (AssetType.BLOG, AssetType.OTHER):
            return True, None

        # Check word count
        is_guide = "guide" in proposal.proposed_title.lower()
        min_words = self.MIN_WORD_COUNT_GUIDE if is_guide else self.MIN_WORD_COUNT_BLOG

        if proposal.estimated_word_count < min_words:
            return False, f"Blog/guide requires minimum {min_words} words, proposed: {proposal.estimated_word_count}"

        # Check funnel role - blogs should be AWARENESS or CONSIDERATION
        if proposal.funnel_role == FunnelRole.CONVERSION:
            return False, "Blog pages should not target conversion intent directly"

        return True, None

    def calculate_expected_value(
        self,
        demand_signal: DemandSignal,
        proposal: AssetProposal,
    ) -> tuple[float, float]:
        """
        Calculate expected value of new asset.

        Returns:
            (expected_value, confidence)
        """
        # Estimate traffic capture
        # Assume we can rank ~position 15 initially for new content
        estimated_ctr = 0.012  # Position 15 CTR

        # Adjust based on competition
        if demand_signal.competition_level == "low":
            estimated_ctr *= 1.5  # Better chance
        elif demand_signal.competition_level == "high":
            estimated_ctr *= 0.5  # Harder to rank

        estimated_monthly_clicks = demand_signal.monthly_impressions * estimated_ctr

        # Value depends on funnel role
        if proposal.funnel_role == FunnelRole.CONVERSION:
            # Direct conversion potential
            conversion_rate = 0.03  # 3% for conversion pages
            value_per_click = conversion_rate * self.aov * self.margin
        elif proposal.funnel_role == FunnelRole.CONSIDERATION:
            # Assist value
            conversion_rate = 0.01  # 1% assist
            value_per_click = conversion_rate * self.aov * self.margin * 0.5  # 50% attribution
        else:  # AWARENESS
            # Top of funnel
            conversion_rate = 0.005  # 0.5% eventual conversion
            value_per_click = conversion_rate * self.aov * self.margin * 0.25  # 25% attribution

        monthly_value = estimated_monthly_clicks * value_per_click
        annual_value = monthly_value * 12

        # Confidence based on demand signal strength
        confidence = 0.5  # Base confidence for new assets is lower

        if demand_signal.monthly_impressions >= 2000:
            confidence += 0.1
        if len(demand_signal.related_queries) >= 5:
            confidence += 0.1
        if demand_signal.competition_level == "low":
            confidence += 0.1

        confidence = min(0.80, confidence)  # Cap at 0.80 for new assets

        return annual_value, confidence

    def evaluate(
        self,
        proposal: AssetProposal,
        existing_assets: list[PageAsset],
    ) -> AssetCreationResult:
        """
        Full evaluation of asset creation proposal.

        Implements mandatory gate with multiple checks.
        """
        # 1. Check demand signal
        demand_signal = self.check_demand_signal(
            proposal.target_queries,
            existing_assets,
        )

        if demand_signal is None:
            return AssetCreationResult(
                proposal=proposal,
                gate_result=AssetCreationGateResult.REJECTED_NO_DEMAND,
                demand_signal=None,
                expected_value=0,
                confidence=0,
                risk_level="high",
                cannibalization_risk=[],
                implementation_steps=[],
                rejection_reason=f"Insufficient demand signal. Need ≥{self.MIN_IMPRESSIONS_FOR_DEMAND} monthly impressions.",
                recommended_action="NO_ACTION",
            )

        # 2. Check cannibalization risk
        has_cannibalization, at_risk_urls = self.check_cannibalization_risk(
            proposal,
            existing_assets,
        )

        if has_cannibalization:
            return AssetCreationResult(
                proposal=proposal,
                gate_result=AssetCreationGateResult.REJECTED_CANNIBALIZATION,
                demand_signal=demand_signal,
                expected_value=0,
                confidence=0,
                risk_level="high",
                cannibalization_risk=at_risk_urls,
                implementation_steps=[],
                rejection_reason=f"High cannibalization risk with {len(at_risk_urls)} existing pages: {at_risk_urls[:3]}",
                recommended_action="NO_ACTION",
            )

        # 3. Blog/guide validation
        if proposal.asset_type in (AssetType.BLOG, AssetType.OTHER):
            is_valid, rejection_reason = self.validate_blog_guide(proposal)
            if not is_valid:
                return AssetCreationResult(
                    proposal=proposal,
                    gate_result=AssetCreationGateResult.REJECTED_BLOG_VALIDATION,
                    demand_signal=demand_signal,
                    expected_value=0,
                    confidence=0,
                    risk_level="medium",
                    cannibalization_risk=[],
                    implementation_steps=[],
                    rejection_reason=rejection_reason,
                    recommended_action="NO_ACTION",
                )

        # 4. Calculate expected value
        expected_value, confidence = self.calculate_expected_value(
            demand_signal,
            proposal,
        )

        # 5. Check ROI gate (5× cost)
        if proposal.estimated_creation_cost > 0:
            roi_multiple = expected_value / proposal.estimated_creation_cost
            if roi_multiple < self.MIN_ROI_MULTIPLE:
                return AssetCreationResult(
                    proposal=proposal,
                    gate_result=AssetCreationGateResult.REJECTED_INSUFFICIENT_ROI,
                    demand_signal=demand_signal,
                    expected_value=expected_value,
                    confidence=confidence,
                    risk_level="medium",
                    cannibalization_risk=[],
                    implementation_steps=[],
                    rejection_reason=f"ROI multiple {roi_multiple:.1f}× below required {self.MIN_ROI_MULTIPLE}×",
                    recommended_action="NO_ACTION",
                )

        # All gates passed
        implementation_steps = [
            f"1. Create {proposal.asset_type.value} page at {proposal.proposed_url}",
            f"2. Target primary query: '{demand_signal.primary_query}'",
            f"3. Include {proposal.estimated_word_count} words of quality content",
            f"4. Add internal links from related {proposal.funnel_role.value} pages",
            "5. Submit to Search Console for indexing",
            "6. Monitor rankings for target queries over 28 days",
        ]

        # Risk level based on confidence
        if confidence >= 0.70:
            risk_level = "low"
        elif confidence >= 0.60:
            risk_level = "medium"
        else:
            risk_level = "high"

        return AssetCreationResult(
            proposal=proposal,
            gate_result=AssetCreationGateResult.APPROVED,
            demand_signal=demand_signal,
            expected_value=expected_value,
            confidence=confidence,
            risk_level=risk_level,
            cannibalization_risk=[],
            implementation_steps=implementation_steps,
            rejection_reason=None,
            recommended_action="NEW_ASSET_CREATION",
        )

    def find_content_gaps(
        self,
        existing_assets: list[PageAsset],
        min_impressions: int = 1000,
    ) -> list[dict]:
        """
        Find potential content gaps based on query analysis.

        Looks for high-impression queries where we don't have dedicated content.
        """
        # Collect all queries and their best-ranking page
        query_coverage: dict[str, dict] = {}

        for asset in existing_assets:
            if not asset.gsc.top_queries:
                continue

            for query_data in asset.gsc.top_queries:
                query = query_data.query.lower()

                if query not in query_coverage:
                    query_coverage[query] = {
                        "query": query_data.query,
                        "impressions": query_data.impressions,
                        "best_position": query_data.avg_position,
                        "best_url": asset.url,
                        "clicks": query_data.clicks,
                    }
                else:
                    # Update if this page ranks better
                    if query_data.avg_position < query_coverage[query]["best_position"]:
                        query_coverage[query]["best_position"] = query_data.avg_position
                        query_coverage[query]["best_url"] = asset.url
                    query_coverage[query]["impressions"] += query_data.impressions
                    query_coverage[query]["clicks"] += query_data.clicks

        # Find gaps: high impressions but poor position
        gaps = []
        for query, data in query_coverage.items():
            if data["impressions"] >= min_impressions and data["best_position"] > 20:
                gaps.append({
                    "query": data["query"],
                    "impressions": data["impressions"],
                    "current_position": data["best_position"],
                    "current_best_page": data["best_url"],
                    "opportunity_type": "content_gap",
                    "suggested_action": "Consider dedicated content for this query",
                })

        # Sort by impressions
        gaps.sort(key=lambda x: x["impressions"], reverse=True)
        return gaps[:10]  # Top 10 gaps
