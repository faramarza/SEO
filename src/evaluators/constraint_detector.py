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
F. Paid Search Constraints - Impression share, budget, rank limitations

Google Ads Data Role:
- When available: Anchors monetization evidence
- When absent: GSC + GA4 remain sufficient (neutral, not negative)
- NEVER used to infer lack of demand or exclude products
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType
from src.data_sources.google_ads_client import AdsAccountData


class ConstraintType(str, Enum):
    """Types of constraints blocking demand capture."""
    VISIBILITY_BLOCKED = "visibility_blocked"  # High impressions, poor position
    CTR_SUPPRESSED = "ctr_suppressed"  # Good position but low CTR (title/snippet issue)
    INTENT_MISMATCH = "intent_mismatch"  # Traffic but no engagement
    COVERAGE_GAP = "coverage_gap"  # Demand exists, no monetization path
    CANNIBALIZATION = "cannibalization"  # Multiple pages competing
    ECONOMIC_INVALID = "economic_invalid"  # Economics don't work
    # Ads-specific constraints
    WEAK_FUNNEL_ROUTING = "weak_funnel_routing"  # Blog with traffic but no downstream routing
    # Ads-specific constraints
    IMPRESSION_SHARE_BUDGET = "impression_share_budget"  # Lost IS due to budget
    IMPRESSION_SHARE_RANK = "impression_share_rank"  # Lost IS due to rank
    PMAX_ABSORPTION = "pmax_absorption"  # PMax absorbing Search traffic
    PAID_COVERAGE_GAP = "paid_coverage_gap"  # High demand, no paid coverage
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

    # Ads-enriched fields
    has_ads_data: bool = False
    monetization_score: float = 0.0  # 0-1, based on ads coverage
    coverage_gap_score: float = 0.0  # 0-1, how much demand we're missing
    ads_constraints: list[ConstraintSignal] = field(default_factory=list)


