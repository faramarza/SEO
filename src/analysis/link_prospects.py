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
SEEDS_PATH = PROJECT_ROOT / "data" / "link_prospect_seeds.json"
STATUS_PATH = PROJECT_ROOT / "data" / "link_prospect_status.json"


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
SEARCHES_PER_QUERY = _env_int("LINK_PROSPECT_SEARCHES_PER_QUERY", 3)
PEERS_PER_QUERY = _env_int("LINK_PROSPECT_PEERS_PER_QUERY", 3)
LINKS_PER_PEER = _env_int("LINK_PROSPECT_LINKS_PER_PEER", 50)
OWN_BACKLINK_DEPTH = _env_int("LINK_PROSPECT_OWN_BACKLINK_DEPTH", 300)
SEED_MAX_QUERIES = _env_int("LINK_PROSPECT_SEED_QUERIES", 8)      # searches per seed topic
IMPACT_EXP = _env_float("LINK_IMPACT_EXP", 0.6)                # spec's 0.60 / 0.40 split
PROB_EXP = _env_float("LINK_PROB_EXP", 0.4)

# Domains that are never outreach prospects: marketplaces/big-box (from serp_intel),
# social/UGC platforms (links are nofollow/user-generated, not editorial), and
# reference sites that don't take link suggestions.
PLATFORM_DOMAINS = ("facebook.", "instagram.", "pinterest.", "youtube.", "tiktok.",
                    "twitter.", "x.com", "reddit.", "linkedin.", "quora.",
                    "wikipedia.", "wikihow.", "medium.com", "yelp.", "bbb.org",
                    "google.", "apple.", "play.google",
                    "stackexchange.", "stackoverflow.",
                    "trustpilot.", "sitejabber.", "g2.com", "capterra.",
                    "steemit.", "feedspot.")

# Direct competitors and manufacturer brands in our catalog space: outreach is
# commercially nonsensical (spec §8) — a rival's blog will not link to us.
# Excluded as PROSPECTS ONLY: as SERP peers they are the competitor-gap
# goldmine (we mine their referring domains on purpose). Matched as exact
# domain or subdomain (never substring — 'hape.com' must not catch 'shape.com').
# Extend via LINK_PROSPECT_COMPETITORS (comma-separated domains).
_COMPETITOR_DEFAULTS = (
    "lovevery.com", "kidkraft.com", "kidkraft.ca", "melissaanddoug.com",
    "hape.com", "hapetoys.com", "iseeme.com", "guidecraft.com",
    "personalcreations.com", "personalizationmall.com", "shutterfly.com",
    "thingsremembered.com", "montessorigeneration.com",
)


def _competitor_domains():
    extra = [d.strip().lower() for d in
             os.environ.get("LINK_PROSPECT_COMPETITORS", "").split(",") if d.strip()]
    return _COMPETITOR_DEFAULTS + tuple(extra)


def _is_competitor(dom: str) -> bool:
    for d in _competitor_domains():
        if dom == d or dom.endswith("." + d):
            return True
    return False

# Forum/community threads are a DIFFERENT opportunity class, not an email
# target: no editor exists, links are typically nofollow UGC (near-zero SEO
# authority), and most forums restrict self-promotion. They stay in the list
# as referral/brand plays — honestly tiered (never 1/2), with a participate-
# genuinely angle and a forum-reply draft instead of an outreach email.
_COMMUNITY_DOM_RE = re.compile(r"^(community|forums?|boards?)\.", re.I)
_COMMUNITY_PATH_RE = re.compile(r"/(forums?|community|boards?|topic|thread)s?/", re.I)


