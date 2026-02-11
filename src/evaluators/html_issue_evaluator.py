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

# Matches any <a ...>...</a> block (for media-wrap analysis)
_ANCHOR_BLOCK_RE = re.compile(
    r'<a\b([^>]*)>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# Matches <video> or <img> tags inside anchor content
_MEDIA_INSIDE_RE = re.compile(r'<(video|img)\b', re.IGNORECASE)

# Strip all HTML tags to get visible text
_STRIP_TAGS_RE = re.compile(r'<[^>]+>', re.DOTALL)

# Matches any <a ...> tag (used for above-fold check)
_ANY_ANCHOR_RE = re.compile(r'<a\b[^>]*>', re.IGNORECASE)


class HTMLIssueEvaluator:
    """
    Detects HTML structural issues that leak link equity or block engagement.

    Operates on PageAsset.body_html (full page) when available, falling back
    to above_fold_html.  The above-fold-only check (#3) always uses
    above_fold_html regardless.  Produces an HTMLIssueResult with concrete
    implementation steps.
    """

    def evaluate(self, asset: PageAsset) -> HTMLIssueResult:
        issues: list[HTMLIssue] = []
        # Use full body HTML for detections that need whole-page coverage;
        # fall back to above_fold_html when body_html is not available.
        full_html = asset.body_html or asset.above_fold_html or ""
        above_fold = asset.above_fold_html or ""

        # --- 1. Span / div CTAs masquerading as links ----------------------
        issues.extend(self._detect_span_ctas(full_html))

        # --- 2. Media-wrapping <a> tags with no anchor text ----------------
        issues.extend(self._detect_empty_media_links(full_html, asset))

        # --- 3. No semantic <a> link above the fold ------------------------
        #     This check intentionally uses above_fold_html only.
        issues.extend(self._detect_missing_above_fold_link(above_fold, asset))

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

    # Auth/login buttons that should NOT be flagged as CTA issues
    _AUTH_PATTERNS = re.compile(
        r'sign\s*in|log\s*in|log\s*out|sign\s*up|register|forgot\s*password',
        re.IGNORECASE,
    )

    # E-commerce form controls that are correctly non-link elements
    _FORM_CONTROL_PATTERNS = re.compile(
        r'^(qty|quantity|\d+|add\s*to\s*cart|add\s*to\s*wish\s*list|'
        r'add\s*to\s*compare|remove|update|delete|clear|'
        r'increase|decrease|minus|plus|\+|\-|×)$',
        re.IGNORECASE,
    )

    def _detect_span_ctas(self, html: str) -> list[HTMLIssue]:
        """Find <span>/<div> elements styled as buttons but not <a> tags."""
        found: list[HTMLIssue] = []
        for match in _SPAN_DIV_CTA_RE.finditer(html):
            tag = match.group(1)
            inner_text = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            if not inner_text:
                continue

            # Skip auth/login buttons — these are intentionally non-link elements
            if self._AUTH_PATTERNS.search(inner_text):
                continue

            # Skip e-commerce form controls (Qty selectors, add-to-cart buttons,
            # wishlist buttons) — these are correctly non-link elements
            if self._FORM_CONTROL_PATTERNS.match(inner_text):
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
        """Find <a> tags wrapping video/img with no visible anchor text.

        Handles arbitrarily nested wrappers (div, span, etc.) between
        the <a> tag and the media element.
        """
        found: list[HTMLIssue] = []
        for match in _ANCHOR_BLOCK_RE.finditer(html):
            attrs = match.group(1)
            inner = match.group(2)

            # Only care about anchors that contain <video> or <img>
            media_match = _MEDIA_INSIDE_RE.search(inner)
            if not media_match:
                continue
            media_type = media_match.group(1)

            # Strip all HTML tags — is there any visible text left?
            visible_text = _STRIP_TAGS_RE.sub("", inner).strip()
            if visible_text:
                continue  # Has real anchor text — not empty

            # Check if there is an aria-label (acceptable alternative)
            if re.search(r'aria-label="[^"]+"', attrs):
                continue

            # Check if the media element has alt text — Google uses img alt
            # as anchor text when <a> wraps <img>. This is NOT an SEO defect.
            if re.search(r'alt="[^"]+"', inner):
                continue

            # Extract href
            href_match = re.search(r'href="([^"]*)"', attrs)
            href = href_match.group(1) if href_match else "unknown"

            # Skip homepage logo links — standard pattern, not an SEO defect
            if href in ("/", "") or href.rstrip("/") == asset.url.rstrip("/"):
                continue
            # Also skip if href is just the root domain
            from urllib.parse import urlparse
            _parsed = urlparse(href)
            if _parsed.path in ("/", "") and not _parsed.query:
                continue

            snippet = match.group(0)[:120]
            found.append(HTMLIssue(
                issue_type="empty_anchor_media_wrap",
                severity="medium",
                description=(
                    f"<a> wrapping <{media_type}> to {href} has no anchor text "
                    f"and no alt text on the media element. Search engines "
                    f"cannot parse link intent."
                ),
                affected_snippet=snippet,
                recommended_fix=(
                    f'Add descriptive alt="..." to the <{media_type}> element '
                    f"(Google uses img alt as anchor text), or add a visually-hidden "
                    f"<span> with anchor text inside the link."
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
