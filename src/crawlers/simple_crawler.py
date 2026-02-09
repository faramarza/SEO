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
    above_fold_html: str = ""
    body_html: str = ""  # Raw body HTML for structural analysis (e.g. HTMLIssueEvaluator)
    internal_outlinks: list = None  # List of {target_url, anchor_text, location}
    error: Optional[str] = None

    def __post_init__(self):
        if self.internal_outlinks is None:
            self.internal_outlinks = []


class HTMLMetaParser(HTMLParser):
    """Parser to extract SEO-relevant metadata from HTML."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "nav", "header", "footer", "svg", "iframe"})
    _ABOVE_FOLD_TAGS = frozenset({"script", "style", "noscript", "svg", "iframe"})

    def __init__(self, base_url: str = ""):
        super().__init__()
        self.canonical_url: Optional[str] = None
        self.title: str = ""
        self.h1: str = ""
        self.meta_description: str = ""
        self.meta_robots: str = ""
        self.word_count: int = 0

        self._base_url = base_url
        self._in_title = False
        self._in_h1 = False
        self._in_body = False
        self._body_text: list[str] = []
        self._h1_found = False
        self._skip_depth = 0  # > 0 means we're inside a skipped element

        # Above-fold HTML: collect TEXT CONTENT after H1 up to ~1500 chars
        # We capture text and semantic tags only — structural wrappers (empty divs) are skipped
        self._above_fold_parts: list[str] = []
        self._above_fold_len = 0
        self._above_fold_skip_depth = 0
        self._above_fold_pending_tags: list[str] = []  # Tags waiting for text content

        # Raw body HTML for full-page structural analysis
        self._body_html_parts: list[str] = []
        self._body_html_len = 0
        self._BODY_HTML_LIMIT = 100_000  # Cap at 100KB to avoid memory issues

        # Internal outlinks
        self._links: list[dict] = []
        self._in_link = False
        self._current_link_href: str = ""
        self._current_link_text: list[str] = []
        self._current_link_location: str = "body"
        self._in_main = False
        self._section_tag: str = ""

    def _is_internal(self, href: str) -> bool:
        """Check if a URL is internal based on base_url."""
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return False
        if not self._base_url:
            return href.startswith("/") and not href.startswith("//")
        try:
            base_parsed = urlparse(self._base_url)
            full = urljoin(self._base_url, href)
            link_parsed = urlparse(full)
            return link_parsed.netloc == base_parsed.netloc
        except Exception:
            return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        attrs_dict = {k.lower(): v for k, v in attrs if v is not None}

        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

        if tag in self._ABOVE_FOLD_TAGS:
            self._above_fold_skip_depth += 1

        # Track section context for link location
        if tag == "main":
            self._in_main = True
        if tag in ("h2", "h3", "h4"):
            self._section_tag = tag

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

        # Track <a> tags for internal outlinks
        # Only collect links that are:
        #   1. After the H1 (content area, not nav/header)
        # This prevents nav/header chrome links from being reported as outlinks.
        # Footer/nav/utility links are filtered out in get_internal_outlinks()
        # by URL pattern, so we don't need to gate on _skip_depth here.
        if tag == "a" and self._in_body and self._h1_found:
            href = attrs_dict.get("href", "")
            if href and self._is_internal(href):
                self._in_link = True
                self._current_link_href = href
                self._current_link_text = []

        # Capture img alt text as fallback anchor text for image links
        if tag == "img" and self._in_link:
            alt = attrs_dict.get("alt", "").strip()
            if alt:
                self._current_link_text.append(alt)

        # Above-fold HTML collection (after H1, content-only — empty structural tags are skipped)
        if self._in_body and self._h1_found and self._above_fold_len < 1500 and self._above_fold_skip_depth == 0:
            if tag not in self._SKIP_TAGS:
                # Semantic tags (p, h2-h6, a, strong, em, ul, ol, li, blockquote, img)
                # are buffered; they'll be emitted when text content follows.
                # Structural tags (div, span, section, article, etc.) are buffered too
                # but dropped if no text content follows before their close tag.
                attr_str = " ".join(f'{k}="{v}"' for k, v in attrs if v is not None and k in ("class", "id", "href", "src", "alt"))
                html_piece = f"<{tag}" + (f" {attr_str}" if attr_str else "") + ">"
                self._above_fold_pending_tags.append(html_piece)

    def handle_endtag(self, tag: str):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self._ABOVE_FOLD_TAGS and self._above_fold_skip_depth > 0:
            self._above_fold_skip_depth -= 1
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
            self._h1_found = True
        elif tag == "body":
            self._in_body = False

        # Close link tag
        if tag == "a" and self._in_link:
            self._in_link = False
            anchor = " ".join(self._current_link_text).strip()
            if self._current_link_href and anchor:
                # Determine location
                location = "body"
                if self._section_tag:
                    location = self._section_tag + "_section"
                self._links.append({
                    "target_url": self._current_link_href,
                    "anchor_text": anchor,
                    "location": location,
                })
            self._current_link_href = ""
            self._current_link_text = []

        # Above-fold closing tag — drop pending tags if they had no text content
        if self._in_body and self._h1_found and self._above_fold_len < 1500 and self._above_fold_skip_depth == 0:
            if tag not in self._SKIP_TAGS:
                # Check if there are pending (unemitted) tags — if so, the tag being
                # closed never had text content, so drop its opening tag from pending
                if self._above_fold_pending_tags:
                    # Remove the last pending opening tag for this tag type
                    for i in range(len(self._above_fold_pending_tags) - 1, -1, -1):
                        if self._above_fold_pending_tags[i].startswith(f"<{tag}"):
                            self._above_fold_pending_tags.pop(i)
                            break
                else:
                    # Tag was already emitted (had text content), so close it
                    piece = f"</{tag}>"
                    self._above_fold_parts.append(piece)
                    self._above_fold_len += len(piece)

    def handle_data(self, data: str):
        if self._in_title:
            self.title += data
        elif self._in_h1:
            self.h1 += data
        elif self._in_body and self._skip_depth == 0 and self._h1_found:
            stripped = data.strip()
            if stripped:
                self._body_text.append(stripped)

        # Link anchor text
        if self._in_link:
            self._current_link_text.append(data)

        # Above-fold text — finding text content flushes pending tags
        if self._in_body and self._h1_found and self._above_fold_len < 1500 and self._above_fold_skip_depth == 0 and self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                # Flush pending tags — they have text content, so they're worth keeping
                for pending in self._above_fold_pending_tags:
                    self._above_fold_parts.append(pending)
                    self._above_fold_len += len(pending)
                self._above_fold_pending_tags.clear()
                # Now add the text
                self._above_fold_parts.append(stripped)
                self._above_fold_len += len(stripped)

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

    def get_above_fold_html(self) -> str:
        """Get above-the-fold HTML snippet (after H1, ~800-1500 chars, no nav/footer)."""
        raw = " ".join(self._above_fold_parts)
        # Collapse whitespace
        return re.sub(r'\s+', ' ', raw).strip()[:1500]

    # URL path patterns that indicate nav/footer/utility pages, not content
    _UTILITY_PATHS = (
        "/customer/", "/account", "/login", "/register", "/create",
        "/wishlist", "/cart", "/checkout",
        "/catalogsearch/", "/search",
        "/contact", "/about", "/privacy", "/terms",
        "/shipping", "/returns", "/faq",
        "/enable-cookies", "/sitemap",
    )

    def get_internal_outlinks(self) -> list[dict]:
        """Get list of internal outlinks found on the page.

        Returns list of {target_url, anchor_text, location}.
        Resolves relative URLs to absolute if base_url was provided.
        Filters out nav/footer/utility links by URL pattern.
        """
        resolved = []
        seen = set()
        for link in self._links:
            href = link["target_url"]
            if self._base_url and not href.startswith(("http://", "https://")):
                href = urljoin(self._base_url, href)
            # Filter out utility/nav/footer links by URL path
            path = href.split("//", 1)[-1].split("/", 1)[-1] if "//" in href else href
            path_lower = ("/" + path).lower()
            if any(p in path_lower for p in self._UTILITY_PATHS):
                continue
            # Deduplicate by (target_url, anchor_text)
            key = (href, link["anchor_text"])
            if key not in seen:
                seen.add(key)
                resolved.append({
                    "target_url": href,
                    "anchor_text": link["anchor_text"],
                    "location": link["location"],
                })
        return resolved

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
                parser = HTMLMetaParser(base_url=url)
                try:
                    parser.feed(response.text)
                except Exception:
                    pass  # Best effort parsing

                # Resolve canonical URL if relative
                canonical = parser.canonical_url
                if canonical and not canonical.startswith(("http://", "https://")):
                    canonical = urljoin(url, canonical)

                # Extract raw body HTML for structural analysis
                body_match = re.search(
                    r'<body[^>]*>(.*)</body>', response.text,
                    re.DOTALL | re.IGNORECASE,
                )
                body_html = body_match.group(1)[:100_000] if body_match else ""

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
                    above_fold_html=parser.get_above_fold_html(),
                    body_html=body_html,
                    internal_outlinks=parser.get_internal_outlinks(),
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
            asset.above_fold_html = result.above_fold_html or asset.above_fold_html
            # Set outlink count and detailed outlinks from crawl
            if result.internal_outlinks:
                asset.outlinks = len(result.internal_outlinks)
                asset.internal_outlinks = result.internal_outlinks

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
