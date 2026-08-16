"""Link Prospect Engine — GSC-driven external-link opportunities.

The central principle: don't ask only "who links to my competitors?" Ask BOTH
  SHOULD-GET — who is topically visible for this page's winnable query and
              publishes the kind of page (resource list, guide, roundup) where a
              link to us is a legitimate addition?  (discovered via Serper)
  CAN-GET   — who demonstrably links out in this topic — specifically, who links
              to the content-type SERP peers that outrank us?  (evidence via
              DataForSEO backlinks; peers come from the SERP intel already
              cached on each winnable page)

Targets are NOT rediscovered: the winnable set from click_yield IS the URL
opportunity ranking (positions 4-20, non-brand demand, ~0 clicks, momentum).
Every prospect carries its factual evidence (which discovery search, which peer
backlink) and two independent 0-100 scores:

  IMPACT       — topical relevance to the target query, authority, page context,
                 dofollow evidence.
  PROBABILITY  — observable willingness to link: links to 1/2+ peers, resource-
                 page pattern, topical publishing activity.

Final = 100 x (I/100)^a x (P/100)^b  (geometric mean — a terrible score on one
dimension can't hide behind a great one), scaled by the target's demand so
outreach time goes where GSC already shows upside. All weights env-tunable.

Deterministic and honest: no LLM in the scoring path, no fabricated metrics,
missing providers degrade to an explicit "reason" instead of fake prospects.
Cached to data/link_prospects.json.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from src.metrics.click_yield import load_click_yield, _tokens, _site_identity
from src.metrics.serp_intel import MARKETPLACES

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "link_prospects.json"


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


MAX_PAGES = _env_int("LINK_PROSPECT_MAX_PAGES", 10)            # winnable pages to prospect for
MAX_STRIKING = _env_int("LINK_PROSPECT_MAX_STRIKING", 5)       # extra "Needs backlinks" striking pages
SEARCHES_PER_QUERY = _env_int("LINK_PROSPECT_SEARCHES_PER_QUERY", 2)
PEERS_PER_QUERY = _env_int("LINK_PROSPECT_PEERS_PER_QUERY", 2)
LINKS_PER_PEER = _env_int("LINK_PROSPECT_LINKS_PER_PEER", 25)
IMPACT_EXP = _env_float("LINK_IMPACT_EXP", 0.6)                # spec's 0.60 / 0.40 split
PROB_EXP = _env_float("LINK_PROB_EXP", 0.4)

# Domains that are never outreach prospects: marketplaces/big-box (from serp_intel),
# social/UGC platforms (links are nofollow/user-generated, not editorial), and
# reference sites that don't take link suggestions.
PLATFORM_DOMAINS = ("facebook.", "instagram.", "pinterest.", "youtube.", "tiktok.",
                    "twitter.", "x.com", "reddit.", "linkedin.", "quora.",
                    "wikipedia.", "wikihow.", "medium.com", "yelp.", "bbb.org",
                    "google.", "apple.", "play.google")

# Hosted-platform / staging subdomains: free site builders, dev previews, and
# hosting sandboxes. These aren't publishers — there is no editor to reach and
# no authority to gain. The classic trap: a STAGING CLONE of a SERP peer links
# to its own production site, which scores as perfect "links to peer, dofollow,
# topically identical" evidence while being outreach-worthless
# (brown-woodcock-*.hostingersite.com taught us this one).
HOSTED_PLATFORM_SUFFIXES = (
    ".hostingersite.com", ".wixsite.com", ".weebly.com", ".wordpress.com",
    ".blogspot.com", ".github.io", ".netlify.app", ".vercel.app", ".pages.dev",
    ".myshopify.com", ".squarespace.com", ".webflow.io", ".godaddysites.com",
    ".site123.me", ".web.app", ".firebaseapp.com", ".000webhostapp.com",
    ".wpenginepowered.com", ".kinsta.cloud", ".myftpupload.com", ".repl.co",
    ".glitch.me", ".neocities.org", ".carrd.co",
)

# Editorial page patterns that signal "this page curates external resources" —
# both an impact signal (contextual placement) and a probability signal (they
# routinely add items).
_RESOURCE_RE = re.compile(
    r"\b(resources?|best|top \d+|roundup|guide|recommended|list of|"
    r"gift guide|curriculum|activities|ideas|tools|links)\b", re.I)


def _domain(url):
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _excluded(dom, own_forms):
    if not dom:
        return True
    if any(m in dom for m in MARKETPLACES):
        return True
    if any(p in dom for p in PLATFORM_DOMAINS):
        return True
    if dom.endswith(HOSTED_PLATFORM_SUFFIXES):
        return True
    if any(f in dom for f in own_forms):
        return True
    return False


def _overlap_ratio(query, *texts):
    """Share of the query's meaningful tokens present in the prospect's text."""
    qt = _tokens(query)
    if not qt:
        return 0.0
    tt = set()
    for t in texts:
        tt |= _tokens(t or "")
    return len(qt & tt) / len(qt)


# ────────────────────────────────────────────────── discovery
def _discovery_searches(query):
    """Bounded, deterministic operator searches for SHOULD-GET prospects: pages
    that curate/recommend resources on the query's topic."""
    q = (query or "").strip()
    cands = [f"{q} resources", f"best {q} recommended", f"{q} gift guide"]
    return cands[:max(1, SEARCHES_PER_QUERY)]


