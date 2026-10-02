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


def build_block(family: str, pairs: list) -> str:
    """The marked 'Related guides' block for a list of (url, anchor) pairs."""
    heading = f"Related {family} guides".strip()
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
