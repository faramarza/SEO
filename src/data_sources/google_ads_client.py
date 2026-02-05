"""
Google Ads API Client (Read-Only)

Provides read-only access to Google Ads data for demand analysis.
This client NEVER calls mutate endpoints.

Permitted Data:
- Search term reports (query-level)
- Campaign/ad group structure
- Impression Share (IS, IS lost to budget, IS lost to rank)
- Cost, conversions, conversion value
- PMax search term insights
- Brand vs non-brand query capture
- Change history (read only)

Prohibited Actions:
- Creating or modifying campaigns
- Changing budgets or bids
- Adding keywords
- Altering asset groups
- Writing creatives
- Enabling or disabling entities
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from pathlib import Path
import json


class CampaignType(str, Enum):
    """Campaign types we track."""
    SEARCH = "search"
    PMAX = "pmax"
    SHOPPING = "shopping"
    DISPLAY = "display"
    VIDEO = "video"
    OTHER = "other"


class QueryMatchType(str, Enum):
    """How the query matched to keywords."""
    EXACT = "exact"
    PHRASE = "phrase"
    BROAD = "broad"
    UNKNOWN = "unknown"


@dataclass
class AdsQueryData:
    """
    Query-level data from Google Ads.

    Used to understand monetization signals, not to judge demand.
    """
    query: str
    campaign_id: str
    campaign_name: str
    campaign_type: CampaignType
    ad_group_id: Optional[str] = None
    ad_group_name: Optional[str] = None

    # Volume metrics
    impressions: int = 0
    clicks: int = 0
    cost: float = 0.0

    # Conversion metrics
    conversions: float = 0.0
    conversion_value: float = 0.0

    # Impression share metrics (key for constraint detection)
    search_impression_share: Optional[float] = None  # 0-1, what we captured
    search_lost_is_budget: Optional[float] = None    # 0-1, lost due to budget
    search_lost_is_rank: Optional[float] = None      # 0-1, lost due to rank

    # Match info
    match_type: QueryMatchType = QueryMatchType.UNKNOWN
    is_brand_query: bool = False

    # Computed
    cpc: float = 0.0
    ctr: float = 0.0
    conversion_rate: float = 0.0
    roas: float = 0.0

    def __post_init__(self):
        """Compute derived metrics."""
        if self.clicks > 0:
            self.cpc = self.cost / self.clicks
        if self.impressions > 0:
            self.ctr = self.clicks / self.impressions
        if self.clicks > 0:
            self.conversion_rate = self.conversions / self.clicks
        if self.cost > 0:
            self.roas = self.conversion_value / self.cost


@dataclass
class CampaignSummary:
    """Summary of a campaign's performance."""
    campaign_id: str
    campaign_name: str
    campaign_type: CampaignType
    status: str  # ENABLED, PAUSED, REMOVED

    impressions: int = 0
    clicks: int = 0
    cost: float = 0.0
    conversions: float = 0.0
    conversion_value: float = 0.0

    # Impression share
    search_impression_share: Optional[float] = None
    search_lost_is_budget: Optional[float] = None
    search_lost_is_rank: Optional[float] = None