def _should_get_for(w, own_forms, fetch_serp, log):
    """Serper discovery: topical resource/roundup pages for one winnable query."""
    found = []
    for search in _discovery_searches(w["query"]):
        serp = fetch_serp(search)
        if not serp:
            log.append(f"Serper unavailable/quota-out at '{search}' — should-get discovery partial.")
            break
        for r in (serp.get("organic_results") or [])[:10]:
            dom = _domain(r.get("url", ""))
            if _excluded(dom, own_forms):
                continue
            found.append({
                "domain": dom,
                "page_url": r.get("url", ""),
                "page_title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "evidence": {
                    "kind": "topical_search",
                    "detail": f"Ranks #{r.get('position')} for discovery search \"{search}\"",
                    "source_url": r.get("url", ""),
                },
            })
    return found


def _can_get_for(w, own_forms, used_peers, fetch_referring_links, log):
    """DataForSEO evidence: referring domains of the content-type SERP peers that
    outrank us for this query (peers come from the winnable page's cached SERP
    intel — no extra SERP spend)."""
    found = []
    peers = [t for t in ((w.get("serp") or {}).get("top5") or [])
             if t.get("kind") == "content" and t.get("domain")
             and not _excluded(t["domain"], own_forms)]
    picked = 0
    for p in peers:
        if picked >= PEERS_PER_QUERY:
            break
        peer_dom = p["domain"]
        if peer_dom in used_peers:
            continue  # already fetched this peer for another query this run
        used_peers.add(peer_dom)
        picked += 1
        res = fetch_referring_links(peer_dom, limit=LINKS_PER_PEER)
        if not res.get("available"):
            log.append(f"Backlinks for peer {peer_dom}: {res.get('reason', 'unavailable')}")
            continue
        for link in res.get("links", []):
            dom = link.get("domain_from", "")
            if _excluded(dom, own_forms) or dom == peer_dom:
                continue
            found.append({
                "domain": dom,
                "page_url": link.get("url_from", ""),
                "page_title": link.get("title_from", ""),
                "snippet": "",
                "dofollow": link.get("dofollow", False),
                "domain_rank": link.get("domain_rank", 0),
                "evidence": {
                    "kind": "peer_backlink",
                    "detail": f"Links to SERP peer {peer_dom} (outranks you at #{p.get('position')} "
                              f"for \"{w['query']}\")" + (" [dofollow]" if link.get("dofollow") else ""),
                    "source_url": link.get("url_from", ""),
                    "peer": peer_dom,
                },
            })
    return found


# ────────────────────────────────────────────────── scoring
def _score_prospect(pr, query):
    """Two independent 0-100 scores + components, deterministic and inspectable."""
    title, snippet, dom = pr.get("page_title", ""), pr.get("snippet", ""), pr["domain"]
    overlap = _overlap_ratio(query, title, snippet, dom.replace("-", " ").replace(".", " "))
    is_resource = bool(_RESOURCE_RE.search(title + " " + pr.get("page_url", "")))
    peer_count = len({e.get("peer") for e in pr["evidence"] if e.get("kind") == "peer_backlink"})
    topical_hit = any(e.get("kind") == "topical_search" for e in pr["evidence"])
    dofollow = any("[dofollow]" in (e.get("detail") or "") for e in pr["evidence"])
    rank = max((pr.get("domain_rank") or 0), 0)  # DataForSEO 0-1000; 0 when unknown

    impact = (
        overlap * 40                                    # topical relevance — highest weight
        + min(rank / 1000.0, 1.0) * 25                  # measured authority when we have it
        + (12 if (topical_hit and not rank) else 0)     # topical SERP visibility as proxy when we don't
        + (20 if is_resource else 0)                    # contextual placement potential
        + (5 if dofollow else 0)
    )
    prob = (
        10                                              # base: qualified, non-excluded site
        + (35 if peer_count >= 2 else 20 if peer_count == 1 else 0)  # proven linker in THIS topic
        + (25 if is_resource else 0)                    # page type that routinely adds items
        + (10 if topical_hit else 0)                    # actively publishing/visible on the topic
        + (10 if dofollow else 0)                       # links editorially, not just UGC
    )
    impact = max(5.0, min(100.0, impact))
    prob = max(5.0, min(100.0, prob))
    return round(impact, 1), round(prob, 1), {
        "relevance_overlap": round(overlap, 2), "domain_rank": rank,
        "resource_page": is_resource, "peers_linked": peer_count,
        "topical_search_hit": topical_hit, "dofollow_evidence": dofollow,
    }


