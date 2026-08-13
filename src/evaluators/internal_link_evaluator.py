"""
Internal Link Reallocation Evaluator

Per doctrine — all internal linking must answer:
"Does this narrow the funnel toward the correct revenue asset?"

Strategies:
1. Authority transfer: High authority → low authority money page
2. Funnel flow: Blog/guide → product/category (primary function)
3. Orphan rescue: Pages with few/no internal links
4. Dilution fix: Pages with too many outlinks

Blog → Product/Category linking rules:
- Prefer CATEGORY when blog covers broad concept or multiple products apply
- Prefer PRODUCT when blog references specific use case mapping to one product
- Max 3 products per blog unless explicitly justified
- Every blog must have at least one primary "next step" link
- No more than one competing primary destination

Blog internal link quality check — blog FAILS if:
- Links to many products without hierarchy
- Routes to low-performing or irrelevant pages
- Has traffic but no clear downstream destination
"""

from dataclasses import dataclass, field
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
    opportunity_type: str  # "authority_transfer", "funnel_flow", "orphan_rescue", "routing_fix"
    source_authority: float
    target_authority: float
    expected_value: float
    anchor_text_suggestion: str
    placement_suggestion: str


@dataclass
class BlogRoutingAssessment:
    """Assessment of a blog's internal link routing quality."""
    has_primary_destination: bool
    primary_destination_url: Optional[str]
    primary_destination_type: Optional[str]  # "product" or "category"
    total_revenue_links: int
    product_links: int
    category_links: int
    has_competing_destinations: bool  # More than one primary
    links_to_low_converters: int
    routing_quality: str  # "good", "weak", "failing"
    issues: list[str]
    recommended_removals: list[str]  # Links to deprioritize/remove
    recommended_additions: list[str]  # Links to add


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
    # Blog routing assessment (populated for blog pages)
    routing_assessment: Optional[BlogRoutingAssessment] = None
    # Routing details for output (required for blog recommendations)
    current_routing_paths: list[str] = field(default_factory=list)
    recommended_destinations: list[str] = field(default_factory=list)
    destination_rationale: str = ""
    links_to_remove: list[str] = field(default_factory=list)