@dataclass
class AdsAccountData:
    """
    Aggregated Google Ads account data for analysis.

    This is the primary interface for constraint detection.
    """
    customer_id: str
    date_range_start: datetime
    date_range_end: datetime

    # Query-level data indexed by query string
    queries: dict[str, AdsQueryData] = field(default_factory=dict)

    # Campaign summaries
    campaigns: dict[str, CampaignSummary] = field(default_factory=dict)

    # Brand queries (for brand vs non-brand analysis)
    brand_terms: list[str] = field(default_factory=list)

    # PMax query insights (limited visibility)
    pmax_queries: dict[str, AdsQueryData] = field(default_factory=dict)

    def get_query_data(self, query: str) -> Optional[AdsQueryData]:
        """Get data for a specific query."""
        return self.queries.get(query.lower())

    def get_queries_for_url(self, url: str) -> list[AdsQueryData]:
        """
        Get queries that might be relevant to a URL.

        Note: This is heuristic - Ads doesn't directly link queries to landing pages
        in the same way GSC does. We use final URL data when available.
        """
        # Would need to join with ad group final URLs
        return []

    def has_monetization_for_query(self, query: str) -> bool:
        """Check if query has any paid monetization."""
        q = query.lower()
        return q in self.queries or q in self.pmax_queries

    def get_impression_share_constraint(self, query: str) -> Optional[dict]:
        """
        Detect if impression share indicates a constraint.

        Returns constraint info if IS < 80% with meaningful lost IS.
        """
        data = self.get_query_data(query)
        if not data:
            return None

        if data.search_impression_share is None:
            return None

        # If we're capturing less than 80% of impressions
        if data.search_impression_share < 0.8:
            constraint = {
                "impression_share": data.search_impression_share,
                "type": None,
                "severity": None,
            }

            # Determine if budget or rank constrained
            if data.search_lost_is_budget and data.search_lost_is_budget > 0.1:
                constraint["type"] = "budget_constrained"
                constraint["severity"] = "high" if data.search_lost_is_budget > 0.3 else "medium"
            elif data.search_lost_is_rank and data.search_lost_is_rank > 0.1:
                constraint["type"] = "rank_constrained"
                constraint["severity"] = "high" if data.search_lost_is_rank > 0.3 else "medium"

            return constraint

        return None

    def is_pmax_absorbing_query(self, query: str) -> bool:
        """
        Detect if PMax is absorbing this query (especially brand/exact).

        If query appears in PMax but not in Search campaigns,
        PMax may be absorbing the traffic.
        """
        q = query.lower()
        in_pmax = q in self.pmax_queries
        in_search = q in self.queries and self.queries[q].campaign_type == CampaignType.SEARCH

        return in_pmax and not in_search

    def get_monetization_summary(self) -> dict:
        """Get high-level monetization summary."""
        total_queries = len(self.queries) + len(self.pmax_queries)
        total_cost = sum(q.cost for q in self.queries.values())
        total_conversions = sum(q.conversions for q in self.queries.values())
        total_value = sum(q.conversion_value for q in self.queries.values())

        brand_queries = [q for q in self.queries.values() if q.is_brand_query]
        nonbrand_queries = [q for q in self.queries.values() if not q.is_brand_query]

        return {
            "total_queries": total_queries,
            "total_cost": total_cost,
            "total_conversions": total_conversions,
            "total_value": total_value,
            "roas": total_value / total_cost if total_cost > 0 else 0,
            "brand_query_count": len(brand_queries),
            "nonbrand_query_count": len(nonbrand_queries),
            "pmax_query_count": len(self.pmax_queries),
        }


