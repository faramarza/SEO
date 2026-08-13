"""DataForSEO client — real AI-Overview citation detection.

Serper's AI-Overview parsing is partial (Google loads AIO async), so serp_intel
can only flag that an AI Overview *exists*, never WHO it cites. DataForSEO's SERP
Advanced endpoint renders the AI Overview and returns its `references` — the exact
pages Google's generative answer cites. That turns "an AI Overview answers this
query" into the actionable signal: is it citing YOU, and if not, who?

Dormant until configured. Set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in the
environment (HTTP Basic auth). With no credentials every call degrades cleanly to
{"available": False, ...} — nothing else in the tool changes. Results are cached
with a TTL and a daily cap, exactly like the Serper client, so cost is bounded and
predictable. This module NEVER fabricates a citation: if the API returns no AI
Overview, that is reported as-is.

API: POST https://api.dataforseo.com/v3/serp/google/organic/live/advanced
Pricing (as of build): live Advanced is a few cents per query — keep the daily cap
small (default 50) and rely on the cache.
"""

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

_CACHE_PATH = Path(__file__).parent.parent.parent / "data" / "aio_citations.json"
_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"

DAILY_LIMIT = int(os.environ.get("DATAFORSEO_DAILY_LIMIT", "50"))
CACHE_DAYS = int(os.environ.get("DATAFORSEO_CACHE_DAYS", "14"))
# US English by default; overridable so the store's real market can be set.
LOCATION_CODE = int(os.environ.get("DATAFORSEO_LOCATION_CODE", "2840"))  # United States
LANGUAGE_CODE = os.environ.get("DATAFORSEO_LANGUAGE_CODE", "en")


def _creds():
    return (os.environ.get("DATAFORSEO_LOGIN", ""),
            os.environ.get("DATAFORSEO_PASSWORD", ""))


def is_configured() -> bool:
    login, password = _creds()
    return bool(login and password)


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            content = _CACHE_PATH.read_text().strip()
            return json.loads(content) if content else {"queries": {}, "daily_usage": {}}
        except (json.JSONDecodeError, OSError):
            return {"queries": {}, "daily_usage": {}}
    return {"queries": {}, "daily_usage": {}}


def _save_cache(cache: dict):
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2) + "\n")
    tmp.replace(_CACHE_PATH)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_fresh(entry: dict) -> bool:
    ts = (entry or {}).get("fetched_at")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() < CACHE_DAYS * 86400
    except Exception:
        return True


def get_daily_usage() -> int:
    return _load_cache().get("daily_usage", {}).get(_today(), 0)


def get_remaining_quota() -> int:
    return max(0, DAILY_LIMIT - get_daily_usage())


def get_cached(query: str) -> Optional[dict]:
    entry = _load_cache().get("queries", {}).get((query or "").lower().strip())
    return entry if (entry and _is_fresh(entry)) else None