class InternalLinkEvaluator:
    """
    Evaluates internal linking opportunities with funnel governance.

    All internal linking suggestions must answer:
    "Does this narrow the funnel toward the correct revenue asset?"

    General rules:
    - Links must move users closer to purchase intent, not sideways
    - Linking should reduce choice ambiguity, not increase it
    """

    # Thresholds
    MIN_AUTHORITY_FOR_SOURCE = 0.4
    MAX_OUTLINKS_BEFORE_DILUTION = 50
    MIN_INLINKS_THRESHOLD = 3
    AUTHORITY_TRANSFER_THRESHOLD = 0.2
    MAX_PRODUCT_LINKS_PER_BLOG = 3

    def __init__(self):
        pass

    def assess_blog_routing(
        self,
        blog_asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> BlogRoutingAssessment:
        """
        Assess the internal link routing quality of a blog page.

        A blog fails routing quality if:
        - It links to many products without hierarchy
        - It routes to low-performing or irrelevant pages
        - It has traffic but no clear downstream destination
        """
        issues = []
        recommended_removals = []
        recommended_additions = []

        # Find revenue pages this blog could link to
        product_pages = [a for a in all_assets if a.asset_type == AssetType.PRODUCT]
        category_pages = [a for a in all_assets if a.asset_type == AssetType.CATEGORY]

        # Find relevant targets by query overlap
        relevant_products = []
        relevant_categories = []

        for p in product_pages:
            relevance = self._check_query_relevance(blog_asset, p)
            if relevance > 0.1:
                relevant_products.append((p, relevance))

        for c in category_pages:
            relevance = self._check_query_relevance(blog_asset, c)
            if relevance > 0.1:
                relevant_categories.append((c, relevance))

        # Sort by relevance
        relevant_products.sort(key=lambda x: x[1], reverse=True)
        relevant_categories.sort(key=lambda x: x[1], reverse=True)

        # Determine best primary destination
        primary_destination_url = None
        primary_destination_type = None

        # Prefer CATEGORY when:
        # - Blog covers broad concept
        # - Multiple products could satisfy the intent
        # Prefer PRODUCT when:
        # - Blog references specific use case mapping to one product
        # - Product has strong conversion performance

        if relevant_categories and relevant_products:
            top_cat_relevance = relevant_categories[0][1]
            top_prod_relevance = relevant_products[0][1]
            top_prod = relevant_products[0][0]

            # If top product has strong conversion AND high relevance, prefer product
            if (top_prod.ga4.purchase_rate_28d > 0.02 and
                    top_prod_relevance > top_cat_relevance * 1.3):
                primary_destination_url = top_prod.url
                primary_destination_type = "product"
            else:
                # Default to category (broader funnel entry)
                primary_destination_url = relevant_categories[0][0].url
                primary_destination_type = "category"
        elif relevant_categories:
            primary_destination_url = relevant_categories[0][0].url
            primary_destination_type = "category"
        elif relevant_products:
            primary_destination_url = relevant_products[0][0].url
            primary_destination_type = "product"

        has_primary = primary_destination_url is not None

        # Count current revenue links (approximate from outlinks)
        # In reality we'd check actual link targets; here we use heuristics
        estimated_product_links = min(blog_asset.outlinks, len(relevant_products))
        estimated_category_links = min(max(0, blog_asset.outlinks - estimated_product_links),
                                       len(relevant_categories))
        total_revenue_links = estimated_product_links + estimated_category_links

        # Check for competing destinations (too many products without hierarchy)
        has_competing = estimated_product_links > self.MAX_PRODUCT_LINKS_PER_BLOG

        if has_competing:
            issues.append(
                f"Blog links to {estimated_product_links} products without clear hierarchy. "
                f"Max {self.MAX_PRODUCT_LINKS_PER_BLOG} unless explicitly justified."
            )
            # Recommend keeping only the most relevant
            for p, rel in relevant_products[self.MAX_PRODUCT_LINKS_PER_BLOG:]:
                recommended_removals.append(p.url)

        # Check for low-converting destinations
        links_to_low_converters = 0
        for p, rel in relevant_products:
            if p.ga4.revenue_28d == 0 and p.ga4.add_to_carts_28d == 0 and p.ga4.sessions_28d > 10:
                links_to_low_converters += 1
                issues.append(f"Links to low-converting product: {p.url}")
                recommended_removals.append(p.url)

        # Check if blog has traffic but no downstream destination
        has_traffic = blog_asset.ga4.sessions_28d >= 10 or blog_asset.gsc.impressions_28d >= 500
        if has_traffic and not has_primary:
            issues.append("Blog has traffic but no clear downstream revenue destination")

        # Build addition recommendations
        if not has_primary and relevant_categories:
            recommended_additions.append(
                f"Add primary 'next step' link to category: {relevant_categories[0][0].url}"
            )
        elif not has_primary and relevant_products:
            recommended_additions.append(
                f"Add primary 'next step' link to product: {relevant_products[0][0].url}"
            )

        if total_revenue_links == 0 and (relevant_products or relevant_categories):
            recommended_additions.append("Add at least one link to a revenue page")

        # Determine routing quality
        if not has_primary and has_traffic:
            routing_quality = "failing"
        elif has_competing or links_to_low_converters > 1:
            routing_quality = "weak"
        elif has_primary and total_revenue_links >= 1:
            routing_quality = "good"
        else:
            routing_quality = "weak"

        return BlogRoutingAssessment(
            has_primary_destination=has_primary,
            primary_destination_url=primary_destination_url,
            primary_destination_type=primary_destination_type,
            total_revenue_links=total_revenue_links,
            product_links=estimated_product_links,
            category_links=estimated_category_links,
            has_competing_destinations=has_competing,
            links_to_low_converters=links_to_low_converters,
            routing_quality=routing_quality,
            issues=issues,
            recommended_removals=recommended_removals,
            recommended_additions=recommended_additions,
        )

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
            if source.url == target_asset.url:
                continue

            authority_gap = source.link_authority_score - target_asset.link_authority_score
            if authority_gap < self.AUTHORITY_TRANSFER_THRESHOLD:
                continue

            if source.outlinks > self.MAX_OUTLINKS_BEFORE_DILUTION:
                continue

            # Blogs make great sources for linking to products
            if source.asset_type == AssetType.BLOG:
                relevance = self._check_query_relevance(source, target_asset)
                if relevance > 0.3:
                    opportunities.append(LinkOpportunity(
                        source_url=source.url,
                        target_url=target_asset.url,
                        opportunity_type="authority_transfer",
                        source_authority=source.link_authority_score,
                        target_authority=target_asset.link_authority_score,
                        expected_value=authority_gap * relevance * 10,
                        anchor_text_suggestion=self._suggest_anchor_text(target_asset),
                        placement_suggestion="In main content, near relevant discussion",
                    ))

        opportunities.sort(key=lambda x: x.expected_value, reverse=True)
        return opportunities[:5]

    def find_funnel_opportunities(
        self,
        source_asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> list[LinkOpportunity]:
        """
        Find opportunities to add links from blog to revenue pages.

        Uses funnel governance rules:
        - Prefer CATEGORY when blog covers broad concept
        - Prefer PRODUCT when specific use case maps to one product
        - Max 3 products per blog
        - Every blog needs at least one "next step" link
        """
        opportunities = []

        if source_asset.asset_type != AssetType.BLOG:
            return opportunities

        if source_asset.ga4.sessions_28d < 10 and source_asset.gsc.impressions_28d < 500:
            return opportunities

        revenue_pages = [a for a in all_assets
                         if a.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY)]

        # Use the store's MEASURED AOV (not the hardcoded 53.19) so funnel-flow value
        # is computed against real economics, consistent with the rest of the tool.
        from src.metrics.opportunity_metrics import _measured_econ
        _aov, _cvr = _measured_econ()
        _margin = 0.275  # mid of config profit_model gross_margin range (0.25–0.30)

        # Separate and score products vs categories
        product_opps = []
        category_opps = []

        for target in revenue_pages:
            relevance = self._check_query_relevance(source_asset, target)
            if relevance < 0.15:
                continue

            # Skip low-converting products
            if (target.asset_type == AssetType.PRODUCT and
                    target.ga4.revenue_28d == 0 and
                    target.ga4.sessions_28d > 10):
                continue

            estimated_sessions = source_asset.ga4.sessions_28d or (
                source_asset.gsc.impressions_28d * 0.02)
            expected_click_through = estimated_sessions * 0.10 * relevance
            expected_value = expected_click_through * target.ga4.purchase_rate_28d * _aov * _margin

            opp = LinkOpportunity(
                source_url=source_asset.url,
                target_url=target.url,
                opportunity_type="funnel_flow",
                source_authority=source_asset.link_authority_score,
                target_authority=target.link_authority_score,
                expected_value=expected_value,
                anchor_text_suggestion=self._suggest_anchor_text(target),
                placement_suggestion=(
                    "As 'next step' CTA block" if target.asset_type == AssetType.CATEGORY
                    else "In buying considerations section"
                ),
            )

            if target.asset_type == AssetType.CATEGORY:
                category_opps.append(opp)
            else:
                product_opps.append(opp)

        # Sort each by expected value
        product_opps.sort(key=lambda x: x.expected_value, reverse=True)
        category_opps.sort(key=lambda x: x.expected_value, reverse=True)

        # Apply governance: max 3 products, prefer categories for broad topics
        opportunities.extend(category_opps[:2])  # Up to 2 category links
        opportunities.extend(product_opps[:self.MAX_PRODUCT_LINKS_PER_BLOG])

        # Ensure at least one destination
        if not opportunities and (product_opps or category_opps):
            opportunities.append((category_opps or product_opps)[0])

        opportunities.sort(key=lambda x: x.expected_value, reverse=True)
        return opportunities[:5]

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
                    expected_value=asset.gsc.impressions_28d * 0.01,
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
            source_words = set(source.url.lower().replace("-", " ").replace("_", " ").split("/")[-1].split())
            target_words = set(target.url.lower().replace("-", " ").replace("_", " ").split("/")[-1].split())
            if source_words & target_words:
                return 0.4
            return 0.1

        source_queries = {q.query.lower() for q in source.gsc.top_queries}
        target_queries = {q.query.lower() for q in target.gsc.top_queries}

        if not source_queries or not target_queries:
            return 0.1

        intersection = source_queries & target_queries
        union = source_queries | target_queries
        return len(intersection) / len(union) if union else 0.0

    def _suggest_anchor_text(self, target: PageAsset) -> str:
        """Suggest anchor text based on target page."""
        if target.gsc.top_queries:
            return target.gsc.top_queries[0].query.title()

        path = target.url.split("/")[-1].replace("-", " ").replace(".html", "").replace("_", " ")
        return path.title()

    def evaluate(
        self,
        asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> LinkReallocationResult:
        """
        Full internal link evaluation with funnel governance.

        All recommendations must narrow the funnel toward the correct revenue asset.
        Blog-specific: includes routing assessment and explicit destination reasoning.
        """
        opportunities = []
        routing_assessment = None
        current_routing_paths = []
        recommended_destinations = []
        destination_rationale = ""
        links_to_remove = []

        # 1. If this is a money page, find authority transfer opportunities
        if asset.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY):
            auth_opps = self.find_authority_transfer_opportunities(asset, all_assets)
            opportunities.extend(auth_opps)

        # 2. If this is a blog, run full routing assessment and funnel opportunities
        if asset.asset_type == AssetType.BLOG:
            # Assess current routing quality
            routing_assessment = self.assess_blog_routing(asset, all_assets)

            # Populate routing output fields (required for blog recommendations)
            if routing_assessment.primary_destination_url:
                current_routing_paths.append(
                    f"{asset.url} → {routing_assessment.primary_destination_url} "
                    f"({routing_assessment.primary_destination_type})"
                )

            links_to_remove = routing_assessment.recommended_removals

            # Find funnel opportunities
            funnel_opps = self.find_funnel_opportunities(asset, all_assets)
            opportunities.extend(funnel_opps)

            # Build recommended destinations with rationale
            for opp in funnel_opps[:3]:
                target_type = "category" if "category" in opp.target_url.lower() else "product"
                recommended_destinations.append(opp.target_url)

            if routing_assessment.primary_destination_url:
                destination_rationale = (
                    f"Primary destination: {routing_assessment.primary_destination_url} "
                    f"({routing_assessment.primary_destination_type}). "
                )
                if routing_assessment.primary_destination_type == "category":
                    destination_rationale += (
                        "Category preferred because blog covers broad concept "
                        "where multiple products could satisfy the intent."
                    )
                else:
                    destination_rationale += (
                        "Product preferred because blog references a specific use case "
                        "that maps clearly to this product."
                    )
            elif routing_assessment.routing_quality == "failing":
                destination_rationale = (
                    "Blog has no measurable routing to revenue assets. "
                    "Cannot improve funnel quality without adding destination links."
                )

            # If routing quality is failing and no improvements possible,
            # recommend NO_ACTION per decision enforcement
            if (routing_assessment.routing_quality == "failing" and
                    not funnel_opps and not routing_assessment.recommended_additions):
                return LinkReallocationResult(
                    url=asset.url,
                    current_inlinks=asset.inlinks,
                    current_outlinks=asset.outlinks,
                    authority_score=asset.link_authority_score,
                    opportunities=[],
                    priority_score=0,
                    recommended_action="NO_ACTION",
                    implementation_steps=[],
                    expected_lift=0.0,
                    confidence=1.0,
                    risk_level="low",
                    routing_assessment=routing_assessment,
                    current_routing_paths=current_routing_paths,
                    recommended_destinations=[],
                    destination_rationale="No routing improvements possible. Blog has low funnel value.",
                    links_to_remove=links_to_remove,
                )

            # Add routing fix opportunities from assessment
            for addition in routing_assessment.recommended_additions:
                _tgt = addition.split(": ")[-1] if ": " in addition else ""
                # Descriptive anchor from the target's own slug (real data) — beats a
                # generic "Shop now", which is poor anchor text for SEO and for the user.
                _slug = _tgt.rstrip("/").split("/")[-1]
                for _ext in (".html", ".htm"):
                    if _slug.endswith(_ext):
                        _slug = _slug[: -len(_ext)]
                _anchor = " ".join(w.capitalize() for w in _slug.replace("_", "-").split("-") if w)[:60]
                if not _anchor:
                    _anchor = "See our collection" if "category" in addition.lower() else "Shop now"
                opportunities.append(LinkOpportunity(
                    source_url=asset.url,
                    target_url=_tgt,
                    opportunity_type="routing_fix",
                    source_authority=asset.link_authority_score,
                    target_authority=0.0,
                    expected_value=asset.gsc.impressions_28d * 0.005,
                    anchor_text_suggestion=_anchor,
                    placement_suggestion="As primary 'next step' block after main content",
                ))

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
                    implementation_steps.append(
                        f"{i}. Add internal links to {opp.target_url} from relevant pages"
                    )
                elif opp.opportunity_type == "routing_fix":
                    implementation_steps.append(
                        f"{i}. {opp.placement_suggestion}: add link to {opp.target_url}"
                    )
                else:
                    implementation_steps.append(
                        f"{i}. Add link from {opp.source_url.split('/')[-1]} to "
                        f"{opp.target_url.split('/')[-1]} with anchor '{opp.anchor_text_suggestion}'"
                    )

            # For blogs, add removal steps
            if asset.asset_type == AssetType.BLOG and links_to_remove:
                for url in links_to_remove[:2]:
                    implementation_steps.append(
                        f"{len(implementation_steps)+1}. Remove/deprioritize link to {url.split('/')[-1]}"
                    )

            expected_lift = min(0.20, len(opportunities) * 0.05)

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
            risk_level="low",
            routing_assessment=routing_assessment,
            current_routing_paths=current_routing_paths,
            recommended_destinations=recommended_destinations,
            destination_rationale=destination_rationale,
            links_to_remove=links_to_remove,
        )
