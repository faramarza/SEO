"""
Opportunity metrics for the Agentic Organic Growth Governor.

Implements:
- Click-Upside Table (position → expected CTR gain)
- Visibility Gap calculation
- DemandScore (intent classification)
- ConversionProxy for non-converting pages
- EVUV (Expected Visibility Uplift Value)
- Assist Value (AV) for blogs/guides
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import re

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType


# ============================================================================
# CLICK-UPSIDE TABLE
# ============================================================================
# Based on aggregated CTR curves. Position improvement → expected CTR gain.
# Source: Industry benchmarks, conservative estimates.

# Expected CTR by position (organic, non-branded)
POSITION_CTR = {
    1: 0.280,   # 28% CTR at position 1
    2: 0.155,   # 15.5%
    3: 0.110,   # 11%
    4: 0.080,   # 8%
    5: 0.065,   # 6.5%
    6: 0.050,   # 5%
    7: 0.040,   # 4%
    8: 0.032,   # 3.2%
    9: 0.026,   # 2.6%
    10: 0.022,  # 2.2%
    15: 0.012,  # 1.2%
    20: 0.008,  # 0.8%
    30: 0.004,  # 0.4%
    50: 0.002,  # 0.2%
    100: 0.001, # 0.1%
}


def _measured_econ():
    """The store's OWN measured AOV/CVR (cached per evaluation), so value estimates
    use real economics instead of the hardcoded 53.19 / 2%. Falls back to those
    constants only when no evaluation exists yet."""
    try:
        from src.analysis.site_benchmarks import compute_site_benchmarks
        bm = compute_site_benchmarks()
        if bm.get("available"):
            return (bm.get("site_aov") or 53.19, bm.get("site_cvr") or 0.02)
    except Exception:
        pass
    return (53.19, 0.02)


def get_expected_ctr(position: float) -> float:
    """
    Get expected CTR for a given position — from the SITE'S OWN measured CTR curve
    when available (via site_benchmarks), falling back to the industry curve below.
    """
    if position <= 0:
        return 0.0
    try:
        from src.analysis.site_benchmarks import site_expected_ctr
        v = site_expected_ctr(position)
        if v and v > 0:
            return v
    except Exception:
        pass
    if position <= 1:
        return POSITION_CTR[1]

    # Find surrounding positions
    positions = sorted(POSITION_CTR.keys())

    for i, pos in enumerate(positions):
        if position <= pos:
            if i == 0:
                return POSITION_CTR[pos]
            lower_pos = positions[i - 1]
            upper_pos = pos
            lower_ctr = POSITION_CTR[lower_pos]
            upper_ctr = POSITION_CTR[upper_pos]

            # Linear interpolation
            ratio = (position - lower_pos) / (upper_pos - lower_pos)
            return lower_ctr + (upper_ctr - lower_ctr) * ratio

    # Beyond position 100
    return 0.001


def calculate_click_upside(current_position: float, target_position: float = 1.0) -> float:
    """
    Calculate the click upside from improving position.

    Returns the multiplicative factor of CTR improvement.
    E.g., moving from position 10 to position 3: 0.11 / 0.022 = 5.0x

    Args:
        current_position: Current average position
        target_position: Target position (default: 1)

    Returns:
        Click upside multiplier (1.0 = no change, 5.0 = 5x more clicks)
    """
    if current_position <= target_position:
        return 1.0  # Already at or above target

    current_ctr = get_expected_ctr(current_position)
    target_ctr = get_expected_ctr(target_position)

    if current_ctr <= 0:
        return 1.0

    return target_ctr / current_ctr


def calculate_visibility_gap(current_position: float, impressions: int) -> float:
    """
    Calculate visibility gap score.

    High impressions + poor position = high visibility gap (opportunity).

    Returns score from 0.0 to 1.0.
    """
    if impressions == 0:
        return 0.0

    # Normalize impressions (log scale)
    import math
    impression_score = min(1.0, math.log10(max(1, impressions)) / 5)  # 100k impressions = 1.0

    # Position penalty (higher position = lower gap)
    if current_position <= 3:
        position_gap = 0.1  # Already visible
    elif current_position <= 10:
        position_gap = 0.4  # Page 1, some opportunity
    elif current_position <= 20:
        position_gap = 0.7  # Page 2, good opportunity
    else:
        position_gap = 1.0  # Page 3+, high opportunity

    return impression_score * position_gap


# ============================================================================
# INTENT CLASSIFICATION (DemandScore)
# ============================================================================

class QueryIntent(str, Enum):
    """Query intent classification."""
    TRANSACTIONAL = "transactional"      # buy, purchase, order, price
    COMMERCIAL = "commercial"            # best, top, review, comparison, vs
    INFORMATIONAL = "informational"      # how to, what is, guide, tutorial
    NAVIGATIONAL = "navigational"        # brand names, specific products
    UNKNOWN = "unknown"


# Intent weights for EVUV calculation
INTENT_WEIGHTS = {
    QueryIntent.TRANSACTIONAL: 1.0,
    QueryIntent.COMMERCIAL: 0.7,
    QueryIntent.INFORMATIONAL: 0.25,
    QueryIntent.NAVIGATIONAL: 0.1,
    QueryIntent.UNKNOWN: 0.3,
}

# Intent classification patterns
TRANSACTIONAL_PATTERNS = [
    r'\b(buy|purchase|order|shop|price|cost|cheap|discount|deal|sale|coupon)\b',
    r'\b(for sale|where to buy|best price|lowest price)\b',
    r'\b(free shipping|fast delivery)\b',
]

COMMERCIAL_PATTERNS = [
    r'\b(best|top|review|compare|comparison|vs|versus|alternative)\b',
    r'\b(recommended|rating|ranked)\b',
    r'\b(\d{4})\b',  # Year (e.g., "best toys 2026")
]

INFORMATIONAL_PATTERNS = [
    r'\b(how to|what is|what are|why|when|guide|tutorial|tips|ideas)\b',
    r'\b(learn|understand|explain|definition|meaning)\b',
    r'\b(benefits|advantages|disadvantages|pros|cons)\b',
]

NAVIGATIONAL_PATTERNS = [
    r'\b(alphabet trains?|alphabet-trains)\b',
    r'\b(login|sign in|account|contact)\b',
]


def classify_query_intent(query: str) -> QueryIntent:
    """
    Classify a search query by intent.

    Priority: Transactional > Commercial > Informational > Navigational > Unknown
    """
    query_lower = query.lower()

    # Check transactional first (highest value)
    for pattern in TRANSACTIONAL_PATTERNS:
        if re.search(pattern, query_lower):
            return QueryIntent.TRANSACTIONAL

    # Check commercial
    for pattern in COMMERCIAL_PATTERNS:
        if re.search(pattern, query_lower):
            return QueryIntent.COMMERCIAL

    # Check informational
    for pattern in INFORMATIONAL_PATTERNS:
        if re.search(pattern, query_lower):
            return QueryIntent.INFORMATIONAL

    # Check navigational
    for pattern in NAVIGATIONAL_PATTERNS:
        if re.search(pattern, query_lower):
            return QueryIntent.NAVIGATIONAL

    return QueryIntent.UNKNOWN


def calculate_demand_score(asset: PageAsset) -> tuple[float, QueryIntent]:
    """
    Calculate demand score based on query intent distribution.

    Returns (score, dominant_intent).
    Score is weighted average of intent values.
    """
    if not asset.gsc.top_queries:
        # No query data - use asset type as proxy
        if asset.asset_type == AssetType.PRODUCT:
            return 0.7, QueryIntent.TRANSACTIONAL
        elif asset.asset_type == AssetType.CATEGORY:
            return 0.5, QueryIntent.COMMERCIAL
        elif asset.asset_type == AssetType.BLOG:
            return 0.25, QueryIntent.INFORMATIONAL
        else:
            return 0.3, QueryIntent.UNKNOWN

    # Weight by clicks (or impressions if no clicks)
    total_weight = 0.0
    weighted_score = 0.0
    intent_counts = {intent: 0.0 for intent in QueryIntent}

    for query in asset.gsc.top_queries:
        weight = query.clicks if query.clicks > 0 else query.impressions * 0.01
        intent = classify_query_intent(query.query)
        intent_weight = INTENT_WEIGHTS[intent]

        weighted_score += intent_weight * weight
        total_weight += weight
        intent_counts[intent] += weight

    if total_weight == 0:
        return 0.3, QueryIntent.UNKNOWN

    score = weighted_score / total_weight
    dominant_intent = max(intent_counts.keys(), key=lambda k: intent_counts[k])

    return score, dominant_intent


# ============================================================================
# CONVERSION PROXY
# ============================================================================

@dataclass
class ConversionProxy:
    """Proxy conversion metrics for non-converting pages."""
    proxy_type: str  # "direct", "assisted", "inferred"
    proxy_rate: float  # Estimated conversion rate
    confidence: float  # Confidence in the proxy (0-1)
    explanation: str


def calculate_conversion_proxy(
    asset: PageAsset,
    site_avg_purchase_rate: float = None,  # None -> the store's MEASURED CVR
    site_avg_revenue_per_session: float = 1.50,
) -> ConversionProxy:
    if site_avg_purchase_rate is None:
        site_avg_purchase_rate = _measured_econ()[1]
    """
    Calculate conversion proxy for pages without direct conversions.

    Hierarchy:
    1. Direct conversion data (if available)
    2. Engagement-weighted proxy (high engagement = higher proxy)
    3. Asset-type proxy (products > categories > blogs)
    """
    # 1. Direct conversion data
    if asset.ga4.purchase_rate_28d > 0:
        return ConversionProxy(
            proxy_type="direct",
            proxy_rate=asset.ga4.purchase_rate_28d,
            confidence=0.95,
            explanation="Direct conversion data available",
        )

    # 2. Engagement-weighted proxy
    engagement_rate = asset.ga4.engagement_rate_28d
    if engagement_rate > 0:
        # Higher engagement = closer to site average
        engagement_multiplier = min(1.5, 0.5 + engagement_rate)

        if asset.asset_type == AssetType.PRODUCT:
            base_rate = site_avg_purchase_rate * 1.2  # Products convert better
        elif asset.asset_type == AssetType.CATEGORY:
            base_rate = site_avg_purchase_rate * 0.8
        elif asset.asset_type == AssetType.BLOG:
            base_rate = site_avg_purchase_rate * 0.15  # Blogs convert much less
        else:
            base_rate = site_avg_purchase_rate * 0.5

        proxy_rate = base_rate * engagement_multiplier

        return ConversionProxy(
            proxy_type="engagement_weighted",
            proxy_rate=proxy_rate,
            confidence=0.5 + (engagement_rate * 0.3),  # Higher engagement = higher confidence
            explanation=f"Engagement-weighted proxy (engagement: {engagement_rate:.1%})",
        )

    # 3. Asset-type proxy (lowest confidence)
    if asset.asset_type == AssetType.PRODUCT:
        proxy_rate = site_avg_purchase_rate * 0.8
        confidence = 0.4
    elif asset.asset_type == AssetType.CATEGORY:
        proxy_rate = site_avg_purchase_rate * 0.5
        confidence = 0.35
    elif asset.asset_type == AssetType.BLOG:
        proxy_rate = site_avg_purchase_rate * 0.05  # Very low for blogs
        confidence = 0.25
    else:
        proxy_rate = site_avg_purchase_rate * 0.3
        confidence = 0.3

    return ConversionProxy(
        proxy_type="asset_type",
        proxy_rate=proxy_rate,
        confidence=confidence,
        explanation=f"Asset-type proxy ({asset.asset_type.value})",
    )


# ============================================================================
# EVUV (Expected Visibility Uplift Value)
# ============================================================================

@dataclass
class EVUVResult:
    """Result of EVUV calculation."""
    evuv: float
    demand_score: float
    visibility_gap: float
    click_upside: float
    conversion_proxy: ConversionProxy
    dominant_intent: QueryIntent
    risk_penalty: float
    breakdown: dict


def calculate_evuv(
    asset: PageAsset,
    aov: float = None,                    # None -> the store's MEASURED AOV
    gross_margin: float = 0.27,
    site_avg_purchase_rate: float = None,  # None -> the store's MEASURED CVR
    target_position: float = 5.0,  # Conservative target: top 5
    risk_multiplier: float = 1.0,
) -> EVUVResult:
    _m_aov, _m_cvr = _measured_econ()
    if aov is None:
        aov = _m_aov
    if site_avg_purchase_rate is None:
        site_avg_purchase_rate = _m_cvr
    """
    Calculate Expected Visibility Uplift Value.

    EVUV = DemandScore × VisibilityGap × ClickUpside × ConversionProxy × AOV × Margin × Confidence
           − RiskPenalty

    Args:
        asset: PageAsset to evaluate
        aov: Average order value
        gross_margin: Gross margin percentage
        site_avg_purchase_rate: Site-wide average purchase rate
        target_position: Target position for click upside calculation
        risk_multiplier: Additional risk multiplier (default 1.0)

    Returns:
        EVUVResult with score and breakdown
    """
    # 1. Calculate demand score
    demand_score, dominant_intent = calculate_demand_score(asset)

    # 2. Calculate visibility gap
    visibility_gap = calculate_visibility_gap(
        asset.gsc.avg_position_28d,
        asset.gsc.impressions_28d,
    )

    # 3. Calculate click upside
    click_upside = calculate_click_upside(
        asset.gsc.avg_position_28d,
        target_position,
    )
    # Cap click upside at 10x to avoid unrealistic projections
    click_upside = min(10.0, click_upside)

    # 4. Get conversion proxy
    conversion_proxy = calculate_conversion_proxy(asset, site_avg_purchase_rate)

    # 5. Calculate base expected value
    # Impressions × expected CTR gain × conversion rate × AOV × margin
    current_sessions = asset.ga4.sessions_28d
    if current_sessions == 0:
        # Estimate from impressions
        current_sessions = int(asset.gsc.impressions_28d * get_expected_ctr(asset.gsc.avg_position_28d))

    incremental_sessions = current_sessions * (click_upside - 1)  # Additional sessions from improvement
    incremental_conversions = incremental_sessions * conversion_proxy.proxy_rate
    incremental_revenue = incremental_conversions * aov
    incremental_profit = incremental_revenue * gross_margin

    # 6. Apply confidence discount
    confidence_discount = (
        demand_score *  # Intent alignment
        conversion_proxy.confidence *  # Conversion reliability
        min(1.0, visibility_gap + 0.3)  # Visibility opportunity (floor at 0.3)
    )

    # 7. Calculate risk penalty
    # Higher risk for:
    # - Low confidence proxies
    # - Informational intent (harder to convert)
    # - High position volatility (implied by large visibility gap)
    base_risk = incremental_profit * 0.15  # 15% base risk
    intent_risk = 0.1 if dominant_intent == QueryIntent.INFORMATIONAL else 0.0
    proxy_risk = 0.1 * (1 - conversion_proxy.confidence)

    risk_penalty = (base_risk + incremental_profit * intent_risk + incremental_profit * proxy_risk) * risk_multiplier

    # 8. Final EVUV
    evuv = (incremental_profit * confidence_discount) - risk_penalty

    return EVUVResult(
        evuv=evuv,
        demand_score=demand_score,
        visibility_gap=visibility_gap,
        click_upside=click_upside,
        conversion_proxy=conversion_proxy,
        dominant_intent=dominant_intent,
        risk_penalty=risk_penalty,
        breakdown={
            "current_sessions": current_sessions,
            "incremental_sessions": incremental_sessions,
            "incremental_conversions": incremental_conversions,
            "incremental_revenue": incremental_revenue,
            "incremental_profit": incremental_profit,
            "confidence_discount": confidence_discount,
            "risk_penalty": risk_penalty,
        },
    )


# ============================================================================
# ASSIST VALUE (AV) for Blogs/Guides
# ============================================================================

@dataclass
class AssistValueResult:
    """Result of Assist Value calculation for blogs/guides."""
    assist_value: float
    demand_exposure: float
    funnel_completion_prob: float  # FCP
    revenue_contribution_coef: float  # RCC
    confidence: float
    risk_penalty: float
    breakdown: dict


def calculate_assist_value(
    asset: PageAsset,
    internal_links_to_products: int = 0,
    internal_links_to_categories: int = 0,
    avg_product_conversion_rate: float = 0.03,
    avg_product_aov: float = None,             # None -> the store's MEASURED AOV
    gross_margin: float = 0.27,
    destination_conversion_rates: Optional[list[float]] = None,
    site_baseline_conversion_rate: float = None,  # None -> the store's MEASURED CVR
) -> AssistValueResult:
    """
    Calculate Assist Value for blogs/guides.

    AV = Demand Exposure (impressions) × FCP × DCQ × Margin − Risk

    Where:
    - Demand Exposure: Impressions (not sessions) — traffic alone must never create value
    - FCP: Funnel Contribution Probability — probability an organic session reaches
           a product or category page via this blog
    - DCQ: Downstream Conversion Quality — relative conversion strength of
           destinations vs site baseline

    High traffic + weak routing = LOW value.
    Lower traffic + strong routing = HIGH value.

    Args:
        asset: PageAsset (should be blog/guide type)
        internal_links_to_products: Number of internal links to product pages
        internal_links_to_categories: Number of internal links to category pages
        avg_product_conversion_rate: Average conversion rate of product pages
        avg_product_aov: Average order value
        gross_margin: Gross margin percentage
        destination_conversion_rates: Conversion rates of linked destination pages
        site_baseline_conversion_rate: Site-wide baseline conversion rate
    """
    _m_aov, _m_cvr = _measured_econ()
    if avg_product_aov is None:
        avg_product_aov = _m_aov
    if site_baseline_conversion_rate is None:
        site_baseline_conversion_rate = _m_cvr
    # 1. Demand Exposure — use IMPRESSIONS, not sessions
    # Traffic (sessions) alone must never create value for blogs
    impressions = asset.gsc.impressions_28d
    engagement_rate = asset.ga4.engagement_rate_28d or 0.0
    demand_exposure = impressions  # Raw impressions as demand signal

    # 2. Funnel Contribution Probability (FCP)
    # Probability that an organic session on this blog reaches a product/category page
    total_revenue_links = internal_links_to_products + internal_links_to_categories

    if total_revenue_links == 0:
        # No links to revenue pages — blog has no routing, therefore near-zero value
        fcp = 0.01  # 1% natural navigation (very low)
    elif total_revenue_links <= 2:
        fcp = 0.06
    elif total_revenue_links <= 5:
        fcp = 0.12
    else:
        fcp = 0.20  # Well-linked content

    # Engagement adjusts FCP: engaged users are more likely to follow links
    fcp *= (0.5 + engagement_rate)
    fcp = min(0.40, fcp)  # Cap at 40%

    # 3. Downstream Conversion Quality (DCQ)
    # Relative conversion strength of destinations vs site baseline
    if destination_conversion_rates and site_baseline_conversion_rate > 0:
        # Use actual destination conversion rates
        avg_dest_rate = sum(destination_conversion_rates) / len(destination_conversion_rates)
        dcq = avg_dest_rate / site_baseline_conversion_rate
        dcq = min(3.0, dcq)  # Cap at 3x baseline
    elif total_revenue_links == 0:
        dcq = 0.0  # No destinations — no conversion quality
    else:
        # Estimate from link composition: products convert better than categories
        product_weight = internal_links_to_products / total_revenue_links
        category_weight = internal_links_to_categories / total_revenue_links
        # Products ~1.5x baseline, categories ~0.8x baseline
        dcq = (product_weight * 1.5) + (category_weight * 0.8)

    # 4. Calculate expected downstream value
    # Demand Exposure (impressions) → estimated sessions → funnel sessions → conversions
    estimated_ctr = asset.gsc.ctr_28d if asset.gsc.ctr_28d > 0 else 0.02
    estimated_sessions = impressions * estimated_ctr
    expected_funnel_sessions = estimated_sessions * fcp
    # DCQ adjusts the effective conversion rate relative to baseline
    effective_conversion_rate = site_baseline_conversion_rate * dcq
    expected_conversions = expected_funnel_sessions * effective_conversion_rate
    expected_revenue = expected_conversions * avg_product_aov
    expected_profit = expected_revenue * gross_margin

    # 5. Confidence
    # Routing quality drives confidence, not traffic volume
    link_confidence = min(1.0, total_revenue_links / 5)
    routing_quality = fcp * dcq  # Combined routing signal
    confidence = 0.3 + (routing_quality * 0.3) + (link_confidence * 0.2)
    # Engagement adds minor confidence (not a primary driver)
    confidence += engagement_rate * 0.1
    confidence = min(0.70, confidence)  # Cap at 70% for assist value

    # 6. Risk penalty
    # Higher risk when routing is weak
    base_risk = expected_profit * 0.25  # 25% base risk for blogs
    routing_risk = expected_profit * 0.15 * (1.0 - min(1.0, fcp * 5))  # Extra risk for weak routing
    risk_penalty = base_risk + routing_risk

    # 7. Final Assist Value
    # High traffic + weak routing = LOW value (fcp and dcq will be near zero)
    # Lower traffic + strong routing = HIGH value (fcp and dcq amplify)
    assist_value = (expected_profit * confidence) - risk_penalty

    return AssistValueResult(
        assist_value=assist_value,
        demand_exposure=demand_exposure,
        funnel_completion_prob=fcp,
        revenue_contribution_coef=dcq,
        confidence=confidence,
        risk_penalty=risk_penalty,
        breakdown={
            "impressions": impressions,
            "estimated_ctr": estimated_ctr,
            "estimated_sessions": estimated_sessions,
            "engagement_rate": engagement_rate,
            "internal_links_products": internal_links_to_products,
            "internal_links_categories": internal_links_to_categories,
            "dcq": dcq,
            "expected_funnel_sessions": expected_funnel_sessions,
            "expected_conversions": expected_conversions,
            "expected_revenue": expected_revenue,
            "expected_profit": expected_profit,
            "routing_quality": routing_quality,
        },
    )