def _is_community(dom: str, page_url: str) -> bool:
    try:
        path = urlparse(page_url or "").path or ""
    except Exception:
        path = ""
    return bool(_COMMUNITY_DOM_RE.search(dom or "") or _COMMUNITY_PATH_RE.search(path))

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
    ".go-vip.net", ".home.blog", ".jimdofree.com", ".jimdosite.com",
    ".cloudapp.azure.com", ".azurewebsites.net", ".amazonaws.com",
    ".herokuapp.com", ".civicplus.com", ".constantcontact.com",
    ".libsyn.com", ".buzzsprout.com", ".podbean.com",
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


# ────────────────────────────────────────────────── seeds & statuses
def _read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_seeds() -> dict:
    """Seed topics (spec: arbitrary buyer ideas beyond GSC data), each stored
    WITH its pre-expanded search queries so discovery runs are deterministic and
    the LLM expansion is a one-time cost per topic."""
    return _read_json(SEEDS_PATH, {"seeds": []})


def save_seeds(data: dict) -> None:
    _write_json(SEEDS_PATH, data)


def load_statuses() -> dict:
    """Per-domain outreach statuses: contacted / won / skipped. 'skipped' is the
    REJECTION MEMORY — a skipped domain is excluded from every future discovery
    run, so the operator never re-triages the same junk twice."""
    return _read_json(STATUS_PATH, {})


def save_statuses(data: dict) -> None:
    _write_json(STATUS_PATH, data)


def _map_seed_to_page(topic: str) -> str:
    """Best-matching page on OUR site for a seed topic (token overlap against
    title/H1/slug) so seed prospects still carry a concrete target page for the
    outreach angle when one genuinely fits; '' when nothing clears the bar —
    never force a bad mapping."""
    try:
        results = json.loads((PROJECT_ROOT / "data" / "latest_evaluation.json")
                             .read_text()).get("results", [])
    except (OSError, json.JSONDecodeError):
        return ""
    st = _tokens(topic)
    if not st:
        return ""
    best, best_score = "", 0.34   # >1/3 of the topic's tokens must match
    for r in results:
        pm = r.get("page_metadata", {}) or {}
        slug = (r.get("url", "").rsplit("/", 1)[-1]).replace("-", " ").replace(".html", "")
        ov = len(st & _tokens(" ".join([pm.get("title", ""), pm.get("h1", ""), slug]))) / len(st)
        if ov > best_score:
            best, best_score = r.get("url", ""), ov
    return best


def _seed_targets() -> list:
    """One direct-search target per expanded seed query. Seeds have no GSC
    demand data (impressions 0, no position) — scoring stays honest about that
    (they take the neutral demand factor, never a fabricated one)."""
    out = []
    for s in load_seeds().get("seeds", []):
        topic = (s.get("topic") or "").strip()
        if not topic:
            continue
        mapped = _map_seed_to_page(topic)
        for q in (s.get("queries") or [])[:SEED_MAX_QUERIES]:
            out.append({"url": mapped, "query": q, "impressions": 0, "position": None,
                        "trend": "n/a", "serp": {}, "_source": "seed",
                        "_seed": topic, "_direct": True})
    return out


# ────────────────────────────────────────────────── discovery
def _discovery_searches(query):
    """Bounded, deterministic operator searches for SHOULD-GET prospects: pages
    that curate/recommend resources on the query's topic. Ordered by expected
    yield for a product niche (gift guides are the biggest link-source genre);
    LINK_PROSPECT_SEARCHES_PER_QUERY controls how deep the list runs."""
    q = (query or "").strip()
    cands = [
        f"{q} gift guide",
        f"{q} resources",
        f"best {q} recommended",
        f"{q} for parents and teachers",
        f'intitle:resources {" ".join(q.split()[:3])}',
    ]
    return cands[:max(1, SEARCHES_PER_QUERY)]


