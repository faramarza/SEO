"""
Simple Crawler for Canonical/Indexability Data

Lightweight crawler that fetches pages and extracts:
- Canonical URL
- Meta robots (indexability)
- Title
- H1
- Word count

Uses httpx for async fetching (faster than sequential requests).
"""

import asyncio
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from src.models.page_asset import PageAsset


@dataclass
class CrawlResult:
    """Result from crawling a single URL."""
    url: str
    status_code: int
    canonical_url: Optional[str]
    indexable: bool
    title: str
    h1: str
    word_count: int
    meta_description: str = ""
    content_preview: str = ""
    error: Optional[str] = None


class HTMLMetaParser(HTMLParser):
    """Parser to extract SEO-relevant metadata from HTML."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "nav", "header", "footer", "svg", "iframe"})

    def __init__(self):
        super().__init__()
        self.canonical_url: Optional[str] = None
        self.title: str = ""
        self.h1: str = ""
        self.meta_description: str = ""
        self.meta_robots: str = ""
        self.word_count: int = 0

        self._in_title = False
        self._in_h1 = False
        self._in_body = False
        self._body_text: list[str] = []
        self._h1_found = False
        self._skip_depth = 0  # > 0 means we're inside a skipped element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        attrs_dict = {k.lower(): v for k, v in attrs if v is not None}

        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

        if tag == "link" and attrs_dict.get("rel", "").lower() == "canonical":
            self.canonical_url = attrs_dict.get("href")

        elif tag == "meta":
            name = attrs_dict.get("name", "").lower()
            if name == "robots":
                self.meta_robots = attrs_dict.get("content", "")
            elif name == "description":
                self.meta_description = attrs_dict.get("content", "")

        elif tag == "title":
            self._in_title = True

        elif tag == "h1" and not self._h1_found:
            self._in_h1 = True

        elif tag == "body":
            self._in_body = True

    def handle_endtag(self, tag: str):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
            self._h1_found = True
        elif tag == "body":
            self._in_body = False

    def handle_data(self, data: str):
        if self._in_title:
            self.title += data
        elif self._in_h1:
            self.h1 += data
        elif self._in_body and self._skip_depth == 0 and self._h1_found:
            stripped = data.strip()
            if stripped:
                self._body_text.append(stripped)

    def get_word_count(self) -> int:
        """Calculate word count from body text."""
        text = " ".join(self._body_text)
        words = re.findall(r'\b\w+\b', text)
        return len(words)

    def get_content_preview(self, max_words: int = 200) -> str:
        """Extract first N words of body text as a content preview."""
        text = " ".join(self._body_text)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        words = text.split()
        return " ".join(words[:max_words])

    @property
    def is_indexable(self) -> bool:
        """Check if page is indexable based on meta robots."""
        robots_lower = self.meta_robots.lower()
        return "noindex" not in robots_lower


class SimpleCrawler:
    """
    Simple async crawler for extracting SEO metadata.

    Usage:
        crawler = SimpleCrawler()
        results = crawler.crawl_urls(["https://example.com/page1", ...])
    """

    def __init__(
        self,
        timeout: float = 10.0,
        max_concurrent: int = 10,
        user_agent: str = "AlphabetTrains-SEO-Crawler/1.0",
    ):
        """
        Initialize crawler.

        Args:
            timeout: Request timeout in seconds
            max_concurrent: Max concurrent requests
            user_agent: User agent string
        """
        self.timeout = timeout
        self.max_concurrent = max_concurrent
        self.user_agent = user_agent
        self._results: dict[str, CrawlResult] = {}

    async def _fetch_url(
        self,
        client: httpx.AsyncClient,
        url: str,
        semaphore: asyncio.Semaphore,
    ) -> CrawlResult:
        """Fetch a single URL and extract metadata."""
        async with semaphore:
            try:
                response = await client.get(
                    url,
                    follow_redirects=True,
                    timeout=self.timeout,
                )

                if response.status_code != 200:
                    return CrawlResult(
                        url=url,
                        status_code=response.status_code,
                        canonical_url=None,
                        indexable=True,
                        title="",
                        h1="",
                        word_count=0,
                        error=f"HTTP {response.status_code}",
                    )

                # Parse HTML
                parser = HTMLMetaParser()
                try:
                    parser.feed(response.text)
                except Exception:
                    pass  # Best effort parsing

                # Resolve canonical URL if relative
                canonical = parser.canonical_url
                if canonical and not canonical.startswith(("http://", "https://")):
                    canonical = urljoin(url, canonical)

                return CrawlResult(
                    url=url,
                    status_code=response.status_code,
                    canonical_url=canonical,
                    indexable=parser.is_indexable,
                    title=parser.title.strip(),
                    h1=parser.h1.strip(),
                    meta_description=parser.meta_description.strip(),
                    word_count=parser.get_word_count(),
                    content_preview=parser.get_content_preview(200),
                )

            except httpx.TimeoutException:
                return CrawlResult(
                    url=url,
                    status_code=0,
                    canonical_url=None,
                    indexable=True,
                    title="",
                    h1="",
                    word_count=0,
                    error="Timeout",
                )
            except Exception as e:
                return CrawlResult(
                    url=url,
                    status_code=0,
                    canonical_url=None,
                    indexable=True,
                    title="",
                    h1="",
                    word_count=0,
                    error=str(e),
                )

    async def _crawl_async(self, urls: list[str]) -> list[CrawlResult]:
        """Crawl URLs asynchronously."""
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            tasks = [
                self._fetch_url(client, url, semaphore)
                for url in urls
            ]
            results = await asyncio.gather(*tasks)

        return list(results)

    def crawl_urls(self, urls: list[str], show_progress: bool = True) -> list[CrawlResult]:
        """
        Crawl a list of URLs and extract metadata.

        Args:
            urls: List of URLs to crawl
            show_progress: Print progress updates

        Returns:
            List of CrawlResult objects
        """
        if not urls:
            return []

        if show_progress:
            print(f"  Crawling {len(urls)} URLs (max {self.max_concurrent} concurrent)...")

        # Run async crawl
        results = asyncio.run(self._crawl_async(urls))

        # Store results
        for result in results:
            self._results[result.url.lower().rstrip('/')] = result

        if show_progress:
            success = sum(1 for r in results if r.error is None)
            print(f"  Completed: {success}/{len(urls)} successful")

        return results

    def enrich_asset(self, asset: PageAsset) -> PageAsset:
        """
        Enrich a PageAsset with crawl data.

        Args:
            asset: PageAsset to enrich

        Returns:
            Enriched PageAsset
        """
        normalized_url = asset.url.lower().rstrip('/')
        result = self._results.get(normalized_url)

        if result and result.error is None:
            asset.has_crawl_data = True
            asset.canonical_url = result.canonical_url or asset.url
            asset.http_status = result.status_code
            asset.indexable = result.indexable
            asset.title = result.title or asset.title
            asset.h1 = result.h1 or asset.h1
            asset.meta_description = result.meta_description or asset.meta_description
            asset.word_count = result.word_count or asset.word_count
            asset.content_preview = result.content_preview or asset.content_preview

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
            normalized_url = asset.url.lower().rstrip('/')
            if normalized_url in self._results:
                result = self._results[normalized_url]
                if result.error is None:
                    self.enrich_asset(asset)
                    enriched += 1

        return len(assets), enriched
