"""Growth Playbook analyzers — Brian Dean (Backlinko) & Neil Patel methods.

Pure functions over evaluation results and rolling history snapshots. Each
returns plain dicts/lists ready to JSON-serialize. No I/O here so the logic
stays unit-testable; the web layer loads the data and passes it in.

Methods implemented:
  - Striking Distance    (Dean/Patel): queries ranking pos ~8-20 with real demand
  - Orphans & Clusters   (Patel):      topic clusters, pillar pages, orphan rescue
  - Pruning Candidates   (Dean):       kill / merge / redirect the dead weight
  - Content Decay        (Patel):      pages sliding over time -> refresh queue
"""

from urllib.parse import urlparse, urljoin


DEFAULT_BASE = "https://alphabet-trains.com/"

# Grammatical filler only — product attributes and audience terms carry real
# topical signal and must stay (kept in sync with web/app.py).
STOP_WORDS = {
    "for", "the", "and", "with", "how", "what", "why", "are",
    "can", "from", "that", "this", "your", "our", "all", "has",
    "its", "you", "was", "get", "not", "but", "will", "more",
    "buy", "shop", "free", "shipping", "sale", "price",
    "online", "store", "review", "reviews",
    "best", "top", "new", "usa", "2024", "2025", "2026",
}

# Position-based CTR benchmarks (organic), used to size the "if it reached
# page 1" upside. Rounded from Backlinko's 4M-result CTR study.
_CTR_BY_POSITION = {
    1: 0.27, 2: 0.16, 3: 0.11, 4: 0.08, 5: 0.06,
    6: 0.05, 7: 0.04, 8: 0.032, 9: 0.028, 10: 0.025,
}


def _norm(u, base_url=""):
    """Normalize a URL for comparison: resolve relative, drop scheme/www/slash."""
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = urljoin(base_url or DEFAULT_BASE, u)
    parsed = urlparse(u.lower())
    netloc = parsed.netloc.replace("www.", "")
    path = parsed.path.rstrip("/") or "/"
    return f"{netloc}{path}"


def _query_words(text):
    """Topical tokens from a query string, filler removed."""
    return {
        w for w in (text or "").lower().split()
        if len(w) > 2 and w not in STOP_WORDS
    }


def _expected_ctr(position):
    """Interpolated organic CTR for a (possibly fractional) SERP position."""
    if position <= 0:
        return 0.0
    p = int(round(position))
    if p in _CTR_BY_POSITION:
        return _CTR_BY_POSITION[p]
    if p <= 10:
        return 0.025
    # Page 2+ decays toward zero.
    return max(0.002, 0.02 / (p - 9))


# ══════════════════════════════════════════════════════════════════════
# 1. STRIKING DISTANCE (Dean / Patel)
# ══════════════════════════════════════════════════════════════════════

def find_striking_distance(results, min_impressions=30, pos_low=6.0,
                           pos_high=20.0, limit=100):
    """Queries ranking just off page 1 (pos ~6-20) with real search demand.

    These are the cheapest ranking wins: the page already ranks and has
    proven demand, it just needs a nudge (on-page relevance + internal links)
    to cross onto page 1 where clicks actually happen.

    Returns rows sorted by upside = incremental clicks available if the query
    reached position 5.
    """
    rows = []
    for r in results:
        url = r.get("url", "")
        asset_type = r.get("asset_type", "other")
        for q in r.get("top_queries", []):
            pos = q.get("position", 0) or 0
            impr = q.get("impressions", 0) or 0
            clicks = q.get("clicks", 0) or 0
            if impr < min_impressions:
                continue
            if not (pos_low <= pos <= pos_high):
                continue
            # Upside: clicks at a realistic page-1 landing spot (pos 5) minus
            # what the query earns today.
            target_ctr = _expected_ctr(5)
            potential_clicks = impr * target_ctr
            upside_clicks = max(0.0, potential_clicks - clicks)
            # How close to page 1 — nearer queries are easier, weight them up.
            proximity = max(0.1, (pos_high - pos) / (pos_high - pos_low))
            score = upside_clicks * (0.5 + 0.5 * proximity)
            rows.append({
                "url": url,
                "asset_type": asset_type,
                "query": q.get("query", ""),
                "position": round(pos, 1),
                "impressions": impr,
                "clicks": clicks,
                "ctr": round((clicks / impr * 100) if impr else 0, 2),
                "potential_clicks_at_pos5": round(potential_clicks),
                "upside_clicks": round(upside_clicks),
                "score": round(score, 1),
            })
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:limit]


# ══════════════════════════════════════════════════════════════════════
# Shared: internal link graph
# ══════════════════════════════════════════════════════════════════════

