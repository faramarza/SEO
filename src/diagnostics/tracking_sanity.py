"""
TrackingSanityDiagnostics — Determine if a page's data is trustworthy.

Purpose: Answer "Can I trust this page's data?" before any capital allocation.

This module does NOT propose optimizations.
It only diagnoses data quality issues.

Six Failure Classes:
1. URL normalization mismatch
2. GA4 ↔ GSC volume divergence (channel-aligned)
3. Canonical ambiguity
4. Landing-page revenue attribution leakage
5. Internal competition / cannibalization
6. Homepage & category aggregation distortion
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset, AssetType


class FailureCode(str, Enum):
    """Failure codes for tracking sanity checks."""
    # Tier A - Blocking
    URL_NORMALIZATION_MISMATCH = "URL_NORMALIZATION_MISMATCH"
    GA4_ORGANIC_ZERO = "GA4_ORGANIC_ZERO"
    INVALID_CHANNEL_COMPARISON = "INVALID_CHANNEL_COMPARISON"
    CANONICAL_AMBIGUITY = "CANONICAL_AMBIGUITY"
    REVENUE_ATTRIBUTION_LEAKAGE = "REVENUE_ATTRIBUTION_LEAKAGE"

    # Tier B - Non-blocking (warnings)
    GA4_GSC_VOLUME_MISMATCH = "GA4_GSC_VOLUME_MISMATCH"
    INTERNAL_CANNIBALIZATION = "INTERNAL_CANNIBALIZATION"
    AGGREGATION_DISTORTION = "AGGREGATION_DISTORTION"


class Severity(str, Enum):
    """Severity levels for failures."""
    HIGH = "HIGH"      # Blocking - page ineligible for Governor actions
    MEDIUM = "MEDIUM"  # Warning - proceed with caution
    LOW = "LOW"        # Informational


@dataclass
class Failure:
    """A single diagnostic failure."""
    code: FailureCode
    severity: Severity
    evidence: dict[str, Any]
    interpretation: str
    recommended_fix: str


@dataclass
class PageDiagnostic:
    """Complete diagnostic result for a page."""
    url: str
    status: str  # PASS | FAIL | WARN
    blocking: bool
    failures: list[Failure] = field(default_factory=list)
    eligible_for_raip: bool = True
    special_classification: Optional[str] = None  # "homepage" | "category_hub" | None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "url": self.url,
            "status": self.status,
            "blocking": self.blocking,
            "eligible_for_raip": self.eligible_for_raip,
            "special_classification": self.special_classification,
            "failure_modes": [
                {
                    "code": f.code.value,
                    "severity": f.severity.value,
                    "evidence": f.evidence,
                    "interpretation": f.interpretation,
                    "recommended_fix": f.recommended_fix,
                }
                for f in self.failures
            ],
        }


class TrackingSanityDiagnostics:
    """
    Diagnose tracking sanity issues for pages.

    Two-tier logic:
    - Tier A (blocking): Channel alignment, URL normalization, canonical issues
    - Tier B (non-blocking): Volume ratio, cannibalization, aggregation

    Special handling for homepage and category pages.
    """

    # Ratio thresholds for GA4 organic sessions / GSC clicks
    RATIO_LOWER_BOUND = 0.7
    RATIO_UPPER_BOUND = 1.3

    # Pages with fewer clicks are exempt from ratio checks (noise)
    MIN_CLICKS_FOR_RATIO_CHECK = 10

    PLATFORM_FIXES = {
        "magento": {
            "channel_zero": (
                "1. Verify GA4 tag fires on this page (check Magento > Stores > Configuration > Sales > Google API)\n"
                "2. Check cookie consent extension (e.g. Amasty GDPR) — users declining = invisible in GA4\n"
                "3. Audit URL Rewrites (Marketing > URL Rewrites) for redirect chains losing referrer\n"
                "4. Check if Full Page Cache is serving stale pages without the GA4 tag"
            ),
            "volume_low": (
                "1. Check Magento cookie consent extension — consent rejection rate is the #1 cause of GA4 undercounting\n"
                "2. Audit URL Rewrites for 301/302 chains (Marketing > URL Rewrites)\n"
                "3. Check if layered navigation creates duplicate URLs without canonical tags\n"
                "4. Verify GA4 tag loads on all page types (product, category, CMS, checkout)"
            ),
            "revenue": (
                "1. Verify Magento GA4 purchase event fires with correct page_location on order confirmation\n"
                "2. Check if checkout redirect (PayPal, payment gateway return) loses attribution\n"
                "3. Audit Magento Enhanced Ecommerce dataLayer implementation\n"
                "4. Check if multi-step checkout loses the original landing page referrer"
            ),
        },
        "shopify": {
            "channel_zero": (
                "1. Verify GA4 tag in Online Store > Preferences or via Google channel app\n"
                "2. Check Shopify cookie consent banner — users declining = invisible in GA4\n"
                "3. Audit URL redirects (Settings > Navigation > URL Redirects) for chains\n"
                "4. Check if Shopify Storefront API / headless sections miss the GA4 tag"
            ),
            "volume_low": (
                "1. Check cookie consent banner rejection rate\n"
                "2. Audit URL redirects for referrer loss\n"
                "3. Verify GA4 fires on all page types including /collections/ and /pages/\n"
                "4. Check if Shopify Markets (multi-currency/region) duplicates sessions"
            ),
            "revenue": (
                "1. Verify purchase event fires on Shopify thank-you page with correct attribution\n"
                "2. Check if offsite payment (PayPal, Shop Pay) loses referrer on return\n"
                "3. Audit Shopify GA4 integration (native or custom pixel)\n"
                "4. Check order status page tracking for duplicate purchase events"
            ),
        },
        "wordpress": {
            "channel_zero": (
                "1. Verify GA4 plugin is active and configured (e.g. Site Kit, MonsterInsights, or manual gtag)\n"
                "2. Check cookie consent plugin (e.g. CookieYes, Complianz) — declining users are invisible\n"
                "3. Audit permalink settings and any redirect plugins (Redirection, Yoast) for chains\n"
                "4. Check if caching plugin (WP Super Cache, W3TC, LiteSpeed) serves stale pages without GA4"
            ),
            "volume_low": (
                "1. Check cookie consent plugin rejection rate\n"
                "2. Audit redirect plugins for referrer-losing chains\n"
                "3. Verify GA4 loads on all post types (posts, pages, custom post types)\n"
                "4. Check if AMP pages have separate GA4 tracking"
            ),
            "revenue": (
                "1. Verify purchase/conversion event fires correctly on thank-you/confirmation page\n"
                "2. Check if WooCommerce or form plugin sends correct attribution data\n"
                "3. Audit Enhanced Ecommerce dataLayer (if WooCommerce)\n"
                "4. Check if redirect after form submission loses landing page referrer"
            ),
        },
        "woocommerce": {
            "channel_zero": (
                "1. Verify GA4 integration plugin (WooCommerce Google Analytics, MonsterInsights, or GTM)\n"
                "2. Check cookie consent plugin — consent rejection = invisible in GA4\n"
                "3. Audit WooCommerce URL structure and permalink redirects\n"
                "4. Check if caching plugin serves pages without GA4 tag"
            ),
            "volume_low": (
                "1. Check cookie consent rejection rate\n"
                "2. Audit redirect chains in permalink structure and Yoast/Rank Math\n"
                "3. Verify GA4 fires on product, category, tag, and shop pages\n"
                "4. Check if variable products create untracked URL parameters"
            ),
            "revenue": (
                "1. Verify WooCommerce purchase event fires on order-received page\n"
                "2. Check if payment gateway redirects (PayPal, Stripe) lose attribution\n"
                "3. Audit WooCommerce Enhanced Ecommerce dataLayer\n"
                "4. Check if guest checkout vs account checkout affects attribution"
            ),
        },
        "spa": {
            "channel_zero": (
                "1. Verify GA4 fires a pageview on every route change (not just initial load)\n"
                "2. Check if client-side hydration delays or blocks the GA4 tag\n"
                "3. Audit history.pushState / popstate event handling for GA4 pageview triggers\n"
                "4. Check if server-side rendering (SSR) and client-side GA4 create duplicate or missing events"
            ),
            "volume_low": (
                "1. Check consent mode implementation in the SPA lifecycle\n"
                "2. Verify route-change pageview events fire correctly\n"
                "3. Check if pre-rendering / SSR pages send a pageview before hydration replaces it\n"
                "4. Audit for duplicate pageviews from both SSR and client-side"
            ),
            "revenue": (
                "1. Verify purchase event fires with correct page_location after client-side checkout\n"
                "2. Check if post-payment redirect back to SPA triggers correct attribution\n"
                "3. Audit dataLayer push timing relative to route changes\n"
                "4. Check if SPA navigation resets GA4 session attribution"
            ),
        },
    }

    # Path fragments that mark a page as utility/policy/navigation — these
    # rank for the brand term site-wide and must not be treated as content
    # that "cannibalizes" other pages.
    _UTILITY_PATH_MARKERS = (
        "about", "contact", "faq", "shipping", "return", "refund", "terms",
        "privacy", "policy", "price-match", "testimonial", "special",
        "shop-by-brand", "warranty", "track", "wishlist", "cart", "checkout",
        "login", "account", "sitemap", "search",
    )

    def __init__(
        self,
        base_url: str = "https://alphabet-trains.com",
        site_platform: str = "magento",
        brand_terms: Optional[list[str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.site_platform = site_platform
        # Brand terms (e.g. "alphabet trains") are excluded from
        # cannibalization analysis — everything ranks for the brand.
        self.brand_terms = [b.lower() for b in (brand_terms or [])]

    def _is_utility_page(self, url: str) -> bool:
        """True for policy/utility/nav pages that shouldn't be judged for
        cannibalization (they only overlap on the brand term)."""
        path = self.normalize_url(url)
        # The blog index and bare /blog listing are also aggregation pages
        if path in ("/blog", "/faqs", "/faq"):
            return True
        return any(marker in path for marker in self._UTILITY_PATH_MARKERS)

    def _is_brand_query(self, query: str) -> bool:
        """True if the query is a brand/navigational term."""
        q = query.lower()
        return any(b in q for b in self.brand_terms) if self.brand_terms else False

    def _get_platform_fix(self, fix_type: str) -> str:
        platform_fixes = self.PLATFORM_FIXES.get(self.site_platform, {})
        if fix_type in platform_fixes:
            return platform_fixes[fix_type]
        default = self.PLATFORM_FIXES.get("magento", {})
        return default.get(fix_type, f"1. Check GA4 tracking configuration for your platform ({self.site_platform})")

    def normalize_url(self, url: str) -> str:
        """
        Normalize URL for comparison.

        Removes:
        - Query parameters
        - Trailing slashes
        - Case differences
        - Magento blog alias (/blog/post/slug ≡ /blog/slug)

        The blog-alias collapse is critical: GSC attributes clicks to one
        form and GA4 attributes sessions to the other, so without collapsing
        them the GSC↔GA4 join produces false "GA4 organic zero" blockers on
        every duplicated blog post.
        """
        parsed = urlparse(url)
        path = parsed.path.lower().rstrip("/")
        if not path:
            path = "/"
        # Collapse the Magento blog alias so both URL forms map to one key
        path = path.replace("/blog/post/", "/blog/")
        return path

    def is_homepage(self, url: str) -> bool:
        """Check if URL is the homepage."""
        path = self.normalize_url(url)
        return path == "/" or path == ""

    def is_category_hub(self, asset: PageAsset) -> bool:
        """Check if page is a category hub (aggregation-biased)."""
        if asset.asset_type == AssetType.CATEGORY:
            return True

        # Also check URL patterns
        url_lower = asset.url.lower()
        category_patterns = [
            "/category/",
            "/collection/",
            "/shop/",
            "/products/",  # plural = likely category
            "-toys.html",  # e.g., montessori-toys.html
        ]
        return any(p in url_lower for p in category_patterns)

    def check_url_normalization(
        self,
        asset: PageAsset,
        ga4_paths: set[str],
    ) -> Optional[Failure]:
        """
        Check 3.1: URL Normalization.

        Fail if GSC URL cannot be matched to GA4 landing page due to:
        - Query string variants
        - Trailing slash differences
        - Case mismatches
        """
        gsc_path = self.normalize_url(asset.url)

        # Check if normalized path exists in GA4
        if gsc_path not in ga4_paths:
            # Check for common variants
            variants = [
                gsc_path + "/",
                gsc_path.rstrip("/"),
                gsc_path.upper(),
            ]
            found = any(v in ga4_paths for v in variants)

            if not found and asset.gsc.clicks_28d > self.MIN_CLICKS_FOR_RATIO_CHECK:
                return Failure(
                    code=FailureCode.URL_NORMALIZATION_MISMATCH,
                    severity=Severity.HIGH,
                    evidence={
                        "gsc_url": asset.url,
                        "normalized_path": gsc_path,
                        "ga4_paths_checked": list(ga4_paths)[:10],
                    },
                    interpretation="GSC page URL does not match any GA4 landing page",
                    recommended_fix=(
                        "1. Check canonical tag matches actual URL\n"
                        "2. Audit redirects (301/302)\n"
                        "3. Consider GA4 custom dimension for normalized landing page"
                    ),
                )

        return None

    def check_channel_alignment(
        self,
        asset: PageAsset,
        organic_sessions: int,
    ) -> Optional[Failure]:
        """
        Check 3.2 Tier A: Channel Alignment (blocking).

        Fail if:
        - GA4 organic sessions = 0 but GSC clicks > 0
        - Using total sessions instead of organic (invalid comparison)

        Homepage and category hubs are downgraded to Tier B because
        they aggregate traffic from many sources and the organic session
        count may be distorted by navigation patterns.
        """
        gsc_clicks = asset.gsc.clicks_28d

        if gsc_clicks > self.MIN_CLICKS_FOR_RATIO_CHECK and organic_sessions == 0:
            # Homepage / category hubs: downgrade to warning — their traffic
            # is aggregated and organic attribution is inherently noisy.
            is_special = self.is_homepage(asset.url) or self.is_category_hub(asset)
            severity = Severity.MEDIUM if is_special else Severity.HIGH

            return Failure(
                code=FailureCode.GA4_ORGANIC_ZERO,
                severity=severity,
                evidence={
                    "gsc_clicks_28d": gsc_clicks,
                    "ga4_organic_sessions_28d": organic_sessions,
                },
                interpretation=(
                    "GSC records clicks but GA4 shows zero organic sessions. "
                    "This indicates tracking loss or attribution failure."
                ),
                recommended_fix=self._get_platform_fix("channel_zero"),
            )

        return None

    # Higher threshold for Tier A volume mismatch — only flag as blocking
    # when both the ratio AND absolute click volume indicate a real problem.
    RATIO_TIER_A_UPPER = 2.0      # GA4 > 2× GSC clicks → may be Tier A
    MIN_CLICKS_FOR_TIER_A = 20    # Need ≥20 clicks for Tier A confidence

    def check_volume_ratio(
        self,
        asset: PageAsset,
        organic_sessions: int,
    ) -> Optional[Failure]:
        """
        Check 3.2 Tier B: Volume Ratio (non-blocking for special pages).

        Warn if GA4 organic sessions / GSC clicks is outside 0.7–1.3 band.

        Severity logic:
        - LOW ratio (GA4 < GSC): HIGH if ≥20 clicks, MEDIUM otherwise
        - HIGH ratio (GA4 > GSC): HIGH only if ratio > 2.0 AND ≥20 clicks.
          Blog posts, social shares, and email traffic commonly inflate
          GA4 sessions beyond GSC organic clicks.
        - Homepage / category hubs: always MEDIUM (expected distortion)
        """
        gsc_clicks = asset.gsc.clicks_28d

        # Skip ratio check if insufficient data
        if gsc_clicks < self.MIN_CLICKS_FOR_RATIO_CHECK:
            return None

        ratio = organic_sessions / gsc_clicks

        if ratio < self.RATIO_LOWER_BOUND or ratio > self.RATIO_UPPER_BOUND:
            is_special = self.is_homepage(asset.url) or self.is_category_hub(asset)

            if ratio < self.RATIO_LOWER_BOUND:
                # LOW ratio — GA4 tracking loss is always concerning
                if is_special:
                    severity = Severity.MEDIUM
                elif gsc_clicks >= self.MIN_CLICKS_FOR_TIER_A:
                    severity = Severity.HIGH
                else:
                    severity = Severity.MEDIUM
                interpretation = (
                    f"GA4 organic sessions ({organic_sessions}) significantly lower than "
                    f"GSC clicks ({gsc_clicks}). Attribution loss suspected."
                )
                fix = self._get_platform_fix("volume_low")
            else:
                # HIGH ratio — GA4 has MORE sessions than GSC organic clicks.
                # This is never data LOSS (it's inflation from direct/brand/
                # internal/social traffic, which GA4 counts but GSC does not),
                # so it must never block the Governor. Always Tier B.
                severity = Severity.MEDIUM
                interpretation = (
                    f"GA4 organic sessions ({organic_sessions}) higher than "
                    f"GSC clicks ({gsc_clicks}). Normal when a page also gets "
                    f"direct/brand/internal traffic — reduces confidence, not blocking."
                )
                fix = (
                    "1. Check GA4 channel grouping rules\n"
                    "2. Verify no internal traffic counted as organic\n"
                    "3. Check for session refresh inflation"
                )

            return Failure(
                code=FailureCode.GA4_GSC_VOLUME_MISMATCH,
                severity=severity,
                evidence={
                    "gsc_clicks_28d": gsc_clicks,
                    "ga4_organic_sessions_28d": organic_sessions,
                    "ratio": round(ratio, 2),
                    "expected_range": f"{self.RATIO_LOWER_BOUND}–{self.RATIO_UPPER_BOUND}",
                },
                interpretation=interpretation,
                recommended_fix=fix,
            )

        return None

    def _is_cms_alias_pattern(self, page_path: str, canon_path: str) -> bool:
        """Check if the canonical mismatch is a known CMS alias pattern.

        Common patterns where a URL is a known alias of its canonical and the
        canonical is correctly set (so GSC consolidates metrics properly):
          /blog/my-post              → /blog/post/my-post     (Magento blog, deeper)
          /cat/subcat/product.html   → /product.html          (Magento product, shallower)
          /news/my-post              → /news/article/my-post  (news CMS)

        The reliable signal is the TERMINAL SLUG: if the page and its
        canonical end in the same slug, they address the same resource and
        differ only in path depth/structure — an intentional alias, not
        ambiguity. Direction (deeper vs shallower) does not matter; a product
        reached via a category path canonicalizing to the clean product URL is
        just as valid as a blog alias.
        """
        page_slug = page_path.rstrip("/").rsplit("/", 1)[-1]
        canon_slug = canon_path.rstrip("/").rsplit("/", 1)[-1]

        # Same terminal slug, different path structure → known alias.
        page_parts = page_path.strip("/").split("/")
        canon_parts = canon_path.strip("/").split("/")
        if page_slug and page_slug == canon_slug and page_parts != canon_parts:
            return True

        return False

    def check_canonical_consistency(
        self,
        asset: PageAsset,
    ) -> Optional[Failure]:
        """
        Check 3.3: Canonical Consistency.

        Fail if page has canonical pointing elsewhere.
        Known CMS alias patterns (e.g. /blog/slug → /blog/post/slug)
        are downgraded to Tier B since the canonical is correctly set.
        """
        if asset.canonical_url and asset.canonical_url != asset.url:
            # Normalize both for comparison
            self_norm = self.normalize_url(asset.url)
            canon_norm = self.normalize_url(asset.canonical_url)

            if self_norm != canon_norm:
                is_alias = self._is_cms_alias_pattern(self_norm, canon_norm)
                severity = Severity.MEDIUM if is_alias else Severity.HIGH

                if is_alias:
                    interpretation = (
                        "Page is a known CMS alias with canonical correctly "
                        "pointing to the primary URL. GSC consolidates metrics "
                        "to the canonical target. Consider redirecting this "
                        "alias to the canonical URL."
                    )
                else:
                    interpretation = (
                        "Page declares a different canonical URL. "
                        "GSC will attribute metrics to canonical, causing data split."
                    )

                return Failure(
                    code=FailureCode.CANONICAL_AMBIGUITY,
                    severity=severity,
                    evidence={
                        "page_url": asset.url,
                        "canonical_url": asset.canonical_url,
                        "is_cms_alias": is_alias,
                    },
                    interpretation=interpretation,
                    recommended_fix=(
                        "1. Fix canonical to self-reference OR\n"
                        "2. Redirect this URL to canonical OR\n"
                        "3. Add noindex to this variant"
                    ),
                )

        return None

    def check_revenue_attribution(
        self,
        asset: PageAsset,
    ) -> Optional[Failure]:
        """
        Check 3.4: Revenue Attribution Leakage.

        Tier A (blocking): Revenue exists but ZERO sessions — impossible
        attribution, indicates a tracking misconfiguration.
        Tier B (warning): Revenue exists with very few sessions (1-4) —
        could be legitimate low-traffic purchases or mild leakage.
        """
        if asset.ga4.revenue_28d > 0 and asset.ga4.sessions_28d < 5:
            # Zero sessions with revenue = definite tracking problem
            # 1-4 sessions = possibly legitimate, just low traffic
            severity = (Severity.HIGH if asset.ga4.sessions_28d == 0
                        else Severity.MEDIUM)

            interpretation = (
                "Revenue attributed to this page but zero sessions. "
                "Checkout/cart pages may be stealing attribution credit."
            ) if asset.ga4.sessions_28d == 0 else (
                f"Revenue (${asset.ga4.revenue_28d:.2f}) attributed with only "
                f"{asset.ga4.sessions_28d} session(s). May be legitimate "
                "low-traffic purchases or mild attribution leakage."
            )

            return Failure(
                code=FailureCode.REVENUE_ATTRIBUTION_LEAKAGE,
                severity=severity,
                evidence={
                    "revenue_28d": asset.ga4.revenue_28d,
                    "sessions_28d": asset.ga4.sessions_28d,
                    "purchases_28d": asset.ga4.purchases_28d,
                },
                interpretation=interpretation,
                recommended_fix=self._get_platform_fix("revenue"),
            )

        return None

    def check_cannibalization(
        self,
        asset: PageAsset,
        all_assets: list[PageAsset],
    ) -> Optional[Failure]:
        """
        Check 3.5: Internal Cannibalization.

        Warn if multiple pages compete for the same queries.
        """
        if not asset.gsc.top_queries:
            return None

        # Utility/policy/nav pages only overlap on the brand term — skip them
        if self._is_utility_page(asset.url):
            return None

        # Non-brand queries only — everything ranks for the brand name, so
        # brand-term overlap is not cannibalization.
        asset_norm = self.normalize_url(asset.url)
        asset_queries = {
            q.query.lower() for q in asset.gsc.top_queries[:5]
            if not self._is_brand_query(q.query)
        }
        if len(asset_queries) < 2:
            return None

        competitors = []
        for other in all_assets:
            if other.url == asset.url:
                continue
            # Same page via a different alias (e.g. /blog/post/x vs /blog/x)
            if self.normalize_url(other.url) == asset_norm:
                continue
            # Skip media files and utility pages as competitors
            other_ext = Path(self.normalize_url(other.url)).suffix.lower()
            if other_ext in self._MEDIA_EXTS:
                continue
            if self._is_utility_page(other.url):
                continue
            if not other.gsc.top_queries:
                continue

            other_queries = {
                q.query.lower() for q in other.gsc.top_queries[:5]
                if not self._is_brand_query(q.query)
            }
            overlap = asset_queries & other_queries

            if len(overlap) >= 2:  # At least 2 shared non-brand queries
                competitors.append({
                    "url": other.url,
                    "shared_queries": list(overlap),
                })

        if competitors:
            return Failure(
                code=FailureCode.INTERNAL_CANNIBALIZATION,
                severity=Severity.MEDIUM,
                evidence={
                    "asset_url": asset.url,
                    "asset_queries": list(asset_queries),
                    "competitors": competitors[:3],  # Top 3
                },
                interpretation=(
                    f"This page competes with {len(competitors)} other page(s) "
                    "for the same search queries."
                ),
                recommended_fix=(
                    "1. Merge intent into single authoritative page\n"
                    "2. Differentiate content to target different intents\n"
                    "3. Use internal linking to signal primary page\n"
                    "4. Consider noindex on secondary variants"
                ),
            )

        return None

    def check_aggregation_distortion(
        self,
        asset: PageAsset,
    ) -> Optional[Failure]:
        """
        Check 3.6: Homepage & Category Aggregation Distortion.

        Warn (never fail) for pages that aggregate traffic/revenue.
        """
        is_homepage = self.is_homepage(asset.url)
        is_category = self.is_category_hub(asset)

        if is_homepage or is_category:
            page_type = "homepage" if is_homepage else "category hub"
            return Failure(
                code=FailureCode.AGGREGATION_DISTORTION,
                severity=Severity.LOW,  # Never blocking
                evidence={
                    "page_type": page_type,
                    "url": asset.url,
                    "sessions_28d": asset.ga4.sessions_28d,
                    "revenue_28d": asset.ga4.revenue_28d,
                },
                interpretation=(
                    f"This {page_type} aggregates traffic from multiple sources. "
                    "Metrics may reflect navigation patterns, not search intent."
                ),
                recommended_fix=(
                    "1. Evaluate for defensive stability only, not optimization\n"
                    "2. Decompose 'assist' vs 'entry' metrics if possible\n"
                    "3. Exclude from RAIP actions unless manually approved"
                ),
            )

        return None

    def diagnose_page(
        self,
        asset: PageAsset,
        all_assets: list[PageAsset],
        ga4_paths: set[str],
        organic_sessions_map: dict[str, int],
    ) -> PageDiagnostic:
        """
        Run all diagnostic checks on a single page.

        Returns PageDiagnostic with status, failures, and eligibility.
        """
        failures = []

        # Get organic sessions for this page
        path = self.normalize_url(asset.url)
        organic_sessions = organic_sessions_map.get(path, 0)

        # === Tier A: Blocking Checks ===

        # 3.1 URL Normalization
        url_check = self.check_url_normalization(asset, ga4_paths)
        if url_check:
            failures.append(url_check)

        # 3.2a Channel Alignment (blocking)
        channel_check = self.check_channel_alignment(asset, organic_sessions)
        if channel_check:
            failures.append(channel_check)

        # 3.3 Canonical Consistency
        canonical_check = self.check_canonical_consistency(asset)
        if canonical_check:
            failures.append(canonical_check)

        # 3.4 Revenue Attribution Leakage
        revenue_check = self.check_revenue_attribution(asset)
        if revenue_check:
            failures.append(revenue_check)

        # === Tier B: Non-Blocking Checks ===

        # 3.2b Volume Ratio (non-blocking for special pages)
        ratio_check = self.check_volume_ratio(asset, organic_sessions)
        if ratio_check:
            failures.append(ratio_check)

        # 3.5 Cannibalization
        cannibal_check = self.check_cannibalization(asset, all_assets)
        if cannibal_check:
            failures.append(cannibal_check)

        # 3.6 Aggregation Distortion
        aggregation_check = self.check_aggregation_distortion(asset)
        if aggregation_check:
            failures.append(aggregation_check)

        # === Determine Status ===

        high_severity = [f for f in failures if f.severity == Severity.HIGH]
        medium_severity = [f for f in failures if f.severity == Severity.MEDIUM]

        blocking = len(high_severity) > 0

        if high_severity:
            status = "FAIL"
        elif medium_severity:
            status = "WARN"
        else:
            status = "PASS"

        # === Special Classification ===

        special = None
        if self.is_homepage(asset.url):
            special = "homepage"
        elif self.is_category_hub(asset):
            special = "category_hub"

        # Homepage and category hubs are never eligible for RAIP optimization
        eligible = not blocking and special is None

        return PageDiagnostic(
            url=asset.url,
            status=status,
            blocking=blocking,
            failures=failures,
            eligible_for_raip=eligible,
            special_classification=special,
        )

    # Non-page file extensions — skip these in diagnostics entirely.
    _MEDIA_EXTS = {
        ".jpeg", ".jpg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
        ".pdf", ".css", ".js", ".woff", ".woff2", ".ttf", ".eot",
        ".mp4", ".webm", ".mp3", ".ogg", ".zip", ".gz",
    }

    # Utility path segments — pages with these in the URL path are not SEO
    # targets and should be excluded from diagnostics entirely.
    _UTILITY_PATH_SEGMENTS = {
        "/checkout", "/cart", "/customer/account", "/catalogsearch",
        "/wishlist", "/review/product", "/sendfriend",
    }

    def diagnose_all(
        self,
        assets: list[PageAsset],
        organic_sessions_map: dict[str, int],
    ) -> list[PageDiagnostic]:
        """
        Run diagnostics on all pages.

        Args:
            assets: List of PageAsset objects
            organic_sessions_map: Dict mapping normalized path -> organic sessions

        Returns list of PageDiagnostic objects.
        """
        # Build set of GA4 paths for URL matching
        ga4_paths = set(organic_sessions_map.keys())

        results = []
        for asset in assets:
            path = self.normalize_url(asset.url)

            # Skip media/non-page resources — they produce false cannibalization flags.
            ext = Path(path).suffix.lower()
            if ext in self._MEDIA_EXTS:
                continue

            # Skip utility pages (checkout, cart, etc.) — not SEO targets.
            if any(seg in path for seg in self._UTILITY_PATH_SEGMENTS):
                continue

            diag = self.diagnose_page(
                asset=asset,
                all_assets=assets,
                ga4_paths=ga4_paths,
                organic_sessions_map=organic_sessions_map,
            )
            results.append(diag)

        return results

    def summary(self, diagnostics: list[PageDiagnostic]) -> dict:
        """Generate summary statistics from diagnostics."""
        total = len(diagnostics)
        passed = sum(1 for d in diagnostics if d.status == "PASS")
        warned = sum(1 for d in diagnostics if d.status == "WARN")
        failed = sum(1 for d in diagnostics if d.status == "FAIL")
        eligible = sum(1 for d in diagnostics if d.eligible_for_raip)

        # Count by failure code
        failure_counts = {}
        for diag in diagnostics:
            for f in diag.failures:
                code = f.code.value
                failure_counts[code] = failure_counts.get(code, 0) + 1

        return {
            "total_pages": total,
            "passed": passed,
            "warned": warned,
            "failed": failed,
            "eligible_for_raip": eligible,
            "pass_rate": f"{passed/total*100:.1f}%" if total > 0 else "N/A",
            "eligibility_rate": f"{eligible/total*100:.1f}%" if total > 0 else "N/A",
            "failure_counts": failure_counts,
        }
