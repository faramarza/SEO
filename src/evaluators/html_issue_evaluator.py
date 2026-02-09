"""
HTML Structural Issue Evaluator

Detects on-page HTML patterns that block link equity, crawl efficiency,
or above-fold engagement:

1. Span/div CTAs that should be <a> tags (JS-only, zero link equity)
2. Links wrapping media (video/img) with no anchor text (crawl waste)
3. Missing above-fold semantic links (no clickable path before scroll)

All three are fully reversible, low-risk HTML fixes.
"""

import re
from dataclasses import dataclass, field

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models.page_asset import PageAsset


@dataclass
class HTMLIssue:
    """A single HTML structural issue."""
    issue_type: str       # "span_cta", "empty_anchor_media_wrap", "no_above_fold_link"
    severity: str         # "high", "medium", "low"
    description: str
    affected_snippet: str  # Shortened HTML for reference
    recommended_fix: str


@dataclass
class HTMLIssueResult:
    """Result of HTML structural evaluation."""
    url: str
    has_issues: bool
    issues: list[HTMLIssue]
    implementation_steps: list[str]
    expected_ctr_lift: float    # Estimated CTR improvement fraction
    confidence: float
    risk_level: str             # Always "low" for HTML fixes
    recommended_action: str     # "HTML_STRUCTURAL_FIX" or "NO_ACTION"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

# Matches <span ...> or <div ...> containing CTA-like text, capturing the tag
_SPAN_DIV_CTA_RE = re.compile(
    r'<(span|div)\b[^>]*class="[^"]*(?:cta|button|btn)[^"]*"[^>]*>'
    r'(.*?)'
    r'</\1>',
    re.IGNORECASE | re.DOTALL,
)

# Matches <a ...> wrapping only a <video> or <img> with no visible text
_MEDIA_WRAP_RE = re.compile(
    r'<a\b([^>]*)>\s*<(video|img)\b[^>]*/?>.*?</a>',
    re.IGNORECASE | re.DOTALL,
)

# Matches any <a ...> tag (used for above-fold check)
_ANY_ANCHOR_RE = re.compile(r'<a\b[^>]*>', re.IGNORECASE)


