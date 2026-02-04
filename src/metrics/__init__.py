"""Metrics module for the Agentic Organic Growth Governor."""

from .opportunity_metrics import (
    # Click-upside
    get_expected_ctr,
    calculate_click_upside,
    calculate_visibility_gap,
    POSITION_CTR,
    # Intent classification
    QueryIntent,
    INTENT_WEIGHTS,
    classify_query_intent,
    calculate_demand_score,
    # Conversion proxy
    ConversionProxy,
    calculate_conversion_proxy,
    # EVUV
    EVUVResult,
    calculate_evuv,
    # Assist Value
    AssistValueResult,
    calculate_assist_value,
)

__all__ = [
    # Click-upside
    "get_expected_ctr",
    "calculate_click_upside",
    "calculate_visibility_gap",
    "POSITION_CTR",
    # Intent classification
    "QueryIntent",
    "INTENT_WEIGHTS",
    "classify_query_intent",
    "calculate_demand_score",
    # Conversion proxy
    "ConversionProxy",
    "calculate_conversion_proxy",
    # EVUV
    "EVUVResult",
    "calculate_evuv",
    # Assist Value
    "AssistValueResult",
    "calculate_assist_value",
]