class ConstraintDetector:
    """
    Detects constraints blocking demand capture.

    Does NOT judge products by performance.
    Does NOT infer lack of demand from lack of sales.

    Google Ads Integration:
    - When Ads data exists: anchors monetization evidence
    - When Ads data absent: GSC + GA4 remain sufficient
    - Ads data is supporting evidence, not a gatekeeper
    - NEVER uses Ads data to infer lack of demand or exclude products
    """

    def __init__(
        self,
        aov: float = 53.19,
        margin: float = 0.27,
        min_economic_profit: float = 5.0,  # Minimum profit per order to be viable
        ads_data: Optional[AdsAccountData] = None,
    ):
        self.aov = aov
        self.margin = margin
        self.min_economic_profit = min_economic_profit
        self.ads_data = ads_data  # Optional - absence is neutral, not negative

        # Expected CTR by position (approximate industry benchmarks)
        self.expected_ctr_by_position = {
            1: 0.30, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05,
            6: 0.04, 7: 0.03, 8: 0.025, 9: 0.02, 10: 0.015,
        }

    def set_ads_data(self, ads_data: AdsAccountData):
        """Set or update Ads data for analysis."""
        self.ads_data = ads_data

    def evaluate(self, asset: PageAsset, all_assets: list[PageAsset] = None) -> ConstraintResult:
        """
        Detect constraints for a single page.

        Evaluates all dimensions independently - no single dimension nullifies others.

        Blog/Guide pages get additional routing constraint evaluation:
        - Traffic must not increase value without funnel contribution
        - Weak routing is flagged as a constraint
        """
        constraints = []
        ads_constraints = []

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

        # E2. Blog routing constraints
        if asset.asset_type == AssetType.BLOG:
            routing_constraints = self._evaluate_blog_routing(asset, all_assets or [])
            constraints.extend(routing_constraints)

        # F. Ads Constraints (if Ads data available)
        has_ads_data = False
        monetization_score = 0.0
        coverage_gap_score = 0.0

        if self.ads_data:
            ads_result = self._evaluate_ads_constraints(asset)
            has_ads_data = ads_result["has_data"]
            monetization_score = ads_result["monetization_score"]
            coverage_gap_score = ads_result["coverage_gap_score"]
            ads_constraints = ads_result["constraints"]

            # Ads constraints are additive, not replacing
            constraints.extend(ads_constraints)

            # Boost confidence if Ads data validates demand
            if has_ads_data and ads_result.get("confirms_demand"):
                demand_score = min(1.0, demand_score * 1.2)

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

        # Ads data increases confidence if present
        if has_ads_data:
            confidence = min(0.95, confidence + 0.1)

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
            has_ads_data=has_ads_data,
            monetization_score=monetization_score,
            coverage_gap_score=coverage_gap_score,
            ads_constraints=ads_constraints,
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
        Evaluate commercial intent from behavior AND query signals.

        Intent is semantic and behavioral, not financial.
        Blogs with commercial-informational queries (e.g. "best X for Y")
        have real intent even with zero GA4 conversions.
        """
        score = 0.0

        # Asset type implies baseline intent
        if asset.asset_type == AssetType.PRODUCT:
            score += 0.4  # Product pages have inherent commercial intent
        elif asset.asset_type == AssetType.CATEGORY:
            score += 0.3
        elif asset.asset_type == AssetType.BLOG:
            score += 0.05  # Blogs have minimal baseline, but queries can boost

        # GSC query commercial intent signals
        # Queries reveal what users actually want — this is the strongest
        # semantic signal for blogs that lack GA4 conversion data.
        if asset.gsc.top_queries:
            commercial_patterns = (
                "best", "buy", "review", "top", "vs", "compare", "price",
                "cheap", "affordable", "worth", "recommend", "guide",
                "for toddlers", "for kids", "for baby", "for children",
                "gift", "set", "kit",
            )
            total_impressions = sum(q.impressions for q in asset.gsc.top_queries)
            commercial_impressions = 0
            for q in asset.gsc.top_queries:
                query_lower = q.query.lower()
                if any(p in query_lower for p in commercial_patterns):
                    commercial_impressions += q.impressions
            if total_impressions > 0:
                commercial_ratio = commercial_impressions / total_impressions
                # Up to 0.35 from query intent (significant weight)
                score += commercial_ratio * 0.35

        # GA4 engagement signals
        if asset.ga4.sessions_28d > 0:
            engagement_rate = asset.ga4.engaged_sessions_28d / asset.ga4.sessions_28d
            score += engagement_rate * 0.2

        # Add-to-cart behavior (strongest behavioral intent signal)
        if asset.ga4.add_to_carts_28d > 0:
            score += 0.25

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
        actual_ctr = asset.gsc.ctr_28d

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
                    competing_display = ", ".join(competing_pages[:3])
                    constraints.append(ConstraintSignal(
                        constraint_type=ConstraintType.CANNIBALIZATION,
                        severity="medium",
                        description=(
                            f"Query '{top_query}' also targets {len(competing_pages)} other page(s): "
                            f"{competing_display}"
                        ),
                        evidence={
                            "query": top_query,
                            "competing_pages": competing_pages[:3],
                        },
                        recommended_action=(
                            f"Review competing page(s) ({competing_display}) and either "
                            f"consolidate content or differentiate targeting for '{top_query}'"
                        ),
                        reversibility="slow",
                    ))

        return constraints

    def _evaluate_ads_constraints(self, asset: PageAsset) -> dict:
        """
        Evaluate constraints from Google Ads data.

        This method answers:
        1. Are there commercial queries already monetizing?
        2. Is Search coverage constrained or absent for known intent?
        3. Is PMax absorbing exact or brand intent?
        4. Is impression share indicating budget or rank suppression?

        IMPORTANT: Absence of Ads data is NEUTRAL, not negative.
        Ads data anchors monetization evidence but doesn't gatekeep.
        """
        result = {
            "has_data": False,
            "monetization_score": 0.0,
            "coverage_gap_score": 0.0,
            "constraints": [],
            "confirms_demand": False,
        }

        if not self.ads_data:
            return result

        if not asset.gsc.top_queries:
            return result

        # Analyze queries for this page
        queries_with_ads = []
        queries_without_ads = []
        total_gsc_impressions = 0
        total_is_lost_budget = 0.0
        total_is_lost_rank = 0.0
        is_count = 0
        pmax_absorbed_queries = []

        for gsc_query in asset.gsc.top_queries:
            query_text = gsc_query.query.lower()
            total_gsc_impressions += gsc_query.impressions

            ads_query = self.ads_data.get_query_data(query_text)

            if ads_query:
                result["has_data"] = True
                queries_with_ads.append({
                    "query": query_text,
                    "ads_impressions": ads_query.impressions,
                    "ads_conversions": ads_query.conversions,
                    "ads_cpc": ads_query.cpc,
                    "impression_share": ads_query.search_impression_share,
                })

                # Track IS losses
                if ads_query.search_lost_is_budget:
                    total_is_lost_budget += ads_query.search_lost_is_budget
                    is_count += 1
                if ads_query.search_lost_is_rank:
                    total_is_lost_rank += ads_query.search_lost_is_rank
                    is_count += 1

                # Conversions confirm demand
                if ads_query.conversions > 0:
                    result["confirms_demand"] = True

            else:
                queries_without_ads.append({
                    "query": query_text,
                    "gsc_impressions": gsc_query.impressions,
                })

            # Check PMax absorption
            if self.ads_data.is_pmax_absorbing_query(query_text):
                pmax_absorbed_queries.append(query_text)

        # Calculate monetization score
        if asset.gsc.top_queries:
            total_queries = len(asset.gsc.top_queries)
            monetized = len(queries_with_ads)
            result["monetization_score"] = monetized / total_queries if total_queries > 0 else 0.0

        # Calculate coverage gap
        high_volume_unmonetized = [
            q for q in queries_without_ads
            if q["gsc_impressions"] >= 500
        ]
        if high_volume_unmonetized and total_gsc_impressions > 0:
            unmonetized_impressions = sum(q["gsc_impressions"] for q in high_volume_unmonetized)
            result["coverage_gap_score"] = unmonetized_impressions / total_gsc_impressions

        constraints = []

        # CONSTRAINT: Budget-constrained impression share
        if is_count > 0:
            avg_is_lost_budget = total_is_lost_budget / is_count
            if avg_is_lost_budget >= 0.15:  # Losing 15%+ to budget
                severity = "critical" if avg_is_lost_budget >= 0.3 else "high"
                constraints.append(ConstraintSignal(
                    constraint_type=ConstraintType.IMPRESSION_SHARE_BUDGET,
                    severity=severity,
                    description=f"Losing {avg_is_lost_budget*100:.0f}% impression share due to budget constraints",
                    evidence={
                        "avg_is_lost_to_budget": round(avg_is_lost_budget, 3),
                        "queries_analyzed": is_count,
                        "sample_queries": [q["query"] for q in queries_with_ads[:3]],
                    },
                    recommended_action="Consider increasing Search budget to capture more demand",
                    reversibility="immediate",
                ))

        # CONSTRAINT: Rank-constrained impression share
        if is_count > 0:
            avg_is_lost_rank = total_is_lost_rank / is_count
            if avg_is_lost_rank >= 0.15:  # Losing 15%+ to rank
                severity = "high" if avg_is_lost_rank >= 0.3 else "medium"
                constraints.append(ConstraintSignal(
                    constraint_type=ConstraintType.IMPRESSION_SHARE_RANK,
                    severity=severity,
                    description=f"Losing {avg_is_lost_rank*100:.0f}% impression share due to ad rank",
                    evidence={
                        "avg_is_lost_to_rank": round(avg_is_lost_rank, 3),
                        "queries_analyzed": is_count,
                    },
                    recommended_action="Improve Quality Score or increase bids to capture more demand",
                    reversibility="slow",
                ))

        # CONSTRAINT: PMax absorbing Search traffic
        if pmax_absorbed_queries:
            constraints.append(ConstraintSignal(
                constraint_type=ConstraintType.PMAX_ABSORPTION,
                severity="medium",
                description=f"PMax is absorbing {len(pmax_absorbed_queries)} queries that could run in Search",
                evidence={
                    "absorbed_queries": pmax_absorbed_queries[:5],
                    "total_absorbed": len(pmax_absorbed_queries),
                },
                recommended_action="Review PMax brand exclusions and Search campaign coverage",
                reversibility="immediate",
            ))

        # CONSTRAINT: High demand, no paid coverage
        if high_volume_unmonetized and len(high_volume_unmonetized) >= 3:
            total_unmon_impressions = sum(q["gsc_impressions"] for q in high_volume_unmonetized)
            if total_unmon_impressions >= 2000:
                constraints.append(ConstraintSignal(
                    constraint_type=ConstraintType.PAID_COVERAGE_GAP,
                    severity="medium",
                    description=f"{len(high_volume_unmonetized)} high-volume queries ({total_unmon_impressions:,} impressions) have no paid coverage",
                    evidence={
                        "unmonetized_queries": [q["query"] for q in high_volume_unmonetized[:5]],
                        "total_impressions": total_unmon_impressions,
                    },
                    recommended_action="Consider adding these queries to Search campaigns",
                    reversibility="immediate",
                ))

        result["constraints"] = constraints
        return result

    def _evaluate_blog_routing(
        self,
        asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> list[ConstraintSignal]:
        """
        Evaluate routing quality for blog/guide pages.

        Blogs are routing infrastructure, not revenue assets.
        A blog with traffic but no measurable routing to revenue assets
        has LOW value regardless of traffic.
        """
        constraints = []

        # Count outlinks to revenue pages (products/categories)
        # We approximate using the outlinks field and asset type distribution
        revenue_pages = [a for a in all_assets
                         if a.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY)]
        total_revenue_pages = len(revenue_pages)

        # If blog has meaningful traffic but few outlinks, it's poorly routed
        has_traffic = asset.ga4.sessions_28d >= 10 or asset.gsc.impressions_28d >= 500
        has_few_outlinks = asset.outlinks < 3

        if has_traffic and has_few_outlinks:
            severity = "high" if asset.gsc.impressions_28d >= 2000 else "medium"
            constraints.append(ConstraintSignal(
                constraint_type=ConstraintType.WEAK_FUNNEL_ROUTING,
                severity=severity,
                description=(
                    f"Blog has {asset.gsc.impressions_28d:,} impressions and "
                    f"{asset.ga4.sessions_28d} sessions but only {asset.outlinks} outlinks. "
                    f"Traffic without routing to revenue pages has low value."
                ),
                evidence={
                    "impressions": asset.gsc.impressions_28d,
                    "sessions": asset.ga4.sessions_28d,
                    "outlinks": asset.outlinks,
                    "total_revenue_pages_on_site": total_revenue_pages,
                },
                recommended_action=(
                    "Add internal links to relevant product/category pages. "
                    "Every blog must have at least one primary 'next step' link."
                ),
                reversibility="immediate",
            ))

        # If blog has traffic but no engagement, routing won't help much
        if has_traffic and asset.ga4.engagement_rate_28d < 0.2 and asset.ga4.sessions_28d > 0:
            constraints.append(ConstraintSignal(
                constraint_type=ConstraintType.INTENT_MISMATCH,
                severity="medium",
                description=(
                    f"Blog has traffic ({asset.ga4.sessions_28d} sessions) but "
                    f"very low engagement ({asset.ga4.engagement_rate_28d:.0%}). "
                    f"Users are not engaging with content, limiting routing effectiveness."
                ),
                evidence={
                    "sessions": asset.ga4.sessions_28d,
                    "engagement_rate": asset.ga4.engagement_rate_28d,
                    "bounce_rate": asset.ga4.bounce_rate_28d,
                },
                recommended_action="Re-order sections to surface intent earlier. Review content relevance.",
                reversibility="immediate",
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

        More data = higher confidence. Missing data = lower confidence.
        A page with 0 GA4 sessions, 0% intent, and 1 click should NOT
        be 85% confident — that's a data-poor assessment.
        """
        confidence = 0.5  # Base

        # ── Rewards: more data → higher confidence ──
        # Impressions (demand signal strength)
        if asset.gsc.impressions_28d >= 5000:
            confidence += 0.15
        elif asset.gsc.impressions_28d >= 1000:
            confidence += 0.08
        elif asset.gsc.impressions_28d >= 100:
            confidence += 0.03

        # Clicks (behavioral validation)
        if asset.gsc.clicks_28d >= 100:
            confidence += 0.1
        elif asset.gsc.clicks_28d >= 20:
            confidence += 0.05

        # GA4 data present (on-site behavior observed)
        if asset.ga4.sessions_28d > 0:
            confidence += 0.1

        # Query data richness
        if asset.gsc.top_queries and len(asset.gsc.top_queries) >= 3:
            confidence += 0.05

        # ── Penalties: missing data → lower confidence ──
        # No GA4 sessions means we have ZERO on-site behavior data
        if asset.ga4.sessions_28d == 0:
            confidence -= 0.1

        # Very low intent means we don't understand user needs well
        if intent_score < 0.1:
            confidence -= 0.1
        elif intent_score < 0.2:
            confidence -= 0.05

        # Very few clicks means behavioral signal is weak
        if asset.gsc.clicks_28d < 5:
            confidence -= 0.1

        # No query data means we can't assess search intent
        if not asset.gsc.top_queries:
            confidence -= 0.05

        # Floor at 0.2 (never zero — we still have URL + type)
        return min(max(confidence, 0.2), 0.95)
