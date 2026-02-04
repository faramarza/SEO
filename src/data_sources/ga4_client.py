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

            return GA4Metrics(
                sessions_28d=sessions,
                engaged_sessions_28d=int(metrics.get('engagedSessions', 0)),
                engagement_rate_28d=float(metrics.get('engagementRate', 0.0)),
                purchases_28d=purchases,
                revenue_28d=float(metrics.get('purchaseRevenue', 0.0)),
                purchase_rate_28d=(purchases / sessions if sessions > 0 else 0.0),
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

                results.append({
                    'page_path': page_path,
                    'ga4_metrics': GA4Metrics(
                        sessions_28d=sessions,
                        engaged_sessions_28d=int(metrics.get('engagedSessions', 0)),
                        engagement_rate_28d=float(metrics.get('engagementRate', 0.0)),
                        purchases_28d=purchases,
                        revenue_28d=float(metrics.get('purchaseRevenue', 0.0)),
                        purchase_rate_28d=(purchases / sessions if sessions > 0 else 0.0),
                    ),
                })

            return results

        except Exception:
            return []
