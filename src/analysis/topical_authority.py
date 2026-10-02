"""Topical Authority Engine — Phase 1: inventory + cluster map.

Topical authority is won by owning a *topic*, not a keyword: a broad pillar
(hub) page that frames the subject, a ring of supporting (spoke) pages —
products, categories, and blog articles — and a tight internal-link mesh that
ties the spokes to the hub and back. Google reads that mesh as "this site is the
authority on X." A small store beats a bigger domain on a topic by covering it
*completely and coherently*, which is exactly what a cluster map makes visible.

This module is Phase 1: it reads what the site ALREADY has — the evaluated pages
(their type, the queries they rank for, the demand behind them) and the real
internal-link graph the crawler captured (content links only; nav/header/footer
are already stripped upstream) — and it:

  1. groups every page into a topical CLUSTER, anchored to the store's real
     product families (from business_context.product_families), never invented;
  2. assigns each cluster a HUB (pillar) page and its SPOKES;
  3. measures the internal-link health of each cluster — do the spokes link up
     to the hub? does the hub link down to the spokes? which spokes are orphans?

Everything here is pure (no network, no fabrication). It only ever reports pages
and links that genuinely exist in the evaluation data. Where it can't find
something — e.g. a family with no category/pillar page — it says so plainly and
flags it, rather than guessing. Phases 2-4 (interlink plan, topic-gap + cadence,
write-to-plan verification) build on this map.
"""
from __future__ import annotations

import re
from collections import defaultdict

from src.analysis.content_gap import _distinctive, _tokens


# ─────────────────────────────── URL / text helpers ──────────────────────────

def _norm_url(url: str) -> str:
    """Canonical form for link matching: lower-cased, scheme/www stripped, no
    trailing slash, no query/fragment. Two links to the same page compare equal
    regardless of how they were written (relative vs absolute, trailing slash)."""
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"\?.*$", "", u)
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def _slug_phrase(url: str) -> str:
    """The URL's own words: the path with the host, extension and separators
    stripped — '/name-trains.html' → 'name trains'. This is the strongest signal
    of what a page is about, straight from how the store itself named it."""
    path = re.sub(r"^https?://[^/]+", "", url or "")
    path = re.sub(r"\.(html?|php|aspx?)$", "", path)
    return re.sub(r"[-_/]+", " ", path).strip()


def _family_slug(family: str) -> str:
    """Normalize a configured family name to a URL-slug fragment, singularized so
    'name trains' matches both /name-trains.html and /name-train/."""
    slug = (family or "").lower().strip().replace(" ", "-")
    if slug.endswith("s") and len(slug) > 3:
        slug = slug[:-1]
    return slug


def _page_tokens(page: dict) -> set:
    """Distinctive tokens describing a page — pooled from its URL slug, title and
    H1. These are matched against each family's tokens to assign the cluster."""
    pm = page.get("page_metadata", {}) or {}
    text = " ".join([
        _slug_phrase(page.get("url", "")),
        pm.get("title", "") or "",
        pm.get("h1", "") or "",
    ])
    return _distinctive(text)


def _singular(tok: str) -> str:
    return tok[:-1] if tok.endswith("s") and len(tok) > 3 else tok


def _sing_set(tokens) -> set:
    return {_singular(t) for t in tokens}


# ─────────────────────────────── family assignment ───────────────────────────

def _family_matchers(families: list) -> list:
    """Each configured family → (display_name, slug, token_set). The token set is
    singularized so 'trains' and 'train' match. Empty/degenerate families are
    skipped so we never create a junk cluster."""
    out = []
    for fam in families or []:
        disp = (fam or "").strip()
        if not disp:
            continue
        slug = _family_slug(disp)
        toks = _sing_set(_distinctive(disp)) or _sing_set(_tokens(disp))
        if not toks:
            continue
        out.append({"name": disp, "slug": slug, "tokens": toks})
    return out


def _assign_family(page: dict, matchers: list):
    """Pick the family a page belongs to, grounded in the page's own naming.

    Score each family by how much of its token set the page's tokens cover, with
    a strong bonus when the family slug appears literally in the URL path (the
    most reliable signal). Returns (family_name, score) or (None, 0) when nothing
    matches well enough — an honest 'unclustered', never a forced guess.
    """
    url_norm = _norm_url(page.get("url", ""))
    page_toks = _sing_set(_page_tokens(page))
    if not page_toks:
        return None, 0.0

    best_name, best_score = None, 0.0
    for m in matchers:
        fam_toks = m["tokens"]
        if not fam_toks:
            continue
        overlap = len(fam_toks & page_toks)
        if not overlap:
            continue
        # Coverage of the family's own words — "name train" page covers both
        # tokens of the "name trains" family → 1.0.
        score = overlap / len(fam_toks)
        # Literal slug match in the URL is near-proof (e.g. 'name-train' in path).
        if m["slug"] and m["slug"] in url_norm:
            score += 1.0
        if score > best_score:
            best_name, best_score = m["name"], score
    # Require covering at least half the family's tokens (or a literal slug hit).
    if best_score >= 0.5:
        return best_name, round(best_score, 3)
    return None, 0.0


