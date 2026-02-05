"""
Canonical/Indexability Fix Evaluator

Identifies and recommends fixes for:
- Canonical mismatches
- Indexability issues (noindex, blocked by robots)
- Duplicate content
- URL parameter issues
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, parse_qs

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset


class CanonicalIssueType:
    """Types of canonical/indexability issues."""
    SELF_CANONICAL_MISSING = "self_canonical_missing"
    CANONICAL_MISMATCH = "canonical_mismatch"
    CANONICAL_TO_REDIRECT = "canonical_to_redirect"
    NOINDEX_WITH_TRAFFIC = "noindex_with_traffic"
    BLOCKED_WITH_TRAFFIC = "blocked_with_traffic"
    PARAM_URL_INDEXED = "param_url_indexed"
    DUPLICATE_CONTENT = "duplicate_content"
    HTTP_HTTPS_MISMATCH = "http_https_mismatch"
    WWW_NONWWW_MISMATCH = "www_nonwww_mismatch"
    TRAILING_SLASH_INCONSISTENT = "trailing_slash_inconsistent"


@dataclass
class CanonicalIssue:
    """A single canonical/indexability issue."""
    issue_type: str
    severity: str  # "critical", "high", "medium", "low"
    description: str
    affected_urls: list[str]
    recommended_fix: str
    estimated_impact: str


@dataclass
class CanonicalFixResult:
    """Result of canonical/indexability evaluation."""
    url: str
    has_issues: bool
    issues: list[CanonicalIssue]
    total_traffic_at_risk: int  # clicks that could be lost
    priority_score: float
    recommended_action: str
    implementation_steps: list[str]
    rollback_plan: str
    confidence: float


class CanonicalEvaluator:
    """
    Evaluates canonical and indexability issues.

    Focuses on issues that:
    1. Cause traffic/ranking loss
    2. Can be fixed with clear technical changes
    3. Are slowly reversible (requires careful implementation)
    """

    def __init__(self, base_url: str = "https://alphabet-trains.com"):
        self.base_url = base_url.rstrip("/")
        self.preferred_protocol = "https"
        self.preferred_www = False  # No www

    def normalize_url(self, url: str) -> str:
        """Normalize URL for comparison."""
        parsed = urlparse(url)
        path = parsed.path.lower().rstrip("/")
        if not path:
            path = "/"
        return f"{self.base_url}{path}"

    def check_canonical_self_reference(
        self,
        asset: PageAsset,
    ) -> Optional[CanonicalIssue]:
        """Check if page has proper self-referencing canonical."""
        # Only check if we have actual crawl data - otherwise we don't know
        if not asset.has_crawl_data:
            return None

        if not asset.canonical_url:
            if asset.gsc.clicks_28d > 10:  # Only flag if page has traffic
                return CanonicalIssue(
                    issue_type=CanonicalIssueType.SELF_CANONICAL_MISSING,
                    severity="medium",
                    description="Page lacks self-referencing canonical tag",
                    affected_urls=[asset.url],
                    recommended_fix=f'Add <link rel="canonical" href="{asset.url}" />',
                    estimated_impact="May cause duplicate content issues",
                )
        return None

    def check_canonical_mismatch(
        self,
        asset: PageAsset,
    ) -> Optional[CanonicalIssue]:
        """Check if canonical points to different URL."""
        # Only check if we have actual crawl data
        if not asset.has_crawl_data:
            return None

        if asset.canonical_url and asset.canonical_url != asset.url:
            # Normalize both
            norm_self = self.normalize_url(asset.url)
            norm_canonical = self.normalize_url(asset.canonical_url)

            if norm_self != norm_canonical:
                severity = "high" if asset.gsc.clicks_28d > 50 else "medium"
                return CanonicalIssue(
                    issue_type=CanonicalIssueType.CANONICAL_MISMATCH,
                    severity=severity,
                    description=f"Canonical points to different URL: {asset.canonical_url}",
                    affected_urls=[asset.url, asset.canonical_url],
                    recommended_fix="Either fix canonical to self-reference OR redirect this URL to canonical",
                    estimated_impact=f"GSC attributes metrics to canonical URL, {asset.gsc.clicks_28d} clicks may be misattributed",
                )
        return None

    def check_param_url_indexed(
        self,
        asset: PageAsset,
    ) -> Optional[CanonicalIssue]:
        """Check if URL with parameters is being indexed."""
        parsed = urlparse(asset.url)
        if parsed.query:
            params = parse_qs(parsed.query)
            # Common tracking params that shouldn't be indexed
            tracking_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'gclid', 'fbclid'}
            has_tracking = bool(set(params.keys()) & tracking_params)

            if has_tracking and asset.gsc.impressions_28d > 0:
                return CanonicalIssue(
                    issue_type=CanonicalIssueType.PARAM_URL_INDEXED,
                    severity="high",
                    description="URL with tracking parameters is indexed in Google",
                    affected_urls=[asset.url],
                    recommended_fix="Add canonical to clean URL, add to robots.txt, or use noindex",
                    estimated_impact="Causes duplicate content and dilutes page authority",
                )
        return None

    def check_protocol_mismatch(
        self,
        asset: PageAsset,
    ) -> Optional[CanonicalIssue]:
        """Check for HTTP/HTTPS mismatches."""
        if asset.url.startswith("http://") and asset.gsc.clicks_28d > 0:
            return CanonicalIssue(
                issue_type=CanonicalIssueType.HTTP_HTTPS_MISMATCH,
                severity="critical",
                description="HTTP URL is indexed instead of HTTPS",
                affected_urls=[asset.url],
                recommended_fix="Implement 301 redirect from HTTP to HTTPS",
                estimated_impact="Security warning in browsers, ranking penalty",
            )
        return None

    def check_noindex_with_traffic(
        self,
        asset: PageAsset,
    ) -> Optional[CanonicalIssue]:
        """Check if noindexed page still receives organic traffic."""
        # Only check if we have actual crawl data
        if not asset.has_crawl_data:
            return None

        if not asset.indexable and asset.gsc.clicks_28d > 10:
            return CanonicalIssue(
                issue_type=CanonicalIssueType.NOINDEX_WITH_TRAFFIC,
                severity="high",
                description="Page is noindexed but still receiving organic clicks",
                affected_urls=[asset.url],
                recommended_fix="Remove noindex if page should be indexed, or investigate why it's still appearing",
                estimated_impact=f"Potential loss of {asset.gsc.clicks_28d} clicks when deindexed",
            )
        return None

    def evaluate(
        self,
        asset: PageAsset,
        all_assets: Optional[list[PageAsset]] = None,
    ) -> CanonicalFixResult:
        """
        Full canonical/indexability evaluation.

        Args:
            asset: Page to evaluate
            all_assets: All pages (for duplicate detection)
        """
        issues = []

        # Run all checks
        checks = [
            self.check_canonical_self_reference(asset),
            self.check_canonical_mismatch(asset),
            self.check_param_url_indexed(asset),
            self.check_protocol_mismatch(asset),
            self.check_noindex_with_traffic(asset),
        ]

        for issue in checks:
            if issue:
                issues.append(issue)

        # Calculate priority
        traffic_at_risk = asset.gsc.clicks_28d
        severity_weights = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        severity_score = sum(severity_weights.get(i.severity, 1) for i in issues)
        priority_score = (traffic_at_risk * severity_score) / 100 if issues else 0

        # Determine recommended action
        if not issues:
            recommended_action = "NO_ACTION"
            implementation_steps = []
        else:
            critical_issues = [i for i in issues if i.severity == "critical"]
            if critical_issues:
                recommended_action = "CANONICAL_FIX"
                implementation_steps = [
                    "Backup current canonical configuration",
                    critical_issues[0].recommended_fix,
                    "Submit URL to GSC for reindexing",
                    "Monitor for 14 days",
                ]
            else:
                recommended_action = "CANONICAL_FIX"
                implementation_steps = [
                    "Review all identified issues",
                    "Prioritize by severity",
                    "Implement fixes in staging first",
                    "Deploy and monitor",
                ]

        # Confidence based on data clarity
        confidence = 0.80 if issues else 1.0
        for issue in issues:
            if issue.severity == "critical":
                confidence = min(confidence, 0.90)  # High confidence for critical
            elif issue.severity == "medium":
                confidence = min(confidence, 0.70)

        return CanonicalFixResult(
            url=asset.url,
            has_issues=len(issues) > 0,
            issues=issues,
            total_traffic_at_risk=traffic_at_risk,
            priority_score=priority_score,
            recommended_action=recommended_action,
            implementation_steps=implementation_steps,
            rollback_plan="Revert canonical tags to previous state; resubmit to GSC",
            confidence=confidence,
        )
