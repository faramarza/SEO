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


# --- Robust JSON-LD @type extraction -----------------------------------------
# Real sites wrap JSON-LD in CDATA / HTML comments, leave trailing commas, or put
# several objects in one <script>. Strict json.loads throws on all of these and
# the whole block — every @type in it — gets silently dropped, so the tool wrongly
# reports schema as "missing" (breadcrumbs, Organization, WebSite, etc. that are
# plainly in the page source). We sanitize first, and if it STILL won't parse we
# regex the @type values straight out of the raw text, so a malformed block can
# never again hide its schema.
_JSONLD_TYPE_RE = re.compile(r'"@type"\s*:\s*("(?:[^"\\]|\\.)*"|\[[^\]]*\])')


def _sanitize_jsonld(raw: str) -> str:
    """Strip the wrappers/typos that break strict JSON but are common in the wild."""
    s = raw.strip()
    s = re.sub(r'^﻿', '', s)                       # BOM
    s = re.sub(r'^\s*<!--', '', s); s = re.sub(r'-->\s*$', '', s)   # HTML comments
    s = re.sub(r'^\s*//?\s*<!\[CDATA\[', '', s)         # //<![CDATA[
    s = re.sub(r'//?\s*\]\]>\s*$', '', s)               # //]]>
    s = re.sub(r',\s*([}\]])', r'\1', s)                # trailing commas
    return s.strip()