# ─────────────────────────────── link graph ──────────────────────────────────

def _build_link_graph(pages: list):
    """From each page's crawled content outlinks, build inlink/outlink maps keyed
    by normalized URL and restricted to pages we actually know about (so a link to
    an off-site or uncrawled URL doesn't invent a node). Anchor text is kept so
    Phase 2 can judge link quality.

    Returns (outlinks, inlinks) where:
      outlinks[src] = list of {url, anchor}
      inlinks[dst]  = list of {url, anchor}
    """
    known = {_norm_url(p.get("url", "")) for p in pages if p.get("url")}
    outlinks: dict[str, list] = defaultdict(list)
    inlinks: dict[str, list] = defaultdict(list)
    seen = set()  # (src, dst) dedup — count a link between two pages once
    for p in pages:
        src = _norm_url(p.get("url", ""))
        if not src:
            continue
        pm = p.get("page_metadata", {}) or {}
        for ol in (pm.get("internal_outlinks") or []):
            dst = _norm_url(ol.get("target_url", ""))
            if not dst or dst == src or dst not in known:
                continue
            if (src, dst) in seen:
                continue
            seen.add((src, dst))
            anchor = (ol.get("anchor_text") or "").strip()
            outlinks[src].append({"url": dst, "anchor": anchor})
            inlinks[dst].append({"url": src, "anchor": anchor})
    return outlinks, inlinks


# ─────────────────────────────── hub selection ───────────────────────────────

def _impr(page: dict) -> int:
    return int(page.get("gsc_impressions", 0) or 0)


def _clicks(page: dict) -> int:
    return int(page.get("gsc_clicks", 0) or 0)


def _title_of(page: dict) -> str:
    pm = page.get("page_metadata", {}) or {}
    return (pm.get("h1") or pm.get("title") or _slug_phrase(page.get("url", "")) or "").strip()


def _pick_hub(members: list, family_slug: str, inlinks: dict):
    """Choose the pillar page for a cluster.

    Preference order, grounded in what a hub actually is:
      1. a CATEGORY page whose slug matches the family (the natural pillar);
      2. otherwise the strongest page by internal inlinks, then search demand.
    When no category page exists the hub is 'inferred' and flagged, so the
    operator knows the real fix is to create/designate a proper pillar page.
    """
    def inl(p):
        return len(inlinks.get(_norm_url(p.get("url", "")), []))

    cats = [p for p in members if (p.get("asset_type") or "").lower() == "category"]
    # Prefer the category whose slug literally carries the family term.
    slug_cats = [p for p in cats
                 if family_slug and family_slug in _norm_url(p.get("url", ""))]
    pool = slug_cats or cats
    if pool:
        hub = max(pool, key=lambda p: (inl(p), _impr(p)))
        return hub, False
    if not members:
        return None, False
    # No category page — infer the de-facto hub from link + demand strength.
    hub = max(members, key=lambda p: (inl(p), _impr(p), _clicks(p)))
    return hub, True


# ─────────────────────────────── main entry ──────────────────────────────────

def build_cluster_map(results: list, product_families: list) -> dict:
    """Phase-1 cluster map: group evaluated pages into topical clusters anchored to
    the store's real product families, assign each a hub + spokes, and measure the
    internal-link health that topical authority depends on.

    `results` is the evaluation `results` list; `product_families` is the
    configured list of family names (business_context.product_families).

    Pure and grounded: every page, link and count comes from the data. Nothing is
    invented. Returns a dict ready for the API / template.
    """
    pages = [r for r in (results or []) if r.get("url")]
    matchers = _family_matchers(product_families)
    outlinks, inlinks = _build_link_graph(pages)

    # Assign every page to a family (or leave it unclustered).
    by_family: dict[str, list] = defaultdict(list)
    unclustered: list = []
    for p in pages:
        fam, score = _assign_family(p, matchers)
        if fam:
            p = dict(p)
            p["_family_score"] = score
            by_family[fam].append(p)
        else:
            unclustered.append(p)

    families_out = []
    # Keep the configured family order, then any families that only emerged from
    # data (shouldn't happen, but defensive). Include configured families even if
    # empty, so a family with NO pages is visible as a gap.
    ordered_names = [m["name"] for m in matchers]
    for name in ordered_names:
        members = by_family.get(name, [])
        slug = _family_slug(name)
        fam_block = _summarize_family(name, slug, members, outlinks, inlinks)
        families_out.append(fam_block)

    totals = {
        "pages_total": len(pages),
        "pages_clustered": sum(len(f["spokes"]) + (1 if f["hub"] else 0) for f in families_out),
        "pages_unclustered": len(unclustered),
        "families_total": len(families_out),
        "families_without_hub": sum(1 for f in families_out if not f["hub"]),
        "families_with_inferred_hub": sum(1 for f in families_out if f.get("hub_inferred")),
    }

    return {
        "phase": 1,
        "families": families_out,
        "unclustered": [_page_card(p, outlinks, inlinks) for p in
                        sorted(unclustered, key=lambda x: -_impr(x))[:200]],
        "totals": totals,
    }