def _tier(impact, prob):
    if impact >= 50 and prob >= 50:
        return 1                       # strong impact AND credible path — work first
    if impact >= 65:
        return 2                       # high-impact / harder — deliberate personalized outreach
    if prob >= 60 and impact >= 30:
        return 3                       # easier win — referring-domain breadth
    return 0                           # watchlist


def _angle(pr):
    """Deterministic outreach angle grounded in the actual evidence — never a
    claim we haven't verified (drafting a real email means reading their page)."""
    if pr["components"]["resource_page"]:
        return ("Resource-page addition: their page curates this topic — suggest your "
                "target page as a concrete addition, stating what it covers that the "
                "current list lacks.")
    if pr["components"]["peers_linked"] >= 1:
        return ("Peer gap: they already link to a site that outranks you for this "
                "query — propose your target page as a comparable (or more specific) "
                "resource on the same topic.")
    return ("Topical reference: they publish on this topic — propose your target "
            "page as a citable reference for a specific claim or product category.")


def _striking_external_targets(existing_urls: set) -> list:
    """Striking-distance pages whose remaining lever is EXTERNAL links — the rows
    the Playbook labels 'Needs backlinks' (query already in title/H1, internal
    links tapped). They aren't all in the winnable set, so add them as prospect
    targets. They carry no cached SERP intel, so they get SHOULD-GET discovery
    only (the can-get side needs the winnable pages' cached peer data)."""
    try:
        eval_path = PROJECT_ROOT / "data" / "latest_evaluation.json"
        results = json.loads(eval_path.read_text()).get("results", [])
        from src.analysis.growth_playbook import find_striking_distance
        raw = find_striking_distance(results)
        rows = raw.get("rows", []) if isinstance(raw, dict) else (raw or [])
    except Exception:
        return []
    # One slot per URL — a page ranking for several variants of the same query
    # ("waldorf vs montessori" / "montessori vs waldorf") must not consume
    # multiple striking slots on near-identical discovery searches. Keep the
    # highest-impression query per URL.
    best_by_url = {}
    for r in rows:
        if r.get("lever") != "external" or not r.get("url") or not r.get("query"):
            continue
        if r["url"] in existing_urls:
            continue
        cur = best_by_url.get(r["url"])
        if cur is None or (r.get("impressions", 0) or 0) > cur["impressions"]:
            best_by_url[r["url"]] = {
                "url": r["url"], "query": r["query"],
                "impressions": r.get("impressions", 0) or 0,
                "position": r.get("position", 0) or 0,
                "trend": "unknown", "serp": {}, "_source": "striking"}
    out = sorted(best_by_url.values(), key=lambda x: -x["impressions"])
    return out[:max(0, MAX_STRIKING)]


