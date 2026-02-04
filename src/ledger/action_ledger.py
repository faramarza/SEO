"""
Action Ledger — Persistent memory and learning for the Governor.

Implements:
- Action records with fingerprinting
- Persistence layer (JSON file)
- Learning rules (fingerprint matching, confidence adjustment)
- Outcome evaluation

Every non-NO-ACTION recommendation creates an Action record.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
import hashlib


class ActionStatus(str, Enum):
    """Status workflow for actions."""
    PROPOSED = "proposed"      # Governor recommended
    APPROVED = "approved"      # Human approved
    IMPLEMENTED = "implemented"  # Changes made
    MEASURED = "measured"      # Evaluation window complete
    CLOSED = "closed"          # Final outcome recorded


class ActionOutcome(str, Enum):
    """Outcome labels for completed actions."""
    POSITIVE = "positive"      # Metrics improved
    NEUTRAL = "neutral"        # No significant change
    NEGATIVE = "negative"      # Metrics declined
    INCONCLUSIVE = "inconclusive"  # Data issues prevent evaluation


@dataclass
class ActionFingerprint:
    """
    Fingerprint for pattern matching.

    Used to identify similar past actions and apply learning rules.
    """
    page_type: str        # product, category, blog, other
    intent_cluster: str   # transactional, commercial, informational, navigational
    action_surface: str   # title, meta, content, links, schema, canonical
    action_type: str      # page_reinvestment, internal_link, title_test, etc.

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ActionFingerprint":
        return cls(**data)

    def matches(self, other: "ActionFingerprint", strict: bool = False) -> bool:
        """
        Check if fingerprints match.

        Args:
            other: Fingerprint to compare
            strict: If True, all fields must match. If False, partial match allowed.
        """
        if strict:
            return (
                self.page_type == other.page_type and
                self.intent_cluster == other.intent_cluster and
                self.action_surface == other.action_surface and
                self.action_type == other.action_type
            )
        else:
            # Partial match: same action_type and at least one other field
            type_match = self.action_type == other.action_type
            other_matches = sum([
                self.page_type == other.page_type,
                self.intent_cluster == other.intent_cluster,
                self.action_surface == other.action_surface,
            ])
            return type_match and other_matches >= 1

    def hash(self) -> str:
        """Generate a hash for the fingerprint."""
        content = f"{self.page_type}:{self.intent_cluster}:{self.action_surface}:{self.action_type}"
        return hashlib.md5(content.encode()).hexdigest()[:12]


@dataclass
class ActionRecord:
    """
    Complete record of a Governor action.

    Persisted to the Action Ledger for learning.
    """
    action_id: str
    url: str
    action_type: str
    score_type: str  # RAIP, EVUV, AV
    score_value: float
    confidence: float
    fingerprint: ActionFingerprint
    recommendation_json: dict  # Exact changes proposed
    status: ActionStatus = ActionStatus.PROPOSED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    implemented_at: Optional[str] = None
    evaluation_window_days: int = 28
    outcome: Optional[ActionOutcome] = None
    outcome_metrics: Optional[dict] = None
    notes: str = ""
    prior_action_refs: list[str] = field(default_factory=list)  # Related past action_ids

    def to_dict(self) -> dict:
        data = asdict(self)
        data["fingerprint"] = self.fingerprint.to_dict()
        data["status"] = self.status.value
        data["outcome"] = self.outcome.value if self.outcome else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ActionRecord":
        data = data.copy()
        data["fingerprint"] = ActionFingerprint.from_dict(data["fingerprint"])
        data["status"] = ActionStatus(data["status"])
        if data.get("outcome"):
            data["outcome"] = ActionOutcome(data["outcome"])
        return cls(**data)

    @property
    def is_complete(self) -> bool:
        """Check if action has reached final state."""
        return self.status in (ActionStatus.MEASURED, ActionStatus.CLOSED)

    @property
    def is_success(self) -> bool:
        """Check if action had positive outcome."""
        return self.outcome == ActionOutcome.POSITIVE

    @property
    def is_failure(self) -> bool:
        """Check if action had negative outcome."""
        return self.outcome == ActionOutcome.NEGATIVE

    def update_status(self, new_status: ActionStatus) -> None:
        """Update action status with timestamp tracking."""
        self.status = new_status
        if new_status == ActionStatus.IMPLEMENTED:
            self.implemented_at = datetime.now().isoformat()


@dataclass
class LearningInsight:
    """Insight from historical actions affecting confidence."""
    fingerprint_hash: str
    matching_actions: int
    positive_count: int
    negative_count: int
    neutral_count: int
    confidence_adjustment: float
    recommendation: str
    action_ids: list[str]


class ActionLedger:
    """
    Persistent storage and learning system for Governor actions.

    Implements:
    - CRUD operations for action records
    - Fingerprint-based pattern matching
    - Learning rules for confidence adjustment
    - Outcome evaluation
    """

    def __init__(self, ledger_path: Optional[Path] = None):
        """
        Initialize Action Ledger.

        Args:
            ledger_path: Path to ledger JSON file. Default: data/action_ledger.json
        """
        if ledger_path is None:
            ledger_path = Path(__file__).parent.parent.parent / "data" / "action_ledger.json"

        self.ledger_path = ledger_path
        self._actions: dict[str, ActionRecord] = {}
        self._load()

    def _load(self) -> None:
        """Load ledger from disk."""
        if self.ledger_path.exists():
            try:
                with open(self.ledger_path) as f:
                    data = json.load(f)
                    self._actions = {
                        action_id: ActionRecord.from_dict(record)
                        for action_id, record in data.get("actions", {}).items()
                    }
            except (json.JSONDecodeError, KeyError):
                self._actions = {}
        else:
            self._actions = {}

    def _save(self) -> None:
        """Save ledger to disk."""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "last_updated": datetime.now().isoformat(),
            "actions": {
                action_id: record.to_dict()
                for action_id, record in self._actions.items()
            },
        }
        with open(self.ledger_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)

    def generate_action_id(self) -> str:
        """Generate unique action ID."""
        today = date.today().isoformat()
        today_actions = [
            a for a in self._actions.values()
            if a.action_id.startswith(today)
        ]
        sequence = len(today_actions) + 1
        return f"{today}-{sequence:03d}"

    def add_action(self, action: ActionRecord) -> None:
        """Add new action to ledger."""
        self._actions[action.action_id] = action
        self._save()

    def get_action(self, action_id: str) -> Optional[ActionRecord]:
        """Get action by ID."""
        return self._actions.get(action_id)

    def update_action(self, action: ActionRecord) -> None:
        """Update existing action."""
        if action.action_id in self._actions:
            self._actions[action.action_id] = action
            self._save()

    def get_all_actions(self) -> list[ActionRecord]:
        """Get all actions."""
        return list(self._actions.values())

    def get_actions_by_url(self, url: str) -> list[ActionRecord]:
        """Get all actions for a specific URL."""
        return [a for a in self._actions.values() if a.url == url]

    def get_actions_by_status(self, status: ActionStatus) -> list[ActionRecord]:
        """Get actions by status."""
        return [a for a in self._actions.values() if a.status == status]

    def find_matching_actions(
        self,
        fingerprint: ActionFingerprint,
        days_lookback: int = 180,
        strict: bool = False,
    ) -> list[ActionRecord]:
        """
        Find past actions with matching fingerprint.

        Args:
            fingerprint: Fingerprint to match
            days_lookback: Only consider actions from this many days ago
            strict: If True, require exact match

        Returns:
            List of matching ActionRecords
        """
        cutoff = datetime.now().timestamp() - (days_lookback * 24 * 60 * 60)

        matching = []
        for action in self._actions.values():
            # Check date
            try:
                action_date = datetime.fromisoformat(action.created_at).timestamp()
                if action_date < cutoff:
                    continue
            except (ValueError, TypeError):
                continue

            # Check fingerprint match
            if action.fingerprint.matches(fingerprint, strict=strict):
                matching.append(action)

        return matching

    def get_learning_insight(
        self,
        fingerprint: ActionFingerprint,
        days_lookback: int = 180,
    ) -> LearningInsight:
        """
        Get learning insight for a fingerprint.

        Analyzes past actions with similar fingerprint to determine
        confidence adjustment.

        Rules (per doctrine):
        - ≥3 NEGATIVE in last 12 months → FORBIDDEN (reduce confidence to block)
        - ≥2 POSITIVE in last 180 days → boost confidence up to +0.10
        """
        matching = self.find_matching_actions(fingerprint, days_lookback, strict=False)
        completed = [a for a in matching if a.is_complete]

        positive_count = sum(1 for a in completed if a.outcome == ActionOutcome.POSITIVE)
        negative_count = sum(1 for a in completed if a.outcome == ActionOutcome.NEGATIVE)
        neutral_count = sum(1 for a in completed if a.outcome == ActionOutcome.NEUTRAL)

        # Calculate confidence adjustment (doctrine: ≥3 NEGATIVE = forbidden)
        if negative_count >= 3:
            adjustment = -0.50  # Effectively blocks action
            recommendation = f"FORBIDDEN: {negative_count} negative outcomes in last 12 months. Action blocked per doctrine."
        elif negative_count == 2:
            adjustment = -0.20
            recommendation = f"CAUTION: {negative_count} negative outcomes. Significant confidence reduction."
        elif negative_count == 1 and positive_count == 0:
            adjustment = -0.10
            recommendation = "WARNING: 1 negative outcome, no positives. Consider caution."
        elif positive_count >= 2:
            adjustment = min(0.10, 0.05 * positive_count)  # +0.05 per positive, cap at +0.10
            recommendation = f"Pattern has {positive_count} positive outcomes. Slight confidence boost allowed."
        elif positive_count == 1 and negative_count == 0:
            adjustment = 0.03
            recommendation = "1 positive outcome. Minor confidence boost."
        else:
            adjustment = 0.0
            recommendation = "Insufficient history for adjustment."

        return LearningInsight(
            fingerprint_hash=fingerprint.hash(),
            matching_actions=len(matching),
            positive_count=positive_count,
            negative_count=negative_count,
            neutral_count=neutral_count,
            confidence_adjustment=adjustment,
            recommendation=recommendation,
            action_ids=[a.action_id for a in matching],
        )

    def apply_learning_rules(
        self,
        base_confidence: float,
        fingerprint: ActionFingerprint,
        min_confidence_threshold: float = 0.65,
    ) -> tuple[float, LearningInsight]:
        """
        Apply learning rules to adjust confidence.

        Args:
            base_confidence: Starting confidence score
            fingerprint: Action fingerprint for pattern matching
            min_confidence_threshold: Minimum confidence required

        Returns:
            (adjusted_confidence, learning_insight)
        """
        insight = self.get_learning_insight(fingerprint)

        adjusted = base_confidence + insight.confidence_adjustment
        adjusted = max(0.0, min(1.0, adjusted))  # Clamp to [0, 1]

        # Add additional check: if too many negatives, cap confidence below threshold
        if insight.negative_count >= 3:
            adjusted = min(adjusted, min_confidence_threshold - 0.05)

        return adjusted, insight

    def record_outcome(
        self,
        action_id: str,
        outcome: ActionOutcome,
        outcome_metrics: Optional[dict] = None,
        notes: str = "",
    ) -> bool:
        """
        Record outcome for a completed action.

        Args:
            action_id: Action ID to update
            outcome: Outcome label
            outcome_metrics: Measured metrics
            notes: Additional notes

        Returns:
            True if successful
        """
        action = self.get_action(action_id)
        if action is None:
            return False

        action.outcome = outcome
        action.outcome_metrics = outcome_metrics
        action.notes = notes
        action.update_status(ActionStatus.MEASURED)

        self.update_action(action)
        return True

    def get_pending_evaluations(self) -> list[ActionRecord]:
        """
        Get actions that need outcome evaluation.

        Returns actions that are IMPLEMENTED and past their evaluation window.
        """
        pending = []
        now = datetime.now()

        for action in self._actions.values():
            if action.status != ActionStatus.IMPLEMENTED:
                continue
            if action.implemented_at is None:
                continue

            try:
                implemented = datetime.fromisoformat(action.implemented_at)
                window_end = implemented.timestamp() + (action.evaluation_window_days * 24 * 60 * 60)

                if now.timestamp() >= window_end:
                    pending.append(action)
            except (ValueError, TypeError):
                continue

        return pending

    def summary(self) -> dict:
        """Generate summary statistics."""
        total = len(self._actions)
        by_status = {}
        by_outcome = {}

        for action in self._actions.values():
            status = action.status.value
            by_status[status] = by_status.get(status, 0) + 1

            if action.outcome:
                outcome = action.outcome.value
                by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

        return {
            "total_actions": total,
            "by_status": by_status,
            "by_outcome": by_outcome,
            "success_rate": (
                by_outcome.get("positive", 0) / sum(by_outcome.values())
                if by_outcome else None
            ),
        }
