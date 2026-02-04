"""
Blog/Guide Validation Rules

Additional validation requirements for blog and guide content:
- Minimum word count requirements
- Clear funnel role (must link to money pages)
- Topic cluster alignment
- Freshness requirements
- Duplicate/thin content checks

Blog/guide pages require stricter validation because they:
- Have higher creation costs
- Take longer to rank
- Must justify their existence via funnel contribution
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType


@dataclass
class BlogGuideValidationResult:
    """Result of blog/guide validation."""
    url: str
    is_valid: bool
    validation_score: float  # 0-1
    issues: list[str]
    warnings: list[str]
    recommendations: list[str]
    funnel_contribution_score: float
    content_quality_score: float
    freshness_score: float


class BlogGuideValidator:
    """
    Validates blog and guide content against doctrine requirements.

    Validation Rules:
    1. Word Count: Blogs ≥1500, Guides ≥2500
    2. Funnel Links: Must link to ≥1 money page
    3. Topic Cluster: Should align with site categories
    4. Freshness: Content should be updated within 12 months
    5. Performance: Must have minimum engagement
    6. Intent Alignment: Content should match informational/commercial intent
    """

    # Word count requirements
    MIN_WORD_COUNT_BLOG = 1500
    MIN_WORD_COUNT_GUIDE = 2500
    MIN_WORD_COUNT_ARTICLE = 1000

    # Funnel requirements
    MIN_MONEY_PAGE_LINKS = 1
    OPTIMAL_MONEY_PAGE_LINKS = 3

    # Performance requirements
    MIN_SESSIONS_FOR_KEEP = 10  # Monthly sessions to justify keeping
    MIN_ENGAGEMENT_RATE = 0.3  # 30% engaged sessions

    # Freshness requirements
    MAX_AGE_MONTHS = 12  # Should update within 12 months

    def __init__(self):
        pass

    def validate(
        self,
        asset: PageAsset,
        money_pages: list[PageAsset],
        crawl_data: Optional[dict] = None,
    ) -> BlogGuideValidationResult:
        """
        Validate a blog/guide page.

        Args:
            asset: The page asset to validate
            money_pages: List of money pages (product/category) for funnel check
            crawl_data: Optional crawl data with word count, links, etc.

        Returns:
            BlogGuideValidationResult with validation details
        """
        issues = []
        warnings = []
        recommendations = []

        # Determine content type
        is_guide = self._is_guide(asset.url)
        min_words = self.MIN_WORD_COUNT_GUIDE if is_guide else self.MIN_WORD_COUNT_BLOG

        # 1. Word count validation
        word_count = crawl_data.get("word_count", 0) if crawl_data else 0
        content_quality_score = self._calculate_content_quality(word_count, min_words)

        if word_count > 0 and word_count < min_words:
            issues.append(f"Word count {word_count} below minimum {min_words}")
        elif word_count == 0 and crawl_data:
            warnings.append("Unable to determine word count")

        # 2. Funnel links validation
        funnel_contribution_score = self._calculate_funnel_contribution(
            asset, money_pages, crawl_data
        )

        if funnel_contribution_score < 0.3:
            issues.append("Insufficient links to money pages")
            recommendations.append(f"Add links to at least {self.MIN_MONEY_PAGE_LINKS} product/category pages")

        # 3. Performance validation
        performance_score = self._calculate_performance_score(asset)

        if asset.ga4.sessions_28d < self.MIN_SESSIONS_FOR_KEEP:
            warnings.append(f"Low traffic: {asset.ga4.sessions_28d} sessions/28d")

        if asset.ga4.engaged_sessions_28d > 0:
            engagement_rate = asset.ga4.engaged_sessions_28d / asset.ga4.sessions_28d if asset.ga4.sessions_28d > 0 else 0
            if engagement_rate < self.MIN_ENGAGEMENT_RATE:
                warnings.append(f"Low engagement rate: {engagement_rate:.1%}")

        # 4. Intent alignment
        intent_score = self._check_intent_alignment(asset)

        if intent_score < 0.5:
            warnings.append("Content may not align with informational intent")

        # 5. Freshness (if we have last modified date)
        freshness_score = self._calculate_freshness(crawl_data)

        # Overall validation score
        validation_score = (
            content_quality_score * 0.3 +
            funnel_contribution_score * 0.3 +
            performance_score * 0.2 +
            freshness_score * 0.2
        )

        # Determine if valid
        is_valid = len(issues) == 0 and validation_score >= 0.5

        # Generate recommendations
        if not is_valid:
            if content_quality_score < 0.5:
                recommendations.append(f"Expand content to at least {min_words} words")
            if funnel_contribution_score < 0.5:
                recommendations.append("Add contextual links to relevant product/category pages")
            if performance_score < 0.3:
                recommendations.append("Consider consolidating or removing low-performing content")

        return BlogGuideValidationResult(
            url=asset.url,
            is_valid=is_valid,
            validation_score=validation_score,
            issues=issues,
            warnings=warnings,
            recommendations=recommendations,
            funnel_contribution_score=funnel_contribution_score,
            content_quality_score=content_quality_score,
            freshness_score=freshness_score,
        )

    def _is_guide(self, url: str) -> bool:
        """Check if URL indicates a guide (vs regular blog post)."""
        url_lower = url.lower()
        guide_indicators = ["guide", "how-to", "tutorial", "complete", "ultimate", "definitive"]
        return any(indicator in url_lower for indicator in guide_indicators)

    def _calculate_content_quality(self, word_count: int, min_words: int) -> float:
        """Calculate content quality score based on word count."""
        if word_count == 0:
            return 0.5  # Unknown, neutral score

        if word_count >= min_words * 1.5:
            return 1.0  # Exceeds requirements
        elif word_count >= min_words:
            return 0.8  # Meets requirements
        elif word_count >= min_words * 0.7:
            return 0.5  # Close to requirements
        elif word_count >= min_words * 0.5:
            return 0.3  # Below requirements
        else:
            return 0.1  # Far below requirements

    def _calculate_funnel_contribution(
        self,
        asset: PageAsset,
        money_pages: list[PageAsset],
        crawl_data: Optional[dict],
    ) -> float:
        """Calculate funnel contribution score based on links to money pages."""
        if not crawl_data or "internal_links" not in crawl_data:
            # Use heuristic based on asset's own revenue contribution
            if asset.ga4.revenue_28d > 0:
                return 0.8  # Has direct revenue
            elif asset.ga4.add_to_carts_28d > 0:
                return 0.6  # Has assist value
            else:
                return 0.3  # Unknown contribution

        internal_links = set(crawl_data.get("internal_links", []))
        money_page_urls = {p.url for p in money_pages}

        # Count links to money pages
        money_links = internal_links & money_page_urls

        if len(money_links) >= self.OPTIMAL_MONEY_PAGE_LINKS:
            return 1.0
        elif len(money_links) >= self.MIN_MONEY_PAGE_LINKS:
            return 0.7
        elif len(money_links) > 0:
            return 0.4
        else:
            return 0.1

    def _calculate_performance_score(self, asset: PageAsset) -> float:
        """Calculate performance score based on traffic and engagement."""
        sessions = asset.ga4.sessions_28d

        if sessions >= 100:
            base_score = 1.0
        elif sessions >= 50:
            base_score = 0.8
        elif sessions >= 20:
            base_score = 0.6
        elif sessions >= self.MIN_SESSIONS_FOR_KEEP:
            base_score = 0.4
        else:
            base_score = 0.2

        # Adjust for engagement
        if sessions > 0:
            engagement_rate = asset.ga4.engaged_sessions_28d / sessions
            if engagement_rate >= 0.5:
                base_score *= 1.1
            elif engagement_rate < 0.2:
                base_score *= 0.8

        return min(1.0, base_score)

    def _check_intent_alignment(self, asset: PageAsset) -> float:
        """Check if content aligns with expected intent."""
        # For blogs/guides, informational queries are expected
        if not asset.gsc.top_queries:
            return 0.5  # Unknown

        informational_signals = ["how", "what", "why", "guide", "best", "tips", "review"]

        informational_count = 0
        for query_data in asset.gsc.top_queries:
            query_lower = query_data.query.lower()
            if any(signal in query_lower for signal in informational_signals):
                informational_count += 1

        if not asset.gsc.top_queries:
            return 0.5

        return min(1.0, informational_count / len(asset.gsc.top_queries) * 2)

    def _calculate_freshness(self, crawl_data: Optional[dict]) -> float:
        """Calculate freshness score based on last modified date."""
        if not crawl_data or "last_modified" not in crawl_data:
            return 0.5  # Unknown, neutral

        try:
            last_modified = datetime.fromisoformat(crawl_data["last_modified"])
            age_days = (datetime.now() - last_modified).days

            if age_days <= 90:  # 3 months
                return 1.0
            elif age_days <= 180:  # 6 months
                return 0.8
            elif age_days <= 365:  # 12 months
                return 0.6
            elif age_days <= 730:  # 2 years
                return 0.4
            else:
                return 0.2
        except (ValueError, TypeError):
            return 0.5

    def validate_batch(
        self,
        assets: list[PageAsset],
        money_pages: list[PageAsset],
        crawl_data_map: Optional[dict[str, dict]] = None,
    ) -> list[BlogGuideValidationResult]:
        """
        Validate multiple blog/guide pages.

        Args:
            assets: List of blog/guide assets to validate
            money_pages: List of money pages for funnel check
            crawl_data_map: Optional dict of URL -> crawl data

        Returns:
            List of validation results
        """
        results = []
        crawl_data_map = crawl_data_map or {}

        for asset in assets:
            if asset.asset_type not in (AssetType.BLOG, AssetType.OTHER):
                continue

            crawl_data = crawl_data_map.get(asset.url)
            result = self.validate(asset, money_pages, crawl_data)
            results.append(result)

        return results

    def get_reinvestment_candidates(
        self,
        validation_results: list[BlogGuideValidationResult],
        min_score: float = 0.4,
    ) -> list[BlogGuideValidationResult]:
        """
        Get blog/guide pages that are candidates for reinvestment.

        These are pages that:
        - Have some value (score >= min_score)
        - But have issues that could be fixed
        """
        candidates = []

        for result in validation_results:
            # Must have some baseline value
            if result.validation_score < min_score:
                continue

            # Must have fixable issues
            if result.is_valid:
                continue  # Already good

            # Must have clear improvement path
            if result.recommendations:
                candidates.append(result)

        # Sort by validation score (higher first - more potential)
        candidates.sort(key=lambda x: x.validation_score, reverse=True)

        return candidates

    def get_removal_candidates(
        self,
        validation_results: list[BlogGuideValidationResult],
        max_score: float = 0.3,
    ) -> list[BlogGuideValidationResult]:
        """
        Get blog/guide pages that are candidates for removal/consolidation.

        These are pages with very low scores and no clear improvement path.
        """
        candidates = []

        for result in validation_results:
            if result.validation_score <= max_score:
                candidates.append(result)

        # Sort by score (lowest first)
        candidates.sort(key=lambda x: x.validation_score)

        return candidates
