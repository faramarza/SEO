"""
Google Search Console client.

Stub implementation - requires credentials configuration.
"""

from datetime import date, timedelta
from typing import Any, Optional

from ..models.page_asset import GSCMetrics, TopQuery


class GSCClient:
    """
    Client for Google Search Console API.

    Pulls:
    - searchanalytics.query (dimensions: page, query, country, device, date)
    - searchanalytics.page (dimensions: page, date, optionally device)
    - Index coverage / sitemaps (if available)

    Metrics:
    - impressions, clicks, CTR, average position
    """

    def __init__(
        self,
        site_url: str,
        credentials_path: Optional[str] = None,
    ):
        """
        Initialize GSC client.

        Args:
            site_url: The property URL in GSC (e.g., 'https://www.example.com/')
            credentials_path: Path to service account JSON credentials
        """
        self.site_url = site_url
        self.credentials_path = credentials_path
        self._service = None

    def _get_service(self) -> Any:
        """
        Initialize the GSC API service.

        Returns None if credentials are not configured.
        """
        if self._service is not None:
            return self._service

        if self.credentials_path is None:
            return None

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=['https://www.googleapis.com/auth/webmasters.readonly']
            )

            self._service = build('searchconsole', 'v1', credentials=credentials)
            return self._service
        except Exception:
            return None

    def test_connection(self) -> bool:
        """Test if the GSC API connection works."""
        service = self._get_service()
        if service is None:
            return False

        try:
            # Try to list sites
            service.sites().list().execute()
            return True
        except Exception:
            return False

    def get_page_metrics(
        self,
        page_url: str,
        days: int = 28,
    ) -> GSCMetrics:
        """
        Get GSC metrics for a specific page.

        Args:
            page_url: The URL to get metrics for
            days: Number of days to look back (default: 28)

        Returns:
            GSCMetrics object with aggregated data
        """
        service = self._get_service()
        if service is None:
            # Return empty metrics if not configured
            return GSCMetrics()

        end_date = date.today() - timedelta(days=3)  # GSC data has ~3 day lag
        start_date = end_date - timedelta(days=days)

        try:
            # Query for page-level metrics
            request = {
                'startDate': start_date.isoformat(),
                'endDate': end_date.isoformat(),
                'dimensions': ['page'],
                'dimensionFilterGroups': [{
                    'filters': [{
                        'dimension': 'page',
                        'operator': 'equals',
                        'expression': page_url
                    }]
                }],
                'rowLimit': 1
            }

            response = service.searchanalytics().query(
                siteUrl=self.site_url,
                body=request
            ).execute()

            if 'rows' not in response or not response['rows']:
                return GSCMetrics()

            row = response['rows'][0]

            # Get top queries for this page
            top_queries = self._get_top_queries(page_url, start_date, end_date)

            # Calculate query dispersion
            query_dispersion = self._calculate_query_dispersion(top_queries)

            return GSCMetrics(
                impressions_28d=int(row.get('impressions', 0)),
                clicks_28d=int(row.get('clicks', 0)),
                ctr_28d=float(row.get('ctr', 0.0)),
                avg_position_28d=float(row.get('position', 0.0)),
                query_dispersion=query_dispersion,
                top_queries=top_queries,
            )

        except Exception:
            return GSCMetrics()

    def _get_top_queries(
        self,
        page_url: str,
        start_date: date,
        end_date: date,
        limit: int = 20,
    ) -> list[TopQuery]:
        """Get top queries for a specific page."""
        service = self._get_service()
        if service is None:
            return []

        try:
            request = {
                'startDate': start_date.isoformat(),
                'endDate': end_date.isoformat(),
                'dimensions': ['query'],
                'dimensionFilterGroups': [{
                    'filters': [{
                        'dimension': 'page',
                        'operator': 'equals',
                        'expression': page_url
                    }]
                }],
                'rowLimit': limit
            }

            response = service.searchanalytics().query(
                siteUrl=self.site_url,
                body=request
            ).execute()

            if 'rows' not in response:
                return []

            return [
                TopQuery(
                    query=row['keys'][0],
                    clicks=int(row.get('clicks', 0)),
                    impressions=int(row.get('impressions', 0)),
                    ctr=float(row.get('ctr', 0.0)),
                    position=float(row.get('position', 0.0)),
                )
                for row in response['rows']
            ]

        except Exception:
            return []

    def _calculate_query_dispersion(self, queries: list[TopQuery]) -> float:
        """
        Calculate query dispersion score.

        0 = single query dominance
        1 = highly dispersed queries

        Uses normalized entropy of click distribution.
        """
        if not queries:
            return 0.0

        total_clicks = sum(q.clicks for q in queries)
        if total_clicks == 0:
            return 0.0

        import math

        # Calculate entropy
        entropy = 0.0
        for q in queries:
            if q.clicks > 0:
                p = q.clicks / total_clicks
                entropy -= p * math.log2(p)

        # Normalize by max entropy (uniform distribution)
        max_entropy = math.log2(len(queries)) if len(queries) > 1 else 1.0

        return entropy / max_entropy if max_entropy > 0 else 0.0
