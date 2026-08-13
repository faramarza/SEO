"""
Google Search Console client.

Stub implementation - requires credentials configuration.
"""

import os
from datetime import date, timedelta
from typing import Any, Optional

from ..models.page_asset import GSCMetrics, TopQuery


def _queries_per_page() -> int:
    """How many top queries to retain per page in get_page_data — env-tunable
    (GSC_QUERIES_PER_PAGE, default 10) so a larger store can widen the long-tail
    query universe fed to click-yield without editing code."""
    try:
        return max(1, int(os.environ.get("GSC_QUERIES_PER_PAGE", "").strip() or 10))
    except (TypeError, ValueError):
        return 10


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
                scopes=[
                    'https://www.googleapis.com/auth/webmasters.readonly',
                    'https://www.googleapis.com/auth/webmasters',  # For URL inspection
                    'https://www.googleapis.com/auth/indexing',    # For indexing requests
                ]
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

    def _query_all_rows(self, service, body: dict) -> list:
        """Run a Search Analytics query paginated to the FULL result set. GSC caps a
        single response at 25k rows; without startRow paging, any property with more
        than 25k page/query pairs is silently truncated (losing the long tail that
        feeds momentum + per-page query lists). Loops startRow until a short page."""
        PAGE = 25000
        body = dict(body)
        body['rowLimit'] = PAGE
        start, rows = 0, []
        while True:
            body['startRow'] = start
            resp = service.searchanalytics().query(siteUrl=self.site_url, body=body).execute()
            batch = resp.get('rows', [])
            if not batch:
                break
            rows.extend(batch)
            start += len(batch)
            if len(batch) < PAGE:
                break
        return rows

    def positions_for_window(self, end_offset_days: int = 0, days: int = 28) -> dict:
        """Return {(page_url, query): avg_position} for a window `days` long ending
        (today - 3 - end_offset_days). Used to compute position MOMENTUM by diffing a
        recent window against the prior one — the one thing GSC gives that a single
        snapshot can't: which queries are rising vs falling."""
        service = self._get_service()
        if service is None:
            return {}
        end_date = date.today() - timedelta(days=3 + end_offset_days)
        start_date = end_date - timedelta(days=days)
        try:
            body = {
                'startDate': start_date.isoformat(),
                'endDate': end_date.isoformat(),
                'dimensions': ['page', 'query'],
            }
            out = {}
            for r in self._query_all_rows(service, body):
                out[(r['keys'][0], r['keys'][1])] = float(r.get('position', 0.0))
            return out
        except Exception as e:
            print(f"GSC positions_for_window error: {e}")
            return {}

    def get_page_data(self, days: int = 28) -> dict[str, dict]:
        """
        Get page-level data for all pages in the property.

        Args:
            days: Number of days to look back

        Returns:
            Dict mapping URL -> page data dict
        """
        service = self._get_service()
        if service is None:
            return {}

        end_date = date.today() - timedelta(days=3)
        start_date = end_date - timedelta(days=days)

        try:
            # Get all pages (paginated to the full set)
            request = {
                'startDate': start_date.isoformat(),
                'endDate': end_date.isoformat(),
                'dimensions': ['page'],
            }

            page_rows = self._query_all_rows(service, request)
            if not page_rows:
                return {}

            result = {}
            for row in page_rows:
                url = row['keys'][0]
                result[url] = {
                    'clicks': int(row.get('clicks', 0)),
                    'impressions': int(row.get('impressions', 0)),
                    'ctr': float(row.get('ctr', 0.0)),
                    'position': float(row.get('position', 0.0)),
                    'queries': [],
                }

            # Fetch query data for all pages (paginated to the full set)
            query_request = {
                'startDate': start_date.isoformat(),
                'endDate': end_date.isoformat(),
                'dimensions': ['page', 'query'],
            }

            query_rows = self._query_all_rows(service, query_request)
            if query_rows:
                for row in query_rows:
                    url = row['keys'][0]
                    query = row['keys'][1]
                    if url in result:
                        result[url]['queries'].append({
                            'query': query,
                            'clicks': int(row.get('clicks', 0)),
                            'impressions': int(row.get('impressions', 0)),
                            'ctr': float(row.get('ctr', 0.0)),
                            'position': float(row.get('position', 0.0)),
                        })

                # Sort queries by impressions (descending) and keep the top N
                # (GSC_QUERIES_PER_PAGE, default 10).
                keep = _queries_per_page()
                for url in result:
                    result[url]['queries'] = sorted(
                        result[url]['queries'],
                        key=lambda x: x['impressions'],
                        reverse=True
                    )[:keep]

            return result

        except Exception as e:
            print(f"GSC API error: {e}")
            return {}

    def inspect_url(self, url: str) -> dict:
        """
        Inspect a URL using the URL Inspection API.

        Returns indexing status and other details.
        """
        service = self._get_service()
        if service is None:
            return {"error": "GSC not configured"}

        try:
            result = service.urlInspection().index().inspect(
                body={
                    "inspectionUrl": url,
                    "siteUrl": self.site_url
                }
            ).execute()

            inspection = result.get("inspectionResult", {})
            index_status = inspection.get("indexStatusResult", {})

            return {
                "url": url,
                "verdict": index_status.get("verdict", "UNKNOWN"),
                "coverage_state": index_status.get("coverageState", "UNKNOWN"),
                "robotstxt_state": index_status.get("robotsTxtState", "UNKNOWN"),
                "indexing_state": index_status.get("indexingState", "UNKNOWN"),
                "last_crawl_time": index_status.get("lastCrawlTime"),
                "page_fetch_state": index_status.get("pageFetchState", "UNKNOWN"),
                "crawled_as": index_status.get("crawledAs", "UNKNOWN"),
                "raw": inspection,
            }
        except Exception as e:
            return {"error": str(e)}

    def request_indexing(self, url: str) -> dict:
        """
        Request (re)indexing of a URL using the Indexing API.

        Note: The Indexing API is primarily for JobPosting and BroadcastEvent
        structured data. For other pages, use inspect_url() to check status.

        Returns success status or error.
        """
        if self.credentials_path is None:
            return {"success": False, "error": "Credentials not configured"}

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path,
                scopes=['https://www.googleapis.com/auth/indexing']
            )

            indexing_service = build('indexing', 'v3', credentials=credentials)

            result = indexing_service.urlNotifications().publish(
                body={
                    "url": url,
                    "type": "URL_UPDATED"  # or "URL_DELETED"
                }
            ).execute()

            return {
                "success": True,
                "url": url,
                "notification_time": result.get("urlNotificationMetadata", {}).get("latestUpdate", {}).get("notifyTime"),
                "raw": result,
            }
        except Exception as e:
            error_msg = str(e)
            # Provide helpful message for common errors
            if "Permission denied" in error_msg or "403" in error_msg:
                return {
                    "success": False,
                    "error": "Indexing API requires special permissions. The URL Inspection API may be used instead.",
                    "suggestion": "Use 'Inspect URL' to check indexing status, then manually request indexing in GSC."
                }
            return {"success": False, "error": error_msg}
