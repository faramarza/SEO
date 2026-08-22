"""
Structured Decision Output Formatter

Formats Governor decisions per doctrine specification:
1. Executive Summary
2. Ranked Queue (prioritized actions)
3. Detailed Recommendations per Action Type
4. Learning References (prior actions, fingerprint matches)

Output formats: JSON, Markdown, Console
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional
from enum import Enum

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.ledger.action_ledger import LearningInsight


class OutputFormat(str, Enum):
    """Output format options."""
    JSON = "json"
    MARKDOWN = "markdown"
    CONSOLE = "console"


@dataclass
class RecommendationItem:
    """A single recommendation in the queue."""
    rank: int
    url: str
    action_type: str
    mode: str  # PRESERVATION, OPPORTUNITY_DISCOVERY, FUNNEL_ALIGNMENT
    expected_value: float
    confidence: float
    priority_score: float
    risk_level: str
    implementation_summary: str
    implementation_steps: list[str]
    learning_reference: Optional[str]


@dataclass
class ExecutiveSummary:
    """Executive summary of evaluation run."""
    run_timestamp: str
    total_pages_evaluated: int
    pages_with_action: int
    pages_no_action: int
    action_rate: float
    total_expected_value: float
    top_action_types: dict[str, int]
    regret_budget_status: str
    tier_a_blockers: int
    tier_b_warnings: int


@dataclass
class FormattedOutput:
    """Complete formatted output."""
    executive_summary: ExecutiveSummary
    ranked_queue: list[RecommendationItem]
    by_action_type: dict[str, list[RecommendationItem]]
    learning_references: list[dict]
    raw_data: dict


class DecisionFormatter:
    """
    Formats Governor decisions for output.

    Implements doctrine output specification:
    - Executive Summary with key metrics
    - Ranked Queue sorted by priority
    - Grouped recommendations by action type
    - Learning references from Action Ledger
    """

    def __init__(self):
        pass

    def format_evaluation_results(
        self,
        evaluation_results: list[dict],
        learning_insights: list[LearningInsight] = None,
        diagnostic_summary: Optional[dict] = None,
        regret_budget_remaining: int = 2,
    ) -> FormattedOutput:
        """
        Format evaluation results into structured output.

        Args:
            evaluation_results: List of evaluation result dicts from Governor
            learning_insights: Learning insights from Action Ledger
            diagnostic_summary: Summary from tracking diagnostics
            regret_budget_remaining: Remaining regret budget

        Returns:
            FormattedOutput with all sections
        """
        # Build executive summary
        total_pages = len(evaluation_results)
        pages_with_action = sum(
            1 for r in evaluation_results
            if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
        )
        pages_no_action = total_pages - pages_with_action

        action_rate = pages_with_action / total_pages if total_pages > 0 else 0

        # Count action types
        action_type_counts: dict[str, int] = {}
        for r in evaluation_results:
            action = r.get("recommended_action", "NO_ACTION")
            action_type_counts[action] = action_type_counts.get(action, 0) + 1

        # Total expected value
        total_ev = sum(r.get("expected_value", 0) for r in evaluation_results)

        # Diagnostic blockers
        tier_a = diagnostic_summary.get("tier_a_failures", 0) if diagnostic_summary else 0
        tier_b = diagnostic_summary.get("tier_b_warnings", 0) if diagnostic_summary else 0

        executive_summary = ExecutiveSummary(
            run_timestamp=datetime.now().isoformat(),
            total_pages_evaluated=total_pages,
            pages_with_action=pages_with_action,
            pages_no_action=pages_no_action,
            action_rate=round(action_rate, 3),
            total_expected_value=round(total_ev, 2),
            top_action_types=action_type_counts,
            regret_budget_status=f"{regret_budget_remaining}/2 remaining",
            tier_a_blockers=tier_a,
            tier_b_warnings=tier_b,
        )

        # Build ranked queue
        ranked_queue = []
        actionable = [
            r for r in evaluation_results
            if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
            and r.get("priority_score", 0) > 0
        ]

        # Sort by priority descending
        actionable.sort(key=lambda x: x.get("priority_score", 0), reverse=True)

        for rank, result in enumerate(actionable, 1):
            item = RecommendationItem(
                rank=rank,
                url=result.get("url", ""),
                action_type=result.get("recommended_action", ""),
                mode=result.get("mode", "UNKNOWN"),
                expected_value=round(result.get("expected_value", 0), 2),
                confidence=round(result.get("confidence", 0), 3),
                priority_score=round(result.get("priority_score", 0), 4),
                risk_level=result.get("risk_level", "unknown"),
                implementation_summary=result.get("implementation_summary", ""),
                implementation_steps=result.get("implementation_steps", []),
                learning_reference=result.get("learning_reference"),
            )
            ranked_queue.append(item)

        # Group by action type
        by_action_type: dict[str, list[RecommendationItem]] = {}
        for item in ranked_queue:
            if item.action_type not in by_action_type:
                by_action_type[item.action_type] = []
            by_action_type[item.action_type].append(item)

        # Format learning references
        learning_refs = []
        if learning_insights:
            for insight in learning_insights:
                learning_refs.append({
                    "fingerprint_hash": insight.fingerprint_hash,
                    "matching_actions": insight.matching_actions,
                    "positive_outcomes": insight.positive_count,
                    "negative_outcomes": insight.negative_count,
                    "confidence_adjustment": insight.confidence_adjustment,
                    "recommendation": insight.recommendation,
                    "related_action_ids": insight.action_ids[:5],
                })

        return FormattedOutput(
            executive_summary=executive_summary,
            ranked_queue=ranked_queue,
            by_action_type=by_action_type,
            learning_references=learning_refs,
            raw_data={
                "evaluation_count": len(evaluation_results),
                "diagnostic_summary": diagnostic_summary,
            },
        )

    def to_json(self, output: FormattedOutput, indent: int = 2) -> str:
        """Convert formatted output to JSON."""
        data = {
            "executive_summary": asdict(output.executive_summary),
            "ranked_queue": [asdict(item) for item in output.ranked_queue],
            "by_action_type": {
                action_type: [asdict(item) for item in items]
                for action_type, items in output.by_action_type.items()
            },
            "learning_references": output.learning_references,
        }
        return json.dumps(data, indent=indent, default=str)

    def to_markdown(self, output: FormattedOutput) -> str:
        """Convert formatted output to Markdown."""
        lines = []

        # Title
        lines.append("# Agentic Organic Growth Governor — Decision Report")
        lines.append("")
        lines.append(f"**Generated:** {output.executive_summary.run_timestamp}")
        lines.append("")

        # Executive Summary
        lines.append("## Executive Summary")
        lines.append("")
        summary = output.executive_summary
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Pages Evaluated | {summary.total_pages_evaluated} |")
        lines.append(f"| Pages with Action | {summary.pages_with_action} |")
        lines.append(f"| Pages NO_ACTION | {summary.pages_no_action} |")
        lines.append(f"| Action Rate | {summary.action_rate:.1%} |")
        lines.append(f"| Total Expected Value | ${summary.total_expected_value:,.2f} |")
        lines.append(f"| Regret Budget | {summary.regret_budget_status} |")
        lines.append(f"| Tier A Blockers | {summary.tier_a_blockers} |")
        lines.append(f"| Tier B Warnings | {summary.tier_b_warnings} |")
        lines.append("")

        # Action Type Distribution
        lines.append("### Action Type Distribution")
        lines.append("")
        for action_type, count in summary.top_action_types.items():
            lines.append(f"- **{action_type}**: {count}")
        lines.append("")

        # Ranked Queue
        lines.append("## Ranked Action Queue")
        lines.append("")

        if not output.ranked_queue:
            lines.append("*No actions recommended. All pages evaluated to NO_ACTION.*")
        else:
            lines.append("| Rank | URL | Action | Mode | EV | Confidence | Priority |")
            lines.append("|------|-----|--------|------|-----|-----------|----------|")

            for item in output.ranked_queue[:20]:  # Top 20
                url_short = item.url.split("/")[-1][:30] or "/"
                lines.append(
                    f"| {item.rank} | {url_short} | {item.action_type} | "
                    f"{item.mode} | ${item.expected_value:.2f} | "
                    f"{item.confidence:.2f} | {item.priority_score:.4f} |"
                )
        lines.append("")

        # Detailed Recommendations by Action Type
        lines.append("## Detailed Recommendations")
        lines.append("")

        for action_type, items in output.by_action_type.items():
            lines.append(f"### {action_type}")
            lines.append("")
            lines.append(f"*{len(items)} recommendations*")
            lines.append("")

            for item in items[:5]:  # Top 5 per type
                lines.append(f"#### #{item.rank}: {item.url}")
                lines.append("")
                lines.append(f"- **Expected Value:** ${item.expected_value:.2f}")
                lines.append(f"- **Confidence:** {item.confidence:.2f}")
                lines.append(f"- **Risk Level:** {item.risk_level}")
                lines.append(f"- **Mode:** {item.mode}")
                lines.append("")

                if item.implementation_steps:
                    lines.append("**Implementation Steps:**")
                    for step in item.implementation_steps:
                        lines.append(f"1. {step}")
                    lines.append("")

                if item.learning_reference:
                    lines.append(f"*Learning Reference: {item.learning_reference}*")
                    lines.append("")

        # Learning References
        if output.learning_references:
            lines.append("## Learning References")
            lines.append("")
            lines.append("Prior actions influencing current confidence scores:")
            lines.append("")

            for ref in output.learning_references:
                lines.append(f"### Pattern: {ref['fingerprint_hash']}")
                lines.append(f"- Matching Actions: {ref['matching_actions']}")
                lines.append(f"- Positive Outcomes: {ref['positive_outcomes']}")
                lines.append(f"- Negative Outcomes: {ref['negative_outcomes']}")
                lines.append(f"- Confidence Adjustment: {ref['confidence_adjustment']:+.2f}")
                lines.append(f"- Recommendation: {ref['recommendation']}")
                lines.append("")

        return "\n".join(lines)

    def to_console(self, output: FormattedOutput) -> str:
        """Convert formatted output to console-friendly format."""
        lines = []

        # Header
        lines.append("=" * 60)
        lines.append("AGENTIC ORGANIC GROWTH GOVERNOR — DECISION REPORT")
        lines.append("=" * 60)
        lines.append("")

        # Executive Summary
        summary = output.executive_summary
        lines.append("EXECUTIVE SUMMARY")
        lines.append("-" * 40)
        lines.append(f"  Pages Evaluated:    {summary.total_pages_evaluated}")
        lines.append(f"  Pages with Action:  {summary.pages_with_action}")
        lines.append(f"  Action Rate:        {summary.action_rate:.1%}")
        lines.append(f"  Total Expected EV:  ${summary.total_expected_value:,.2f}")
        lines.append(f"  Regret Budget:      {summary.regret_budget_status}")
        lines.append("")

        if summary.tier_a_blockers > 0:
            lines.append(f"  ⚠️  TIER A BLOCKERS: {summary.tier_a_blockers}")
        if summary.tier_b_warnings > 0:
            lines.append(f"  ⚡ Tier B Warnings: {summary.tier_b_warnings}")
        lines.append("")

        # Action Distribution
        lines.append("ACTION DISTRIBUTION")
        lines.append("-" * 40)
        for action_type, count in summary.top_action_types.items():
            bar = "█" * min(count, 30)
            lines.append(f"  {action_type:30} {count:4} {bar}")
        lines.append("")

        # Top Recommendations
        lines.append("TOP RECOMMENDATIONS")
        lines.append("-" * 40)

        if not output.ranked_queue:
            lines.append("  No actions recommended.")
        else:
            for item in output.ranked_queue[:10]:
                url_short = item.url.split("/")[-1][:25] or "/"
                lines.append(
                    f"  #{item.rank:2} {url_short:25} {item.action_type:25} "
                    f"EV=${item.expected_value:7.2f} C={item.confidence:.2f}"
                )
        lines.append("")

        lines.append("=" * 60)

        return "\n".join(lines)

    def format_and_output(
        self,
        evaluation_results: list[dict],
        output_format: OutputFormat = OutputFormat.CONSOLE,
        output_path: Optional[Path] = None,
        **kwargs,
    ) -> str:
        """
        Format results and optionally write to file.

        Args:
            evaluation_results: Results from Governor evaluation
            output_format: Desired output format
            output_path: Optional path to write output
            **kwargs: Additional args for format_evaluation_results

        Returns:
            Formatted string output
        """
        formatted = self.format_evaluation_results(evaluation_results, **kwargs)

        if output_format == OutputFormat.JSON:
            output_str = self.to_json(formatted)
        elif output_format == OutputFormat.MARKDOWN:
            output_str = self.to_markdown(formatted)
        else:
            output_str = self.to_console(formatted)

        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w') as f:
                f.write(output_str)

        return output_str
