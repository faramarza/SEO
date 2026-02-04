"""
DecisionEnvelope — the single output format for all Governor decisions.

Doctrine #9: Every output must include decision, evidence, RAIP, risk, and
why doing nothing may still be better.
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class DecisionType(str, Enum):
    """Allowed decision types (exhaustive per doctrine)."""
    NO_ACTION = "NO_ACTION"
    PAGE_REINVESTMENT = "PAGE_REINVESTMENT"
    INTERNAL_LINK_REALLOCATION = "INTERNAL_LINK_REALLOCATION"
    NEW_PAGE_CREATION = "NEW_PAGE_CREATION"
    OBSERVE_ONLY = "OBSERVE_ONLY"


class Reversibility(str, Enum):
    """Reversibility classification for risk assessment."""
    FULLY_REVERSIBLE = "fully_reversible"
    SLOWLY_REVERSIBLE = "slowly_reversible"
    IRREVERSIBLE = "irreversible"


class RAIPEstimate(BaseModel):
    """
    Risk-Adjusted Incremental Profit calculation.

    RAIP = (Expected Incremental Gross Profit × Confidence Score) − Downside Risk

    If RAIP ≤ 0 → NO ACTION
    """
    expected_incremental_gross_profit: float = Field(
        default=0.0,
        description="Expected gross profit from the action"
    )
    confidence_multiplier: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (must be >= 0.65 to pass gate)"
    )
    downside_risk: float = Field(
        default=0.0,
        ge=0.0,
        description="Estimated monetary downside if action fails"
    )

    @property
    def raip(self) -> float:
        """Calculate RAIP value."""
        return (
            self.expected_incremental_gross_profit * self.confidence_multiplier
        ) - self.downside_risk

    @property
    def passes_confidence_gate(self) -> bool:
        """Check if confidence meets minimum threshold (0.65)."""
        return self.confidence_multiplier >= 0.65

    @property
    def is_positive(self) -> bool:
        """Returns True if RAIP > 0."""
        return self.raip > 0


class Evidence(BaseModel):
    """A single piece of evidence supporting the decision."""
    source: str = Field(description="Data source: GSC, GA4, Crawl, LinkGraph")
    fact: str = Field(description="Concise statement of the fact")
    value: str = Field(description="The specific value or metric")


class RiskAssessment(BaseModel):
    """Risk classification for the proposed action."""
    reversibility: Reversibility = Field(default=Reversibility.FULLY_REVERSIBLE)
    failure_modes: list[str] = Field(
        default_factory=list,
        description="Enumerated ways this action could fail"
    )
    cannibalization_risk: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Risk of cannibalizing existing pages"
    )

    @property
    def is_irreversible(self) -> bool:
        """Check if action is irreversible (requires regret budget)."""
        return self.reversibility == Reversibility.IRREVERSIBLE


class ActionPlan(BaseModel):
    """Specific steps for implementing the action."""
    steps: list[str] = Field(default_factory=list)
    rollback: list[str] = Field(
        default_factory=list,
        description="Steps to reverse the action if needed"
    )


class DecisionEnvelope(BaseModel):
    """
    The single output format for all Governor decisions.

    Doctrine #9: No verbosity. No persuasion. No enthusiasm.
    """
    decision: DecisionType = Field(default=DecisionType.NO_ACTION)
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Overall confidence in this decision"
    )
    raip_estimate: RAIPEstimate = Field(default_factory=RAIPEstimate)
    evidence: list[Evidence] = Field(default_factory=list)
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    action_plan: ActionPlan = Field(default_factory=ActionPlan)

    # Required by doctrine
    target_urls: list[str] = Field(
        default_factory=list,
        description="URLs affected by this decision"
    )
    why_no_action_may_be_better: str = Field(
        default="",
        description="Mandatory: why inaction might be the right choice"
    )

    @property
    def is_action(self) -> bool:
        """Returns True if this is an actionable decision (not NO_ACTION/OBSERVE_ONLY)."""
        return self.decision not in (DecisionType.NO_ACTION, DecisionType.OBSERVE_ONLY)

    @property
    def passes_all_gates(self) -> bool:
        """
        Check if decision passes all doctrine gates.

        Gate 1: RAIP > 0
        Gate 2: Confidence >= 0.65
        Gate 3: Expected profit >= 5× cost (checked externally)
        Gate 4: Reversible or has regret budget (checked externally)
        """
        if not self.is_action:
            return True  # NO_ACTION always passes
        return (
            self.raip_estimate.is_positive and
            self.raip_estimate.passes_confidence_gate
        )

    def to_markdown(self) -> str:
        """
        Generate human-readable markdown output.

        Doctrine #9: Concise, factual. No verbosity.
        """
        lines = [
            f"## Decision: {self.decision.value}",
            f"**Confidence:** {self.confidence:.2f}",
            "",
            "### RAIP Estimate",
            f"- Expected Incremental Profit: ${self.raip_estimate.expected_incremental_gross_profit:.2f}",
            f"- Confidence Multiplier: {self.raip_estimate.confidence_multiplier:.2f}",
            f"- Downside Risk: ${self.raip_estimate.downside_risk:.2f}",
            f"- **RAIP: ${self.raip_estimate.raip:.2f}**",
            "",
        ]

        if self.evidence:
            lines.append("### Evidence")
            for e in self.evidence:
                lines.append(f"- [{e.source}] {e.fact}: {e.value}")
            lines.append("")

        if self.target_urls:
            lines.append("### Target URLs")
            for url in self.target_urls:
                lines.append(f"- {url}")
            lines.append("")

        lines.extend([
            "### Risk Assessment",
            f"- Reversibility: {self.risk.reversibility.value}",
        ])
        if self.risk.failure_modes:
            lines.append("- Failure modes:")
            for fm in self.risk.failure_modes:
                lines.append(f"  - {fm}")
        if self.risk.cannibalization_risk > 0:
            lines.append(f"- Cannibalization risk: {self.risk.cannibalization_risk:.1%}")
        lines.append("")

        if self.action_plan.steps:
            lines.append("### Action Plan")
            for i, step in enumerate(self.action_plan.steps, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

        if self.action_plan.rollback:
            lines.append("### Rollback Plan")
            for i, step in enumerate(self.action_plan.rollback, 1):
                lines.append(f"{i}. {step}")
            lines.append("")

        if self.why_no_action_may_be_better:
            lines.extend([
                "### Why NO ACTION May Still Be Better",
                self.why_no_action_may_be_better,
            ])

        return "\n".join(lines)

    @classmethod
    def no_action(
        cls,
        reason: str,
        evidence: list[Evidence] | None = None
    ) -> "DecisionEnvelope":
        """Factory method for NO_ACTION decisions."""
        return cls(
            decision=DecisionType.NO_ACTION,
            confidence=1.0,  # High confidence in doing nothing
            evidence=evidence or [],
            why_no_action_may_be_better=reason,
        )

    @classmethod
    def observe_only(
        cls,
        reason: str,
        evidence: list[Evidence] | None = None
    ) -> "DecisionEnvelope":
        """Factory method for OBSERVE_ONLY decisions (regret budget exhausted)."""
        return cls(
            decision=DecisionType.OBSERVE_ONLY,
            confidence=1.0,
            evidence=evidence or [],
            why_no_action_may_be_better=reason,
        )