class GoogleAdsClient:
    """
    Read-only Google Ads API client.

    IMPORTANT: This client only reads data. It never calls mutate endpoints.

    Supports two authentication methods:
    1. OAuth2 with google-ads.yaml (recommended for user accounts)
    2. Service Account with credentials.json (for automation)
    """

    def __init__(
        self,
        credentials_path: Optional[str] = None,
        customer_id: Optional[str] = None,
        brand_terms: Optional[list[str]] = None,
        use_service_account: bool = False,
        developer_token: Optional[str] = None,
        login_customer_id: Optional[str] = None,
    ):
        """
        Initialize the Google Ads client.

        Args:
            credentials_path: Path to google-ads.yaml OR service account JSON
            customer_id: Google Ads customer ID (without dashes)
            brand_terms: List of brand terms to identify brand queries
            use_service_account: If True, use service account auth instead of OAuth
            developer_token: Required for API access (get from Google Ads UI)
            login_customer_id: MCC account ID if using manager account
        """
        self.credentials_path = credentials_path
        self.customer_id = customer_id
        self.brand_terms = [t.lower() for t in (brand_terms or [])]
        self.use_service_account = use_service_account
        self.developer_token = developer_token
        self.login_customer_id = login_customer_id
        self._client = None
        self._initialized = False

    def _init_client(self) -> bool:
        """Initialize the Google Ads API client."""
        if self._initialized:
            return self._client is not None

        self._initialized = True

        try:
            from google.ads.googleads.client import GoogleAdsClient as GAdsClient

            if self.use_service_account:
                # Service Account authentication (like GSC/GA4)
                return self._init_service_account_client(GAdsClient)
            else:
                # OAuth2 authentication (google-ads.yaml)
                return self._init_oauth_client(GAdsClient)

        except ImportError:
            print("google-ads package not installed. Run: pip install google-ads")
        except Exception as e:
            print(f"Failed to initialize Google Ads client: {e}")

        return False

    def _init_oauth_client(self, GAdsClient) -> bool:
        """Initialize using OAuth2 (google-ads.yaml)."""
        try:
            if self.credentials_path and Path(self.credentials_path).exists():
                self._client = GAdsClient.load_from_storage(self.credentials_path)
                return True
            else:
                # Try default location
                default_path = Path.home() / "google-ads.yaml"
                if default_path.exists():
                    self._client = GAdsClient.load_from_storage(str(default_path))
                    return True
        except Exception as e:
            print(f"OAuth init failed: {e}")
        return False

    def _init_service_account_client(self, GAdsClient) -> bool:
        """Initialize using Service Account (credentials.json)."""
        try:
            from google.oauth2 import service_account

            if not self.developer_token:
                print("Error: developer_token required for Google Ads API")
                return False

            if not self.credentials_path or not Path(self.credentials_path).exists():
                print(f"Error: Service account file not found: {self.credentials_path}")
                return False

            # Load service account credentials
            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=["https://www.googleapis.com/auth/adwords"],
            )

            # If using domain-wide delegation, impersonate a user
            # credentials = credentials.with_subject("user@yourdomain.com")

            # Build config dict for GoogleAdsClient
            config = {
                "developer_token": self.developer_token,
                "use_proto_plus": True,
            }

            if self.login_customer_id:
                config["login_customer_id"] = self.login_customer_id

            self._client = GAdsClient(credentials=credentials, **config)
            return True

        except Exception as e:
            print(f"Service account init failed: {e}")
            return False

    def _is_brand_query(self, query: str) -> bool:
        """Check if query contains brand terms."""
        q = query.lower()
        return any(term in q for term in self.brand_terms)

    def _parse_campaign_type(self, campaign_type_str: str) -> CampaignType:
        """Parse campaign type from API response."""
        type_map = {
            "SEARCH": CampaignType.SEARCH,
            "PERFORMANCE_MAX": CampaignType.PMAX,
            "SHOPPING": CampaignType.SHOPPING,
            "DISPLAY": CampaignType.DISPLAY,
            "VIDEO": CampaignType.VIDEO,
        }
        return type_map.get(campaign_type_str, CampaignType.OTHER)

    def fetch_search_terms(
        self,
        days: int = 28,
        min_impressions: int = 10,
    ) -> AdsAccountData:
        """
        Fetch search term report data.

        This is READ-ONLY - no mutations.
        """
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        account_data = AdsAccountData(
            customer_id=self.customer_id or "",
            date_range_start=start_date,
            date_range_end=end_date,
            brand_terms=self.brand_terms,
        )

        if not self._init_client():
            return account_data

        try:
            ga_service = self._client.get_service("GoogleAdsService")

            # Search term report query (READ-ONLY)
            # Note: Impression share metrics not available at search term level
            query = f"""
                SELECT
                    search_term_view.search_term,
                    campaign.id,
                    campaign.name,
                    campaign.advertising_channel_type,
                    ad_group.id,
                    ad_group.name,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.cost_micros,
                    metrics.conversions,
                    metrics.conversions_value
                FROM search_term_view
                WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}'
                    AND '{end_date.strftime("%Y-%m-%d")}'
                    AND metrics.impressions >= {min_impressions}
                ORDER BY metrics.impressions DESC
                LIMIT 10000
            """

            response = ga_service.search(
                customer_id=self.customer_id,
                query=query,
            )

            for row in response:
                query_text = row.search_term_view.search_term.lower()
                campaign_type = self._parse_campaign_type(
                    row.campaign.advertising_channel_type.name
                )

                query_data = AdsQueryData(
                    query=query_text,
                    campaign_id=str(row.campaign.id),
                    campaign_name=row.campaign.name,
                    campaign_type=campaign_type,
                    ad_group_id=str(row.ad_group.id) if row.ad_group.id else None,
                    ad_group_name=row.ad_group.name if row.ad_group.name else None,
                    impressions=row.metrics.impressions,
                    clicks=row.metrics.clicks,
                    cost=row.metrics.cost_micros / 1_000_000,
                    conversions=row.metrics.conversions,
                    conversion_value=row.metrics.conversions_value,
                    # Impression share not available at search term level
                    # Get from campaign-level data instead
                    search_impression_share=None,
                    search_lost_is_budget=None,
                    search_lost_is_rank=None,
                    is_brand_query=self._is_brand_query(query_text),
                )

                if campaign_type == CampaignType.PMAX:
                    account_data.pmax_queries[query_text] = query_data
                else:
                    account_data.queries[query_text] = query_data

        except Exception as e:
            print(f"Error fetching search terms: {e}")

        return account_data

    def fetch_campaign_summary(self, days: int = 28) -> dict[str, CampaignSummary]:
        """
        Fetch campaign-level performance summary.

        This is READ-ONLY - no mutations.
        """
        campaigns = {}

        if not self._init_client():
            return campaigns

        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        try:
            ga_service = self._client.get_service("GoogleAdsService")

            # Note: Removed impression share metrics - they require specific
            # account/campaign setups and cause query failures for many accounts
            query = f"""
                SELECT
                    campaign.id,
                    campaign.name,
                    campaign.status,
                    campaign.advertising_channel_type,
                    metrics.impressions,
                    metrics.clicks,
                    metrics.cost_micros,
                    metrics.conversions,
                    metrics.conversions_value
                FROM campaign
                WHERE segments.date BETWEEN '{start_date.strftime("%Y-%m-%d")}'
                    AND '{end_date.strftime("%Y-%m-%d")}'
                    AND campaign.status != 'REMOVED'
            """

            response = ga_service.search(
                customer_id=self.customer_id,
                query=query,
            )

            for row in response:
                campaign_id = str(row.campaign.id)
                campaigns[campaign_id] = CampaignSummary(
                    campaign_id=campaign_id,
                    campaign_name=row.campaign.name,
                    campaign_type=self._parse_campaign_type(
                        row.campaign.advertising_channel_type.name
                    ),
                    status=row.campaign.status.name,
                    impressions=row.metrics.impressions,
                    clicks=row.metrics.clicks,
                    cost=row.metrics.cost_micros / 1_000_000,
                    conversions=row.metrics.conversions,
                    conversion_value=row.metrics.conversions_value,
                    # Impression share metrics removed - not reliably available
                    search_impression_share=None,
                    search_lost_is_budget=None,
                    search_lost_is_rank=None,
                )

        except Exception as e:
            print(f"Error fetching campaign summary: {e}")

        return campaigns

    def load_from_cache(self, cache_path: str) -> Optional[AdsAccountData]:
        """Load Ads data from a cached JSON file."""
        path = Path(cache_path)
        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)

            account_data = AdsAccountData(
                customer_id=data.get("customer_id", ""),
                date_range_start=datetime.fromisoformat(data["date_range_start"]),
                date_range_end=datetime.fromisoformat(data["date_range_end"]),
                brand_terms=data.get("brand_terms", []),
            )

            for q_data in data.get("queries", []):
                query = AdsQueryData(
                    query=q_data["query"],
                    campaign_id=q_data["campaign_id"],
                    campaign_name=q_data["campaign_name"],
                    campaign_type=CampaignType(q_data["campaign_type"]),
                    impressions=q_data.get("impressions", 0),
                    clicks=q_data.get("clicks", 0),
                    cost=q_data.get("cost", 0),
                    conversions=q_data.get("conversions", 0),
                    conversion_value=q_data.get("conversion_value", 0),
                    search_impression_share=q_data.get("search_impression_share"),
                    search_lost_is_budget=q_data.get("search_lost_is_budget"),
                    search_lost_is_rank=q_data.get("search_lost_is_rank"),
                    is_brand_query=q_data.get("is_brand_query", False),
                )
                account_data.queries[query.query] = query

            return account_data

        except Exception as e:
            print(f"Error loading Ads cache: {e}")
            return None

    def save_to_cache(self, account_data: AdsAccountData, cache_path: str):
        """Save Ads data to cache for offline use."""
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "customer_id": account_data.customer_id,
            "date_range_start": account_data.date_range_start.isoformat(),
            "date_range_end": account_data.date_range_end.isoformat(),
            "brand_terms": account_data.brand_terms,
            "queries": [
                {
                    "query": q.query,
                    "campaign_id": q.campaign_id,
                    "campaign_name": q.campaign_name,
                    "campaign_type": q.campaign_type.value,
                    "impressions": q.impressions,
                    "clicks": q.clicks,
                    "cost": q.cost,
                    "conversions": q.conversions,
                    "conversion_value": q.conversion_value,
                    "search_impression_share": q.search_impression_share,
                    "search_lost_is_budget": q.search_lost_is_budget,
                    "search_lost_is_rank": q.search_lost_is_rank,
                    "is_brand_query": q.is_brand_query,
                }
                for q in account_data.queries.values()
            ],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2)
