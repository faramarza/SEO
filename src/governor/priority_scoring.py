"""
Priority Scoring for the Agentic Organic Growth Governor.

Implements:
Priority = (ExpectedValue × Confidence × ReversibilityMultiplier × UrgencyMultiplier) ÷ Effort
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Reversibility(str, Enum):
    """Action reversibility classification."""
    FULLY_REVERSIBLE = "fully_reversible"
    SLOWLY_REVERSIBLE = "slowly_reversible"
    IRREVERSIBLE = "irreversible"


class ActionType(str, Enum):
    """Allowed action types per doctrine."""
    NO_ACTION = "no_action"
    OBSERVE_ONLY = "observe_only"
    INTERNAL_LINK_REALLOCATION = "internal_link_reallocation"
    TITLE_META_TEST = "title_meta_test"
    PAGE_REINVESTMENT = "page_reinvestment"
    CANONICAL_FIX = "canonical_fix"
    NEW_ASSET_CREATION = "new_asset_creation"


# Reversibility multipliers
REVERSIBILITY_MULTIPLIERS = {
    Reversibility.FULLY_REVERSIBLE: 1.0,
    Reversibility.SLOWLY_REVERSIBLE: 0.6,
    Reversibility.IRREVERSIBLE: 0.0,  # Forbidden unless explicitly authorized
}

# Action type to reversibility mapping
ACTION_REVERSIBILITY = {
    ActionType.NO_ACTION: Reversibility.FULLY_REVERSIBLE,
    ActionType.OBSERVE_ONLY: Reversibility.FULLY_REVERSIBLE,
    ActionType.INTERNAL_LINK_REALLOCATION: Reversibility.FULLY_REVERSIBLE,
    ActionType.TITLE_META_TEST: Reversibility.FULLY_REVERSIBLE,
    ActionType.PAGE_REINVESTMENT: Reversibility.SLOWLY_REVERSIBLE,
    ActionType.CANONICAL_FIX: Reversibility.SLOWLY_REVERSIBLE,
    ActionType.NEW_ASSET_CREATION: Reversibility.SLOWLY_REVERSIBLE,
}

# Default effort in operator minutes
ACTION_EFFORT = {
    ActionType.NO_ACTION: 0,
    ActionType.OBSERVE_ONLY: 5,
    ActionType.INTERNAL_LINK_REALLOCATION: 30,
    ActionType.TITLE_META_TEST: 15,
    ActionType.PAGE_REINVESTMENT: 120,
    ActionType.CANONICAL_FIX: 45,
    ActionType.NEW_ASSET_CREATION: 240,
}


@dataclass
class PriorityScore:
    """Result of priority calculation."""
    priority: float
    expected_value: float
    confidence: float
    reversibility: Reversibility
    reversibility_multiplier: float
    urgency_multiplier: float
    effort: int
    breakdown: dict

    @property
    def is_actionable(self) -> bool:
        """Check if priority is high enough to consider."""
        return self.priority > 0 and self.reversibility != Reversibility.IRREVERSIBLE


def calculate_urgency_multiplier(
    impressions: int,
    position: float,
    has_revenue: bool,
) -> float:
    """
    Calculate urgency multiplier.

    Higher urgency for:
    - High impressions (demand exists)
    - Near page 1 (achievable)
    - Has revenue signal (proven value)

    Returns multiplier from 0.8 to 1.3.
    """
    base = 1.0

    # Demand factor (impressions)
    if impressions >= 10000:
        base += 0.1
    elif impressions >= 1000:
        base += 0.05

    # Position factor (near page 1 = more urgent)
    if 5 < position <= 15:
        base += 0.1  # Just off page 1, high potential
    elif position <= 5:
        base += 0.05  # Already visible, moderate urgency

    # Revenue factor
    if has_revenue:
        base += 0.05

    return min(1.3, max(0.8, base))


def calculate_priority(
    expected_value: float,
    confidence: float,
    action_type: ActionType,
    effort_minutes: Optional[int] = None,
    impressions: int = 0,
    position: float = 100,
    has_revenue: bool = False,
) -> PriorityScore:
    """
    Calculate priority score for an action.

    Priority = (ExpectedValue × Confidence × ReversibilityMultiplier × UrgencyMultiplier) ÷ Effort

    Args:
        expected_value: RAIP, EVUV, or AV score
        confidence: Confidence score (0-1)
        action_type: Type of action
        effort_minutes: Override default effort estimate
        impressions: GSC impressions (for urgency)
        position: GSC average position (for urgency)
        has_revenue: Whether page has revenue signal

    Returns:
        PriorityScore with all components
    """
    # Get reversibility
    reversibility = ACTION_REVERSIBILITY.get(action_type, Reversibility.SLOWLY_REVERSIBLE)
    reversibility_multiplier = REVERSIBILITY_MULTIPLIERS[reversibility]

    # Get effort
    effort = effort_minutes if effort_minutes is not None else ACTION_EFFORT.get(action_type, 60)
    effort = max(1, effort)  # Avoid division by zero

    # Calculate urgency
    urgency_multiplier = calculate_urgency_multiplier(impressions, position, has_revenue)

    # Calculate priority
    if reversibility == Reversibility.IRREVERSIBLE:
        priority = 0.0  # Forbidden
    elif expected_value <= 0:
        priority = 0.0  # No value
    else:
        priority = (
            expected_value *
            confidence *
            reversibility_multiplier *
            urgency_multiplier
        ) / effort

    return PriorityScore(
        priority=priority,
        expected_value=expected_value,
        confidence=confidence,
        reversibility=reversibility,
        reversibility_multiplier=reversibility_multiplier,
        urgency_multiplier=urgency_multiplier,
        effort=effort,
        breakdown={
            "expected_value": expected_value,
            "confidence": confidence,
            "reversibility_multiplier": reversibility_multiplier,
            "urgency_multiplier": urgency_multiplier,
            "effort_minutes": effort,
            "formula": "(EV × Conf × RevMult × UrgMult) ÷ Effort",
        },
    )


def rank_opportunities(
    opportunities: list[tuple[str, float, float, ActionType, dict]],
) -> list[tuple[str, PriorityScore]]:
    """
    Rank a list of opportunities by priority.

    Args:
        opportunities: List of (url, expected_value, confidence, action_type, extra_params)

    Returns:
        List of (url, PriorityScore) sorted by priority descending
    """
    scored = []

    for url, ev, conf, action_type, params in opportunities:
        score = calculate_priority(
            expected_value=ev,
            confidence=conf,
            action_type=action_type,
            impressions=params.get("impressions", 0),
            position=params.get("position", 100),
            has_revenue=params.get("has_revenue", False),
        )
        scored.append((url, score))

    # Sort by priority descending
    scored.sort(key=lambda x: x[1].priority, reverse=True)

    return scored
