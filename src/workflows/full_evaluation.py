"""
Full Evaluation Workflow

Integrates all components into a complete evaluation pipeline:
1. Load data from GSC/GA4
2. Run tracking diagnostics
3. Build page inventory and link graph
4. Run all evaluators per mode
5. Apply learning rules
6. Generate structured output

This is the main entry point for running the Governor.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType, GSCMetrics, GA4Metrics, TopQuery
from src.models.governance_state import GovernanceState
from src.models.decision_envelope import DecisionType
from src.models.cost_model import ActionCostModel
from src.models.profit_model import ProfitModel
from src.governor.agentic_governor import GovernorConfig
from src.data_sources.gsc_client import GSCClient
from src.data_sources.ga4_client import GA4Client
from src.diagnostics.tracking_sanity import TrackingSanityDiagnostics
from src.governor.agentic_governor import AgenticGovernor, GovernorMode
from src.governor.priority_scoring import ActionType, calculate_priority
from src.evaluators.title_meta_evaluator import TitleMetaEvaluator
from src.evaluators.canonical_evaluator import CanonicalEvaluator
from src.evaluators.internal_link_evaluator import InternalLinkEvaluator
from src.evaluators.asset_creation_evaluator import AssetCreationEvaluator
from src.evaluators.constraint_detector import ConstraintDetector, CaptureClass, ConstraintType
from src.evaluators.html_issue_evaluator import HTMLIssueEvaluator
from src.evaluators.page_quality_evaluator import evaluate_page_quality
from src.ledger.action_ledger import ActionLedger, ActionFingerprint, ActionRecord
from src.output.decision_formatter import DecisionFormatter, OutputFormat
from src.crawlers.page_inventory import PageInventory
from src.crawlers.link_graph import LinkGraph
from src.crawlers.beamusup_importer import BeamUsUpImporter
from src.crawlers.simple_crawler import SimpleCrawler
from src.data_sources.google_ads_client import GoogleAdsClient, AdsAccountData
from src.data_sources.crux_client import CrUXClient


@dataclass
class WorkflowConfig:
    """Configuration for the evaluation workflow."""
    # Data source credentials
    gsc_property: str
    ga4_property_id: str
    credentials_path: str

    # Business parameters
    aov: float = 53.19
    margin: float = 0.27
    # Organic click → purchase conversion rate. Used to monetize incremental
    # clicks consistently across all evaluators (was an inconsistent 0.1/0.5/2x).
    conv_rate: float = 0.03

    # Governance — lane-aware confidence thresholds
    # EXPLORATION (additive, reversible): lower bar
    # PRESERVATION (irreversible, high-risk): higher bar
    exploration_confidence_threshold: float = 0.55
    preservation_confidence_threshold: float = 0.75
    min_confidence_threshold: float = 0.55  # Lowest threshold (backward compat)
    regret_budget_year: int = 2
    profit_to_cost_ratio_gate: float = 5.0

    # Output
    output_format: OutputFormat = OutputFormat.CONSOLE
    output_path: Optional[Path] = None

    # Modes to evaluate
    modes: list[GovernorMode] = None

    # Diagnostic settings
    run_diagnostics: bool = True
    block_on_tier_a: bool = True

    # Google Ads settings (optional - absence is neutral)
    google_ads_customer_id: Optional[str] = None
    google_ads_config_path: Optional[str] = None
    google_ads_developer_token: Optional[str] = None
    google_ads_login_customer_id: Optional[str] = None
    google_ads_use_service_account: bool = False
    brand_terms: Optional[list[str]] = None
    product_families: Optional[list[str]] = None

    # Site platform for platform-specific diagnostics
    site_platform: str = "magento"

    @classmethod
    def from_json(cls, path: Path) -> "WorkflowConfig":
        """Load config from JSON file."""
        with open(path) as f:
            data = json.load(f)

        # Get Google Ads config if present
        ads_config = data.get("data_sources", {}).get("google_ads", {})

        return cls(
            gsc_property=data["data_sources"]["gsc"]["property_url"],
            ga4_property_id=data["data_sources"]["ga4"]["property_id"],
            credentials_path=data["data_sources"]["gsc"]["credentials_path"],
            aov=data.get("profit_model", {}).get("aov", 53.19),
            margin=data.get("profit_model", {}).get("gross_margin_low", 0.27),
            conv_rate=data.get("profit_model", {}).get("site_avg_purchase_rate", 0.03),
            exploration_confidence_threshold=data.get("governance", {}).get("exploration_confidence_threshold", 0.55),
            preservation_confidence_threshold=data.get("governance", {}).get("preservation_confidence_threshold", 0.75),
            min_confidence_threshold=data.get("governance", {}).get("min_confidence_threshold", 0.55),
            regret_budget_year=data.get("governance", {}).get("regret_budget_year", 2),
            profit_to_cost_ratio_gate=data.get("governance", {}).get("profit_to_cost_ratio_gate", 5.0),
            google_ads_customer_id=ads_config.get("customer_id"),
            google_ads_config_path=ads_config.get("config_path"),
            google_ads_developer_token=ads_config.get("developer_token"),
            google_ads_login_customer_id=ads_config.get("login_customer_id"),
            google_ads_use_service_account=ads_config.get("use_service_account", False),
            brand_terms=ads_config.get("brand_terms", []),
            product_families=data.get("business_context", {}).get("product_families", []),
            site_platform=data.get("data_sources", {}).get("site_platform", "magento"),
        )


class FullEvaluationWorkflow:
    """
    Complete evaluation workflow integrating all Governor components.

    Pipeline:
    1. Data Loading: GSC + GA4 metrics
    2. Diagnostics: Tracking sanity checks
    3. Inventory: Page data + link graph
    4. Evaluation: All modes and action types
    5. Learning: Apply ledger rules
    6. Output: Structured decision report
    """

    def __init__(self, config: WorkflowConfig):
        """
        Initialize workflow with configuration.

        Args:
            config: Workflow configuration
        """
        self.config = config

        # Initialize clients
        self.gsc_client = GSCClient(
            site_url=config.gsc_property,
            credentials_path=config.credentials_path,
        )
        self.ga4_client = GA4Client(
            property_id=config.ga4_property_id,
            credentials_path=config.credentials_path,
        )

        # Initialize components
        self.diagnostics = TrackingSanityDiagnostics(
            site_platform=config.site_platform,
            brand_terms=config.brand_terms,
        )
        self.ledger = ActionLedger()

        # Create Governor dependencies
        cost_model = ActionCostModel()
        profit_model = ProfitModel(
            aov=config.aov,
            gross_margin_low=config.margin,
            gross_margin_high=config.margin + 0.03,
        )
        governance_state = GovernanceState()
        governor_config = GovernorConfig(
            aov=config.aov,
            gross_margin=config.margin,
            min_confidence_threshold=config.exploration_confidence_threshold,
            profit_to_cost_ratio_gate=config.profit_to_cost_ratio_gate,
        )

        self.governor = AgenticGovernor(
            cost_model=cost_model,
            profit_model=profit_model,
            governance_state=governance_state,
            config=governor_config,
            ledger=self.ledger,
        )
        self.formatter = DecisionFormatter()

        # Evaluators
        self.title_evaluator = TitleMetaEvaluator()
        self.canonical_evaluator = CanonicalEvaluator()
        self.link_evaluator = InternalLinkEvaluator()
        self.asset_evaluator = AssetCreationEvaluator(
            aov=config.aov,
            margin=config.margin,
        )
        self.constraint_detector = ConstraintDetector(
            aov=config.aov,
            margin=config.margin,
        )
        self.html_evaluator = HTMLIssueEvaluator()
        # CrUX (Core Web Vitals) — no-op without GOOGLE_API_KEY; 7-day cache.
        self.crux_client = CrUXClient()

        # Google Ads client (optional - absence is neutral, not negative)
        self.ads_client: Optional[GoogleAdsClient] = None
        self.ads_data: Optional[AdsAccountData] = None

        if config.google_ads_customer_id:
            self.ads_client = GoogleAdsClient(
                credentials_path=config.google_ads_config_path or config.credentials_path,
                customer_id=config.google_ads_customer_id,
                brand_terms=config.brand_terms,
                use_service_account=config.google_ads_use_service_account,
                developer_token=config.google_ads_developer_token,
                login_customer_id=config.google_ads_login_customer_id,
            )

        # Page inventory and link graph
        domain = config.gsc_property.replace("sc-domain:", "")
        self.page_inventory = PageInventory(base_domain=domain)
        self.link_graph = LinkGraph()

        # State
        self._assets: list[PageAsset] = []
        self._diagnostic_results: list = []
        self._evaluation_results: list = []

        # Build product-family slugs for URL classification.
        # Config product_families like ["name trains", "step stools"] become
        # slug fragments ["name-train", "step-stool"] to match URL patterns.
        families = config.product_families or []
        self._product_family_slugs = []
        for fam in families:
            slug = fam.lower().strip().replace(" ", "-")
            # Strip trailing 's' to match both singular and plural URLs
            # "name trains" → "name-train" matches /name-trains.html
            if slug.endswith("s"):
                slug = slug[:-1]
            self._product_family_slugs.append(slug)

        # Load sitemap-derived type map if available.  The sitemap import
        # in the dashboard writes data/sitemap_types.json with URL→type
        # derived from sub-sitemap filenames (e.g. sitemap_products_1.xml).
        # This is the most reliable classification source.
        self._sitemap_types: dict[str, str] = {}
        sitemap_types_path = Path(__file__).parent.parent.parent / "data" / "sitemap_types.json"
        if sitemap_types_path.exists():
            try:
                with open(sitemap_types_path) as f:
                    self._sitemap_types = json.load(f)
                print(f"  Loaded sitemap type map: {len(self._sitemap_types)} URLs")
            except (json.JSONDecodeError, IOError):
                pass

    # Tracking/marketing query parameters to strip from URLs
    _STRIP_PARAMS = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "gclsrc", "gbraid", "wbraid", "dclid",
        "fbclid", "msclkid", "twclid",
        "mc_cid", "mc_eid",
        "ref", "source",
        # Pagination & filtering — these create duplicate entries for the
        # same underlying page.  Metrics should merge into the canonical URL.
        "p", "page", "pg", "start", "offset",
        "product_list_limit", "limit", "product_list_order", "product_list_dir",
        "product_list_mode", "order", "dir", "sort", "sortby", "sort_by",
        "mode", "view",
    }

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip tracking parameters from a URL and normalize."""
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=False)
        # Remove tracking params
        cleaned = {
            k: v for k, v in params.items()
            if k.lower() not in FullEvaluationWorkflow._STRIP_PARAMS
        }
        # Rebuild query string (sorted for consistency)
        new_query = urlencode(cleaned, doseq=True) if cleaned else ""
        normalized = urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/") or "/",
            parsed.params,
            new_query,
            "",  # drop fragment
        ))
        return normalized

    def _normalize_url_data(self, data: dict) -> dict:
        """Normalize URL keys and merge metrics for duplicates."""
        merged = {}
        for raw_url, metrics in data.items():
            clean_url = self._normalize_url(raw_url)
            if clean_url not in merged:
                merged[clean_url] = dict(metrics)
            else:
                # Merge: sum numeric metrics, keep best queries
                existing = merged[clean_url]
                for key in ("clicks", "impressions", "sessions", "users",
                            "engaged_sessions", "conversions", "revenue"):
                    if key in metrics:
                        existing[key] = existing.get(key, 0) + metrics[key]
                # Keep the better position (lower = better)
                if "position" in metrics and "position" in existing:
                    existing["position"] = min(existing["position"], metrics["position"])
                # Keep the better CTR
                if "ctr" in metrics and "ctr" in existing:
                    existing["ctr"] = max(existing["ctr"], metrics["ctr"])
                # Merge queries (deduplicate by query text)
                if "queries" in metrics:
                    existing_queries = {q["query"]: q for q in existing.get("queries", [])}
                    for q in metrics["queries"]:
                        if q["query"] not in existing_queries:
                            existing_queries[q["query"]] = q
                        else:
                            eq = existing_queries[q["query"]]
                            eq["clicks"] = eq.get("clicks", 0) + q.get("clicks", 0)
                            eq["impressions"] = eq.get("impressions", 0) + q.get("impressions", 0)
                            if q.get("position", 100) < eq.get("position", 100):
                                eq["position"] = q["position"]
                    existing["queries"] = list(existing_queries.values())
        return merged

    def load_data(self, days: int = 28) -> list[PageAsset]:
        """
        Load data from GSC and GA4.

        Args:
            days: Number of days of data to load

        Returns:
            List of PageAsset objects
        """
        print(f"Loading data from GSC and GA4 ({days} days)...")

        # Get GSC data
        gsc_data_raw = self.gsc_client.get_page_data(days=days)
        print(f"  GSC: {len(gsc_data_raw)} URLs (raw)")

        # Get GA4 data (organic only)
        ga4_data_raw = self.ga4_client.get_page_data(days=days, organic_only=True)
        print(f"  GA4: {len(ga4_data_raw)} URLs (raw)")

        # Normalize URLs: strip tracking params, merge duplicates
        gsc_data = self._normalize_url_data(gsc_data_raw)
        ga4_data_normalized = self._normalize_url_data(ga4_data_raw)
        print(f"  After normalization: GSC {len(gsc_data)}, GA4 {len(ga4_data_normalized)}")

        # GA4 returns paths ("/page.html"), GSC returns full URLs
        # ("https://domain.com/page.html").  Align GA4 keys to full URLs
        # so the merge matches correctly.
        gsc_prop = self.config.gsc_property
        if gsc_prop.startswith("sc-domain:"):
            domain = gsc_prop.replace("sc-domain:", "").strip("/")
            base_url = f"https://{domain}"
        elif gsc_prop.startswith("http"):
            # URL-prefix property (e.g. "https://alphabet-trains.com/")
            base_url = gsc_prop.rstrip("/")
        else:
            base_url = f"https://{gsc_prop.strip('/')}"

        ga4_data = {}
        for key, value in ga4_data_normalized.items():
            if key.startswith("http://") or key.startswith("https://"):
                full_url = key  # Already a full URL
            elif key.startswith("/"):
                full_url = base_url + key
            else:
                full_url = base_url + "/" + key
            # Normalize through the same pipeline as GSC URLs
            full_url = self._normalize_url(full_url)
            if full_url in ga4_data:
                # Merge duplicate (same page reached via different GA4 paths)
                existing = ga4_data[full_url]
                for k in ("sessions", "users", "engaged_sessions",
                          "conversions", "revenue", "add_to_carts"):
                    if k in value:
                        existing[k] = existing.get(k, 0) + value[k]
            else:
                ga4_data[full_url] = value
        print(f"  GA4 after URL alignment: {len(ga4_data)}")

        # Merge into PageAssets
        all_urls = set(gsc_data.keys()) | set(ga4_data.keys())
        assets = []

        for url in all_urls:
            gsc = gsc_data.get(url, {})
            ga4 = ga4_data.get(url, {})

            # Create GSC metrics
            gsc_metrics = GSCMetrics(
                clicks_28d=gsc.get("clicks", 0),
                impressions_28d=gsc.get("impressions", 0),
                avg_position_28d=gsc.get("position", 100),
                ctr_28d=gsc.get("ctr", 0),
                top_queries=[
                    TopQuery(
                        query=q["query"],
                        clicks=q["clicks"],
                        impressions=q["impressions"],
                        position=q["position"],
                        ctr=q["ctr"],
                    )
                    for q in gsc.get("queries", [])
                ],
            )

            # Create GA4 metrics
            sessions = ga4.get("sessions", 0)
            engaged_sessions = ga4.get("engaged_sessions", 0)
            purchases = ga4.get("conversions", 0)  # ecommercePurchases from GA4

            ga4_metrics = GA4Metrics(
                sessions_28d=sessions,
                users_28d=ga4.get("users", 0),
                engaged_sessions_28d=engaged_sessions,
                engagement_rate_28d=engaged_sessions / sessions if sessions > 0 else 0.0,
                purchases_28d=purchases,
                purchase_rate_28d=purchases / sessions if sessions > 0 else 0.0,
                conversions_28d=purchases,
                revenue_28d=ga4.get("revenue", 0),
                add_to_carts_28d=ga4.get("add_to_carts", 0),
                bounce_rate_28d=ga4.get("bounce_rate", 0),
            )

            # Determine asset type from URL pattern
            asset_type = self._classify_asset_type(url)

            asset = PageAsset(
                url=url,
                asset_type=asset_type,
                gsc=gsc_metrics,
                ga4=ga4_metrics,
            )
            assets.append(asset)

        self._assets = assets
        print(f"  Merged: {len(assets)} PageAssets")

        # Import URLs to page inventory
        self.page_inventory.import_from_gsc_urls([a.url for a in assets])

        # Load Google Ads data if available (optional - absence is neutral)
        self._load_ads_data(days)

        return assets

    def _load_ads_data(self, days: int = 28) -> None:
        """
        Load Google Ads data for demand analysis.

        IMPORTANT: Absence of Ads data is NEUTRAL, not negative.
        Ads data anchors monetization evidence but doesn't gatekeep.

        Permitted data (read-only):
        - Search term reports (query-level)
        - Impression Share metrics
        - Cost, conversions, conversion value
        - PMax search term insights
        """
        if not self.ads_client:
            print("  Ads: No Google Ads configured (neutral - GSC+GA4 sufficient)")
            return

        print("  Loading Google Ads data...")

        try:
            # Check for cached data first
            cache_path = Path(__file__).parent.parent.parent / "data" / "ads_cache.json"

            if cache_path.exists():
                self.ads_data = self.ads_client.load_from_cache(str(cache_path))
                if self.ads_data:
                    print(f"  Ads: Loaded {len(self.ads_data.queries)} queries from cache")

            if not self.ads_data:
                # Fetch fresh data (READ-ONLY - no mutations)
                self.ads_data = self.ads_client.fetch_search_terms(days=days)
                print(f"  Ads: Fetched {len(self.ads_data.queries)} queries")

                # Cache for offline use
                self.ads_client.save_to_cache(self.ads_data, str(cache_path))

            # Update constraint detector with Ads data
            if self.ads_data:
                self.constraint_detector.set_ads_data(self.ads_data)

                # Log summary
                summary = self.ads_data.get_monetization_summary()
                print(f"  Ads: {summary['total_queries']} queries, "
                      f"${summary['total_cost']:.2f} cost, "
                      f"{summary['total_conversions']:.0f} conversions")

        except Exception as e:
            print(f"  Ads: Failed to load ({e}) - continuing with GSC+GA4 only")
            # Absence of Ads data is neutral, not a failure
            self.ads_data = None

    # Utility / info page slugs — these are never products or categories.
    _UTILITY_SLUGS = {
        "faq", "faqs", "about", "about-us", "contact", "contact-us",
        "return-policy", "privacy-policy", "terms-of-service", "terms",
        "shipping", "shipping-policy", "price-match-policy", "testimonials",
        "reviews", "sitemap", "search", "cart", "checkout", "account",
        "login", "register", "wishlist", "gift-cards", "gift-certificates",
    }

    # Generic listing-page slugs that indicate category / collection pages.
    _CATEGORY_SLUGS = {
        "all-products", "featured-products", "latest-products", "new-arrivals",
        "best-sellers", "sale", "clearance", "shop-all", "shop-by",
        "made-in-usa-montessori-toys",
    }

    def _classify_asset_type(self, url: str) -> AssetType:
        """
        Classify URL into asset type.

        Priority: sitemap-derived type (most reliable) → URL pattern heuristics.
        Handles both structured URLs (/product/..., /category/...) and flat
        URL schemes where products and categories sit at the root level
        (e.g. /name-trains.html, /3-letter-name-train.html).
        """
        # ── Sitemap-derived type (highest priority) ──
        sitemap_type = self._sitemap_types.get(url)
        if sitemap_type:
            type_map = {"product": AssetType.PRODUCT, "category": AssetType.CATEGORY,
                        "blog": AssetType.BLOG, "other": AssetType.OTHER}
            if sitemap_type in type_map:
                mapped = type_map[sitemap_type]
                # Safety net: some sitemaps put blog posts in a generic
                # sub-sitemap (e.g. sitemap_pages.xml) causing them to be
                # classified as "other".  If the URL clearly lives under
                # /blog/ or /article/, override to BLOG.
                if mapped == AssetType.OTHER:
                    _path = urlparse(url.lower()).path
                    if "/blog" in _path or "/article" in _path:
                        return AssetType.BLOG
                return mapped

        parsed = urlparse(url.lower())
        path = parsed.path.rstrip("/")

        # ── Non-page resources (images, fonts, scripts, etc.) ──
        _MEDIA_EXTS = {
            ".jpeg", ".jpg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
            ".pdf", ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
            ".mp4", ".webm", ".mp3", ".ogg", ".zip", ".gz",
        }
        ext = Path(path).suffix.lower()
        if ext in _MEDIA_EXTS:
            return AssetType.OTHER

        # ── Blog ──
        if "/blog" in path or "/article" in path:
            return AssetType.BLOG

        # ── FAQ / help pages ──
        if "/faq" in path or "/help" in path:
            return AssetType.OTHER

        # ── Structured paths (sites with /product/ or /category/) ──
        if "/product" in path or "/p/" in path:
            return AssetType.PRODUCT
        if "/category" in path or "/c/" in path or "/collections" in path:
            return AssetType.CATEGORY

        # ── Homepage ──
        if not path or path == "/":
            return AssetType.CATEGORY

        # Extract slug (last path component, without extension)
        slug = path.split("/")[-1]
        slug_no_ext = slug.rsplit(".", 1)[0] if "." in slug else slug

        # ── Utility / info pages ──
        if slug_no_ext in self._UTILITY_SLUGS:
            return AssetType.OTHER
        # Catch policy-like pages by keyword
        if any(kw in slug_no_ext for kw in ("policy", "terms-of")):
            return AssetType.OTHER

        # ── Known category slugs ──
        if slug_no_ext in self._CATEGORY_SLUGS:
            return AssetType.CATEGORY

        # ── Product-family matching ──
        # Config-driven: if the slug contains a product family name AND
        # is a short generic slug, it's a category page.  The max word
        # count scales with family slug length so short families like
        # "rug" (1 word) don't over-match specific product names like
        # "literacy-squares-seating-rug" (4 words).
        word_count = len(slug_no_ext.split("-"))
        starts_with_digit = slug_no_ext[0].isdigit() if slug_no_ext else False
        for family_slug in self._product_family_slugs:
            if family_slug in slug_no_ext:
                family_words = len(family_slug.split("-"))
                max_words = family_words + 2  # e.g. "rug"→3, "name-train"→4
                if not starts_with_digit and word_count <= max_words:
                    return AssetType.CATEGORY

        # ── Default: root-level pages are products ──
        # On e-commerce sites with flat URL structures, products vastly
        # outnumber categories (typically 50-100x).  Root-level .html
        # pages that don't match any category pattern are products.
        if path.count("/") <= 1:
            return AssetType.PRODUCT

        return AssetType.OTHER

    def _load_crawl_data(self, csv_path: str) -> None:
        """
        Load crawl data from Beam Us Up CSV and enrich assets.

        Args:
            csv_path: Path to Beam Us Up CSV export
        """
        print(f"Loading crawl data from {csv_path}...")

        importer = BeamUsUpImporter()
        try:
            url_count = importer.load_csv(Path(csv_path))
            print(f"  Loaded: {url_count} URLs from crawl")

            total, enriched = importer.enrich_assets(self._assets)
            print(f"  Enriched: {enriched}/{total} assets with crawl data")
        except FileNotFoundError:
            print(f"  Warning: Crawl data file not found: {csv_path}")
        except Exception as e:
            print(f"  Warning: Failed to load crawl data: {e}")

    def _run_crawler(self) -> None:
        """
        Crawl pages to get canonical/indexability data.
        """
        if not self._assets:
            print("No pages to crawl.")
            return

        print("Crawling pages for canonical/indexability data...")
        urls = [asset.url for asset in self._assets]

        crawler = SimpleCrawler(
            timeout=10.0,
            max_concurrent=10,
        )

        # Crawl all URLs
        crawler.crawl_urls(urls, show_progress=True)

        # Enrich assets with crawl data
        total, enriched = crawler.enrich_assets(self._assets)
        print(f"  Enriched: {enriched}/{total} assets with crawl data")

        # Fetch URL inspection data for a sample of pages
        print("  Fetching URL inspection data...")
        inspection_count = 0
        for asset in self._assets[:50]:  # API has daily quotas, limit to top 50
            try:
                inspection = self.gsc_client.inspect_url(asset.url)
                if "error" not in inspection:
                    asset._url_inspection = inspection
                    inspection_count += 1
                else:
                    asset._url_inspection = None
            except Exception:
                asset._url_inspection = None
            time.sleep(0.2)  # Rate limiting
        print(f"  URL inspection: {inspection_count}/{min(len(self._assets), 50)} URLs inspected")

    def _enrich_cwv(self, max_pages: int = 100) -> None:
        """Attach Core Web Vitals (CrUX) to the highest-traffic assets.

        No-op when GOOGLE_API_KEY is not set. Results are cached for 7 days,
        so repeat runs cost nothing. Bounded to the top pages by impressions
        to respect the CrUX rate limit and keep runs fast.
        """
        if not self.crux_client.api_key:
            print("  Core Web Vitals: skipped (GOOGLE_API_KEY not set)")
            return
        ranked = sorted(
            self._assets,
            key=lambda a: a.gsc.impressions_28d,
            reverse=True,
        )[:max_pages]
        print(f"  Fetching Core Web Vitals for top {len(ranked)} pages...")
        fetched = 0
        for asset in ranked:
            try:
                cwv = self.crux_client.get_cwv(asset.url)
                if cwv:
                    asset._cwv = cwv
                    fetched += 1
                else:
                    asset._cwv = None
            except Exception:
                asset._cwv = None
        print(f"  Core Web Vitals: {fetched}/{len(ranked)} pages have CrUX data")

    def run_diagnostics(self) -> dict:
        """
        Run tracking sanity diagnostics.

        Returns:
            Diagnostic summary
        """
        print("Running tracking diagnostics...")

        # Build organic sessions map with normalized paths
        organic_sessions_map = {}
        for asset in self._assets:
            normalized_path = self.diagnostics.normalize_url(asset.url)
            # Use GA4 sessions as proxy for organic (GA4 client filters organic by default)
            # Use max when multiple assets normalize to the same path (e.g.
            # http:// vs https:// variants of the homepage).
            sessions = asset.ga4.sessions_28d
            if normalized_path in organic_sessions_map:
                organic_sessions_map[normalized_path] = max(
                    organic_sessions_map[normalized_path], sessions
                )
            else:
                organic_sessions_map[normalized_path] = sessions

        # Run diagnostics using the correct method
        results = self.diagnostics.diagnose_all(
            assets=self._assets,
            organic_sessions_map=organic_sessions_map,
        )
        self._diagnostic_results = results

        # Summarize using the diagnostics summary method
        summary = self.diagnostics.summary(results)

        print(f"  Total pages: {summary['total_pages']}")
        print(f"  Passed: {summary['passed']}")
        print(f"  Warned: {summary['warned']}")
        print(f"  Failed (blocking): {summary['failed']}")
        print(f"  Eligible for RAIP: {summary['eligible_for_raip']}")

        return summary

    def build_link_graph(self) -> dict:
        """
        Build internal link graph from page inventory.

        Returns:
            Link graph summary
        """
        print("Building link graph...")

        # Get pages from inventory
        pages = self.page_inventory.get_all_pages()

        if pages:
            self.link_graph.build_from_inventory(pages)

            # Update assets with link metrics
            for asset in self._assets:
                metrics = self.link_graph.get_page_metrics(asset.url)
                if metrics:
                    asset.inlinks = metrics.inlinks
                    # Only use link_graph outlinks if crawler didn't already populate them.
                    # The crawler parses actual on-page links (accurate); the link_graph
                    # builds from PageInventory which may have empty placeholders.
                    if not asset.internal_outlinks:
                        asset.outlinks = metrics.outlinks
                    asset.link_authority_score = metrics.authority_score

        summary = self.link_graph.summary()
        print(f"  Nodes: {summary['total_nodes']}")
        print(f"  Edges: {summary['total_edges']}")
        print(f"  Orphans: {summary['orphan_pages']}")

        return summary

    def evaluate_all(self, modes: list[GovernorMode] = None) -> list[dict]:
        """
        Run all evaluations across all modes.

        Args:
            modes: Modes to evaluate. Default: all modes

        Returns:
            List of evaluation results
        """
        if modes is None:
            modes = self.config.modes or [
                GovernorMode.PRESERVATION,
                GovernorMode.OPPORTUNITY_DISCOVERY,
                GovernorMode.FUNNEL_ALIGNMENT,
            ]

        print(f"Running evaluations for modes: {[m.value for m in modes]}...")

        all_results = []

        for asset in self._assets:
            result = self._evaluate_asset(asset, modes)
            all_results.append(result)

        self._evaluation_results = all_results

        # Summary
        actions = [r for r in all_results if r.get("recommended_action") != "NO_ACTION"]
        print(f"  Total evaluated: {len(all_results)}")
        print(f"  With actions: {len(actions)}")

        return all_results

    def _evaluate_asset(
        self,
        asset: PageAsset,
        modes: list[GovernorMode],
    ) -> dict:
        """
        Evaluate a single asset across all modes and evaluators.

        Returns best recommendation after applying learning rules.
        """
        # FIRST: Run constraint detection (new paradigm)
        constraint_result = self.constraint_detector.evaluate(asset, self._assets)

        # Store constraint data to include in all results
        # data_confidence = constraint_detector's assessment of data quality for this page
        # This is separate from action-specific confidence of the winning candidate.
        constraint_data = {
            "capture_class": constraint_result.capture_class.value,
            "demand_score": round(constraint_result.demand_score, 2),
            "intent_score": round(constraint_result.intent_score, 2),
            "visibility_score": round(constraint_result.visibility_score, 2),
            "data_confidence": round(constraint_result.confidence, 2),
            "primary_constraint": constraint_result.primary_constraint.value if constraint_result.primary_constraint else None,
            "constraints": [
                {
                    "constraint_type": c.constraint_type.value,
                    "severity": c.severity,
                    "description": c.description,
                    "evidence": c.evidence,
                    "recommended_action": c.recommended_action,
                }
                for c in constraint_result.constraints
            ],
            "extracted_queries": constraint_result.extracted_queries[:5],  # Top 5
            # Full query data with metrics for actionability
            "top_queries": [
                {
                    "query": q.query,
                    "impressions": q.impressions,
                    "clicks": q.clicks,
                    "position": round(q.position, 1),
                    "ctr": round(q.ctr * 100, 2),  # As percentage
                }
                for q in asset.gsc.top_queries[:10]  # Top 10 queries
            ],
            # Ads-enriched fields
            "has_ads_data": constraint_result.has_ads_data,
            "monetization_score": round(constraint_result.monetization_score, 2),
            "coverage_gap_score": round(constraint_result.coverage_gap_score, 2),
            # Raw GSC/GA4 metrics for data quality comparison
            "gsc_impressions": asset.gsc.impressions_28d,
            "gsc_clicks": asset.gsc.clicks_28d,
            "gsc_ctr": round(asset.gsc.ctr_28d, 4),
            "gsc_position": round(asset.gsc.avg_position_28d, 1),
            "ga4_sessions": asset.ga4.sessions_28d,
            "ga4_users": asset.ga4.users_28d,
            "ga4_engaged_sessions": asset.ga4.engaged_sessions_28d,
            "ga4_engagement_rate": round(asset.ga4.engagement_rate_28d, 4),
            "ga4_revenue": round(asset.ga4.revenue_28d, 2),
            "ga4_purchases": asset.ga4.purchases_28d,
            "ga4_bounce_rate": round(asset.ga4.bounce_rate_28d, 4),
            # Core Web Vitals (CrUX) — present only for pages we fetched
            "cwv": getattr(asset, "_cwv", None),
        }

        # Page Quality scorecard for product/category pages (rule-based)
        if asset.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY):
            constraint_data["page_quality"] = evaluate_page_quality(
                url=asset.url,
                asset_type=asset.asset_type.value,
                title=asset.title,
                meta_description=asset.meta_description,
                h1=asset.h1,
                canonical_url=asset.canonical_url,
                word_count=asset.word_count,
                schema_types=getattr(asset, "schema_types", []),
                above_fold_html=asset.above_fold_html,
                body_html=asset.body_html,
                internal_outlinks=asset.internal_outlinks,
                breadcrumb_links=getattr(asset, "breadcrumb_links", []),
                has_crawl_data=asset.has_crawl_data,
            )

        candidates = []
        is_blog = asset.asset_type == AssetType.BLOG

        # If visibility is blocked but demand exists, prioritize visibility fix
        # Lowered from 0.4 to 0.2 (≥100 impressions) to surface more blocked pages.
        if (constraint_result.primary_constraint == ConstraintType.VISIBILITY_BLOCKED
            and constraint_result.demand_score >= 0.2):
            # Calculate expected value based on potential, not current revenue
            potential_clicks = asset.gsc.impressions_28d * 0.05  # ~5% CTR at good position
            expected_value = self._ev_from_clicks(potential_clicks)
            candidates.append({
                "mode": "CONSTRAINT_RESOLUTION",
                "action": "VISIBILITY_FIX",
                "expected_value": expected_value,
                "confidence": constraint_result.confidence,
                "risk_level": "low",
                "implementation_steps": constraint_result.recommended_actions,
                "source": "constraint_detector",
            })

        # If CTR is suppressed, prioritize title test
        if constraint_result.primary_constraint == ConstraintType.CTR_SUPPRESSED:
            ctr_constraint = next(
                (c for c in constraint_result.constraints if c.constraint_type == ConstraintType.CTR_SUPPRESSED),
                None
            )
            if ctr_constraint:
                missed_clicks = ctr_constraint.evidence.get("missed_clicks", 0)
                expected_value = self._ev_from_clicks(missed_clicks)
                candidates.append({
                    "mode": "CONSTRAINT_RESOLUTION",
                    "action": "TITLE_META_TEST",
                    "expected_value": expected_value,
                    "confidence": constraint_result.confidence,
                    "risk_level": "low",
                    "implementation_steps": [ctr_constraint.recommended_action],
                    "source": "constraint_detector",
                })

        # Handle Ads-specific constraints (if Ads data available)
        # (Not applicable to blogs — blogs do not get paid recommendations)
        if constraint_result.has_ads_data and not is_blog:
            for ads_constraint in constraint_result.ads_constraints:
                if ads_constraint.constraint_type == ConstraintType.IMPRESSION_SHARE_BUDGET:
                    # Budget constraint - high value opportunity
                    is_lost = ads_constraint.evidence.get("avg_is_lost_to_budget", 0)
                    potential_impressions = asset.gsc.impressions_28d * is_lost
                    expected_value = self._ev_from_clicks(potential_impressions * 0.03)
                    candidates.append({
                        "mode": "CONSTRAINT_RESOLUTION",
                        "action": "PAID_BUDGET_INCREASE",
                        "expected_value": expected_value,
                        "confidence": constraint_result.confidence,
                        "risk_level": "low",
                        "implementation_steps": [ads_constraint.recommended_action],
                        "source": "constraint_detector_ads",
                    })

                elif ads_constraint.constraint_type == ConstraintType.PAID_COVERAGE_GAP:
                    # High demand queries without paid coverage
                    unmon_impressions = ads_constraint.evidence.get("total_impressions", 0)
                    expected_value = self._ev_from_clicks(unmon_impressions * 0.02)
                    candidates.append({
                        "mode": "CONSTRAINT_RESOLUTION",
                        "action": "EXPAND_PAID_COVERAGE",
                        "expected_value": expected_value,
                        "confidence": constraint_result.confidence * 0.8,  # Slightly lower confidence
                        "risk_level": "medium",
                        "implementation_steps": [ads_constraint.recommended_action],
                        "source": "constraint_detector_ads",
                    })

                elif ads_constraint.constraint_type == ConstraintType.PMAX_ABSORPTION:
                    # PMax absorbing queries - review needed
                    candidates.append({
                        "mode": "CONSTRAINT_RESOLUTION",
                        "action": "REVIEW_PMAX_COVERAGE",
                        "expected_value": 0,  # Can't estimate without more data
                        "confidence": 0.6,
                        "risk_level": "low",
                        "implementation_steps": [ads_constraint.recommended_action],
                        "source": "constraint_detector_ads",
                    })

        # Run main Governor evaluation for each mode
        for mode in modes:
            # Call the appropriate Governor method based on mode
            if mode == GovernorMode.PRESERVATION:
                result = self.governor.evaluate_preservation(asset)
            elif mode == GovernorMode.OPPORTUNITY_DISCOVERY:
                result = self.governor.evaluate_opportunity(asset)
            elif mode == GovernorMode.FUNNEL_ALIGNMENT:
                result = self.governor.evaluate_funnel(asset)
            else:
                continue

            if result.decision not in (DecisionType.NO_ACTION, DecisionType.OBSERVE_ONLY):
                # Derive risk level from reversibility
                risk_level = "low"
                if result.priority and result.priority.reversibility:
                    rev = result.priority.reversibility.value
                    if rev == "irreversible":
                        risk_level = "high"
                    elif rev == "slowly_reversible":
                        risk_level = "medium"

                candidates.append({
                    "mode": mode.value,
                    "action": result.decision.value,
                    "expected_value": result.priority.expected_value if result.priority else result.score_value,
                    "confidence": result.confidence,
                    "risk_level": risk_level,
                    "implementation_steps": result.action_plan.steps if result.action_plan else [],
                    "source": "governor",
                })

        # Run specialized evaluators
        # Title/Meta evaluation — lowered from 500 to 100 so more pages
        # get title analysis; confidence scores handle data-quality risk.
        if asset.gsc.impressions_28d >= 100:
            title_result = self.title_evaluator.evaluate(asset)
            # TitleTestResult uses should_test and expected_ctr_lift
            if title_result.should_test and title_result.recommended_variant:
                # expected_ctr_lift is a CTR rate delta; × impressions = incremental
                # clicks (was incorrectly multiplied by GA4 sessions before).
                title_incr_clicks = title_result.expected_ctr_lift * asset.gsc.impressions_28d
                candidates.append({
                    "mode": "OPPORTUNITY_DISCOVERY",
                    "action": "TITLE_META_TEST",
                    "expected_value": self._ev_from_clicks(title_incr_clicks),
                    "confidence": title_result.confidence,
                    "risk_level": title_result.risk_level,
                    "implementation_steps": [title_result.recommended_variant.title] if title_result.recommended_variant else [],
                    "source": "title_evaluator",
                })

        # Canonical evaluation
        canonical_result = self.canonical_evaluator.evaluate(asset)
        # CanonicalFixResult uses has_issues and total_traffic_at_risk
        if canonical_result.has_issues and canonical_result.recommended_action != "NO_ACTION":
            # Build detailed issue information
            issues_detail = []
            for issue in canonical_result.issues:
                issues_detail.append({
                    "type": issue.issue_type,
                    "severity": issue.severity,
                    "description": issue.description,
                    "fix": issue.recommended_fix,
                    "impact": issue.estimated_impact,
                })

            candidates.append({
                "mode": "PRESERVATION",
                "action": canonical_result.recommended_action,
                # traffic_at_risk is monthly clicks at risk — monetize the same
                # way as click-acquisition actions for a comparable dollar value.
                "expected_value": self._ev_from_clicks(float(canonical_result.total_traffic_at_risk)),
                "confidence": canonical_result.confidence,
                "risk_level": "low",  # Canonical fixes are generally low risk
                "implementation_steps": canonical_result.implementation_steps,
                "source": "canonical_evaluator",
                "issues": issues_detail,
                "rollback_plan": canonical_result.rollback_plan,
            })

        # Internal link evaluation
        link_result = self.link_evaluator.evaluate(asset, self._assets)
        # LinkReallocationResult has all standard attributes including routing assessment
        if link_result.recommended_action != "NO_ACTION":
            link_candidate = {
                "mode": "FUNNEL_ALIGNMENT",
                "action": link_result.recommended_action,
                # expected_lift is a conversion-rate lift on existing sessions →
                # incremental conversions, monetized directly (was arbitrary ×2).
                "expected_value": self._ev_from_conversions(link_result.expected_lift * asset.ga4.sessions_28d),
                "confidence": link_result.confidence,
                "risk_level": link_result.risk_level,
                "implementation_steps": link_result.implementation_steps,
                "source": "link_evaluator",
            }
            # For blog pages, include routing details (required per output requirements)
            if is_blog:
                link_candidate["routing_data"] = {
                    "current_routing_paths": link_result.current_routing_paths,
                    "recommended_destinations": link_result.recommended_destinations,
                    "destination_rationale": link_result.destination_rationale,
                    "links_to_remove": link_result.links_to_remove,
                }
                if link_result.routing_assessment:
                    link_candidate["routing_data"]["routing_quality"] = link_result.routing_assessment.routing_quality
                    link_candidate["routing_data"]["has_primary_destination"] = link_result.routing_assessment.has_primary_destination
            candidates.append(link_candidate)

        # HTML structural evaluation — only meaningful with crawl data.
        # Detects span/div CTAs, empty media-wrapping anchors, and missing
        # above-fold links (link-equity + engagement leaks).
        if asset.has_crawl_data:
            html_result = self.html_evaluator.evaluate(asset)
            if html_result.has_issues and html_result.recommended_action != "NO_ACTION":
                html_incr_clicks = html_result.expected_ctr_lift * asset.gsc.impressions_28d
                candidates.append({
                    "mode": "FUNNEL_ALIGNMENT",
                    "action": "HTML_STRUCTURAL_FIX",
                    "expected_value": self._ev_from_clicks(html_incr_clicks),
                    "confidence": html_result.confidence,
                    "risk_level": html_result.risk_level,
                    "implementation_steps": html_result.implementation_steps,
                    "source": "html_evaluator",
                    "issues": [
                        {
                            "type": i.issue_type,
                            "severity": i.severity,
                            "description": i.description,
                            "fix": i.recommended_fix,
                        }
                        for i in html_result.issues
                    ],
                })

        # Core Web Vitals — poor real-user performance is a genuine ranking &
        # conversion drag. Only surface for pages with real demand so we don't
        # prescribe expensive dev work on zero-traffic pages.
        cwv = getattr(asset, "_cwv", None)
        if cwv and cwv.get("overall_rating") == "poor" and asset.gsc.impressions_28d >= 100:
            failing = []
            if cwv.get("lcp_rating") == "poor":
                failing.append(f"LCP {cwv.get('lcp_ms')}ms")
            if cwv.get("inp_rating") == "poor":
                failing.append(f"INP {cwv.get('inp_ms')}ms")
            if cwv.get("cls_rating") == "poor":
                failing.append(f"CLS {cwv.get('cls')}")
            # Conservative EV: fixing speed recovers ~2% of impressions as clicks.
            cwv_incr_clicks = asset.gsc.impressions_28d * 0.02
            candidates.append({
                "mode": "PRESERVATION",
                "action": "PAGE_SPEED_FIX",
                "expected_value": self._ev_from_clicks(cwv_incr_clicks),
                "confidence": 0.7 if cwv.get("level") == "url" else 0.5,
                "risk_level": "low",
                "implementation_steps": [
                    f"Core Web Vitals rated POOR ({cwv.get('level', 'url')}-level): "
                    + ", ".join(failing) + ".",
                    "Prioritize the failing metric: LCP → optimize hero image/server "
                    "response; INP → reduce JS execution; CLS → set image/embed dimensions.",
                    "Re-check CrUX after deploy (data updates on a 28-day rolling window).",
                ],
                "source": "crux_cwv",
            })

        # FALLBACK: Create opportunities for pages that no specific evaluator
        # caught. The system should surface ALL pages so operators can see the
        # full inventory, not just the high-traffic tail.
        if not candidates:
            if asset.gsc.impressions_28d > 0:
                # Any impressions = demand exists. Create a basic opportunity.
                potential_ctr = 0.03 if asset.gsc.avg_position_28d > 20 else 0.05
                potential_clicks = asset.gsc.impressions_28d * potential_ctr
                expected_value = self._ev_from_clicks(potential_clicks)

                steps = constraint_result.recommended_actions if constraint_result.recommended_actions else [
                    "Review title tag, meta description, and on-page content",
                    "Add internal links from relevant category or blog pages",
                    "Monitor for 28 days",
                ]

                candidates.append({
                    "mode": "OPPORTUNITY_DISCOVERY",
                    "action": "PAGE_REINVESTMENT",
                    "expected_value": max(expected_value, 0.50),
                    # Use constraint confidence directly — PAGE_REINVESTMENT is a
                    # low-risk review action, no need to penalize further.
                    "confidence": max(constraint_result.confidence, self.config.exploration_confidence_threshold),
                    "risk_level": "low",
                    "implementation_steps": steps,
                    "source": "demand_coverage_gap",
                })

            else:
                # Zero impressions — page may not be indexed, missing from
                # sitemap, lacking internal links, or cannibalised.
                # Surface ALL asset types so operators can investigate.
                candidates.append({
                    "mode": "OPPORTUNITY_DISCOVERY",
                    "action": "VISIBILITY_FIX",
                    # Zero impressions: nominal value so it surfaces for review
                    # but never outranks pages with real demand.
                    "expected_value": self._ev_from_clicks(1),
                    "confidence": self.config.exploration_confidence_threshold,
                    "risk_level": "low",
                    "implementation_steps": [
                        "Check if page is indexed (use URL Inspection in GSC)",
                        "Verify page is in XML sitemap",
                        "Review meta robots and canonical tag",
                        "Ensure page has internal links from category/navigation",
                    ],
                    "source": "zero_visibility_review",
                })

        # Apply learning rules to adjust confidence
        for candidate in candidates:
            fingerprint = ActionFingerprint(
                page_type=asset.asset_type.value,
                intent_cluster=self._get_intent_cluster(asset),
                action_surface=self._get_action_surface(candidate["action"]),
                action_type=candidate["action"],
            )

            adjusted_conf, insight = self.ledger.apply_learning_rules(
                base_confidence=candidate["confidence"],
                fingerprint=fingerprint,
                min_confidence_threshold=self.config.exploration_confidence_threshold,
            )

            candidate["original_confidence"] = candidate["confidence"]
            candidate["confidence"] = adjusted_conf
            candidate["learning_adjustment"] = insight.confidence_adjustment
            candidate["learning_reference"] = insight.recommendation if insight.matching_actions > 0 else None

        # Calculate priority for each candidate
        for candidate in candidates:
            action_type = self._resolve_action_type(candidate["action"])

            priority = calculate_priority(
                expected_value=candidate["expected_value"],
                confidence=candidate["confidence"],
                action_type=action_type,
                impressions=asset.gsc.impressions_28d,
                position=asset.gsc.avg_position_28d,
                has_revenue=asset.ga4.revenue_28d > 0,
            )
            candidate["priority_score"] = priority.priority

        # Select best candidate by priority
        if candidates:
            candidates.sort(key=lambda x: x.get("priority_score", 0), reverse=True)
            best = candidates[0]

            # Summarize ALL applicable actions for this page (not just the winner)
            # so operators can see the full set of options in the detail view.
            all_candidates = [
                {
                    "action": c["action"],
                    "mode": c["mode"],
                    "expected_value": round(c["expected_value"], 2),
                    "confidence": round(c["confidence"], 2),
                    "priority_score": round(c.get("priority_score", 0), 2),
                    "risk_level": c.get("risk_level", "low"),
                    "source": c.get("source", ""),
                    "summary": c["implementation_steps"][0] if c.get("implementation_steps") else "",
                }
                for c in candidates
            ]

            # Check confidence threshold — lane-aware
            # High-risk (irreversible) actions need PRESERVATION threshold (0.75)
            # Low/medium-risk (reversible) actions need EXPLORATION threshold (0.55)
            is_preservation = best.get("risk_level") == "high"
            confidence_threshold = (
                self.config.preservation_confidence_threshold if is_preservation
                else self.config.exploration_confidence_threshold
            )
            if best["confidence"] < confidence_threshold:
                lane = "PRESERVATION" if is_preservation else "EXPLORATION"
                return {
                    "url": asset.url,
                    "asset_type": asset.asset_type.value,
                    "recommended_action": "OBSERVE_ONLY",
                    "mode": best["mode"],
                    "expected_value": best["expected_value"],
                    "confidence": round(constraint_result.confidence, 2),
                    "action_confidence": round(best["confidence"], 2),
                    "priority_score": 0,
                    "risk_level": best.get("risk_level", "low"),
                    "implementation_steps": [],
                    "reason": f"Action confidence {best['confidence']:.2f} below {lane} threshold {confidence_threshold}",
                    "all_candidates": all_candidates,
                    "page_metadata": {
                        "title": asset.title,
                        "h1": asset.h1,
                        "meta_description": asset.meta_description,
                        "canonical_url": asset.canonical_url,
                        "word_count": asset.word_count,
                        "content_preview": asset.content_preview,
                        "above_fold_html": asset.above_fold_html,
                        "internal_outlinks": asset.internal_outlinks,
                        "breadcrumb_links": getattr(asset, "breadcrumb_links", []),
                        "schema_types": getattr(asset, "schema_types", []),
                        "url_inspection": getattr(asset, "_url_inspection", None),
                        "has_crawl_data": asset.has_crawl_data,
                    },
                    **constraint_data,  # Include constraint detection data
                }

            result = {
                "url": asset.url,
                "asset_type": asset.asset_type.value,
                "recommended_action": best["action"],
                "mode": best["mode"],
                "expected_value": best["expected_value"],
                # Use data quality confidence (from constraint_detector) as headline.
                # This reflects how much data we have, not action-specific confidence.
                "confidence": round(constraint_result.confidence, 2),
                "action_confidence": round(best["confidence"], 2),
                "priority_score": best.get("priority_score", 0),
                "risk_level": best["risk_level"],
                "implementation_steps": best["implementation_steps"],
                "implementation_summary": best["implementation_steps"][0] if best["implementation_steps"] else "",
                "learning_reference": best.get("learning_reference"),
                "source": best["source"],
                "all_candidates": all_candidates,
                # Page metadata from crawl (used by AI subsystem and modal display)
                "page_metadata": {
                    "title": asset.title,
                    "h1": asset.h1,
                    "meta_description": asset.meta_description,
                    "canonical_url": asset.canonical_url,
                    "word_count": asset.word_count,
                    "content_preview": asset.content_preview,
                    "above_fold_html": asset.above_fold_html,
                    "internal_outlinks": asset.internal_outlinks,
                    "breadcrumb_links": getattr(asset, "breadcrumb_links", []),
                    "schema_types": getattr(asset, "schema_types", []),
                    "url_inspection": getattr(asset, "_url_inspection", None),
                    "has_crawl_data": asset.has_crawl_data,
                },
                **constraint_data,  # Include constraint detection data
            }

            # Include optional fields from specific evaluators
            if "issues" in best:
                result["issues"] = best["issues"]
            if "rollback_plan" in best:
                result["rollback_plan"] = best["rollback_plan"]

            # For blog recommendations, include routing details per output requirements
            if is_blog and "routing_data" in best:
                result["routing_data"] = best["routing_data"]

            return result

        # No action recommended - but still include constraint data!
        return {
            "url": asset.url,
            "asset_type": asset.asset_type.value,
            "recommended_action": "NO_ACTION",
            "mode": "PRESERVATION",
            "expected_value": 0,
            "confidence": 1.0,
            "priority_score": 0,
            "risk_level": "none",
            "implementation_steps": [],
            "reason": "No actionable opportunities identified",
            "page_metadata": {
                "title": asset.title,
                "h1": asset.h1,
                "meta_description": asset.meta_description,
                "canonical_url": asset.canonical_url,
                "word_count": asset.word_count,
                "content_preview": asset.content_preview,
                "above_fold_html": asset.above_fold_html,
                "internal_outlinks": asset.internal_outlinks,
                "breadcrumb_links": getattr(asset, "breadcrumb_links", []),
                "schema_types": getattr(asset, "schema_types", []),
                "url_inspection": getattr(asset, "_url_inspection", None),
                "has_crawl_data": asset.has_crawl_data,
            },
            **constraint_data,  # Include constraint detection data
        }

    # Map action strings that have no direct ActionType member to their
    # closest scoring equivalent (for effort/reversibility).
    _ACTION_TYPE_ALIASES = {
        "VISIBILITY_FIX": ActionType.INTERNAL_LINK_REALLOCATION,
        "HTML_STRUCTURAL_FIX": ActionType.INTERNAL_LINK_REALLOCATION,
        "PAGE_SPEED_FIX": ActionType.PAGE_REINVESTMENT,
        "CONTENT_CLARIFY": ActionType.PAGE_REINVESTMENT,
        "CONSOLIDATION_REVIEW": ActionType.PAGE_REINVESTMENT,
        "CONTENT_PRUNE": ActionType.PAGE_REINVESTMENT,
        "PAID_BUDGET_INCREASE": ActionType.OBSERVE_ONLY,
        "EXPAND_PAID_COVERAGE": ActionType.OBSERVE_ONLY,
        "REVIEW_PMAX_COVERAGE": ActionType.OBSERVE_ONLY,
    }

    def _resolve_action_type(self, action_str: str) -> ActionType:
        """Resolve a candidate action string to an ActionType.

        Candidate actions are uppercase strings (e.g. "TITLE_META_TEST") but
        the enum VALUES are lowercase ("title_meta_test"), so the old
        ActionType(value) lookup always raised ValueError and every action
        silently collapsed to OBSERVE_ONLY — nullifying the effort and
        reversibility differentiation in priority scoring. Look up by member
        NAME (case-insensitive), then fall back to the alias map.
        """
        if not action_str:
            return ActionType.OBSERVE_ONLY
        key = action_str.upper()
        try:
            return ActionType[key]  # by member name, not value
        except KeyError:
            pass
        if key in self._ACTION_TYPE_ALIASES:
            return self._ACTION_TYPE_ALIASES[key]
        try:
            return ActionType(action_str.lower())  # last resort: by value
        except ValueError:
            return ActionType.OBSERVE_ONLY

    def _ev_from_clicks(self, incremental_clicks: float) -> float:
        """Monetize incremental monthly organic clicks into recoverable margin.

        Unified formula for all click-acquisition actions (visibility, CTR,
        title, canonical-at-risk). EV = clicks × conv_rate × AOV × margin.
        Every candidate that adds/protects clicks uses this so the Est. Value
        column is comparable across action types.
        """
        return max(0.0, incremental_clicks) * self.config.conv_rate * self.config.aov * self.config.margin

    def _ev_from_conversions(self, incremental_conversions: float) -> float:
        """Monetize incremental monthly conversions (already click-independent).

        Used for routing/internal-link actions where the lift is on the
        conversion rate of existing sessions, not on click acquisition.
        EV = conversions × AOV × margin.
        """
        return max(0.0, incremental_conversions) * self.config.aov * self.config.margin

    def _get_intent_cluster(self, asset: PageAsset) -> str:
        """Get dominant intent cluster for asset."""
        if asset.ga4.revenue_28d > 0:
            return "transactional"
        elif asset.asset_type in (AssetType.PRODUCT, AssetType.CATEGORY):
            return "commercial"
        elif asset.asset_type == AssetType.BLOG:
            return "informational"
        return "unknown"

    def _get_action_surface(self, action: str) -> str:
        """Get action surface from action type."""
        surfaces = {
            "TITLE_META_TEST": "title",
            "CANONICAL_FIX": "canonical",
            "INTERNAL_LINK_REALLOCATION": "links",
            "PAGE_REINVESTMENT": "content",
            "NEW_ASSET_CREATION": "content",
        }
        return surfaces.get(action, "other")

    def _append_history_snapshot(self, data_dir: Path):
        """Append a lightweight site-level snapshot to evaluation_history.json.

        Keeps the last 52 runs so the dashboard can plot site-level trend
        (total impressions/clicks/revenue, action rate) over time.
        """
        history_path = data_dir / "evaluation_history.json"
        history = []
        if history_path.exists():
            try:
                with open(history_path) as f:
                    history = json.load(f).get("snapshots", [])
            except (json.JSONDecodeError, OSError):
                history = []

        total_impr = sum(r.get("gsc_impressions", 0) or 0 for r in self._evaluation_results)
        total_clicks = sum(r.get("gsc_clicks", 0) or 0 for r in self._evaluation_results)
        total_rev = sum(r.get("ga4_revenue", 0) or 0 for r in self._evaluation_results)
        actionable = sum(
            1 for r in self._evaluation_results
            if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
        )
        history.append({
            "timestamp": datetime.now().isoformat(),
            "total_pages": len(self._evaluation_results),
            "actionable_pages": actionable,
            "total_impressions": total_impr,
            "total_clicks": total_clicks,
            "total_revenue": round(total_rev, 2),
        })
        history = history[-52:]  # keep last year of weekly runs

        with open(history_path, "w") as f:
            json.dump({"snapshots": history}, f, indent=2, default=str)

    def save_results_for_dashboard(self):
        """Save evaluation and diagnostic results for the web dashboard."""
        data_dir = Path(__file__).parent.parent.parent / "data"
        data_dir.mkdir(exist_ok=True)

        # ── Trend: compare against the previous run before overwriting it ──
        # Run-over-run deltas turn a static snapshot into a direction: is this
        # page gaining or losing impressions/clicks/position/revenue?
        prev_path = data_dir / "latest_evaluation.json"
        prev_metrics = {}
        prev_ts = None
        if prev_path.exists():
            try:
                with open(prev_path) as f:
                    prev = json.load(f)
                prev_ts = prev.get("timestamp")
                for r in prev.get("results", []):
                    u = r.get("url")
                    if u:
                        prev_metrics[u] = {
                            "impressions": r.get("gsc_impressions", 0) or 0,
                            "clicks": r.get("gsc_clicks", 0) or 0,
                            "position": r.get("gsc_position", 0) or 0,
                            "revenue": r.get("ga4_revenue", 0) or 0,
                        }
            except (json.JSONDecodeError, OSError):
                pass

        for r in self._evaluation_results:
            prev = prev_metrics.get(r.get("url"))
            if not prev:
                continue
            cur_pos = r.get("gsc_position", 0) or 0
            prev_pos = prev["position"]
            r["trend"] = {
                "prev_timestamp": prev_ts,
                "impressions_delta": (r.get("gsc_impressions", 0) or 0) - prev["impressions"],
                "clicks_delta": (r.get("gsc_clicks", 0) or 0) - prev["clicks"],
                # Positive = moved UP the SERP (lower position number = better).
                "position_delta": round(prev_pos - cur_pos, 1) if (cur_pos and prev_pos) else None,
                "revenue_delta": round((r.get("ga4_revenue", 0) or 0) - prev["revenue"], 2),
            }

        # Append a site-level snapshot to the rolling history (last 52 runs)
        self._append_history_snapshot(data_dir)

        # Save evaluation results
        eval_data = {
            "timestamp": datetime.now().isoformat(),
            "total_pages": len(self._assets),
            "pages_with_action": sum(
                1 for r in self._evaluation_results
                if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None)
            ),
            "action_rate": round(
                sum(1 for r in self._evaluation_results
                    if r.get("recommended_action") not in ("NO_ACTION", "OBSERVE_ONLY", None))
                / max(len(self._evaluation_results), 1) * 100, 1
            ),
            "total_expected_value": round(
                sum(r.get("expected_value", 0) for r in self._evaluation_results), 2
            ),
            "results": self._evaluation_results,
        }

        with open(data_dir / "latest_evaluation.json", "w") as f:
            json.dump(eval_data, f, indent=2, default=str)

        # Save diagnostic results
        if self._diagnostic_results:
            diag_data = {
                "timestamp": datetime.now().isoformat(),
                "results": [
                    {
                        "url": d.url,
                        "status": d.status,
                        "blocking": d.blocking,
                        "tier": "A" if d.blocking else ("B" if d.status == "WARN" else "PASS"),
                        "eligible_for_raip": d.eligible_for_raip,
                        "special_classification": d.special_classification,
                        "failures": [
                            {
                                "code": f.code.value,
                                "severity": f.severity.value,
                                "interpretation": f.interpretation,
                                "recommended_fix": f.recommended_fix,
                            }
                            for f in d.failures
                        ],
                    }
                    for d in self._diagnostic_results
                ],
            }

            with open(data_dir / "diagnostic_results.json", "w") as f:
                json.dump(diag_data, f, indent=2)

        print(f"  Results saved to {data_dir}")

    def generate_output(self) -> str:
        """
        Generate formatted output report.

        Returns:
            Formatted report string
        """
        print("Generating output report...")

        # Get learning insights for referenced patterns
        learning_insights = []
        for result in self._evaluation_results:
            if result.get("learning_reference"):
                # Could fetch from ledger, but for now just include in output
                pass

        # Format diagnostic summary
        diagnostic_summary = None
        if self._diagnostic_results:
            # PageDiagnostic objects have blocking (Tier A) and status attributes
            diagnostic_summary = {
                "tier_a_failures": sum(1 for r in self._diagnostic_results if r.blocking),
                "tier_b_warnings": sum(1 for r in self._diagnostic_results if r.status == "WARN"),
                "eligible_for_raip": sum(1 for r in self._diagnostic_results if r.eligible_for_raip),
            }

        # Get regret budget from ledger
        ledger_summary = self.ledger.summary()
        # Count irreversible actions in last year
        # (simplified - would need to track properly)
        regret_budget_remaining = self.config.regret_budget_year

        output = self.formatter.format_and_output(
            evaluation_results=self._evaluation_results,
            output_format=self.config.output_format,
            output_path=self.config.output_path,
            diagnostic_summary=diagnostic_summary,
            regret_budget_remaining=regret_budget_remaining,
        )

        return output

    def run(
        self,
        days: int = 28,
        crawl_data_path: Optional[str] = None,
        run_crawler: bool = False,
    ) -> str:
        """
        Run the complete evaluation workflow.

        Args:
            days: Days of data to analyze
            crawl_data_path: Optional path to Beam Us Up CSV export
            run_crawler: Whether to crawl pages for canonical/indexability data

        Returns:
            Formatted output report
        """
        print("=" * 60)
        print("AGENTIC ORGANIC GROWTH GOVERNOR")
        print("Full Evaluation Workflow")
        print("=" * 60)
        print()

        # Step 1: Load data
        self.load_data(days=days)
        print()

        # Step 1b: Enrich with crawl data
        if run_crawler:
            self._run_crawler()
            print()
        elif crawl_data_path:
            self._load_crawl_data(crawl_data_path)
            print()

        # Step 2: Run diagnostics
        if self.config.run_diagnostics:
            diag_summary = self.run_diagnostics()
            print()

            # Check for blockers (failed = Tier A blocking failures)
            if self.config.block_on_tier_a and diag_summary["failed"] > 0:
                print("⚠️  BLOCKED: Tier A diagnostic failures detected.")
                print(f"   {diag_summary['failed']} pages have blocking issues.")
                print("   Resolve tracking issues before proceeding.")
                return "BLOCKED: Tier A diagnostic failures"

        # Step 3: Build link graph
        self.build_link_graph()
        print()

        # Step 3b: Enrich with Core Web Vitals (CrUX) — cached 7 days
        self._enrich_cwv()
        print()

        # Step 4: Run evaluations
        self.evaluate_all()
        print()

        # Step 5: Save results for dashboard
        self.save_results_for_dashboard()
        print()

        # Step 6: Generate output
        output = self.generate_output()
        print()

        print("Workflow complete.")
        return output