class HTMLIssueEvaluator:
    """
    Detects HTML structural issues that leak link equity or block engagement.

    Operates on the raw HTML stored in PageAsset.above_fold_html and the
    internal_outlinks list.  Produces an HTMLIssueResult with concrete
    implementation steps.
    """

    def evaluate(self, asset: PageAsset) -> HTMLIssueResult:
        issues: list[HTMLIssue] = []
        html = asset.above_fold_html or ""

        # --- 1. Span / div CTAs masquerading as links ----------------------
        issues.extend(self._detect_span_ctas(html))

        # --- 2. Media-wrapping <a> tags with no anchor text ----------------
        issues.extend(self._detect_empty_media_links(html, asset))

        # --- 3. No semantic <a> link above the fold ------------------------
        issues.extend(self._detect_missing_above_fold_link(html, asset))

        # Build implementation steps
        steps = self._build_steps(issues)

        # Estimate CTR lift: high-severity issues each worth ~1-2 pp,
        # medium ~0.5 pp.  Cap at 5 pp total.
        lift = 0.0
        for iss in issues:
            lift += 0.015 if iss.severity == "high" else 0.005
        lift = min(lift, 0.05)

        return HTMLIssueResult(
            url=asset.url,
            has_issues=len(issues) > 0,
            issues=issues,
            implementation_steps=steps,
            expected_ctr_lift=round(lift, 4),
            confidence=0.85 if issues else 1.0,
            risk_level="low",
            recommended_action="HTML_STRUCTURAL_FIX" if issues else "NO_ACTION",
        )

    # ------------------------------------------------------------------
    # Detectors
    # ------------------------------------------------------------------

    def _detect_span_ctas(self, html: str) -> list[HTMLIssue]:
        """Find <span>/<div> elements styled as buttons but not <a> tags."""
        found: list[HTMLIssue] = []
        for match in _SPAN_DIV_CTA_RE.finditer(html):
            tag = match.group(1)
            inner_text = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            if not inner_text:
                continue
            snippet = match.group(0)[:120]

            # Check for id/class misspellings as a bonus detail
            misspelling_note = ""
            id_match = re.search(r'id="([^"]*)"', match.group(0))
            if id_match:
                id_val = id_match.group(1)
                if "montesorri" in id_val.lower():
                    misspelling_note = (
                        f' Note: id="{id_val}" contains misspelling '
                        f'(should be "montessori").'
                    )

            found.append(HTMLIssue(
                issue_type="span_cta",
                severity="high",
                description=(
                    f'"{inner_text}" is a <{tag}>, not an <a> tag. '
                    f"Passes zero link equity and relies entirely on JavaScript."
                    f"{misspelling_note}"
                ),
                affected_snippet=snippet,
                recommended_fix=(
                    f"Convert <{tag}> to <a href=\"[destination]\"> with the same "
                    f"styling. Fix id misspelling if present."
                ),
            ))
        return found

    def _detect_empty_media_links(
        self, html: str, asset: PageAsset
    ) -> list[HTMLIssue]:
        """Find <a> tags wrapping video/img with no visible anchor text."""
        found: list[HTMLIssue] = []
        for match in _MEDIA_WRAP_RE.finditer(html):
            attrs = match.group(1)
            media_type = match.group(2)

            # Extract href for deduplication note
            href_match = re.search(r'href="([^"]*)"', attrs)
            href = href_match.group(1) if href_match else "unknown"

            # Check if there is an aria-label (acceptable alternative)
            if re.search(r'aria-label="[^"]+"', attrs):
                continue

            snippet = match.group(0)[:120]
            found.append(HTMLIssue(
                issue_type="empty_anchor_media_wrap",
                severity="medium",
                description=(
                    f"<a> wrapping <{media_type}> to {href} has no anchor text "
                    f"or aria-label. Search engines cannot parse link intent. "
                    f"Duplicate of a text link below wastes crawl budget."
                ),
                affected_snippet=snippet,
                recommended_fix=(
                    f'Add aria-label="[descriptive text]" to the <a> tag, or '
                    f"add a visually-hidden <span> with anchor text inside the link."
                ),
            ))
        return found

    def _detect_missing_above_fold_link(
        self, html: str, asset: PageAsset
    ) -> list[HTMLIssue]:
        """Flag pages where above-fold HTML has no semantic <a> link."""
        if not html:
            return []

        # If any real <a> tag exists above the fold, pass
        if _ANY_ANCHOR_RE.search(html):
            return []

        # Only flag category/product pages — blogs may legitimately lack early links
        if asset.asset_type.value not in ("category", "product"):
            return []

        return [HTMLIssue(
            issue_type="no_above_fold_link",
            severity="medium",
            description=(
                "No semantic <a> link between the H1 and the first content section. "
                "Users landing from search see no clickable path until they scroll. "
                "Hero CTA may be a JS-only element (span/div)."
            ),
            affected_snippet="(above-fold region — no <a> tags found)",
            recommended_fix=(
                "Add at least one <a href=\"...\"> link in the hero or intro "
                "section — e.g. convert the primary CTA to a real anchor tag."
            ),
        )]

    # ------------------------------------------------------------------
    # Step builder
    # ------------------------------------------------------------------

    def _build_steps(self, issues: list[HTMLIssue]) -> list[str]:
        steps: list[str] = []
        step_num = 1

        span_issues = [i for i in issues if i.issue_type == "span_cta"]
        media_issues = [i for i in issues if i.issue_type == "empty_anchor_media_wrap"]
        fold_issues = [i for i in issues if i.issue_type == "no_above_fold_link"]

        for iss in span_issues:
            cta_text = re.sub(r'<[^>]+>', '', iss.affected_snippet).strip()[:40]
            steps.append(
                f"{step_num}. Convert span/div CTA \"{cta_text}\" to an <a> tag "
                f"with proper href and fix any id misspellings"
            )
            step_num += 1

        if media_issues:
            count = len(media_issues)
            steps.append(
                f"{step_num}. Add aria-label or visually-hidden text to "
                f"{count} media-wrapping <a> tag{'s' if count > 1 else ''} "
                f"(video/img links with no anchor text)"
            )
            step_num += 1

        for iss in fold_issues:
            steps.append(
                f"{step_num}. Add a semantic <a> link in the above-fold / hero "
                f"section so users have an immediate clickable path"
            )
            step_num += 1

        return steps
