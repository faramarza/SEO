"""
GovernanceState — counterfactual memory and regret budget tracking.

Doctrine #7: You are allowed 2 irreversible mistakes per calendar year.
Doctrine #8: You must remember past actions and their outcomes.
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class ActionOutcome(str, Enum):
    """Observed outcome of a past action."""
    PENDING = "pending"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class PastAction(BaseModel):
    """Record of a past action for counterfactual memory."""
    action_id: str = Field(description="Unique identifier, e.g., '2026-02-03-001'")
    action_type: str = Field(description="Type of action taken")
    target_urls: list[str] = Field(default_factory=list)
    implemented_on: date
    expected_raip: float = Field(description="RAIP estimate at time of decision")
    observed_outcome: ActionOutcome = Field(default=ActionOutcome.PENDING)
    notes: str = Field(default="")
    was_irreversible: bool = Field(default=False)

    @property
    def is_failure(self) -> bool:
        """Returns True if this action resulted in negative outcome."""
        return self.observed_outcome == ActionOutcome.NEGATIVE

    @property
    def is_regret(self) -> bool:
        """Returns True if this was an irreversible failure (counts against regret budget)."""
        return self.was_irreversible and self.is_failure


class GovernanceState(BaseModel):
    """
    Tracks regret budget and counterfactual memory.

    Doctrine #7: Once regret budget is exhausted → OBSERVE-ONLY MODE.
    Doctrine #8: Repeated failures reduce confidence scores automatically.
    """
    regret_budget_year: int = Field(default=2, ge=0)
    regret_budget_used: int = Field(default=0, ge=0)
    observe_only_mode: bool = Field(default=False)
    current_year: int = Field(default_factory=lambda: date.today().year)
    past_actions: list[PastAction] = Field(default_factory=list)

    @property
    def regret_budget_remaining(self) -> int:
        """Remaining irreversible mistakes allowed this year."""
        return max(0, self.regret_budget_year - self.regret_budget_used)

    @property
    def should_be_observe_only(self) -> bool:
        """Determine if system should be in observe-only mode."""
        return self.regret_budget_remaining == 0

    def record_action(self, action: PastAction) -> None:
        """Record a new action in counterfactual memory."""
        self.past_actions.append(action)

    def record_outcome(
        self,
        action_id: str,
        outcome: ActionOutcome,
        notes: str = ""
    ) -> bool:
        """
        Record the outcome of a past action.

        Returns True if the action was found and updated.
        """
        for action in self.past_actions:
            if action.action_id == action_id:
                action.observed_outcome = outcome
                if notes:
                    action.notes = notes
                # Update regret budget if this was an irreversible failure
                if action.is_regret:
                    self.regret_budget_used += 1
                    if self.should_be_observe_only:
                        self.observe_only_mode = True
                return True
        return False

    def get_failure_patterns(self) -> list[str]:
        """
        Identify repeated failure patterns.

        Used to reduce confidence on similar future actions.
        """
        failures = [a for a in self.past_actions if a.is_failure]
        patterns = []

        # Group by action type
        action_types = {}
        for f in failures:
            action_types[f.action_type] = action_types.get(f.action_type, 0) + 1

        for action_type, count in action_types.items():
            if count >= 2:
                patterns.append(f"repeated_{action_type}_failure")

        return patterns

    def confidence_penalty(self, action_type: str) -> float:
        """
        Calculate confidence penalty based on past failures.

        Each past failure of the same type reduces confidence by 0.1.
        """
        failures = [
            a for a in self.past_actions
            if a.is_failure and a.action_type == action_type
        ]
        return min(0.3, len(failures) * 0.1)  # Cap at 0.3 penalty

    def reset_for_new_year(self) -> None:
        """Reset regret budget for new calendar year."""
        current = date.today().year
        if current > self.current_year:
            self.current_year = current
            self.regret_budget_used = 0
            self.observe_only_mode = False

    def generate_action_id(self) -> str:
        """Generate a unique action ID for today."""
        today = date.today().isoformat()
        today_actions = [
            a for a in self.past_actions
            if a.action_id.startswith(today)
        ]
        sequence = len(today_actions) + 1
        return f"{today}-{sequence:03d}"