def _should_get_for(w, own_forms, fetch_serp, log):
    """Serper discovery: topical resource/roundup pages. GSC-derived targets get
    the operator-pattern searches; seed targets (_direct) search their expanded
    buyer query verbatim (the expansion already produced guide-shaped queries)
    AND derive SERP peers inline so the can-get side works for seeds too."""
    found = []
    searches = [w["query"]] if w.get("_direct") else _discovery_searches(w["query"])
    for search in searches:
        serp = fetch_serp(search)
        if not serp:
            log.append(f"Serper unavailable/quota-out at '{search}' — should-get discovery partial.")
            break
        organic = (serp.get("organic_results") or [])[:10]
        if w.get("_direct") and not (w.get("serp") or {}).get("top5"):
            top5 = []
            for r in organic[:5]:
                dom = _domain(r.get("url", ""))
                if not dom:
                    continue
                kind = ("own" if any(f in dom for f in own_forms)
                        else "marketplace" if any(m in dom for m in MARKETPLACES)
                        else "content")
                top5.append({"position": r.get("position"), "domain": dom, "kind": kind})
            w["serp"] = {"top5": top5}
        for r in organic:
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
                    "detail": f"Ranks #{r.get('position')} for "
                              + (f"seed search \"{search}\"" if w.get("_direct")
                                 else f"discovery search \"{search}\""),
                    "source_url": r.get("url", ""),
                },
            })
    return found


def _can_get_for(w, own_forms, used_peers, fetch_referring_links, log, cap=None):
    """DataForSEO evidence: referring domains of the content-type SERP peers that
    outrank us for this query (peers come from the winnable page's cached SERP
    intel — no extra SERP spend). `cap` overrides peers-per-target (seed targets
    use 1 to keep the shared daily backlink budget for GSC-backed targets)."""
    found = []
    peers = [t for t in ((w.get("serp") or {}).get("top5") or [])
             if t.get("kind") == "content" and t.get("domain")
             and not _excluded(t["domain"], own_forms)]
    picked = 0
    for p in peers:
        if picked >= (cap or PEERS_PER_QUERY):
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


# Service-industry tokens in a DOMAIN NAME that has no business publishing kids'
# content: the guest-post-farm signature (an injury-law blog running "Montessori
# busy board benefits" exists to sell link placements). Soft signal: probability
# penalty + a visible risk note, never silent exclusion.
_OFF_NICHE_TOKENS = ("lawyer", "attorney", "injury", "legalservice", "casino",
                     "betting", "poker", "loan", "lending", "creditrepair",
                     "insurance", "crypto", "forex", "vpn", "webhosting",
                     "seoagency", "linkbuilding")


def _score_prospect(pr, query):
    """Two independent 0-100 scores + components, deterministic and inspectable."""
    title, snippet, dom = pr.get("page_title", ""), pr.get("snippet", ""), pr["domain"]
    overlap = _overlap_ratio(query, title, snippet, dom.replace("-", " ").replace(".", " "))
    is_resource = bool(_RESOURCE_RE.search(title + " " + pr.get("page_url", "")))
    peer_count = len({e.get("peer") for e in pr["evidence"] if e.get("kind") == "peer_backlink"})
    topical_hit = any(e.get("kind") == "topical_search" for e in pr["evidence"])
    dofollow = any("[dofollow]" in (e.get("detail") or "") for e in pr["evidence"])
    rank = max((pr.get("domain_rank") or 0), 0)  # DataForSEO 0-1000; 0 when unknown

    # Dead editorial: a title dated 2+ years back ("Gift Ideas 2011") is a page
    # nobody updates — the resource-page pattern is real but the door is closed.
    year_m = re.search(r"\b(20\d{2})\b", title or "")
    stale_year = int(year_m.group(1)) if year_m and int(year_m.group(1)) <= datetime.now().year - 2 else None
    # Off-niche service domain publishing on-topic content = likely paid-placement
    # farm; flag and discount, but keep visible so the operator decides.
    off_niche = any(t in dom.replace("-", "").replace(".", "") for t in _OFF_NICHE_TOKENS)
    # Forum/community thread: nofollow UGC — real referral/brand value, near-zero
    # link equity, so the link-value side takes a haircut.
    community = _is_community(dom, pr.get("page_url", ""))

    impact = (
        overlap * 40                                    # topical relevance — highest weight
        + min(rank / 1000.0, 1.0) * 25                  # measured authority when we have it
        + (12 if (topical_hit and not rank) else 0)     # topical SERP visibility as proxy when we don't
        + (20 if is_resource else 0)                    # contextual placement potential
        + (5 if dofollow else 0)
        - (15 if community else 0)
    )
    prob = (
        10                                              # base: qualified, non-excluded site
        + (35 if peer_count >= 2 else 20 if peer_count == 1 else 0)  # proven linker in THIS topic
        + (25 if is_resource else 0)                    # page type that routinely adds items
        + (10 if topical_hit else 0)                    # actively publishing/visible on the topic
        + (10 if dofollow else 0)                       # links editorially, not just UGC
        - (20 if stale_year else 0)                     # nobody updates an old-dated listicle
        - (15 if off_niche else 0)
    )
    impact = max(5.0, min(100.0, impact))
    prob = max(5.0, min(100.0, prob))
    return round(impact, 1), round(prob, 1), {
        "relevance_overlap": round(overlap, 2), "domain_rank": rank,
        "resource_page": is_resource, "peers_linked": peer_count,
        "topical_search_hit": topical_hit, "dofollow_evidence": dofollow,
        "stale_title_year": stale_year, "off_niche_domain": off_niche,
        "community": community,
    }


