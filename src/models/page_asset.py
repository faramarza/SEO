"""
PageAsset model — the primary object for the Capital Governor.

Each indexable URL is treated as a financial asset with measurable attributes.
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, HttpUrl, field_validator


class AssetType(str, Enum):
    """Classification of page asset types."""
    PRODUCT = "product"
    CATEGORY = "category"
    BLOG = "blog"
    OTHER = "other"


class TopQuery(BaseModel):
    """A single query from GSC with associated metrics."""
    query: str
    clicks: int = Field(ge=0)
    impressions: int = Field(ge=0)
    ctr: float = Field(ge=0.0, le=1.0)
    position: float = Field(ge=0.0)


class GSCMetrics(BaseModel):
    """Google Search Console metrics for a page (28-day rolling window)."""
    impressions_28d: int = Field(default=0, ge=0)
    clicks_28d: int = Field(default=0, ge=0)
    ctr_28d: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_position_28d: float = Field(default=0.0, ge=0.0)
    query_dispersion: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="0 = single query dominance, 1 = highly dispersed queries"
    )
    top_queries: list[TopQuery] = Field(default_factory=list)

    @field_validator("ctr_28d", mode="before")
    @classmethod
    def validate_ctr(cls, v: float) -> float:
        """Ensure CTR is within valid bounds."""
        if v < 0:
            return 0.0
        if v > 1:
            return 1.0
        return v


class GA4Metrics(BaseModel):
    """Google Analytics 4 metrics for a page (28-day rolling window)."""
    sessions_28d: int = Field(default=0, ge=0)
    engaged_sessions_28d: int = Field(default=0, ge=0)
    engagement_rate_28d: float = Field(default=0.0, ge=0.0, le=1.0)
    purchases_28d: int = Field(default=0, ge=0)
    revenue_28d: float = Field(default=0.0, ge=0.0)
    purchase_rate_28d: float = Field(default=0.0, ge=0.0, le=1.0)
    add_to_carts_28d: int = Field(default=0, ge=0)
    users_28d: int = Field(default=0, ge=0)
    conversions_28d: int = Field(default=0, ge=0)
    bounce_rate_28d: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def revenue_per_session(self) -> float:
        """Calculate revenue per landing session."""
        if self.sessions_28d == 0:
            return 0.0
        return self.revenue_28d / self.sessions_28d


class PageAsset(BaseModel):
    """
    Primary object representing an indexable URL as a financial asset.

    Doctrine reminder: Evaluate pages, not keywords.
    Keywords are supporting evidence only.
    """
    url: str = Field(description="Full URL of the page")
    asset_type: AssetType = Field(default=AssetType.OTHER)
    canonical_url: Optional[str] = Field(default=None)
    has_crawl_data: bool = Field(default=False, description="Whether we have crawl data for this page")
    http_status: int = Field(default=200)
    indexable: bool = Field(default=True)
    title: str = Field(default="")
    h1: str = Field(default="")
    meta_description: str = Field(default="")
    word_count: int = Field(default=0, ge=0)
    content_preview: str = Field(default="", description="First ~200 words of page body text")
    above_fold_html: str = Field(default="", description="Above-the-fold HTML snippet after H1")
    body_html: str = Field(default="", description="Raw body HTML for structural analysis")

    # Internal link graph metrics
    inlinks: int = Field(default=0, ge=0, description="Number of internal pages linking to this page")
    outlinks: int = Field(default=0, ge=0, description="Number of internal links from this page")
    internal_outlinks: list = Field(default_factory=list, description="Detailed outlinks: [{target_url, anchor_text, location}]")
    breadcrumb_links: list = Field(default_factory=list, description="Breadcrumb nav links: [{target_url, anchor_text}]")
    schema_types: list = Field(default_factory=list, description="JSON-LD @type values found on page")
    link_authority_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="PageRank-ish score derived from internal link graph"
    )

    # Data source metrics
    gsc: GSCMetrics = Field(default_factory=GSCMetrics)
    ga4: GA4Metrics = Field(default_factory=GA4Metrics)

    @property
    def is_canonical(self) -> bool:
        """Check if this URL is its own canonical."""
        return self.canonical_url is None or self.canonical_url == self.url

    @property
    def clicks_to_sessions_ratio(self) -> Optional[float]:
        """
        Tracking sanity check: GSC clicks should roughly equal GA4 sessions.

        Returns None if insufficient data.
        Healthy range: 0.7 - 1.3
        """
        if self.gsc.clicks_28d == 0:
            return None
        if self.ga4.sessions_28d == 0:
            return 0.0
        return self.ga4.sessions_28d / self.gsc.clicks_28d

    @property
    def tracking_sanity_ok(self) -> bool:
        """
        Returns True if tracking appears consistent.

        If False → Governor should enter OBSERVE_ONLY for this asset.
        """
        ratio = self.clicks_to_sessions_ratio
        if ratio is None:
            return True  # No data to validate
        return 0.5 <= ratio <= 2.0  # Generous bounds for noise

    @property
    def has_revenue_signal(self) -> bool:
        """Returns True if this page has demonstrated revenue contribution."""
        return self.ga4.revenue_28d > 0 or self.ga4.purchases_28d > 0

    @property
    def revenue_per_session(self) -> float:
        """Revenue per landing session from organic search."""
        return self.ga4.revenue_per_session

    def compute_cannibalization_risk(self, other: "PageAsset", overlap_threshold: float = 0.3) -> float:
        """
        Compute cannibalization risk between this page and another.

        Returns a score from 0.0 (no risk) to 1.0 (high risk).
        Based on query overlap in top_queries.
        """
        if not self.gsc.top_queries or not other.gsc.top_queries:
            return 0.0

        self_queries = {q.query.lower() for q in self.gsc.top_queries}
        other_queries = {q.query.lower() for q in other.gsc.top_queries}

        if not self_queries or not other_queries:
            return 0.0

        intersection = self_queries & other_queries
        union = self_queries | other_queries

        jaccard = len(intersection) / len(union) if union else 0.0
        return jaccard