def _types_via_regex(raw: str) -> list:
    """Last-resort: pull every @type value out of a block regardless of validity."""
    out = []
    for m in _JSONLD_TYPE_RE.finditer(raw or ""):
        val = m.group(1)
        if val.startswith('['):
            out += re.findall(r'"((?:[^"\\]|\\.)*)"', val)
        else:
            out.append(val.strip('"'))
    return out


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
    headings: list = None  # Ordered section headings (h2/h3/h4 text)
    breadcrumb_links: list = None  # List of {target_url, anchor_text} from breadcrumb nav
    schema_types: list = None  # List of JSON-LD @type values found on the page
    error: Optional[str] = None

    def __post_init__(self):
        if self.internal_outlinks is None:
            self.internal_outlinks = []
        if self.headings is None:
            self.headings = []
        if self.breadcrumb_links is None:
            self.breadcrumb_links = []
        if self.schema_types is None:
            self.schema_types = []


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

        # Section headings (h2/h3/h4 text) — real content structure for link placement
        self._headings: list[str] = []
        self._in_heading = False
        self._heading_buf: list[str] = []

        # Breadcrumb links (captured separately from content outlinks)
        self._breadcrumb_links: list[dict] = []
        self._in_breadcrumb_nav = False
        self._breadcrumb_depth = 0  # Track nesting to know when breadcrumb nav closes
        self._in_breadcrumb_link = False
        self._breadcrumb_link_href: str = ""
        self._breadcrumb_link_text: list[str] = []

        # Schema/structured data (JSON-LD)
        self._schema_types: list[str] = []
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []

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

        # Detect breadcrumb nav: <nav class="breadcrumbs"> or aria-label="breadcrumb"
        if tag == "nav":
            cls = (attrs_dict.get("class", "") or "").lower()
            aria = (attrs_dict.get("aria-label", "") or "").lower()
            if not self._in_breadcrumb_nav and ("breadcrumb" in cls or "breadcrumb" in aria):
                self._in_breadcrumb_nav = True
                self._breadcrumb_depth = 1
            elif self._in_breadcrumb_nav:
                # Nested nav inside breadcrumb (rare but possible)
                self._breadcrumb_depth += 1

        # Capture links inside breadcrumb nav
        if tag == "a" and self._in_breadcrumb_nav:
            href = attrs_dict.get("href", "")
            if href and self._is_internal(href):
                self._in_breadcrumb_link = True
                self._breadcrumb_link_href = href
                self._breadcrumb_link_text = []

        if tag == "script" and (attrs_dict.get("type", "") or "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_parts = []

        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

        if tag in self._ABOVE_FOLD_TAGS:
            self._above_fold_skip_depth += 1

        # Track section context for link location
        if tag == "main":
            self._in_main = True
        if tag in ("h2", "h3", "h4"):
            self._section_tag = tag
            self._in_heading = True
            self._heading_buf = []

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
        #   1. After the H1 (content area, not top nav/header)
        #   2. Not inside a skipped tag (nav, header, footer, etc.)
        # This prevents nav/header/footer chrome links from being reported as outlinks.
        # Utility links are further filtered out in get_internal_outlinks()
        # by URL pattern.
        if tag == "a" and self._in_body and self._h1_found and self._skip_depth == 0:
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
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            import json as _json
            raw = "".join(self._jsonld_parts)
            try:
                self._collect_schema_types(_json.loads(_sanitize_jsonld(raw)))
            except (ValueError, TypeError):
                # Malformed even after sanitizing — recover the @types anyway so a
                # single bad block never hides real schema from the audit.
                self._schema_types.extend(_types_via_regex(raw))
            self._jsonld_parts = []

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

        if tag in ("h2", "h3", "h4") and self._in_heading:
            self._in_heading = False
            heading = re.sub(r'\s+', ' ', " ".join(self._heading_buf)).strip()
            self._heading_buf = []
            if 2 < len(heading) <= 90 and len(self._headings) < 25:
                if heading not in self._headings:
                    self._headings.append(heading)

        # Close breadcrumb link
        if tag == "a" and self._in_breadcrumb_link:
            self._in_breadcrumb_link = False
            text = " ".join(self._breadcrumb_link_text).strip()
            if self._breadcrumb_link_href:
                self._breadcrumb_links.append({
                    "target_url": self._breadcrumb_link_href,
                    "anchor_text": text,
                })
            self._breadcrumb_link_href = ""
            self._breadcrumb_link_text = []

        # Close breadcrumb nav
        if tag == "nav" and self._in_breadcrumb_nav:
            self._breadcrumb_depth -= 1
            if self._breadcrumb_depth <= 0:
                self._in_breadcrumb_nav = False

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
        if self._in_jsonld:
            self._jsonld_parts.append(data)
            return

        if self._in_title:
            self.title += data
        elif self._in_h1:
            self.h1 += data
        elif self._in_body and self._skip_depth == 0 and self._h1_found:
            stripped = data.strip()
            if stripped:
                self._body_text.append(stripped)

        # Section heading text (h2/h3/h4)
        if self._in_heading:
            self._heading_buf.append(data)

        # Link anchor text
        if self._in_link:
            self._current_link_text.append(data)

        # Breadcrumb link text
        if self._in_breadcrumb_link:
            self._breadcrumb_link_text.append(data)

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

    def get_headings(self) -> list:
        """Return the ordered list of section headings (h2/h3/h4 text)."""
        return list(self._headings)

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

    def get_breadcrumb_links(self) -> list[dict]:
        """Get breadcrumb links (parent pages in hierarchy).

        Returns list of {target_url, anchor_text}.
        The last link is typically the immediate parent category.
        """
        resolved = []
        for link in self._breadcrumb_links:
            href = link["target_url"]
            if self._base_url and not href.startswith(("http://", "https://")):
                href = urljoin(self._base_url, href)
            resolved.append({
                "target_url": href,
                "anchor_text": link["anchor_text"],
            })
        return resolved

    def _collect_schema_types(self, node) -> None:
        """Recursively collect every @type value anywhere in a JSON-LD block.

        Magento (and Yoast, Rank Math, most CMSs) nest schema inside a top-level
        @graph array and put the money types inside the Product node itself —
        offers, aggregateRating, brand, review. A shallow top-level read misses
        all of it and makes the tool wrongly report 'No Offer schema' on a page
        that has it. Walk the whole structure and handle @type being a string or
        a list (e.g. ["Product", "Offer"])."""
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                self._schema_types.append(t)
            elif isinstance(t, list):
                for tv in t:
                    if isinstance(tv, str):
                        self._schema_types.append(tv)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    self._collect_schema_types(v)
        elif isinstance(node, list):
            for item in node:
                self._collect_schema_types(item)

    def get_schema_types(self) -> list[str]:
        """Get JSON-LD schema @type values found on the page."""
        return list(set(self._schema_types))

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
        # Gentle by default — this crawls the client's OWN live store. High
        # concurrency with no delay hammers Magento (each bot hit is a full
        # uncached PHP render), which caused the store's slow window. 2-wide with
        # a per-request delay keeps us a good neighbour to real shoppers.
        max_concurrent: int = 2,
        request_delay: float = 0.5,
        # Identifiable, throttleable UA in the standard well-behaved-bot format:
        # the token lets the store allow-list or rate-limit us on purpose; the
        # Mozilla/(compatible;...) shell keeps servers from serving stripped HTML.
        # (The JSON-LD parsing fix — not the UA — is what fixed schema detection.)
        user_agent: str = ("Mozilla/5.0 (compatible; AlphabetTrains-SEO-Crawler/1.0; "
                           "+first-party site audit)"),
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
        self.request_delay = request_delay
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
            # Space out requests so even a full crawl stays gentle on the store.
            if self.request_delay:
                await asyncio.sleep(self.request_delay)
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
                    parser.close()  # flush any buffered trailing data
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
                    headings=parser.get_headings(),
                    breadcrumb_links=parser.get_breadcrumb_links(),
                    schema_types=parser.get_schema_types(),
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
            if result.headings:
                asset.headings = result.headings
            # Set outlink count and detailed outlinks from crawl
            if result.internal_outlinks:
                asset.outlinks = len(result.internal_outlinks)
                asset.internal_outlinks = result.internal_outlinks
            # Set breadcrumb links for parent category detection
            if result.breadcrumb_links:
                asset.breadcrumb_links = result.breadcrumb_links
            if result.schema_types:
                asset.schema_types = result.schema_types

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