# DataForSEO's 0-1000 rank runs conservative — national outlets (Fox News, ABC)
# often sit in the 600s, so 700 let them slip into "work first". Env-tunable.
MEGA_RANK = _env_int("LINK_PROSPECT_MEGA_RANK", 550)


def _tier(impact, prob, rank=0, community=False):
    # Forum/community thread: never a LINK-campaign Tier 1/2 — links are
    # nofollow UGC. At best an "easier" referral/brand play.
    if community:
        return 3 if (prob >= 50 and impact >= 25) else 0
    # A mega-publisher (ABC News, Good Housekeeping…) is never "work first" or
    # an "easier win" — the probability model overrates them (they have resource
    # pages and peer links galore, and ignore cold pitches). Always Tier 2:
    # worth deliberate, personalized outreach, never the quick lane.
    mega = rank >= MEGA_RANK
    if impact >= 50 and prob >= 50:
        return 2 if mega else 1        # strong impact AND credible path — work first
    if impact >= 65:
        return 2                       # high-impact / harder — deliberate personalized outreach
    if prob >= 60 and impact >= 30:
        return 2 if mega else 3        # easier win — referring-domain breadth
    return 0                           # watchlist


def _angle(pr):
    """Deterministic outreach angle grounded in the actual evidence — never a
    claim we haven't verified (drafting a real email means reading their page)."""
    if pr.get("prospect_type") == "community":
        return ("Forum/community thread — do NOT email. Participate genuinely: "
                "answer the thread's question helpfully, disclose you run the shop, "
                "and share your page only if the thread invites suggestions. Links "
                "here are typically nofollow — the value is referral traffic and "
                "brand visibility, not SEO authority. Check the forum's "
                "self-promotion rules first.")
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
    own_bl = dfs.fetch_referring_links(_dom, limit=OWN_BACKLINK_DEPTH) if dfs.is_configured() else \
        {"available": False, "reason": "DataForSEO not configured"}
    if own_bl.get("available"):
        already = {lk["domain_from"] for lk in own_bl.get("links", []) if lk.get("domain_from")}
    else:
        log.append(f"Own-backlink check unavailable ({own_bl.get('reason')}) — "
                   "can't exclude domains that already link to you.")

    targets = winnable[:MAX_PAGES]
    # Also prospect for the Playbook's "Needs backlinks" striking-distance pages,
    # so the two features cross-link instead of talking past each other.
    targets = targets + _striking_external_targets({w["url"] for w in targets})
    # Seed topics (buyer ideas beyond GSC data) contribute direct-search targets.
    targets = targets + _seed_targets()
    if not targets:
        return {"available": False,
                "reason": "No target pages found — run Click Yield (winnable pages), "
                          "an evaluation (striking-distance pages), or add a seed "
                          "topic; they define what deserves link-building effort.",
                "generated_at": datetime.now().isoformat(timespec="seconds")}
    max_impr = max((w.get("impressions") or 1) for w in targets)
    used_peers: set = set()
    by_domain: dict = {}
    skipped_already = 0
    skipped_rejected = 0
    skipped_competitor = 0
    # Rejection memory: domains the operator explicitly skipped never come back.
    rejected = {d for d, v in load_statuses().items()
                if (v or {}).get("status") == "skipped"}

    for w in targets:
        raw = _should_get_for(w, own_forms, fetch_serp, log)
        if dfs.is_configured():
            raw += _can_get_for(w, own_forms, used_peers, dfs.fetch_referring_links, log,
                                cap=1 if w.get("_direct") else None)
        elif "DataForSEO not configured" not in " ".join(log):
            log.append("DataForSEO not configured — CAN-GET (peer-backlink) discovery skipped; "
                       "prospects below are SHOULD-GET only.")

        for cand in raw:
            dom = cand["domain"]
            if dom in already:
                skipped_already += 1
                continue
            if dom in rejected:
                skipped_rejected += 1
                continue
            if _is_competitor(dom):
                skipped_competitor += 1
                continue
            # Dedup by domain per target pairing; merge evidence, keep the
            # richest page info. Seeds may map to no site page, so fall back to
            # the seed topic to keep keys distinct across seeds.
            key = (dom, w["url"] or w.get("_seed") or w["query"])
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
                    "target_seed": w.get("_seed", ""),
                    "evidence": [],
                }
            pr["evidence"].append(cand["evidence"])
            pr["domain_rank"] = max(pr.get("domain_rank") or 0, cand.get("domain_rank") or 0)
            if cand.get("page_title") and not pr.get("page_title"):
                pr["page_title"], pr["page_url"] = cand["page_title"], cand.get("page_url", "")
                pr["snippet"] = cand.get("snippet", "")

    if skipped_competitor:
        log.append(f"Excluded {skipped_competitor} direct-competitor result(s) as prospects "
                   "— a rival won't link to us; their backlinks are still mined as peer "
                   "evidence (extend the list via LINK_PROSPECT_COMPETITORS).")

    prospects = []
    for pr in by_domain.values():
        impact, prob, comps = _score_prospect(pr, pr["target_query"])
        pr["impact"], pr["probability"], pr["components"] = impact, prob, comps
        pr["prospect_type"] = "community" if comps.get("community") else "publisher"
        url_opp = (pr["target_impressions"] or 0) / max_impr
        final = 100.0 * (impact / 100.0) ** IMPACT_EXP * (prob / 100.0) ** PROB_EXP
        pr["final_score"] = round(final * (0.6 + 0.4 * url_opp), 1)  # demand-scaled, floored at 60%
        pr["tier"] = _tier(impact, prob, comps.get("domain_rank") or 0,
                           community=comps.get("community", False))
        pr["angle"] = _angle(pr)
        # Visible risk notes — the operator decides, nothing is silently hidden.
        notes = []
        if comps.get("off_niche_domain"):
            notes.append("Off-niche service domain publishing on-topic content — "
                         "possible guest-post/paid-placement farm; check editorial "
                         "quality before outreach.")
        if comps.get("stale_title_year"):
            notes.append(f"Page title dated {comps['stale_title_year']} — likely "
                         "unmaintained; an addition request may go nowhere.")
        if notes:
            pr["risk_note"] = " ".join(notes)
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
            "excluded_rejected": skipped_rejected,
            "excluded_competitors": skipped_competitor,
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
