"""
Internal Link Graph Builder

Builds and analyzes the internal link structure:
- Link authority calculation (simplified PageRank)
- Orphan page detection
- Link dilution analysis
- Hub/authority identification
- Crawl depth calculation
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from collections import defaultdict


@dataclass
class LinkEdge:
    """A directed edge in the link graph."""
    source_url: str
    target_url: str
    anchor_text: str
    is_navigation: bool  # In nav/header/footer
    is_content: bool     # In main content area
    follow: bool         # Not nofollow


@dataclass
class PageLinkMetrics:
    """Link metrics for a single page."""
    url: str
    inlinks: int
    outlinks: int
    authority_score: float
    hub_score: float
    crawl_depth: int
    is_orphan: bool
    dilution_warning: bool
    inlink_sources: list[str]
    outlink_targets: list[str]


class LinkGraph:
    """
    Internal link graph builder and analyzer.

    Provides:
    - Graph construction from crawl data
    - Authority score calculation (simplified PageRank)
    - Orphan detection
    - Link dilution warnings
    - Hub/authority analysis
    """

    # Thresholds
    ORPHAN_THRESHOLD = 3  # Pages with < 3 inlinks are orphans
    DILUTION_THRESHOLD = 50  # Pages with > 50 outlinks have dilution
    PAGERANK_ITERATIONS = 20
    PAGERANK_DAMPING = 0.85

    def __init__(self, cache_path: Optional[Path] = None):
        """
        Initialize link graph.

        Args:
            cache_path: Path to cache file. Default: data/link_graph.json
        """
        if cache_path is None:
            cache_path = Path(__file__).parent.parent.parent / "data" / "link_graph.json"
        self.cache_path = cache_path

        # Graph structure
        self._edges: list[LinkEdge] = []
        self._outlinks: dict[str, list[str]] = defaultdict(list)  # source -> [targets]
        self._inlinks: dict[str, list[str]] = defaultdict(list)   # target -> [sources]
        self._nodes: set[str] = set()

        # Computed metrics
        self._authority_scores: dict[str, float] = {}
        self._hub_scores: dict[str, float] = {}
        self._crawl_depths: dict[str, int] = {}

        self._load_cache()

    def _load_cache(self) -> None:
        """Load graph from cache."""
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    data = json.load(f)

                    self._edges = [
                        LinkEdge(**edge) for edge in data.get("edges", [])
                    ]
                    self._authority_scores = data.get("authority_scores", {})
                    self._hub_scores = data.get("hub_scores", {})
                    self._crawl_depths = data.get("crawl_depths", {})

                    # Rebuild adjacency lists
                    for edge in self._edges:
                        self._outlinks[edge.source_url].append(edge.target_url)
                        self._inlinks[edge.target_url].append(edge.source_url)
                        self._nodes.add(edge.source_url)
                        self._nodes.add(edge.target_url)

            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    def _save_cache(self) -> None:
        """Save graph to cache."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "last_updated": datetime.now().isoformat(),
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            "edges": [asdict(e) for e in self._edges],
            "authority_scores": self._authority_scores,
            "hub_scores": self._hub_scores,
            "crawl_depths": self._crawl_depths,
        }
        with open(self.cache_path, 'w') as f:
            json.dump(data, f, indent=2)

    def add_edge(
        self,
        source_url: str,
        target_url: str,
        anchor_text: str = "",
        is_navigation: bool = False,
        is_content: bool = True,
        follow: bool = True,
    ) -> None:
        """Add a link edge to the graph."""
        edge = LinkEdge(
            source_url=source_url,
            target_url=target_url,
            anchor_text=anchor_text,
            is_navigation=is_navigation,
            is_content=is_content,
            follow=follow,
        )
        self._edges.append(edge)
        self._outlinks[source_url].append(target_url)
        self._inlinks[target_url].append(source_url)
        self._nodes.add(source_url)
        self._nodes.add(target_url)

    def build_from_inventory(self, page_inventory: dict) -> None:
        """
        Build graph from page inventory data.

        Args:
            page_inventory: Dict of URL -> PageCrawlData
        """
        # Clear existing data
        self._edges = []
        self._outlinks = defaultdict(list)
        self._inlinks = defaultdict(list)
        self._nodes = set()

        for url, page_data in page_inventory.items():
            self._nodes.add(url)

            # Add internal links as edges
            for target_url in page_data.internal_links:
                self.add_edge(
                    source_url=url,
                    target_url=target_url,
                    anchor_text="",  # Would need to extract from crawl
                    is_navigation=False,
                    is_content=True,
                    follow=True,
                )

        # Compute metrics
        self._compute_authority_scores()
        self._compute_crawl_depths()
        self._save_cache()

    def _compute_authority_scores(self) -> None:
        """
        Compute authority scores using simplified PageRank.

        Authority = how many important pages link to this page
        Hub = how many important pages this page links to
        """
        if not self._nodes:
            return

        n = len(self._nodes)
        nodes = list(self._nodes)

        # Initialize scores
        authority = {url: 1.0 / n for url in nodes}
        hub = {url: 1.0 / n for url in nodes}

        # Iterative computation
        for _ in range(self.PAGERANK_ITERATIONS):
            new_authority = {}
            new_hub = {}

            for url in nodes:
                # Authority: sum of hub scores of pages linking to this
                auth_sum = sum(
                    hub.get(source, 0)
                    for source in self._inlinks.get(url, [])
                )

                # Hub: sum of authority scores of pages this links to
                hub_sum = sum(
                    authority.get(target, 0)
                    for target in self._outlinks.get(url, [])
                )

                new_authority[url] = auth_sum
                new_hub[url] = hub_sum

            # Normalize
            auth_norm = sum(new_authority.values()) or 1
            hub_norm = sum(new_hub.values()) or 1

            authority = {url: score / auth_norm for url, score in new_authority.items()}
            hub = {url: score / hub_norm for url, score in new_hub.items()}

        # Apply damping and store
        self._authority_scores = {
            url: self.PAGERANK_DAMPING * score + (1 - self.PAGERANK_DAMPING) / n
            for url, score in authority.items()
        }
        self._hub_scores = {
            url: self.PAGERANK_DAMPING * score + (1 - self.PAGERANK_DAMPING) / n
            for url, score in hub.items()
        }

    def _compute_crawl_depths(self, homepage_url: Optional[str] = None) -> None:
        """
        Compute crawl depth from homepage using BFS.

        Args:
            homepage_url: URL of homepage. If None, uses node with most outlinks.
        """
        if not self._nodes:
            return

        # Find homepage (highest outlinks or specified)
        if homepage_url is None:
            homepage_url = max(
                self._nodes,
                key=lambda url: len(self._outlinks.get(url, []))
            )

        # BFS from homepage
        depths = {homepage_url: 0}
        queue = [homepage_url]

        while queue:
            current = queue.pop(0)
            current_depth = depths[current]

            for target in self._outlinks.get(current, []):
                if target not in depths:
                    depths[target] = current_depth + 1
                    queue.append(target)

        # Unreachable pages get depth -1
        self._crawl_depths = {
            url: depths.get(url, -1)
            for url in self._nodes
        }

    def get_page_metrics(self, url: str) -> Optional[PageLinkMetrics]:
        """Get link metrics for a specific page."""
        if url not in self._nodes:
            return None

        inlinks = self._inlinks.get(url, [])
        outlinks = self._outlinks.get(url, [])

        return PageLinkMetrics(
            url=url,
            inlinks=len(inlinks),
            outlinks=len(outlinks),
            authority_score=self._authority_scores.get(url, 0),
            hub_score=self._hub_scores.get(url, 0),
            crawl_depth=self._crawl_depths.get(url, -1),
            is_orphan=len(inlinks) < self.ORPHAN_THRESHOLD,
            dilution_warning=len(outlinks) > self.DILUTION_THRESHOLD,
            inlink_sources=inlinks[:10],  # Top 10
            outlink_targets=outlinks[:10],
        )

    def get_orphan_pages(self) -> list[str]:
        """Get list of orphan pages (< 3 inlinks)."""
        return [
            url for url in self._nodes
            if len(self._inlinks.get(url, [])) < self.ORPHAN_THRESHOLD
        ]

    def get_diluted_pages(self) -> list[str]:
        """Get list of pages with too many outlinks."""
        return [
            url for url in self._nodes
            if len(self._outlinks.get(url, [])) > self.DILUTION_THRESHOLD
        ]

    def get_top_authority_pages(self, limit: int = 20) -> list[tuple[str, float]]:
        """Get pages with highest authority scores."""
        sorted_pages = sorted(
            self._authority_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return sorted_pages[:limit]

    def get_top_hub_pages(self, limit: int = 20) -> list[tuple[str, float]]:
        """Get pages with highest hub scores."""
        sorted_pages = sorted(
            self._hub_scores.items(),
            key=lambda x: x[1],
            reverse=True
        )
        return sorted_pages[:limit]

    def find_link_opportunities(
        self,
        source_url: str,
        target_url: str,
    ) -> dict:
        """
        Analyze potential link opportunity.

        Returns analysis of adding a link from source to target.
        """
        source_metrics = self.get_page_metrics(source_url)
        target_metrics = self.get_page_metrics(target_url)

        if not source_metrics or not target_metrics:
            return {"valid": False, "reason": "URL not in graph"}

        # Check if link already exists
        already_linked = target_url in self._outlinks.get(source_url, [])

        # Calculate potential value
        authority_transfer = source_metrics.authority_score * 0.1  # 10% transfer estimate
        value_score = authority_transfer * (1 if not already_linked else 0)

        return {
            "valid": True,
            "already_linked": already_linked,
            "source_authority": source_metrics.authority_score,
            "source_outlinks": source_metrics.outlinks,
            "source_dilution_warning": source_metrics.dilution_warning,
            "target_authority": target_metrics.authority_score,
            "target_inlinks": target_metrics.inlinks,
            "target_is_orphan": target_metrics.is_orphan,
            "estimated_value": value_score,
            "recommendation": (
                "Link already exists" if already_linked
                else "Good opportunity" if value_score > 0.01
                else "Low value opportunity"
            ),
        }

    def get_unreachable_pages(self) -> list[str]:
        """Get pages not reachable from homepage."""
        return [
            url for url, depth in self._crawl_depths.items()
            if depth == -1
        ]

    def summary(self) -> dict:
        """Generate graph summary."""
        orphans = self.get_orphan_pages()
        diluted = self.get_diluted_pages()
        unreachable = self.get_unreachable_pages()

        avg_inlinks = (
            sum(len(self._inlinks.get(url, [])) for url in self._nodes) / len(self._nodes)
            if self._nodes else 0
        )
        avg_outlinks = (
            sum(len(self._outlinks.get(url, [])) for url in self._nodes) / len(self._nodes)
            if self._nodes else 0
        )

        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "avg_inlinks": round(avg_inlinks, 1),
            "avg_outlinks": round(avg_outlinks, 1),
            "orphan_pages": len(orphans),
            "diluted_pages": len(diluted),
            "unreachable_pages": len(unreachable),
            "max_crawl_depth": max(self._crawl_depths.values()) if self._crawl_depths else 0,
        }

    def export_for_visualization(self) -> dict:
        """Export graph data for visualization tools."""
        return {
            "nodes": [
                {
                    "id": url,
                    "authority": self._authority_scores.get(url, 0),
                    "hub": self._hub_scores.get(url, 0),
                    "depth": self._crawl_depths.get(url, -1),
                    "inlinks": len(self._inlinks.get(url, [])),
                    "outlinks": len(self._outlinks.get(url, [])),
                }
                for url in self._nodes
            ],
            "edges": [
                {
                    "source": edge.source_url,
                    "target": edge.target_url,
                    "anchor": edge.anchor_text,
                }
                for edge in self._edges
            ],
        }
