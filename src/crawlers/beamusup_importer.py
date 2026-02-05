"""
Beam Us Up Crawler Data Importer

Imports crawl data from Beam Us Up CSV exports to enrich PageAsset objects
with canonical URLs, indexability status, and other crawl data.

Export from Beam Us Up: File > Export > All URLs (CSV)
"""

import csv
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src.models.page_asset import PageAsset


class BeamUsUpImporter:
    """
    Imports crawl data from Beam Us Up CSV exports.

    Beam Us Up exports include columns like:
    - URL
    - Canonical
    - Status Code
    - Indexability (Indexable/Non-Indexable)
    - Title
    - H1
    - Word Count
    """

    # Common column name variations in Beam Us Up exports
    COLUMN_MAPPINGS = {
        'url': ['url', 'address', 'page url'],
        'canonical': ['canonical', 'canonical url', 'canonical link'],
        'status_code': ['status code', 'status', 'http status'],
        'indexable': ['indexability', 'indexable', 'index status'],
        'title': ['title', 'page title', 'title 1'],
        'h1': ['h1', 'h1-1', 'h1 1'],
        'word_count': ['word count', 'words', 'content words'],
    }

    def __init__(self):
        self._crawl_data: dict[str, dict] = {}

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for matching."""
        if not url:
            return ""
        # Remove trailing slash for consistent matching
        return url.rstrip('/').lower()

    def _find_column(self, headers: list[str], field: str) -> Optional[int]:
        """Find column index for a field, checking common variations."""
        headers_lower = [h.lower().strip() for h in headers]
        for variant in self.COLUMN_MAPPINGS.get(field, [field]):
            if variant.lower() in headers_lower:
                return headers_lower.index(variant.lower())
        return None

    def load_csv(self, csv_path: Path) -> int:
        """
        Load crawl data from Beam Us Up CSV export.

        Args:
            csv_path: Path to the CSV file

        Returns:
            Number of URLs loaded
        """
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {csv_path}")

        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            headers = next(reader)

            # Find column indices
            url_col = self._find_column(headers, 'url')
            canonical_col = self._find_column(headers, 'canonical')
            status_col = self._find_column(headers, 'status_code')
            indexable_col = self._find_column(headers, 'indexable')
            title_col = self._find_column(headers, 'title')
            h1_col = self._find_column(headers, 'h1')
            word_count_col = self._find_column(headers, 'word_count')

            if url_col is None:
                raise ValueError(f"Could not find URL column in CSV. Headers: {headers}")

            count = 0
            for row in reader:
                if len(row) <= url_col:
                    continue

                url = row[url_col].strip()
                if not url:
                    continue

                normalized_url = self._normalize_url(url)

                # Extract data
                data = {
                    'url': url,
                    'canonical_url': row[canonical_col].strip() if canonical_col is not None and len(row) > canonical_col else None,
                    'status_code': int(row[status_col]) if status_col is not None and len(row) > status_col and row[status_col].isdigit() else 200,
                    'indexable': self._parse_indexable(row[indexable_col] if indexable_col is not None and len(row) > indexable_col else 'Indexable'),
                    'title': row[title_col].strip() if title_col is not None and len(row) > title_col else '',
                    'h1': row[h1_col].strip() if h1_col is not None and len(row) > h1_col else '',
                    'word_count': int(row[word_count_col]) if word_count_col is not None and len(row) > word_count_col and row[word_count_col].isdigit() else 0,
                }

                self._crawl_data[normalized_url] = data
                count += 1

        return count

    def _parse_indexable(self, value: str) -> bool:
        """Parse indexability value from various formats."""
        if not value:
            return True
        value_lower = value.lower().strip()
        return value_lower in ('indexable', 'yes', 'true', '1', 'index')

    def enrich_asset(self, asset: PageAsset) -> PageAsset:
        """
        Enrich a PageAsset with crawl data.

        Args:
            asset: PageAsset to enrich

        Returns:
            Enriched PageAsset (mutates in place and returns)
        """
        normalized_url = self._normalize_url(asset.url)
        crawl_data = self._crawl_data.get(normalized_url)

        if crawl_data:
            asset.has_crawl_data = True
            asset.canonical_url = crawl_data.get('canonical_url') or asset.url
            asset.http_status = crawl_data.get('status_code', 200)
            asset.indexable = crawl_data.get('indexable', True)
            asset.title = crawl_data.get('title', asset.title)
            asset.h1 = crawl_data.get('h1', asset.h1)
            asset.word_count = crawl_data.get('word_count', asset.word_count)

        return asset

    def enrich_assets(self, assets: list[PageAsset]) -> tuple[int, int]:
        """
        Enrich multiple PageAssets with crawl data.

        Args:
            assets: List of PageAssets to enrich

        Returns:
            Tuple of (total assets, enriched count)
        """
        enriched = 0
        for asset in assets:
            normalized_url = self._normalize_url(asset.url)
            if normalized_url in self._crawl_data:
                self.enrich_asset(asset)
                enriched += 1

        return len(assets), enriched

    @property
    def loaded_urls(self) -> int:
        """Number of URLs loaded from crawl data."""
        return len(self._crawl_data)