def _build_graph(results):
    """norm_url -> {url, title, asset_type, gsc_*, query_words, inlinks, outlinks}."""
    graph = {}
    for r in results:
        url = r.get("url", "")
        norm = _norm(url)
        if not norm:
            continue
        pm = r.get("page_metadata", {})
        title = pm.get("title", "") or pm.get("h1", "") or url
        words = set()
        for q in r.get("top_queries", []):
            words |= _query_words(q.get("query", ""))
        graph[norm] = {
            "url": url,
            "norm": norm,
            "title": title[:100],
            "asset_type": r.get("asset_type", "other"),
            "gsc_impressions": r.get("gsc_impressions", 0) or 0,
            "gsc_clicks": r.get("gsc_clicks", 0) or 0,
            "gsc_position": round(r.get("gsc_position", 0) or 0, 1),
            "word_count": pm.get("word_count", 0) or 0,
            "query_words": words,
            "outlink_norms": set(),
            "inlink_norms": set(),
            "_pm": pm,
        }
    # Wire edges
    for norm, page in graph.items():
        for ol in page["_pm"].get("internal_outlinks", []):
            tnorm = _norm(ol.get("target_url", ""), page["url"])
            if tnorm and tnorm != norm and tnorm in graph:
                page["outlink_norms"].add(tnorm)
                graph[tnorm]["inlink_norms"].add(norm)
    return graph


# ══════════════════════════════════════════════════════════════════════
# 2. ORPHANS & TOPIC CLUSTERS (Patel)
# ══════════════════════════════════════════════════════════════════════

def _union_find_clusters(graph, min_shared=3):
    """Group pages that share >= min_shared query words (union-find)."""
    norms = list(graph.keys())
    parent = {n: n for n in norms}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(norms)):
        a = norms[i]
        wa = graph[a]["query_words"]
        if len(wa) < min_shared:
            continue
        for j in range(i + 1, len(norms)):
            b = norms[j]
            if len(graph[b]["query_words"]) < min_shared:
                continue
            if len(wa & graph[b]["query_words"]) >= min_shared:
                union(a, b)

    clusters = {}
    for n in norms:
        clusters.setdefault(find(n), []).append(n)
    return [members for members in clusters.values() if len(members) >= 3]


def find_orphans_and_clusters(results, orphan_min_impressions=20):
    """Orphan pages (no internal inlinks) with demand, plus topic clusters
    with an identified pillar and missing cluster->pillar links.
    """
    graph = _build_graph(results)

    # Orphans: demand-bearing pages nothing links to internally. Skip the
    # homepage (naturally linked everywhere) and utility/other with no demand.
    orphans = []
    for norm, page in graph.items():
        path = urlparse(page["url"].lower()).path.rstrip("/")
        if path in ("", "/"):
            continue  # homepage
        if page["inlink_norms"]:
            continue
        if page["gsc_impressions"] < orphan_min_impressions:
            continue
        orphans.append({
            "url": page["url"],
            "title": page["title"],
            "asset_type": page["asset_type"],
            "impressions": page["gsc_impressions"],
            "clicks": page["gsc_clicks"],
            "position": page["gsc_position"],
        })
    orphans.sort(key=lambda o: o["impressions"], reverse=True)

    # Topic clusters + pillar detection
    clusters_out = []
    for members in _union_find_clusters(graph):
        pages = [graph[m] for m in members]
        # Pillar = the page best positioned to hold authority: prefer a
        # category page, break ties on impressions.
        def pillar_key(p):
            return (1 if p["asset_type"] == "category" else 0, p["gsc_impressions"])
        pillar = max(pages, key=pillar_key)
        # Shared topic label = most common query words across the cluster.
        from collections import Counter
        wc = Counter()
        for p in pages:
            wc.update(p["query_words"])
        topic = ", ".join(w for w, _ in wc.most_common(3))
        # Which cluster pages don't link to the pillar yet?
        missing_links = []
        for p in pages:
            if p["norm"] == pillar["norm"]:
                continue
            if pillar["norm"] not in p["outlink_norms"]:
                missing_links.append({
                    "url": p["url"],
                    "title": p["title"],
                    "asset_type": p["asset_type"],
                    "impressions": p["gsc_impressions"],
                })
        missing_links.sort(key=lambda x: x["impressions"], reverse=True)
        clusters_out.append({
            "topic": topic,
            "size": len(pages),
            "pillar": {
                "url": pillar["url"],
                "title": pillar["title"],
                "asset_type": pillar["asset_type"],
                "impressions": pillar["gsc_impressions"],
                "position": pillar["gsc_position"],
                "is_orphan": not pillar["inlink_norms"],
            },
            "missing_pillar_links": missing_links,
            "members": [
                {"url": p["url"], "title": p["title"],
                 "impressions": p["gsc_impressions"]}
                for p in sorted(pages, key=lambda x: x["gsc_impressions"], reverse=True)
            ],
        })
    # Most valuable clusters first (by total impressions), and only those
    # with an actual gap worth acting on.
    clusters_out.sort(
        key=lambda c: sum(m["impressions"] for m in c["members"]), reverse=True)

    return {"orphans": orphans, "clusters": clusters_out}


# ══════════════════════════════════════════════════════════════════════
# 3. PRUNING CANDIDATES (Dean)
# ══════════════════════════════════════════════════════════════════════

