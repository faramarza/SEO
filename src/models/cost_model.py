"""
ActionCostModel — consistent costing for the 5× revenue gate.

Doctrine: You don't need perfect costing — you need consistent costing.
"""

from enum import Enum
from pydantic import BaseModel, Field


class ActionType(str, Enum):
    """Allowed action types per doctrine (exhaustive list)."""
    NO_ACTION = "no_action"
    PAGE_REINVESTMENT = "page_reinvestment"
    INTERNAL_LINK_REALLOCATION = "internal_link_reallocation"
    NEW_PAGE_CREATION = "new_page_creation"


class ActionCostModel(BaseModel):
    """
    Cost model for evaluating the 5× gate.

    Hard Revenue Gate (Doctrine #4):
    - Expected Incremental Gross Profit ≥ 5× cost of action
    - Confidence ≥ lane threshold (exploration 0.55 / preservation 0.75)
    """
    cost_units: str = Field(default="operator_minutes")
    page_reinvestment: int = Field(
        default=120,
        ge=1,
        description="Cost in operator_minutes for page reinvestment"
    )
    internal_link_change: int = Field(
        default=30,
        ge=1,
        description="Cost in operator_minutes for internal link change"
    )
    new_page_creation: int = Field(
        default=240,
        ge=1,
        description="Cost in operator_minutes for new page creation"
    )

    def get_cost(self, action_type: ActionType) -> int:
        """Return the cost for a given action type."""
        if action_type == ActionType.NO_ACTION:
            return 0
        if action_type == ActionType.PAGE_REINVESTMENT:
            return self.page_reinvestment
        if action_type == ActionType.INTERNAL_LINK_REALLOCATION:
            return self.internal_link_change
        if action_type == ActionType.NEW_PAGE_CREATION:
            return self.new_page_creation
        return 0

    def minutes_to_cost_value(self, minutes: int, hourly_rate: float = 50.0) -> float:
        """
        Convert operator minutes to monetary cost value.

        Default hourly rate: $50/hour (conservative for skilled work).
        """
        return (minutes / 60.0) * hourly_rate
