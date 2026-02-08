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

    def __init__(self, base_url: str = "https://alphabet-trains.com"):
        self.base_url = base_url.rstrip("/")

    def normalize_url(self, url: str) -> str:
        """
        Normalize URL for comparison.

        Removes:
        - Query parameters
        - Trailing slashes
        - Case differences
        """
        parsed = urlparse(url)
        path = parsed.path.lower().rstrip("/")
        if not path:
            path = "/"
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
        """
        gsc_clicks = asset.gsc.clicks_28d

        if gsc_clicks > self.MIN_CLICKS_FOR_RATIO_CHECK and organic_sessions == 0:
            return Failure(
                code=FailureCode.GA4_ORGANIC_ZERO,
                severity=Severity.HIGH,
                evidence={
                    "gsc_clicks_28d": gsc_clicks,
                    "ga4_organic_sessions_28d": organic_sessions,
                },
                interpretation=(
                    "GSC records clicks but GA4 shows zero organic sessions. "
                    "This indicates tracking loss or attribution failure."
                ),
                recommended_fix=(
                    "1. Verify GA4 tracking fires on this page\n"
                    "2. Check consent mode / cookie blocking\n"
                    "3. Audit redirects that may lose referrer\n"
                    "4. Check SPA routing issues"
                ),
            )

        return None

    def check_volume_ratio(
        self,
        asset: PageAsset,
        organic_sessions: int,
    ) -> Optional[Failure]:
        """
        Check 3.2 Tier B: Volume Ratio (non-blocking for special pages).

        Warn if GA4 organic sessions / GSC clicks is outside 0.7–1.3 band.
        """
        gsc_clicks = asset.gsc.clicks_28d

        # Skip ratio check if insufficient data
        if gsc_clicks < self.MIN_CLICKS_FOR_RATIO_CHECK:
            return None

        ratio = organic_sessions / gsc_clicks

        if ratio < self.RATIO_LOWER_BOUND or ratio > self.RATIO_UPPER_BOUND:
            # Determine severity based on page type
            is_special = self.is_homepage(asset.url) or self.is_category_hub(asset)
            severity = Severity.MEDIUM if is_special else Severity.HIGH

            if ratio < self.RATIO_LOWER_BOUND:
                interpretation = (
                    f"GA4 organic sessions ({organic_sessions}) significantly lower than "
                    f"GSC clicks ({gsc_clicks}). Attribution loss suspected."
                )
                fix = (
                    "1. Check GA4 consent mode implementation\n"
                    "2. Audit redirects for referrer loss\n"
                    "3. Verify SPA hydration doesn't block tracking\n"
                    "4. Check for duplicate page tracking"
                )
            else:
                interpretation = (
                    f"GA4 organic sessions ({organic_sessions}) higher than "
                    f"GSC clicks ({gsc_clicks}). Possible misattribution."
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

    def check_canonical_consistency(
        self,
        asset: PageAsset,
    ) -> Optional[Failure]:
        """
        Check 3.3: Canonical Consistency.

        Fail if page has canonical pointing elsewhere.
        """
        if asset.canonical_url and asset.canonical_url != asset.url:
            # Normalize both for comparison
            self_norm = self.normalize_url(asset.url)
            canon_norm = self.normalize_url(asset.canonical_url)

            if self_norm != canon_norm:
                return Failure(
                    code=FailureCode.CANONICAL_AMBIGUITY,
                    severity=Severity.HIGH,
                    evidence={
                        "page_url": asset.url,
                        "canonical_url": asset.canonical_url,
                    },
                    interpretation=(
                        "Page declares a different canonical URL. "
                        "GSC will attribute metrics to canonical, causing data split."
                    ),
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

        Fail if revenue exists but sessions ≈ 0 (attribution mismatch).
        """
        if asset.ga4.revenue_28d > 0 and asset.ga4.sessions_28d < 5:
            return Failure(
                code=FailureCode.REVENUE_ATTRIBUTION_LEAKAGE,
                severity=Severity.HIGH,
                evidence={
                    "revenue_28d": asset.ga4.revenue_28d,
                    "sessions_28d": asset.ga4.sessions_28d,
                    "purchases_28d": asset.ga4.purchases_28d,
                },
                interpretation=(
                    "Revenue attributed to this page but almost no sessions. "
                    "Checkout/cart pages may be stealing attribution credit."
                ),
                recommended_fix=(
                    "1. Verify purchase event includes correct page_location\n"
                    "2. Check enhanced ecommerce setup\n"
                    "3. Audit attribution model (first-click vs last-click)"
                ),
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

        asset_queries = {q.query.lower() for q in asset.gsc.top_queries[:5]}

        competitors = []
        for other in all_assets:
            if other.url == asset.url:
                continue
            # Skip media files as competitors
            other_ext = Path(self.normalize_url(other.url)).suffix.lower()
            if other_ext in self._MEDIA_EXTS:
                continue
            if not other.gsc.top_queries:
                continue

            other_queries = {q.query.lower() for q in other.gsc.top_queries[:5]}
            overlap = asset_queries & other_queries

            if len(overlap) >= 2:  # At least 2 shared queries
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
            # Skip media/non-page resources — they produce false cannibalization flags.
            path = self.normalize_url(asset.url)
            ext = Path(path).suffix.lower()
            if ext in self._MEDIA_EXTS:
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
