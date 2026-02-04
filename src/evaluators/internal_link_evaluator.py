"""
Internal Link Reallocation Evaluator

Identifies opportunities to:
- Transfer authority from high-authority pages to money pages
- Fix orphaned pages
- Improve funnel flow from content to conversion pages
- Reduce over-linking (dilution)
"""

from dataclasses import dataclass
from typing import Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType


@dataclass
class LinkOpportunity:
    """A single internal linking opportunity."""
    source_url: str
    target_url: str
    opportunity_type: str  # "authority_transfer", "funnel_flow", "orphan_rescue"
    source_authority: float
    target_authority: float
    expected_value: float
    anchor_text_suggestion: str
    placement_suggestion: str


@dataclass
class LinkReallocationResult:
    """Result of internal link evaluation."""
    url: str
    current_inlinks: int
    current_outlinks: int
    authority_score: float
    opportunities: list[LinkOpportunity]
    priority_score: float
    recommended_action: str
    implementation_steps: list[str]
    expected_lift: float
    confidence: float
    risk_level: str


class InternalLinkEvaluator:
    """
    Evaluates internal linking opportunities.

    Strategies:
    1. Authority transfer: High authority → low authority money page
    2. Funnel flow: Blog/guide → product/category
    3. Orphan rescue: Pages with few/no internal links
    4. Dilution fix: Pages with too many outlinks
    """

    # Thresholds
    MIN_AUTHORITY_FOR_SOURCE = 0.4  # Source should have meaningful authority
    MAX_OUTLINKS_BEFORE_DILUTION = 50
    MIN_INLINKS_THRESHOLD = 3  # Pages below this are "orphaned"
    AUTHORITY_TRANSFER_THRESHOLD = 0.2  # Source should be this much higher than target

    def __init__(self):
        pass

    def find_authority_transfer_opportunities(
        self,
        target_asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> list[LinkOpportunity]:
        """
        Find high-authority pages that could link to target.

        Target should be a money page (product/category) with lower authority.
        """
        opportunities = []

        # Target must be a money page
        if target_asset.asset_type not in (AssetType.PRODUCT, AssetType.CATEGORY):
            return opportunities

        # Target should have some revenue signal or visibility
        if target_asset.ga4.revenue_28d == 0 and target_asset.gsc.impressions_28d < 100:
            return opportunities

        for source in all_assets:
            # Skip self
            if source.url == target_asset.url:
                continue

            # Source should have higher authority
            authority_gap = source.link_authority_score - target_asset.link_authority_score
            if authority_gap < self.AUTHORITY_TRANSFER_THRESHOLD:
                continue

            # Source should not be over-linked
            if source.outlinks > self.MAX_OUTLINKS_BEFORE_DILUTION:
                continue

            # Blogs make great sources for linking to products
            if source.asset_type == AssetType.BLOG:
                # Check topical relevance via query overlap
                relevance = self._check_query_relevance(source, target_asset)
                if relevance > 0.3:
                    opportunities.append(LinkOpportunity(
                        source_url=source.url,
                        target_url=target_asset.url,
                        opportunity_type="authority_transfer",
                        source_authority=source.link_authority_score,
                        target_authority=target_asset.link_authority_score,
                        expected_value=authority_gap * relevance * 10,  # Rough value estimate
                        anchor_text_suggestion=self._suggest_anchor_text(target_asset),
                        placement_suggestion="In main content, near relevant discussion",
                    ))

        # Sort by expected value
        opportunities.sort(key=lambda x: x.expected_value, reverse=True)
        return opportunities[:5]  # Top 5

    def find_funnel_opportunities(
        self,
        source_asset: PageAsset,
        revenue_pages: list[PageAsset],
    ) -> list[LinkOpportunity]:
        """
        Find opportunities to add links from content to revenue pages.

        Source should be a blog/guide.
        """
        opportunities = []

        if source_asset.asset_type != AssetType.BLOG:
            return opportunities

        # Source should have traffic
        if source_asset.ga4.sessions_28d < 10:
            return opportunities

        for target in revenue_pages:
            if target.ga4.revenue_28d == 0:
                continue

            relevance = self._check_query_relevance(source_asset, target)
            if relevance > 0.2:
                # Expected value based on funnel math
                expected_sessions = source_asset.ga4.sessions_28d * 0.10 * relevance  # 10% CTR on internal link
                expected_value = expected_sessions * target.ga4.purchase_rate_28d * 53.19 * 0.27  # AOV * margin

                opportunities.append(LinkOpportunity(
                    source_url=source_asset.url,
                    target_url=target.url,
                    opportunity_type="funnel_flow",
                    source_authority=source_asset.link_authority_score,
                    target_authority=target.link_authority_score,
                    expected_value=expected_value,
                    anchor_text_suggestion=self._suggest_anchor_text(target),
                    placement_suggestion="In buying considerations section or as CTA",
                ))

        opportunities.sort(key=lambda x: x.expected_value, reverse=True)
        return opportunities[:3]

    def check_orphan_status(
        self,
        asset: PageAsset,
    ) -> Optional[LinkOpportunity]:
        """Check if page is orphaned (few internal links)."""
        if asset.inlinks < self.MIN_INLINKS_THRESHOLD:
            if asset.gsc.impressions_28d > 100 or asset.ga4.revenue_28d > 0:
                return LinkOpportunity(
                    source_url="[MULTIPLE_SOURCES_NEEDED]",
                    target_url=asset.url,
                    opportunity_type="orphan_rescue",
                    source_authority=0.0,
                    target_authority=asset.link_authority_score,
                    expected_value=asset.gsc.impressions_28d * 0.01,  # Rough value
                    anchor_text_suggestion=self._suggest_anchor_text(asset),
                    placement_suggestion="Add links from relevant category/blog pages",
                )
        return None

    def _check_query_relevance(
        self,
        source: PageAsset,
        target: PageAsset,
    ) -> float:
        """Check topical relevance between two pages via query overlap."""
        if not source.gsc.top_queries or not target.gsc.top_queries:
            # Fall back to URL-based heuristic
            source_words = set(source.url.lower().replace("-", " ").replace("_", " ").split("/")[-1].split())
            target_words = set(target.url.lower().replace("-", " ").replace("_", " ").split("/")[-1].split())
            if source_words & target_words:
                return 0.4
            return 0.1

        source_queries = {q.query.lower() for q in source.gsc.top_queries}
        target_queries = {q.query.lower() for q in target.gsc.top_queries}

        if not source_queries or not target_queries:
            return 0.1

        # Jaccard similarity
        intersection = source_queries & target_queries
        union = source_queries | target_queries
        return len(intersection) / len(union) if union else 0.0

    def _suggest_anchor_text(self, target: PageAsset) -> str:
        """Suggest anchor text based on target page."""
        if target.gsc.top_queries:
            # Use top query as anchor
            return target.gsc.top_queries[0].query.title()

        # Fall back to URL-based suggestion
        path = target.url.split("/")[-1].replace("-", " ").replace(".html", "").replace("_", " ")
        return path.title()

    def evaluate(
        self,
        asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> LinkReallocationResult:
        """
        Full internal link evaluation.

        Identifies all linking opportunities for/from this page.
        """
        opportunities = []

        # 1. If this is a money page, find authority transfer opportunities
        if asset.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY):
            auth_opps = self.find_authority_transfer_opportunities(asset, all_assets)
            opportunities.extend(auth_opps)

        # 2. If this is a blog, find funnel opportunities
        if asset.asset_type == AssetType.BLOG:
            revenue_pages = [a for a in all_assets if a.ga4.revenue_28d > 0]
            funnel_opps = self.find_funnel_opportunities(asset, revenue_pages)
            opportunities.extend(funnel_opps)

        # 3. Check orphan status
        orphan_opp = self.check_orphan_status(asset)
        if orphan_opp:
            opportunities.append(orphan_opp)

        # Calculate priority
        total_expected_value = sum(o.expected_value for o in opportunities)
        priority_score = total_expected_value / 10 if opportunities else 0

        # Recommended action
        if not opportunities:
            recommended_action = "NO_ACTION"
            implementation_steps = []
            expected_lift = 0.0
        else:
            recommended_action = "INTERNAL_LINK_REALLOCATION"
            implementation_steps = []
            for i, opp in enumerate(opportunities[:3], 1):
                if opp.opportunity_type == "orphan_rescue":
                    implementation_steps.append(f"{i}. Add internal links to {opp.target_url} from relevant pages")
                else:
                    implementation_steps.append(
                        f"{i}. Add link from {opp.source_url.split('/')[-1]} to {opp.target_url.split('/')[-1]} "
                        f"with anchor '{opp.anchor_text_suggestion}'"
                    )
            expected_lift = min(0.20, len(opportunities) * 0.05)  # Conservative lift estimate

        return LinkReallocationResult(
            url=asset.url,
            current_inlinks=asset.inlinks,
            current_outlinks=asset.outlinks,
            authority_score=asset.link_authority_score,
            opportunities=opportunities,
            priority_score=priority_score,
            recommended_action=recommended_action,
            implementation_steps=implementation_steps,
            expected_lift=expected_lift,
            confidence=0.65 if opportunities else 1.0,
            risk_level="low",  # Internal links are fully reversible
        )