def main():
    """Main entry point for running the workflow."""
    import argparse

    parser = argparse.ArgumentParser(description="Run Agentic Organic Growth Governor")
    parser.add_argument("--config", type=str, default="config/defaults.json",
                        help="Path to config file")
    parser.add_argument("--days", type=int, default=28,
                        help="Days of data to analyze")
    parser.add_argument("--format", type=str, default="console",
                        choices=["console", "json", "markdown"],
                        help="Output format")
    parser.add_argument("--output", type=str, help="Output file path")
    parser.add_argument("--skip-diagnostics", action="store_true",
                        help="Skip tracking diagnostics")
    parser.add_argument("--no-block", action="store_true",
                        help="Don't block on Tier A diagnostic failures")
    parser.add_argument("--crawl", action="store_true",
                        help="Crawl pages to get canonical/indexability data")
    parser.add_argument("--crawl-data", type=str,
                        help="Path to Beam Us Up CSV export (alternative to --crawl)")

    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if config_path.exists():
        config = WorkflowConfig.from_json(config_path)
    else:
        print(f"Config file not found: {config_path}")
        print("Using default configuration...")
        config = WorkflowConfig(
            gsc_property="sc-domain:example.com",
            ga4_property_id="123456789",
            credentials_path="credentials.json",
        )

    # Override from command line
    config.output_format = OutputFormat(args.format)
    if args.output:
        config.output_path = Path(args.output)
    config.run_diagnostics = not args.skip_diagnostics
    if args.no_block:
        config.block_on_tier_a = False

    # Run workflow
    workflow = FullEvaluationWorkflow(config)
    output = workflow.run(
        days=args.days,
        crawl_data_path=args.crawl_data,
        run_crawler=args.crawl,
    )

    print(output)


if __name__ == "__main__":
    main()
