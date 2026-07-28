"""
Page Inventory Crawler/Loader

Crawls site pages to extract:
- Title tags
- Meta descriptions
- H1 tags
- Word count
- Canonical URLs
- Schema markup presence
- Internal/external links

Supports both live crawling and loading from cached data.
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin
import hashlib

try:
    import requests
    from bs4 import BeautifulSoup
    HAS_CRAWL_DEPS = True
except ImportError:
    HAS_CRAWL_DEPS = False


def _collect_schema_types(node, out: list) -> None:
    """Recursively collect every JSON-LD @type in a block, including @graph
    containers and nested nodes (offers, aggregateRating, brand, breadcrumb).
    Handles @type as a string or a list. A shallow top-level read misses the
    nested money types Magento/Yoast emit inside the Product node."""
    if isinstance(node, dict):
        t = node.get('@type')
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, list):
            out.extend(tv for tv in t if isinstance(tv, str))
        for v in node.values():
            if isinstance(v, (dict, list)):
                _collect_schema_types(v, out)
    elif isinstance(node, list):
        for item in node:
            _collect_schema_types(item, out)


@dataclass
class PageCrawlData:
    """Data extracted from a single page crawl."""
    url: str
    title: str
    meta_description: str
    h1: str
    h1_count: int
    word_count: int
    canonical_url: Optional[str]
    canonical_is_self: bool
    has_schema_markup: bool
    schema_types: list[str]
    internal_links: list[str]
    external_links: list[str]
    response_code: int
    crawl_time: str
    content_hash: str
    robots_meta: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PageCrawlData":
        return cls(**data)


class PageInventory:
    """
    Page inventory crawler and loader.

    Can either:
    1. Crawl pages live (requires requests + beautifulsoup4)
    2. Load from cached inventory file
    """

    def __init__(
        self,
        base_domain: str,
        cache_path: Optional[Path] = None,
        user_agent: str = "AlphabetTrains-SEO-Governor/1.0",
    ):
        """
        Initialize page inventory.

        Args:
            base_domain: Domain to crawl (e.g., "alphabet-trains.com")
            cache_path: Path to cache file. Default: data/page_inventory.json
            user_agent: User agent for crawling
        """
        self.base_domain = base_domain
        self.user_agent = user_agent

        if cache_path is None:
            cache_path = Path(__file__).parent.parent.parent / "data" / "page_inventory.json"
        self.cache_path = cache_path

        self._inventory: dict[str, PageCrawlData] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """Load inventory from cache file."""
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    data = json.load(f)
                    self._inventory = {
                        url: PageCrawlData.from_dict(page_data)
                        for url, page_data in data.get("pages", {}).items()
                    }
            except (json.JSONDecodeError, KeyError):
                self._inventory = {}

    def _save_cache(self) -> None:
        """Save inventory to cache file."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "base_domain": self.base_domain,
            "last_updated": datetime.now().isoformat(),
            "page_count": len(self._inventory),
            "pages": {
                url: page.to_dict()
                for url, page in self._inventory.items()
            },
        }
        with open(self.cache_path, 'w') as f:
            json.dump(data, f, indent=2)

    def crawl_page(self, url: str, timeout: int = 10) -> Optional[PageCrawlData]:
        """
        Crawl a single page and extract data.

        Args:
            url: URL to crawl
            timeout: Request timeout in seconds

        Returns:
            PageCrawlData or None if crawl failed
        """
        if not HAS_CRAWL_DEPS:
            raise ImportError("Crawling requires: pip install requests beautifulsoup4")

        try:
            headers = {"User-Agent": self.user_agent}
            response = requests.get(url, headers=headers, timeout=timeout)
            response_code = response.status_code

            if response_code != 200:
                return PageCrawlData(
                    url=url,
                    title="",
                    meta_description="",
                    h1="",
                    h1_count=0,
                    word_count=0,
                    canonical_url=None,
                    canonical_is_self=False,
                    has_schema_markup=False,
                    schema_types=[],
                    internal_links=[],
                    external_links=[],
                    response_code=response_code,
                    crawl_time=datetime.now().isoformat(),
                    content_hash="",
                    robots_meta="",
                )

            soup = BeautifulSoup(response.text, 'html.parser')

            # Extract title
            title_tag = soup.find('title')
            title = title_tag.get_text(strip=True) if title_tag else ""

            # Extract meta description
            meta_desc_tag = soup.find('meta', attrs={'name': 'description'})
            meta_description = meta_desc_tag.get('content', '') if meta_desc_tag else ""

            # Extract H1s
            h1_tags = soup.find_all('h1')
            h1 = h1_tags[0].get_text(strip=True) if h1_tags else ""
            h1_count = len(h1_tags)

            # Extract canonical
            canonical_tag = soup.find('link', attrs={'rel': 'canonical'})
            canonical_url = canonical_tag.get('href') if canonical_tag else None
            canonical_is_self = self._normalize_url(canonical_url) == self._normalize_url(url) if canonical_url else False

            # Extract robots meta
            robots_tag = soup.find('meta', attrs={'name': 'robots'})
            robots_meta = robots_tag.get('content', '') if robots_tag else ""

            # Count words in body text
            body = soup.find('body')
            if body:
                # Remove script and style elements
                for element in body.find_all(['script', 'style', 'nav', 'header', 'footer']):
                    element.decompose()
                text = body.get_text(separator=' ', strip=True)
                word_count = len(text.split())
            else:
                word_count = 0

            # Extract schema markup
            schema_scripts = soup.find_all('script', attrs={'type': 'application/ld+json'})
            has_schema_markup = len(schema_scripts) > 0
            schema_types = []
            for script in schema_scripts:
                try:
                    schema_data = json.loads(script.string)
                    _collect_schema_types(schema_data, schema_types)
                except (json.JSONDecodeError, TypeError):
                    pass
            schema_types = list(set(schema_types))

            # Extract links
            internal_links = []
            external_links = []
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                if not href or href.startswith('#') or href.startswith('javascript:'):
                    continue

                # Resolve relative URLs
                full_url = urljoin(url, href)
                parsed = urlparse(full_url)

                if self.base_domain in parsed.netloc:
                    internal_links.append(full_url)
                elif parsed.scheme in ('http', 'https'):
                    external_links.append(full_url)

            # Content hash for change detection
            content_hash = hashlib.md5(response.text.encode()).hexdigest()[:16]

            return PageCrawlData(
                url=url,
                title=title,
                meta_description=meta_description,
                h1=h1,
                h1_count=h1_count,
                word_count=word_count,
                canonical_url=canonical_url,
                canonical_is_self=canonical_is_self,
                has_schema_markup=has_schema_markup,
                schema_types=schema_types,
                internal_links=list(set(internal_links)),  # Dedupe
                external_links=list(set(external_links)),
                response_code=response_code,
                crawl_time=datetime.now().isoformat(),
                content_hash=content_hash,
                robots_meta=robots_meta,
            )

        except Exception as e:
            # Return error record
            return PageCrawlData(
                url=url,
                title="",
                meta_description="",
                h1="",
                h1_count=0,
                word_count=0,
                canonical_url=None,
                canonical_is_self=False,
                has_schema_markup=False,
                schema_types=[],
                internal_links=[],
                external_links=[],
                response_code=0,
                crawl_time=datetime.now().isoformat(),
                content_hash="",
                robots_meta=str(e),
            )

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for comparison."""
        if not url:
            return ""
        # Remove trailing slash, lowercase
        url = url.lower().rstrip('/')
        # Remove protocol
        url = re.sub(r'^https?://', '', url)
        # Remove www
        url = re.sub(r'^www\.', '', url)
        return url

    def crawl_urls(
        self,
        urls: list[str],
        force_refresh: bool = False,
        max_age_hours: int = 24,
    ) -> dict[str, PageCrawlData]:
        """
        Crawl multiple URLs, using cache when available.

        Args:
            urls: List of URLs to crawl
            force_refresh: If True, ignore cache
            max_age_hours: Max age of cached data to use

        Returns:
            Dict of URL -> PageCrawlData
        """
        results = {}
        to_crawl = []

        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

        for url in urls:
            if not force_refresh and url in self._inventory:
                cached = self._inventory[url]
                try:
                    crawl_time = datetime.fromisoformat(cached.crawl_time).timestamp()
                    if crawl_time >= cutoff:
                        results[url] = cached
                        continue
                except (ValueError, TypeError):
                    pass
            to_crawl.append(url)

        # Crawl pages not in cache
        for url in to_crawl:
            data = self.crawl_page(url)
            if data:
                results[url] = data
                self._inventory[url] = data

        # Save updated cache
        if to_crawl:
            self._save_cache()

        return results

    def get_page(self, url: str) -> Optional[PageCrawlData]:
        """Get page data from inventory."""
        return self._inventory.get(url)

    def get_all_pages(self) -> dict[str, PageCrawlData]:
        """Get all pages in inventory."""
        return self._inventory.copy()

    def load_from_sitemap(self, sitemap_url: str) -> list[str]:
        """
        Load URLs from sitemap.

        Args:
            sitemap_url: URL of sitemap.xml

        Returns:
            List of URLs found in sitemap
        """
        if not HAS_CRAWL_DEPS:
            raise ImportError("Sitemap loading requires: pip install requests beautifulsoup4")

        urls = []
        try:
            headers = {"User-Agent": self.user_agent}
            response = requests.get(sitemap_url, headers=headers, timeout=30)

            if response.status_code != 200:
                return urls

            soup = BeautifulSoup(response.text, 'xml')

            # Handle sitemap index
            sitemaps = soup.find_all('sitemap')
            if sitemaps:
                for sitemap in sitemaps:
                    loc = sitemap.find('loc')
                    if loc:
                        urls.extend(self.load_from_sitemap(loc.get_text(strip=True)))

            # Handle regular sitemap
            url_tags = soup.find_all('url')
            for url_tag in url_tags:
                loc = url_tag.find('loc')
                if loc:
                    urls.append(loc.get_text(strip=True))

        except Exception:
            pass

        return urls

    def import_from_gsc_urls(self, gsc_urls: list[str]) -> None:
        """
        Import URL list from GSC data.

        Creates placeholder entries that can be crawled later.
        """
        for url in gsc_urls:
            if url not in self._inventory:
                # Create placeholder
                self._inventory[url] = PageCrawlData(
                    url=url,
                    title="[NOT_CRAWLED]",
                    meta_description="",
                    h1="",
                    h1_count=0,
                    word_count=0,
                    canonical_url=None,
                    canonical_is_self=False,
                    has_schema_markup=False,
                    schema_types=[],
                    internal_links=[],
                    external_links=[],
                    response_code=0,
                    crawl_time="",
                    content_hash="",
                    robots_meta="",
                )
        self._save_cache()

    def summary(self) -> dict:
        """Generate inventory summary."""
        total = len(self._inventory)
        crawled = sum(1 for p in self._inventory.values() if p.response_code == 200)
        with_schema = sum(1 for p in self._inventory.values() if p.has_schema_markup)

        # Word count distribution
        word_counts = [p.word_count for p in self._inventory.values() if p.word_count > 0]
        avg_word_count = sum(word_counts) / len(word_counts) if word_counts else 0

        return {
            "total_urls": total,
            "crawled": crawled,
            "not_crawled": total - crawled,
            "with_schema_markup": with_schema,
            "avg_word_count": round(avg_word_count),
            "total_internal_links": sum(len(p.internal_links) for p in self._inventory.values()),
        }