def _page_card(page: dict, outlinks: dict, inlinks: dict) -> dict:
    """Compact, display-ready view of a page with its link counts."""
    u = _norm_url(page.get("url", ""))
    return {
        "url": page.get("url", ""),
        "title": _title_of(page),
        "asset_type": (page.get("asset_type") or "other").lower(),
        "inlinks": len(inlinks.get(u, [])),
        "outlinks": len(outlinks.get(u, [])),
        "impressions": _impr(page),
        "clicks": _clicks(page),
    }


def _summarize_family(name: str, slug: str, members: list,
                      outlinks: dict, inlinks: dict) -> dict:
    """Build one family's cluster block: hub, spokes, counts, demand, link health,
    and plain-language flags for the gaps that hold topical authority back."""
    hub_page, inferred = _pick_hub(members, slug, inlinks)
    hub_url = _norm_url(hub_page.get("url", "")) if hub_page else ""

    counts = {"product": 0, "category": 0, "blog": 0, "other": 0}
    for p in members:
        counts[(p.get("asset_type") or "other").lower() if
                (p.get("asset_type") or "other").lower() in counts else "other"] += 1

    spokes = [p for p in members if _norm_url(p.get("url", "")) != hub_url] if hub_page else list(members)

    # Link health, all restricted to within-site known links.
    hub_out_targets = {l["url"] for l in outlinks.get(hub_url, [])} if hub_url else set()
    spoke_cards = []
    linking_to_hub = 0
    orphan_spokes = []
    missing_spoke_to_hub = []
    missing_hub_to_spoke = []
    for p in sorted(spokes, key=lambda x: -_impr(x)):
        su = _norm_url(p.get("url", ""))
        s_out = {l["url"] for l in outlinks.get(su, [])}
        s_in = inlinks.get(su, [])
        links_to_hub = bool(hub_url) and hub_url in s_out
        linked_from_hub = su in hub_out_targets
        if links_to_hub:
            linking_to_hub += 1
        else:
            if hub_url:
                missing_spoke_to_hub.append(p.get("url", ""))
        if hub_url and not linked_from_hub:
            missing_hub_to_spoke.append(p.get("url", ""))
        if len(s_in) == 0:
            orphan_spokes.append(p.get("url", ""))
        card = _page_card(p, outlinks, inlinks)
        card["links_to_hub"] = links_to_hub
        card["linked_from_hub"] = linked_from_hub
        spoke_cards.append(card)

    demand = {
        "impressions": sum(_impr(p) for p in members),
        "clicks": sum(_clicks(p) for p in members),
    }

    # Plain-language flags — the actionable gaps, worst first.
    flags = []
    if not members:
        flags.append(f"No pages found for “{name}”. You sell this but have no "
                     f"indexed page cluster for it — a pillar page is the first step.")
    else:
        if not hub_page:
            flags.append("No hub (pillar) page — this cluster has no page to "
                         "concentrate authority on.")
        elif inferred:
            flags.append(f"No category/pillar page found; treating "
                         f"“{_title_of(hub_page)}” as the de-facto hub. Consider a "
                         f"dedicated pillar page for “{name}”.")
        n_spokes = len(spoke_cards)
        if hub_url and n_spokes:
            if linking_to_hub < n_spokes:
                flags.append(f"{n_spokes - linking_to_hub} of {n_spokes} supporting "
                             f"pages don't link up to the hub.")
            hub_to = sum(1 for c in spoke_cards if c["linked_from_hub"])
            if hub_to < n_spokes:
                flags.append(f"The hub links down to only {hub_to} of {n_spokes} "
                             f"supporting pages.")
        if orphan_spokes:
            flags.append(f"{len(orphan_spokes)} page(s) are orphans (no internal "
                         f"links point to them).")
        if counts["blog"] == 0 and members:
            flags.append("No supporting blog/guide content in this cluster — "
                         "topical depth comes from articles around the products.")

    return {
        "family": name,
        "slug": slug,
        "hub": _page_card(hub_page, outlinks, inlinks) if hub_page else None,
        "hub_inferred": inferred,
        "spokes": spoke_cards,
        "counts": {**counts, "total": len(members)},
        "demand": demand,
        "link_health": {
            "spokes_total": len(spoke_cards),
            "spokes_linking_to_hub": linking_to_hub,
            "hub_links_to_spokes": sum(1 for c in spoke_cards if c["linked_from_hub"]),
            "orphan_spokes": orphan_spokes[:50],
            "missing_spoke_to_hub": missing_spoke_to_hub[:50],
            "missing_hub_to_spoke": missing_hub_to_spoke[:50],
        },
        "flags": flags,
    }
