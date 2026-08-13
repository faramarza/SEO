"""
Google Analytics 4 client.

Stub implementation - requires credentials configuration.
"""

from datetime import date, timedelta
from typing import Any, Optional

from ..models.page_asset import GA4Metrics


class GA4Client:
    """
    Client for Google Analytics 4 Data API.

    Pulls:
    - Sessions/users by landing page
    - Engagement (engaged sessions, engagement time)
    - Ecommerce: purchases, revenue, add_to_cart, begin_checkout

    Metrics (per landing page):
    - sessions, engaged_sessions, engagement_rate
    - purchases, revenue, purchase_rate
    """

    def __init__(
        self,
        property_id: str,
        credentials_path: Optional[str] = None,
    ):
        """
        Initialize GA4 client.

        Args:
            property_id: The GA4 property ID (e.g., '123456789')
            credentials_path: Path to service account JSON credentials
        """
        self.property_id = property_id
        self.credentials_path = credentials_path
        self._client = None

    def _get_client(self) -> Any:
        """
        Initialize the GA4 Data API client.

        Returns None if credentials are not configured.
        """
        if self._client is not None:
            return self._client

        if self.credentials_path is None:
            return None

        try:
            from google.analytics.data_v1beta import BetaAnalyticsDataClient
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=['https://www.googleapis.com/auth/analytics.readonly']
            )

            self._client = BetaAnalyticsDataClient(credentials=credentials)
            return self._client
        except Exception:
            return None

    def test_connection(self) -> bool:
        """Test if the GA4 API connection works."""
        client = self._get_client()
        if client is None:
            return False

        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Metric,
            )

            # Simple test query
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
                metrics=[Metric(name="sessions")],
                limit=1,
            )

            client.run_report(request)
            return True
        except Exception:
            return False

    def get_page_metrics(
        self,
        page_path: str,
        days: int = 28,
    ) -> GA4Metrics:
        """
        Get GA4 metrics for a specific landing page.

        Args:
            page_path: The page path to get metrics for (e.g., '/products/widget')
            days: Number of days to look back (default: 28)

        Returns:
            GA4Metrics object with aggregated data
        """
        client = self._get_client()
        if client is None:
            return GA4Metrics()

        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                Filter,
                FilterExpression,
            )

            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=days)

            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                date_ranges=[DateRange(
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                )],
                dimensions=[Dimension(name="landingPage")],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="engagedSessions"),
                    Metric(name="engagementRate"),
                    Metric(name="ecommercePurchases"),
                    Metric(name="purchaseRevenue"),
                    Metric(name="totalUsers"),
                    Metric(name="addToCarts"),
                    Metric(name="bounceRate"),
                ],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="landingPage",
                        string_filter=Filter.StringFilter(
                            value=page_path,
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    ),
                ),
                limit=1,
            )

            response = client.run_report(request)

            if not response.rows:
                return GA4Metrics()

            row = response.rows[0]
            metrics = {
                m.name: float(row.metric_values[i].value)
                for i, m in enumerate(response.metric_headers)
            }

            sessions = int(metrics.get('sessions', 0))
            purchases = int(metrics.get('ecommercePurchases', 0))
            engaged_sessions = int(metrics.get('engagedSessions', 0))

            return GA4Metrics(
                sessions_28d=sessions,
                users_28d=int(metrics.get('totalUsers', 0)),
                engaged_sessions_28d=engaged_sessions,
                engagement_rate_28d=float(metrics.get('engagementRate', 0.0)) or (engaged_sessions / sessions if sessions > 0 else 0.0),
                purchases_28d=purchases,
                conversions_28d=purchases,
                revenue_28d=float(metrics.get('purchaseRevenue', 0.0)),
                purchase_rate_28d=(purchases / sessions if sessions > 0 else 0.0),
                add_to_carts_28d=int(metrics.get('addToCarts', 0)),
                bounce_rate_28d=float(metrics.get('bounceRate', 0.0)),
            )

        except Exception:
            return GA4Metrics()

    def get_landing_page_report(
        self,
        days: int = 28,
        min_sessions: int = 10,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """
        Get metrics for all landing pages with minimum traffic.

        Returns a list of dicts with page_path and GA4Metrics.
        """
        client = self._get_client()
        if client is None:
            return []

        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                OrderBy,
            )

            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=days)

            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                date_ranges=[DateRange(
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                )],
                dimensions=[Dimension(name="landingPage")],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="engagedSessions"),
                    Metric(name="engagementRate"),
                    Metric(name="ecommercePurchases"),
                    Metric(name="purchaseRevenue"),
                    Metric(name="totalUsers"),
                    Metric(name="addToCarts"),
                    Metric(name="bounceRate"),
                ],
                order_bys=[OrderBy(
                    metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                    desc=True,
                )],
                limit=limit,
            )

            response = client.run_report(request)
            results = []

            for row in response.rows:
                page_path = row.dimension_values[0].value
                metrics = {
                    m.name: float(row.metric_values[i].value)
                    for i, m in enumerate(response.metric_headers)
                }

                sessions = int(metrics.get('sessions', 0))
                if sessions < min_sessions:
                    continue

                purchases = int(metrics.get('ecommercePurchases', 0))
                engaged_sessions = int(metrics.get('engagedSessions', 0))

                results.append({
                    'page_path': page_path,
                    'ga4_metrics': GA4Metrics(
                        sessions_28d=sessions,
                        users_28d=int(metrics.get('totalUsers', 0)),
                        engaged_sessions_28d=engaged_sessions,
                        engagement_rate_28d=float(metrics.get('engagementRate', 0.0)) or (engaged_sessions / sessions if sessions > 0 else 0.0),
                        purchases_28d=purchases,
                        conversions_28d=purchases,
                        revenue_28d=float(metrics.get('purchaseRevenue', 0.0)),
                        purchase_rate_28d=(purchases / sessions if sessions > 0 else 0.0),
                        add_to_carts_28d=int(metrics.get('addToCarts', 0)),
                        bounce_rate_28d=float(metrics.get('bounceRate', 0.0)),
                    ),
                })

            return results

        except Exception:
            return []

    def get_item_data(self, days: int = 28) -> dict:
        """Item-scoped ecommerce data (which PRODUCTS actually sell), across all
        channels, by item name. The landing-page pull can't tell you a product's
        real sales; this can — so review/CRO priority can rank by what makes money.
        Returns {item_name: {purchased, revenue, added_to_cart, viewed}}."""
        client = self._get_client()
        if client is None:
            return {}
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest, DateRange, Dimension, Metric, OrderBy,
            )
            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=days)
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                date_ranges=[DateRange(start_date=start_date.isoformat(),
                                       end_date=end_date.isoformat())],
                dimensions=[Dimension(name="itemName")],
                metrics=[
                    Metric(name="itemsViewed"),
                    Metric(name="itemsAddedToCart"),
                    Metric(name="itemsPurchased"),
                    Metric(name="itemRevenue"),
                ],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="itemRevenue"),
                                   desc=True)],
                limit=10000,
            )
            response = client.run_report(request)
            out = {}
            for row in response.rows:
                name = row.dimension_values[0].value
                m = {h.name: row.metric_values[i].value
                     for i, h in enumerate(response.metric_headers)}
                out[name] = {
                    "viewed": int(float(m.get("itemsViewed", 0))),
                    "added_to_cart": int(float(m.get("itemsAddedToCart", 0))),
                    "purchased": int(float(m.get("itemsPurchased", 0))),
                    "revenue": round(float(m.get("itemRevenue", 0)), 2),
                }
            return out
        except Exception as e:
            print(f"GA4 item-data error: {e}")
            return {}

    def get_account_totals(self, days: int = 28) -> dict:
        """Account-wide ecommerce totals across ALL channels (no landing-page
        breakdown, no organic filter). Average Order Value is a BUSINESS constant
        — it must come from all sales, not the organic sliver — so revenue-based
        features have a reliable AOV even when organic conversions are sparse.
        Returns {sessions, purchases, revenue, add_to_carts, aov}."""
        client = self._get_client()
        if client is None:
            return {}
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest, DateRange, Metric,
            )
            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=days)
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                date_ranges=[DateRange(start_date=start_date.isoformat(),
                                       end_date=end_date.isoformat())],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="ecommercePurchases"),
                    Metric(name="purchaseRevenue"),
                    Metric(name="addToCarts"),
                ],
            )
            response = client.run_report(request)
            if not response.rows:
                return {"sessions": 0, "purchases": 0, "revenue": 0.0,
                        "add_to_carts": 0, "aov": None}
            m = {h.name: response.rows[0].metric_values[i].value
                 for i, h in enumerate(response.metric_headers)}
            purchases = int(float(m.get("ecommercePurchases", 0)))
            revenue = float(m.get("purchaseRevenue", 0))
            return {
                "sessions": int(float(m.get("sessions", 0))),
                "purchases": purchases,
                "revenue": round(revenue, 2),
                "add_to_carts": int(float(m.get("addToCarts", 0))),
                "aov": round(revenue / purchases, 2) if purchases > 0 and revenue > 0 else None,
            }
        except Exception as e:
            print(f"GA4 account-totals error: {e}")
            return {}

    def get_page_data(self, days: int = 28, organic_only: bool = True) -> dict[str, dict]:
        """
        Get page-level data for all landing pages.

        Args:
            days: Number of days to look back
            organic_only: Filter to organic search traffic only

        Returns:
            Dict mapping URL -> page data dict
        """
        client = self._get_client()
        if client is None:
            return {}

        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                Filter,
                FilterExpression,
                OrderBy,
            )

            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=days)

            dimensions = [Dimension(name="landingPage")]

            # Add channel filter for organic only
            dimension_filter = None
            if organic_only:
                dimension_filter = FilterExpression(
                    filter=Filter(
                        field_name="sessionDefaultChannelGroup",
                        string_filter=Filter.StringFilter(
                            value="Organic Search",
                            match_type=Filter.StringFilter.MatchType.EXACT,
                        ),
                    ),
                )

            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                date_ranges=[DateRange(
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                )],
                dimensions=dimensions,
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="engagedSessions"),
                    Metric(name="ecommercePurchases"),
                    Metric(name="purchaseRevenue"),
                    Metric(name="addToCarts"),
                    Metric(name="bounceRate"),
                ],
                dimension_filter=dimension_filter,
                order_bys=[OrderBy(
                    metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                    desc=True,
                )],
                limit=25000,
            )

            response = client.run_report(request)
            result = {}

            for row in response.rows:
                url = row.dimension_values[0].value
                metrics = {
                    m.name: row.metric_values[i].value
                    for i, m in enumerate(response.metric_headers)
                }

                result[url] = {
                    'sessions': int(float(metrics.get('sessions', 0))),
                    'users': int(float(metrics.get('totalUsers', 0))),
                    'engaged_sessions': int(float(metrics.get('engagedSessions', 0))),
                    'conversions': int(float(metrics.get('ecommercePurchases', 0))),
                    'revenue': float(metrics.get('purchaseRevenue', 0)),
                    'add_to_carts': int(float(metrics.get('addToCarts', 0))),
                    'bounce_rate': float(metrics.get('bounceRate', 0)),
                }

            return result

        except Exception as e:
            print(f"GA4 API error: {e}")
            return {}
