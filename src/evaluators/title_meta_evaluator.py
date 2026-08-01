"""
Title/Meta Test Evaluator

Per doctrine:
- ≤ 60 characters
- No separators unless explicitly authorized
- Unique, intent-aligned
- Provide max 2 variants and select 1 recommended
- Fully reversible action
"""

from dataclasses import dataclass
from typing import Optional
import re

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType
from src.metrics.opportunity_metrics import (
    calculate_demand_score,
    calculate_visibility_gap,
    calculate_click_upside,
    QueryIntent,
)


@dataclass
class TitleVariant:
    """A proposed title variant."""
    title: str
    character_count: int
    rationale: str
    intent_alignment: float  # 0-1
    is_recommended: bool


@dataclass
class TitleTestResult:
    """Result of title/meta evaluation."""
    url: str
    current_title: str
    current_issues: list[str]
    should_test: bool
    variants: list[TitleVariant]
    recommended_variant: Optional[TitleVariant]
    expected_ctr_lift: float
    confidence: float
    risk_level: str  # "low", "medium", "high"
    measurement_plan: dict
    rollback_plan: str


class TitleMetaEvaluator:
    """
    Evaluates title/meta opportunities and generates test variants.

    Rules per doctrine:
    - Max 2 variants
    - ≤ 60 characters
    - No separators unless authorized
    - Intent-aligned
    """

    MAX_TITLE_LENGTH = 60
    MIN_IMPRESSIONS_FOR_TEST = 500  # Need enough data for measurement
    MIN_CTR_GAP = 0.01  # At least 1% below expected CTR

    def __init__(self, brand_suffix: str = ""):
        """
        Args:
            brand_suffix: If set, append to titles (e.g., "| Alphabet Trains")
        """
        self.brand_suffix = brand_suffix

    def analyze_current_title(self, title: str, asset: PageAsset) -> list[str]:
        """Identify issues with current title."""
        issues = []

        if not title:
            issues.append("MISSING: No title found")
            return issues

        # Length check
        if len(title) > self.MAX_TITLE_LENGTH:
            issues.append(f"TOO_LONG: {len(title)} chars (max {self.MAX_TITLE_LENGTH})")

        # Duplicate check (would need site-wide data)
        # For now, skip

        # Separator check
        separators = ['|', '-', '–', '—', ':']
        sep_count = sum(1 for s in separators if s in title)
        if sep_count > 1:
            issues.append(f"MULTIPLE_SEPARATORS: {sep_count} separators found")

        # Intent alignment check
        demand_score, intent = calculate_demand_score(asset)
        if intent == QueryIntent.TRANSACTIONAL:
            # Transactional titles should have action words
            action_words = ['buy', 'shop', 'order', 'get', 'price']
            if not any(w in title.lower() for w in action_words):
                issues.append("INTENT_MISMATCH: Transactional queries but no action words in title")

        # CTR underperformance
        if asset.gsc.impressions_28d > self.MIN_IMPRESSIONS_FOR_TEST:
            expected_ctr = self._get_expected_ctr_for_position(asset.gsc.avg_position_28d)
            actual_ctr = asset.gsc.ctr_28d
            if actual_ctr < expected_ctr - self.MIN_CTR_GAP:
                issues.append(f"LOW_CTR: {actual_ctr:.1%} vs expected {expected_ctr:.1%}")

        return issues

    def _get_expected_ctr_for_position(self, position: float) -> float:
        """Get expected CTR for a position."""
        from src.metrics.opportunity_metrics import get_expected_ctr
        return get_expected_ctr(position)

    def generate_variants(
        self,
        asset: PageAsset,
        current_title: str,
        primary_query: Optional[str] = None,
    ) -> list[TitleVariant]:
        """
        Generate up to 2 title variants.

        Per doctrine: max 2 variants, select 1 recommended.
        """
        variants = []
        demand_score, intent = calculate_demand_score(asset)

        # Get primary query from top queries if not provided
        if not primary_query and asset.gsc.top_queries:
            primary_query = asset.gsc.top_queries[0].query

        if not primary_query:
            return variants

        # Variant 1: Query-focused (front-load the primary query)
        variant1_title = self._create_query_focused_title(primary_query, intent, asset.asset_type, current_title)
        if variant1_title and variant1_title != current_title:
            variants.append(TitleVariant(
                title=variant1_title,
                character_count=len(variant1_title),
                rationale="Front-loads primary query for better SERP matching",
                intent_alignment=demand_score,
                is_recommended=False,
            ))

        # Variant 2: Benefit-focused (for transactional/commercial intent)
        if intent in (QueryIntent.TRANSACTIONAL, QueryIntent.COMMERCIAL):
            variant2_title = self._create_benefit_focused_title(primary_query, asset.asset_type)
            if variant2_title and variant2_title != current_title and variant2_title != variant1_title:
                variants.append(TitleVariant(
                    title=variant2_title,
                    character_count=len(variant2_title),
                    rationale="Adds benefit/value proposition for commercial intent",
                    intent_alignment=demand_score * 0.9,
                    is_recommended=False,
                ))

        # Select recommended variant (highest intent alignment)
        if variants:
            best = max(variants, key=lambda v: v.intent_alignment)
            best.is_recommended = True

        return variants[:2]  # Max 2 per doctrine

    def _create_query_focused_title(
        self,
        query: str,
        intent: QueryIntent,
        asset_type: AssetType,
        current_title: str = "",
    ) -> Optional[str]:
        """Front-load the REAL under-clicked query onto the page's ACTUAL title —
        do NOT fabricate store-specific suffixes ('for Kids', 'Collection') or
        invent claims. The genuine CTR fix here is: get the searcher's words into
        the title while keeping the page's own identity. If the title already
        contains the query, the title isn't the problem — return None (the fix is
        the meta description, and the grounded CTR-Recovery→Rewrite tool handles
        nuanced rewrites)."""
        q = (query or "").strip()
        cur = (current_title or "").strip()
        if not q:
            return None
        if cur and q.lower() in cur.lower():
            return None  # query already present — no honest deterministic improvement
        if cur:
            # Keep the page's real words; just lead with the query.
            merged = f"{q.title()}: {cur}"
            title = merged if len(merged) <= self.MAX_TITLE_LENGTH else q.title()
        else:
            title = q.title()
        if len(title) > self.MAX_TITLE_LENGTH:
            title = title[:self.MAX_TITLE_LENGTH - 1].rstrip() + "…"
        return title if title and title != current_title else None

    def _create_benefit_focused_title(
        self,
        query: str,
        asset_type: AssetType,
    ) -> Optional[str]:
        """Deliberately returns None. A benefit-led title requires a REAL,
        page-specific value prop (age, material, free shipping, a number) — which
        can't be produced deterministically without inventing claims like 'That
        Kids Love' or 'for Learning'. Benefit-led rewrites are handled by the
        grounded CTR-Recovery→Rewrite LLM, which reads the actual page. So we emit
        no fabricated benefit variant here."""
        return None

    def evaluate(
        self,
        asset: PageAsset,
        current_title: str = "",
    ) -> TitleTestResult:
        """
        Full evaluation for title/meta test opportunity.

        Asset-type constraints:
        - PRODUCT: May suggest meta title/description improvements
        - CATEGORY: May suggest title and H1 alignment with commercial intent
        - BLOG/GUIDE: Must NEVER suggest traffic-driven meta title changes or new keywords
        - OTHER: Standard evaluation

        Returns TitleTestResult with variants and recommendation.
        """
        # BLOG/GUIDE pages: title tests are NOT allowed per doctrine
        # Blogs must never get traffic-driven meta title changes
        if asset.asset_type == AssetType.BLOG:
            return TitleTestResult(
                url=asset.url,
                current_title=current_title,
                current_issues=[],
                should_test=False,
                variants=[],
                recommended_variant=None,
                expected_ctr_lift=0.0,
                confidence=1.0,
                risk_level="low",
                measurement_plan={},
                rollback_plan="N/A — Blog title tests not permitted. Focus on routing quality instead.",
            )

        # Analyze current title
        issues = self.analyze_current_title(current_title, asset)

        # Check if we should test
        should_test = (
            len(issues) > 0 and
            asset.gsc.impressions_28d >= self.MIN_IMPRESSIONS_FOR_TEST
        )

        # Generate variants if testing makes sense
        variants = []
        recommended = None
        expected_lift = 0.0

        if should_test:
            variants = self.generate_variants(asset, current_title)
            recommended = next((v for v in variants if v.is_recommended), None)

            # Estimate CTR lift (conservative)
            if "LOW_CTR" in str(issues):
                expected_lift = 0.15  # 15% relative CTR improvement
            elif "INTENT_MISMATCH" in str(issues):
                expected_lift = 0.10
            else:
                expected_lift = 0.05

        # Confidence based on data quality
        if asset.gsc.impressions_28d >= 5000:
            confidence = 0.75
        elif asset.gsc.impressions_28d >= 1000:
            confidence = 0.60
        else:
            confidence = 0.45

        return TitleTestResult(
            url=asset.url,
            current_title=current_title,
            current_issues=issues,
            should_test=should_test and len(variants) > 0,
            variants=variants,
            recommended_variant=recommended,
            expected_ctr_lift=expected_lift,
            confidence=confidence,
            risk_level="low",  # Title tests are fully reversible
            measurement_plan={
                "metric": "CTR",
                "baseline_period": "28 days before",
                "test_period": "28 days after",
                "success_threshold": "+5% relative CTR",
            },
            rollback_plan="Revert to original title if CTR declines >10%",
        )
