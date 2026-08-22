"""
Demand Analyzer

Combines GSC, GA4, and Google Ads data to assess true demand.

Key Principles:
1. Absence of sales is never evidence of absence of demand
2. Impressions = demand signal
3. Ads data anchors monetization evidence but doesn't gatekeep
4. When Ads data is absent, GSC + GA4 remain sufficient

Data Hierarchy:
- When Ads data exists: anchors monetization, GSC validates pressure, GA4 validates behavior
- When Ads data absent: GSC + GA4 sufficient for Search validation tests
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset
from src.data_sources.google_ads_client import AdsAccountData


class DemandSignalStrength(str, Enum):
    """Strength of demand signal."""
    STRONG = "strong"       # Multiple sources confirm demand
    MODERATE = "moderate"   # One source shows demand
    WEAK = "weak"          # Minimal signals
    UNKNOWN = "unknown"    # No data


class MonetizationStatus(str, Enum):
    """Current monetization status."""
    FULLY_MONETIZED = "fully_monetized"      # Ads running, good IS
    PARTIALLY_MONETIZED = "partially_monetized"  # Ads running, IS constrained
    NOT_MONETIZED = "not_monetized"          # No ads for these queries
    PMAX_ABSORBED = "pmax_absorbed"          # PMax capturing, not Search


@dataclass
class QueryDemandSignal:
    """
    Demand signal for a specific query.

    Combines data from GSC and Ads to understand true demand.
    """
    query: str

    # GSC signals (demand pressure)
    gsc_impressions: int = 0
    gsc_clicks: int = 0
    gsc_position: float = 0.0
    gsc_ctr: float = 0.0

    # Ads signals (monetization evidence)
    ads_impressions: int = 0
    ads_clicks: int = 0
    ads_cost: float = 0.0
    ads_conversions: float = 0.0
    ads_conversion_value: float = 0.0
    ads_impression_share: Optional[float] = None
    ads_lost_is_budget: Optional[float] = None
    ads_lost_is_rank: Optional[float] = None
    ads_cpc: float = 0.0

    # Computed
    is_brand: bool = False
    is_pmax_absorbed: bool = False

    @property
    def has_ads_data(self) -> bool:
        """Check if we have Ads data for this query."""
        return self.ads_impressions > 0 or self.ads_cost > 0

    @property
    def has_gsc_data(self) -> bool:
        """Check if we have GSC data for this query."""
        return self.gsc_impressions > 0

    @property
    def total_search_impressions(self) -> int:
        """Combined search impressions (organic + paid)."""
        return self.gsc_impressions + self.ads_impressions

    @property
    def estimated_market_size(self) -> Optional[int]:
        """
        Estimate total market size from impression share.

        If we know our impression share, we can estimate total market.
        """
        if self.ads_impression_share and self.ads_impression_share > 0:
            return int(self.ads_impressions / self.ads_impression_share)
        return None

    @property
    def capture_rate(self) -> Optional[float]:
        """
        What % of total market are we capturing (organic + paid)?
        """
        market = self.estimated_market_size
        if market and market > 0:
            return min(1.0, self.total_search_impressions / market)
        return None

    @property
    def monetization_status(self) -> MonetizationStatus:
        """Determine monetization status."""
        if not self.has_ads_data:
            return MonetizationStatus.NOT_MONETIZED

        if self.is_pmax_absorbed:
            return MonetizationStatus.PMAX_ABSORBED

        # Check impression share
        if self.ads_impression_share is not None:
            if self.ads_impression_share >= 0.8:
                return MonetizationStatus.FULLY_MONETIZED
            else:
                return MonetizationStatus.PARTIALLY_MONETIZED

        # If we have ads but no IS data, assume partially monetized
        return MonetizationStatus.PARTIALLY_MONETIZED

    @property
    def demand_strength(self) -> DemandSignalStrength:
        """Assess overall demand strength."""
        signals = 0

        # GSC impressions signal demand
        if self.gsc_impressions >= 1000:
            signals += 2
        elif self.gsc_impressions >= 100:
            signals += 1

        # Ads data confirms commercial value
        if self.ads_conversions > 0:
            signals += 2
        elif self.ads_clicks > 0:
            signals += 1

        # CPC indicates competitive value
        if self.ads_cpc >= 2.0:
            signals += 1

        if signals >= 4:
            return DemandSignalStrength.STRONG
        elif signals >= 2:
            return DemandSignalStrength.MODERATE
        elif signals >= 1:
            return DemandSignalStrength.WEAK
        return DemandSignalStrength.UNKNOWN


@dataclass
class DemandAnalysis:
    """
    Complete demand analysis for a page/asset.

    Combines signals from all queries associated with the page.
    """
    url: str

    # Query-level signals
    query_signals: list[QueryDemandSignal] = field(default_factory=list)

    # Aggregated metrics
    total_gsc_impressions: int = 0
    total_gsc_clicks: int = 0
    total_ads_impressions: int = 0
    total_ads_conversions: float = 0.0
    total_ads_value: float = 0.0

    # Derived scores (0-1)
    demand_score: float = 0.0          # Based on impressions/market size
    monetization_score: float = 0.0     # Based on ads coverage
    coverage_gap_score: float = 0.0     # How much demand we're missing

    # Constraints detected
    has_impression_share_constraint: bool = False
    has_pmax_absorption: bool = False
    has_coverage_gap: bool = False

    # Evidence
    evidence: dict = field(default_factory=dict)

    @property
    def has_ads_data(self) -> bool:
        """Check if any query has Ads data."""
        return any(q.has_ads_data for q in self.query_signals)

    @property
    def top_queries(self) -> list[QueryDemandSignal]:
        """Get top queries by impression volume."""
        return sorted(
            self.query_signals,
            key=lambda q: q.total_search_impressions,
            reverse=True
        )[:10]

    @property
    def monetized_queries(self) -> list[QueryDemandSignal]:
        """Get queries with active monetization."""
        return [q for q in self.query_signals if q.has_ads_data]

    @property
    def unmonetized_queries(self) -> list[QueryDemandSignal]:
        """Get queries without ads coverage."""
        return [q for q in self.query_signals if not q.has_ads_data and q.has_gsc_data]


class DemandAnalyzer:
    """
    Analyzes demand by combining GSC, GA4, and Ads data.

    Key role: Answer these questions:
    1. Are there commercial queries already monetizing?
    2. Is Search coverage constrained or absent for known intent?
    3. Is PMax absorbing exact or brand intent?
    4. Is impression share indicating budget or rank suppression?

    What Ads data must NEVER be used for:
    - Inferring lack of demand
    - Judging product worth
    - Excluding products from analysis
    """

    def __init__(
        self,
        ads_data: Optional[AdsAccountData] = None,
        brand_terms: Optional[list[str]] = None,
    ):
        self.ads_data = ads_data
        self.brand_terms = [t.lower() for t in (brand_terms or [])]

    def analyze_page(self, asset: PageAsset) -> DemandAnalysis:
        """
        Analyze demand for a single page.

        Combines GSC queries with Ads data to understand true demand.
        """
        analysis = DemandAnalysis(url=asset.url)

        # Build query signals from GSC data
        if asset.gsc.top_queries:
            for gsc_query in asset.gsc.top_queries:
                signal = QueryDemandSignal(
                    query=gsc_query.query,
                    gsc_impressions=gsc_query.impressions,
                    gsc_clicks=gsc_query.clicks,
                    gsc_position=gsc_query.position,
                    gsc_ctr=gsc_query.ctr,
                )

                # Enrich with Ads data if available
                if self.ads_data:
                    self._enrich_with_ads_data(signal)

                analysis.query_signals.append(signal)

        # Aggregate metrics
        self._aggregate_metrics(analysis)

        # Compute scores
        self._compute_scores(analysis, asset)

        # Detect constraints
        self._detect_constraints(analysis)

        return analysis

    def _enrich_with_ads_data(self, signal: QueryDemandSignal):
        """Enrich query signal with Ads data."""
        if not self.ads_data:
            return

        ads_query = self.ads_data.get_query_data(signal.query)
        if ads_query:
            signal.ads_impressions = ads_query.impressions
            signal.ads_clicks = ads_query.clicks
            signal.ads_cost = ads_query.cost
            signal.ads_conversions = ads_query.conversions
            signal.ads_conversion_value = ads_query.conversion_value
            signal.ads_impression_share = ads_query.search_impression_share
            signal.ads_lost_is_budget = ads_query.search_lost_is_budget
            signal.ads_lost_is_rank = ads_query.search_lost_is_rank
            signal.ads_cpc = ads_query.cpc
            signal.is_brand = ads_query.is_brand_query

        # Check PMax absorption
        if self.ads_data.is_pmax_absorbing_query(signal.query):
            signal.is_pmax_absorbed = True

    def _aggregate_metrics(self, analysis: DemandAnalysis):
        """Aggregate metrics across all queries."""
        analysis.total_gsc_impressions = sum(q.gsc_impressions for q in analysis.query_signals)
        analysis.total_gsc_clicks = sum(q.gsc_clicks for q in analysis.query_signals)
        analysis.total_ads_impressions = sum(q.ads_impressions for q in analysis.query_signals)
        analysis.total_ads_conversions = sum(q.ads_conversions for q in analysis.query_signals)
        analysis.total_ads_value = sum(q.ads_conversion_value for q in analysis.query_signals)

    def _compute_scores(self, analysis: DemandAnalysis, asset: PageAsset):
        """Compute demand and monetization scores."""
        # Demand score based on GSC impressions (primary demand signal)
        # Impressions = demand. Period.
        imps = analysis.total_gsc_impressions
        if imps >= 10000:
            analysis.demand_score = 1.0
        elif imps >= 5000:
            analysis.demand_score = 0.8
        elif imps >= 1000:
            analysis.demand_score = 0.6
        elif imps >= 500:
            analysis.demand_score = 0.4
        elif imps >= 100:
            analysis.demand_score = 0.2
        else:
            analysis.demand_score = 0.1  # Never zero - absence of data isn't absence of demand

        # Monetization score based on Ads coverage
        if analysis.has_ads_data:
            monetized = len(analysis.monetized_queries)
            total = len(analysis.query_signals)
            if total > 0:
                # What % of our queries have ads?
                coverage = monetized / total

                # Weight by impression share
                avg_is = 0.0
                is_count = 0
                for q in analysis.monetized_queries:
                    if q.ads_impression_share is not None:
                        avg_is += q.ads_impression_share
                        is_count += 1

                if is_count > 0:
                    avg_is /= is_count
                    analysis.monetization_score = coverage * avg_is
                else:
                    analysis.monetization_score = coverage * 0.5  # Assume 50% IS if unknown
        else:
            # No Ads data is neutral, not negative
            analysis.monetization_score = 0.0  # Unknown, not bad

        # Coverage gap score - how much demand are we NOT capturing?
        if analysis.query_signals:
            total_market = 0
            total_captured = 0

            for q in analysis.query_signals:
                market = q.estimated_market_size
                if market:
                    total_market += market
                    total_captured += q.total_search_impressions
                else:
                    # No market estimate - use GSC impressions as baseline
                    total_market += q.gsc_impressions
                    total_captured += q.gsc_impressions

            if total_market > 0:
                analysis.coverage_gap_score = 1.0 - (total_captured / total_market)
            else:
                analysis.coverage_gap_score = 0.0

        # Store evidence
        analysis.evidence = {
            "gsc_impressions": analysis.total_gsc_impressions,
            "gsc_clicks": analysis.total_gsc_clicks,
            "ads_impressions": analysis.total_ads_impressions,
            "ads_conversions": analysis.total_ads_conversions,
            "ads_value": analysis.total_ads_value,
            "monetized_query_count": len(analysis.monetized_queries),
            "total_query_count": len(analysis.query_signals),
            "has_ads_data": analysis.has_ads_data,
        }

    def _detect_constraints(self, analysis: DemandAnalysis):
        """Detect constraints from Ads data."""
        # Impression share constraint
        for q in analysis.query_signals:
            if q.ads_impression_share is not None and q.ads_impression_share < 0.7:
                analysis.has_impression_share_constraint = True
                break

        # PMax absorption
        for q in analysis.query_signals:
            if q.is_pmax_absorbed:
                analysis.has_pmax_absorption = True
                break

        # Coverage gap (high demand, no monetization)
        high_demand_unmonetized = [
            q for q in analysis.query_signals
            if q.gsc_impressions >= 1000 and not q.has_ads_data
        ]
        if high_demand_unmonetized:
            analysis.has_coverage_gap = True

    def get_demand_summary(self, analysis: DemandAnalysis) -> dict:
        """Get human-readable demand summary."""
        return {
            "demand_strength": self._classify_demand_strength(analysis),
            "demand_score": round(analysis.demand_score, 2),
            "monetization_score": round(analysis.monetization_score, 2),
            "coverage_gap": round(analysis.coverage_gap_score, 2),
            "total_impressions": analysis.total_gsc_impressions,
            "has_ads_data": analysis.has_ads_data,
            "constraints": {
                "impression_share_limited": analysis.has_impression_share_constraint,
                "pmax_absorbing": analysis.has_pmax_absorption,
                "coverage_gap": analysis.has_coverage_gap,
            },
            "top_queries": [
                {
                    "query": q.query,
                    "impressions": q.total_search_impressions,
                    "monetized": q.has_ads_data,
                    "demand_strength": q.demand_strength.value,
                }
                for q in analysis.top_queries[:5]
            ],
        }

    def _classify_demand_strength(self, analysis: DemandAnalysis) -> str:
        """Classify overall demand strength."""
        if analysis.demand_score >= 0.8:
            return "strong"
        elif analysis.demand_score >= 0.4:
            return "moderate"
        elif analysis.demand_score >= 0.2:
            return "weak"
        return "minimal"