def _domain(url: str) -> str:
    try:
        return (urlparse(url or "").netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def _walk_ai_overview(node, urls: list):
    """Recursively pull every cited URL out of an ai_overview item. DataForSEO
    nests citations in `references` (url/domain/title) and sometimes inline
    `links`/`url` fields inside the component `items`; collect them all."""
    if isinstance(node, dict):
        for key in ("references", "links", "items"):
            v = node.get(key)
            if isinstance(v, list):
                for it in v:
                    _walk_ai_overview(it, urls)
        u = node.get("url")
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
        dom = node.get("domain")
        if isinstance(dom, str) and dom and not node.get("url"):
            # A reference given as a bare domain (no full URL).
            urls.append("https://" + dom.lstrip("/"))
    elif isinstance(node, list):
        for it in node:
            _walk_ai_overview(it, urls)


def _extract_citations(result_items: list, own_domain: str) -> dict:
    """From a SERP result's items, find the AI Overview and its cited sources."""
    own = (own_domain or "").lower().replace("www.", "")
    aio_present = False
    urls: list = []
    for item in (result_items or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() in ("ai_overview", "ai_overview_reference"):
            aio_present = True
            _walk_ai_overview(item, urls)

    # Dedup, preserving order.
    seen, cited_urls = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            cited_urls.append(u)
    cited_domains, dom_seen = [], set()
    for u in cited_urls:
        d = _domain(u)
        if d and d not in dom_seen:
            dom_seen.add(d)
            cited_domains.append(d)

    own_urls = [u for u in cited_urls if own and own in _domain(u)]
    return {
        "ai_overview_present": aio_present,
        "cited_urls": cited_urls,
        "cited_domains": cited_domains,
        "own_cited": bool(own_urls),
        "own_urls": own_urls,
        "competitors_cited": [d for d in cited_domains if not (own and own in d)][:10],
    }


def fetch_aio_citation(query: str, own_domain: str = "") -> Optional[dict]:
    """Fetch the live AI Overview for `query` and report who it cites. Returns
    {"available": False, "reason": ...} when not configured / quota out / API error,
    so callers can degrade gracefully; a real result carries available=True."""
    if not is_configured():
        return {"available": False, "reason": "DataForSEO not configured — set "
                "DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD to enable AI-Overview "
                "citation detection."}

    cached = get_cached(query)
    if cached:
        return cached

    if get_remaining_quota() <= 0:
        return {"available": False, "reason": "DataForSEO daily cap reached "
                f"({DAILY_LIMIT}/day). Raise DATAFORSEO_DAILY_LIMIT or wait for reset."}

    import httpx
    login, password = _creds()
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    body = [{
        "keyword": query,
        "location_code": LOCATION_CODE,
        "language_code": LANGUAGE_CODE,
        "device": "desktop",
        "people_also_ask_click_depth": 0,
    }]
    try:
        resp = httpx.post(_ENDPOINT,
                          headers={"Authorization": f"Basic {token}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=30.0)
        if resp.status_code in (401, 403):
            return {"available": False, "reason": "DataForSEO auth failed — check "
                    "DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD."}
        if resp.status_code != 200:
            return {"available": False, "reason": f"DataForSEO API error {resp.status_code}."}
        data = resp.json()
    except Exception as e:
        return {"available": False, "reason": f"DataForSEO request failed: {e}"}

    # Navigate tasks[0].result[0].items[], defensively.
    try:
        task0 = (data.get("tasks") or [])[0]
        result0 = (task0.get("result") or [])[0]
        items = result0.get("items") or []
    except (IndexError, AttributeError, TypeError):
        items = []

    citations = _extract_citations(items, own_domain)
    out = {
        "available": True,
        "query": query,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **citations,
    }

    cache = _load_cache()
    cache.setdefault("queries", {})[query.lower().strip()] = out
    cache.setdefault("daily_usage", {})[_today()] = get_daily_usage() + 1
    _save_cache(cache)
    return out


def check_aio_batch(queries: list, own_domain: str = "") -> dict:
    """Check AI-Overview citations for several queries, respecting the daily cap.
    Returns a summary: which queries have an AIO, where you're cited vs not, and
    the competitor domains winning the citations. Degrades to available=False."""
    if not is_configured():
        return {"available": False, "reason": "DataForSEO not configured.",
                "results": []}
    results = []
    for q in queries:
        text = (q.get("query") if isinstance(q, dict) else str(q)) or ""
        if not text:
            continue
        r = fetch_aio_citation(text, own_domain)
        if r and r.get("available"):
            results.append(r)
        elif get_remaining_quota() <= 0:
            break

    with_aio = [r for r in results if r.get("ai_overview_present")]
    you_cited = [r for r in with_aio if r.get("own_cited")]
    comp_counts = {}
    for r in with_aio:
        for d in r.get("competitors_cited", []):
            comp_counts[d] = comp_counts.get(d, 0) + 1
    return {
        "available": True,
        "checked": len(results),
        "with_ai_overview": len(with_aio),
        "you_cited": len(you_cited),
        "you_absent": len(with_aio) - len(you_cited),
        "top_cited_competitors": dict(sorted(comp_counts.items(), key=lambda kv: -kv[1])[:10]),
        "results": results,
    }
