"""Build the internal-link insertion for a page's body — a clean, natural
"Related guides" block that the content writer appends to a hub page's
description, so the tool can add the interlink-plan links for the operator
instead of making them hand-edit HTML.

Pure and deterministic (no network). Everything the tool writes lives inside a
single marked block so it can be found, de-duplicated, updated, and fully
reverted. Nothing outside the marker is ever touched.
"""
from __future__ import annotations

import re

MARK_START = "<!-- governor-related-links -->"
MARK_END = "<!-- /governor-related-links -->"


def _esc_text(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _esc_attr(s: str) -> str:
    # URLs: quote double-quotes and angle brackets; leave the rest intact.
    return (str(s or "")
            .replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _unesc(s: str) -> str:
    # Reverse _esc_text, so a link read back out of the block isn't re-escaped.
    return (str(s or "")
            .replace("&quot;", '"').replace("&gt;", ">").replace("&lt;", "<")
            .replace("&amp;", "&"))


def strip_block(html: str) -> str:
    """Remove the Governor block (if present), returning the page's own content."""
    out = re.sub(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), "",
                 html or "", flags=re.S)
    return out.rstrip()


def existing_links(html: str) -> list:
    """(url, anchor) pairs already inside the Governor block, so re-applying
    merges rather than duplicates."""
    m = re.search(re.escape(MARK_START) + r"(.*?)" + re.escape(MARK_END),
                  html or "", re.S)
    if not m:
        return []
    pairs = re.findall(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', m.group(1), re.S)
    return [(_unesc(u), _unesc(re.sub(r"\s+", " ", a).strip())) for u, a in pairs]


def _title_case(s: str) -> str:
    small = {"and", "or", "for", "the", "a", "an", "of", "to", "in", "with"}
    words = (s or "").split()
    return " ".join(w if (i and w.lower() in small) else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))


def build_block(family: str, pairs: list) -> str:
    """The marked related-links block for a list of (url, anchor) pairs. Heading is
    honest and cased: "Related Name Trains" — NOT "guides" (the links are a mix of
    products, categories and articles, not all guides)."""
    heading = f"Related {_title_case(family)}".strip()
    lis = "\n".join(
        f'      <li><a href="{_esc_attr(u)}">{_esc_text(a)}</a></li>'
        for u, a in pairs if u and a)
    return (f"{MARK_START}\n"
            f'<div class="governor-related" style="margin-top:1.5em;">\n'
            f"  <h3>{_esc_text(heading)}</h3>\n"
            f"  <ul>\n{lis}\n  </ul>\n"
            f"</div>\n{MARK_END}")


def merge_description(current: str, family: str, new_links: list) -> str:
    """Return the page description with the new links folded into the Governor
    block — existing content untouched, existing block links kept, new links
    appended, de-duplicated by URL (first anchor wins)."""
    base = strip_block(current)
    seen = {}
    order = []
    for u, a in existing_links(current):
        if u not in seen:
            seen[u] = a
            order.append(u)
    for l in (new_links or []):
        u = (l.get("url") or "").strip()
        a = (l.get("anchor") or "").strip()
        if u and u not in seen:
            seen[u] = a
            order.append(u)
    pairs = [(u, seen[u]) for u in order]
    if not pairs:
        return current
    block = build_block(family, pairs)
    sep = "\n\n" if base.strip() else ""
    return base + sep + block


def added_pairs(current: str, family: str, new_links: list) -> list:
    """Just the (url, anchor) pairs that would be NEWLY added (for the preview)."""
    have = {u for u, _ in existing_links(current)}
    out = []
    for l in (new_links or []):
        u = (l.get("url") or "").strip()
        if u and u not in have:
            out.append((u, (l.get("anchor") or "").strip()))
    return out


# ---------------------------------------------------------------------------
# In-prose (contextual) linking — the SAFE variant.
#
# We never rewrite a sentence or add words. We only wrap a phrase that ALREADY
# EXISTS in the page's own copy in an <a> tag. The AI's only job is to pick that
# phrase; the mechanics below do the wrapping deterministically and refuse
# anything that would touch a tag, an existing link, or the Governor box. If no
# existing phrase fits, the caller falls back to the appended "Related" block.
# Because the apply path stores and restores the whole original description, a
# woven link reverts exactly like the block — no per-sentence bookkeeping.
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.I | re.S)
# <style>/<script> blocks, CONTENT included — CSS/JS is not prose. Their text
# (rule bodies, comments like "/* Name Trains category */") must never be offered
# as an anchor or wrapped in a link, or we'd corrupt the stylesheet/script.
_STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.I | re.S)