# ────────────────────────────────────────────────── main
def compute_link_prospects(config: dict) -> dict:
    """Build the ranked prospect list from the cached winnable set. Best-effort
    per provider: missing Serper/DataForSEO shrinks the pool with an explicit
    note, never fabricates."""
    from src.data_sources.serp_client import fetch_serp
    from src.data_sources import dataforseo_client as dfs

    cy = load_click_yield()
    winnable = (cy or {}).get("winnable") or []

    _dom, own_forms = _site_identity(config)
    log: list = []

    # Domains that ALREADY link to us — one bounded fetch, used to exclude
    # prospects we've already won (spec §9A referring-domain uniqueness).
    already = set()
    own_bl = dfs.fetch_referring_links(_dom, limit=100) if dfs.is_configured() else \
        {"available": False, "reason": "DataForSEO not configured"}
    if own_bl.get("available"):
        already = {l["domain_from"] for l in own_bl.get("links", []) if l.get("domain_from")}
    else:
        log.append(f"Own-backlink check unavailable ({own_bl.get('reason')}) — "
                   "can't exclude domains that already link to you.")

    targets = winnable[:MAX_PAGES]
    # Also prospect for the Playbook's "Needs backlinks" striking-distance pages,
    # so the two features cross-link instead of talking past each other.
    targets = targets + _striking_external_targets({w["url"] for w in targets})
    if not targets:
        return {"available": False,
                "reason": "No target pages found — run Click Yield (winnable pages) "
                          "and/or an evaluation (striking-distance pages) first; they "
                          "define which URLs deserve link-building effort.",
                "generated_at": datetime.now().isoformat(timespec="seconds")}
    max_impr = max((w.get("impressions") or 1) for w in targets)
    used_peers: set = set()
    by_domain: dict = {}
    skipped_already = 0

    for w in targets:
        raw = _should_get_for(w, own_forms, fetch_serp, log)
        if dfs.is_configured():
            raw += _can_get_for(w, own_forms, used_peers, dfs.fetch_referring_links, log)
        elif "DataForSEO not configured" not in " ".join(log):
            log.append("DataForSEO not configured — CAN-GET (peer-backlink) discovery skipped; "
                       "prospects below are SHOULD-GET only.")

        for cand in raw:
            dom = cand["domain"]
            if dom in already:
                skipped_already += 1
                continue
            # Dedup by domain per target-URL pairing; merge evidence, keep the
            # richest page info.
            key = (dom, w["url"])
            pr = by_domain.get(key)
            if pr is None:
                pr = by_domain[key] = {
                    "domain": dom,
                    "page_url": cand.get("page_url", ""),
                    "page_title": cand.get("page_title", ""),
                    "snippet": cand.get("snippet", ""),
                    "domain_rank": cand.get("domain_rank", 0),
                    "target_url": w["url"],
                    "target_query": w["query"],
                    "target_position": w.get("position"),
                    "target_impressions": w.get("impressions", 0),
                    "target_trend": w.get("trend", "unknown"),
                    "target_source": w.get("_source", "winnable"),
                    "evidence": [],
                }
            pr["evidence"].append(cand["evidence"])
            pr["domain_rank"] = max(pr.get("domain_rank") or 0, cand.get("domain_rank") or 0)
            if cand.get("page_title") and not pr.get("page_title"):
                pr["page_title"], pr["page_url"] = cand["page_title"], cand.get("page_url", "")
                pr["snippet"] = cand.get("snippet", "")

    prospects = []
    for pr in by_domain.values():
        impact, prob, comps = _score_prospect(pr, pr["target_query"])
        pr["impact"], pr["probability"], pr["components"] = impact, prob, comps
        url_opp = (pr["target_impressions"] or 0) / max_impr
        final = 100.0 * (impact / 100.0) ** IMPACT_EXP * (prob / 100.0) ** PROB_EXP
        pr["final_score"] = round(final * (0.6 + 0.4 * url_opp), 1)  # demand-scaled, floored at 60%
        pr["tier"] = _tier(impact, prob)
        pr["angle"] = _angle(pr)
        # De-noise evidence for display (dedup identical lines).
        seen_e = set()
        pr["evidence"] = [e for e in pr["evidence"]
                          if not (e["detail"] in seen_e or seen_e.add(e["detail"]))][:6]
        prospects.append(pr)

    # Tiered prospects first (so the [:200] cap can never squeeze them out in
    # favor of watchlist rows), then by score within each group.
    prospects.sort(key=lambda p: (p["tier"] == 0, -p["final_score"]))
    tiers = {1: 0, 2: 0, 3: 0, 0: 0}
    for p in prospects:
        tiers[p["tier"]] += 1

    return {
        "available": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "targets_considered": len(targets),
        "prospects": prospects[:200],
        "summary": {
            "total": len(prospects), "tier1": tiers[1], "tier2": tiers[2],
            "tier3": tiers[3], "watchlist": tiers[0],
            "excluded_already_linking": skipped_already,
            "own_referring_domains": len(already),
        },
        "provider_notes": log,
        "weights": {"impact_exp": IMPACT_EXP, "prob_exp": PROB_EXP,
                    "max_pages": MAX_PAGES, "searches_per_query": SEARCHES_PER_QUERY,
                    "peers_per_query": PEERS_PER_QUERY},
    }


def save_link_prospects(data: dict, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_link_prospects(path: Path = CACHE_PATH):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