def find_pruning_candidates(results, max_impressions=15, thin_words=300):
    """Dead-weight pages: no traffic, no demand, thin and/or orphaned.

    Conservative by design — only flags blog/other pages (never products or
    categories, which carry commercial/structural value even when quiet) and
    always frames the output as a REVIEW candidate with a suggested
    disposition, never an automatic delete.
    """
    graph = _build_graph(results)
    candidates = []
    for norm, page in graph.items():
        asset_type = page["asset_type"]
        # Protect commercial and structural pages.
        if asset_type in ("product", "category"):
            continue
        path = urlparse(page["url"].lower()).path.rstrip("/")
        if path in ("", "/"):
            continue
        impr = page["gsc_impressions"]
        clicks = page["gsc_clicks"]
        words = page["word_count"]
        is_orphan = not page["inlink_norms"]
        # Dead: negligible demand and no clicks.
        if impr > max_impressions or clicks > 0:
            continue
        thin = 0 < words < thin_words
        if not (thin or is_orphan):
            continue
        # Disposition: a topically-related stronger page -> redirect/merge;
        # otherwise prune (noindex/remove).
        best_rel, best_overlap = None, 0
        for other_norm, other in graph.items():
            if other_norm == norm:
                continue
            overlap = len(page["query_words"] & other["query_words"])
            if overlap > best_overlap and other["gsc_impressions"] > impr:
                best_overlap, best_rel = overlap, other
        if best_rel and best_overlap >= 2:
            disposition = "merge_redirect"
            reason = (f"Thin/dead but overlaps a stronger page "
                      f"({best_overlap} shared terms). Merge content and 301 to it.")
            target = {"url": best_rel["url"], "title": best_rel["title"]}
        else:
            disposition = "prune"
            reason = ("No demand, no clicks, "
                      + ("thin content" if thin else "orphaned")
                      + ", no strong related page. Noindex or remove.")
            target = None
        candidates.append({
            "url": page["url"],
            "title": page["title"],
            "asset_type": asset_type,
            "impressions": impr,
            "clicks": clicks,
            "word_count": words,
            "is_orphan": is_orphan,
            "disposition": disposition,
            "reason": reason,
            "redirect_target": target,
        })
    # Prune-worst first: lowest impressions, thinnest.
    candidates.sort(key=lambda c: (c["impressions"], c["word_count"]))
    return candidates


# ══════════════════════════════════════════════════════════════════════
# 4. CONTENT DECAY (Patel)
# ══════════════════════════════════════════════════════════════════════

def find_content_decay(snapshots, min_peak_clicks=3, decay_ratio=0.6, limit=50):
    """Pages sliding over time -> refresh queue.

    `snapshots` is a chronological list of evaluation dicts (oldest first),
    each with a "results" list of per-page {url, gsc_clicks, gsc_impressions,
    gsc_position}. A page is decaying when its current clicks have fallen to
    <= decay_ratio of its peak, and it once earned real traffic (min_peak_clicks).

    Returns [] with a status when there isn't enough history to judge.
    """
    if len(snapshots) < 2:
        return {
            "decaying": [],
            "status": "insufficient_history",
            "message": ("Content decay needs at least 2 evaluation snapshots. "
                        "Run the evaluation again after some time passes to build "
                        "a trend."),
            "snapshots_available": len(snapshots),
        }

    # Per-URL click/impression/position series in chronological order.
    series = {}
    for snap in snapshots:
        date = (snap.get("timestamp", "") or "")[:10]
        for r in snap.get("results", []):
            url = r.get("url", "")
            if not url:
                continue
            series.setdefault(url, {"dates": [], "clicks": [], "impr": [],
                                    "pos": [], "asset_type": r.get("asset_type", "other")})
            s = series[url]
            s["dates"].append(date)
            s["clicks"].append(r.get("gsc_clicks", 0) or 0)
            s["impr"].append(r.get("gsc_impressions", 0) or 0)
            s["pos"].append(r.get("gsc_position", 0) or 0)

    decaying = []
    for url, s in series.items():
        clicks = s["clicks"]
        if len(clicks) < 2:
            continue
        peak = max(clicks)
        peak_idx = clicks.index(peak)
        current = clicks[-1]
        # Must have earned real traffic once, and the peak must precede now.
        if peak < min_peak_clicks or peak_idx == len(clicks) - 1:
            continue
        if current > peak * decay_ratio:
            continue  # still healthy
        drop_pct = round((1 - current / peak) * 100) if peak else 0
        impr = s["impr"]
        pos = s["pos"]
        decaying.append({
            "url": url,
            "asset_type": s["asset_type"],
            "peak_clicks": peak,
            "current_clicks": current,
            "drop_pct": drop_pct,
            "peak_date": s["dates"][peak_idx],
            "current_date": s["dates"][-1],
            "impressions_current": impr[-1] if impr else 0,
            "impressions_peak": impr[peak_idx] if peak_idx < len(impr) else 0,
            "position_current": round(pos[-1], 1) if pos else 0,
            "position_peak": round(pos[peak_idx], 1) if peak_idx < len(pos) else 0,
            "clicks_series": clicks,
        })
    # Biggest losers (by absolute clicks lost) first.
    decaying.sort(key=lambda d: (d["peak_clicks"] - d["current_clicks"]), reverse=True)
    return {
        "decaying": decaying[:limit],
        "status": "ok",
        "snapshots_available": len(snapshots),
    }