def visible_text(html: str) -> str:
    """The page's visible prose only: drop the Governor block, then <style>/<script>
    blocks (content and all), then the remaining tags; collapse whitespace. This is
    what the AI phrase-picker sees, so it can never mistake CSS/JS for copy."""
    s = strip_block(html or "")
    s = _STYLE_SCRIPT_RE.sub(" ", s)
    s = _TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _protected_ranges(html: str) -> list:
    """Byte ranges we must NOT touch: inside a <style>/<script> block, inside any
    tag, inside an existing <a>…</a>, or inside the Governor block."""
    ranges = []
    for m in _STYLE_SCRIPT_RE.finditer(html):
        ranges.append((m.start(), m.end()))
    for m in _ANCHOR_RE.finditer(html):
        ranges.append((m.start(), m.end()))
    for m in _TAG_RE.finditer(html):
        ranges.append((m.start(), m.end()))
    mb = re.search(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), html, re.S)
    if mb:
        ranges.append((mb.start(), mb.end()))
    return ranges


def _overlaps(a: int, b: int, ranges: list) -> bool:
    return any(not (b <= s or a >= e) for s, e in ranges)


def _phrase_pattern(phrase: str):
    """Case-insensitive, whitespace-flexible matcher for a phrase, bounded so it
    doesn't match inside a bigger word."""
    words = [w for w in phrase.split() if w]
    if not words:
        return None
    body = r"\s+".join(re.escape(w) for w in words)
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.I)


def url_already_linked(html: str, url: str) -> bool:
    """True if `url` is already the href of some <a> anywhere in the body."""
    u = (url or "").strip()
    if not u:
        return False
    for m in _ANCHOR_RE.finditer(html or ""):
        hm = re.search(r'href="([^"]+)"', m.group(0))
        if hm and hm.group(1).strip() == u:
            return True
    return False


def linkify_phrase(html: str, phrase: str, url: str) -> tuple:
    """Wrap the FIRST free occurrence of `phrase` (an existing phrase in the copy)
    in a link to `url`. 'Free' = not inside a tag, an existing link, or the
    Governor block. Returns (new_html, ok). ok=False means no safe spot — nothing
    changed."""
    html = html or ""
    phrase = (phrase or "").strip()
    url = (url or "").strip()
    pat = _phrase_pattern(phrase)
    if not pat or not url:
        return html, False
    ranges = _protected_ranges(html)
    for m in pat.finditer(html):
        if _overlaps(m.start(), m.end(), ranges):
            continue
        matched = m.group(0)          # keep the page's own casing/spacing
        anchor = f'<a href="{_esc_attr(url)}">{matched}</a>'
        return html[:m.start()] + anchor + html[m.end():], True
    return html, False


def weave_context_snippet(html: str, phrase: str, radius: int = 90) -> tuple:
    """A plain-text window around the first linkable occurrence of `phrase`, for
    the human before/after preview. Returns (snippet, matched_phrase) or ('', '')
    when there's no free spot."""
    html = html or ""
    pat = _phrase_pattern((phrase or "").strip())
    if not pat:
        return "", ""
    ranges = _protected_ranges(html)
    for m in pat.finditer(html):
        if _overlaps(m.start(), m.end(), ranges):
            continue
        lo = max(0, m.start() - radius)
        hi = min(len(html), m.end() + radius)
        window = _STYLE_SCRIPT_RE.sub(" ", html[lo:hi])
        window = _TAG_RE.sub(" ", window)
        snippet = re.sub(r"\s+", " ", window).strip()
        matched = re.sub(r"\s+", " ", m.group(0)).strip()
        return snippet, matched
    return "", ""


def weave_phrases(current: str, placements: list) -> tuple:
    """Apply a list of {url, phrase} placements to the description, wrapping each
    phrase in a link to its url. Skips any url already linked in the body. Returns
    (new_html, woven, missed) where woven/missed are the placement dicts that did /
    did not find a safe spot — `missed` is what the caller falls back to the box."""
    html = current or ""
    woven, missed = [], []
    for p in (placements or []):
        u = (p.get("url") or "").strip()
        ph = (p.get("phrase") or "").strip()
        if not u or not ph or url_already_linked(html, u):
            missed.append(p)
            continue
        new_html, ok = linkify_phrase(html, ph, u)
        if ok:
            html = new_html
            woven.append(p)
        else:
            missed.append(p)
    return html, woven, missed
