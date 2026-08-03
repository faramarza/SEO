"""
Serper.dev API client for SERP competitor analysis.

Fetches actual Google search results for target queries to understand:
- What competitor titles/descriptions look like
- Which SERP features are present (featured snippets, shopping, etc.)
- How the site's listing compares to competitors

Pricing: $1 per 1,000 queries. 2,500 free credits on signup.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SERP_CACHE_PATH = Path(__file__).parent.parent.parent / "data" / "serp_cache.json"
DAILY_LIMIT = 100


def _load_cache() -> dict:
    if SERP_CACHE_PATH.exists():
        try:
            with open(SERP_CACHE_PATH) as f:
                content = f.read().strip()
                if not content:
                    return {"queries": {}, "daily_usage": {}}
                return json.loads(content)
        except (json.JSONDecodeError, OSError):
            return {"queries": {}, "daily_usage": {}}
    return {"queries": {}, "daily_usage": {}}


def _save_cache(cache: dict):
    SERP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = SERP_CACHE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(cache, f, indent=2)
        f.write("\n")
    tmp_path.replace(SERP_CACHE_PATH)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_daily_usage() -> int:
    cache = _load_cache()
    return cache.get("daily_usage", {}).get(_today(), 0)


def get_remaining_quota() -> int:
    return max(0, DAILY_LIMIT - get_daily_usage())


def get_cached_serp(query: str) -> Optional[dict]:
    cache = _load_cache()
    return cache.get("queries", {}).get(query.lower().strip())


def get_cached_query_set() -> set:
    """Return the set of all cached query keys in one file read.

    Avoids the N+1 pattern where callers checking SERP coverage for many
    pages call get_cached_serp() (a full file read+parse) once per query.
    """
    cache = _load_cache()
    return set(cache.get("queries", {}).keys())


def fetch_serp(query: str, api_key: str = "") -> Optional[dict]:
    """Fetch SERP results for a query via Serper.dev Google Search API.

    Returns dict with organic results, SERP features detected, and metadata.
    Returns None if quota exhausted or API error.
    """
    import httpx

    if not api_key:
        api_key = os.environ.get("SERPER_API_KEY", "")

    if not api_key:
        return None

    cache = _load_cache()
    today = _today()
    daily_count = cache.get("daily_usage", {}).get(today, 0)

    if daily_count >= DAILY_LIMIT:
        return None

    query_key = query.lower().strip()
    existing = cache.get("queries", {}).get(query_key)
    if existing:
        return existing

    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={
                "q": query,
                "num": 10,
                "gl": "us",
                "hl": "en",
            },
            timeout=15.0,
        )

        if resp.status_code == 429:
            return None
        if resp.status_code == 401 or resp.status_code == 403:
            print(f"[SERP] Serper API key invalid or quota exhausted")
            return None
        if resp.status_code != 200:
            print(f"[SERP] API error {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
    except Exception as e:
        print(f"[SERP] Request failed for '{query}': {e}")
        return None

    organic_results = []
    for item in data.get("organic", []):
        organic_results.append({
            "position": item.get("position", len(organic_results) + 1),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "url": item.get("link", ""),
            "display_url": item.get("link", ""),
        })

    serp_features = []
    if data.get("answerBox"):
        serp_features.append("featured_snippet")
    if data.get("knowledgeGraph"):
        serp_features.append("knowledge_graph")
    if data.get("shopping"):
        serp_features.append("shopping")
    if data.get("topStories"):
        serp_features.append("top_stories")
    if data.get("videos"):
        serp_features.append("video")
    if data.get("images"):
        serp_features.append("images")
    if data.get("peopleAlsoAsk"):
        serp_features.append("people_also_ask")
    # Opportunistic: Serper's AIO parsing is partial (Google loads it async), but if
    # it IS present we flag it. For reliable AI-Overview citation data, a purpose-
    # built API (DataForSEO Google Organic SERP) is the phase-2 source.
    if data.get("aiOverview") or data.get("ai_overview"):
        serp_features.append("ai_overview")
    if data.get("relatedSearches"):
        serp_features.append("related_searches")

    search_info = data.get("searchParameters", {})

    result = {
        "query": query,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_results": data.get("searchInformation", {}).get("totalResults", 0),
        "organic_results": organic_results,
        "serp_features": serp_features,
        "spelling_suggestion": search_info.get("autocorrect"),
        "people_also_ask": [p.get("question", "") for p in data.get("peopleAlsoAsk", [])],
    }

    if "queries" not in cache:
        cache["queries"] = {}
    if "daily_usage" not in cache:
        cache["daily_usage"] = {}

    cache["queries"][query_key] = result
    cache["daily_usage"][today] = daily_count + 1
    _save_cache(cache)

    return result


def fetch_serp_batch(queries: list[str], api_key: str = "") -> list[dict]:
    """Fetch SERP results for multiple queries, respecting daily quota.

    Returns list of results (may be shorter than input if quota runs out).
    Sleeps 200ms between requests to be polite.
    """
    results = []
    for q in queries:
        if get_remaining_quota() <= 0:
            break
        result = fetch_serp(q, api_key)
        if result:
            results.append(result)
            time.sleep(0.2)
    return results


def get_serp_summary_for_opportunity(opportunity: dict) -> Optional[dict]:
    """Get aggregated SERP data for an opportunity's top queries.

    Returns a summary with competitor analysis ready for AI consumption.
    """
    queries = opportunity.get("top_queries", [])
    if not queries:
        return None

    url = opportunity.get("url", "")
    serp_data = []
    queries_with_data = 0
    queries_total = min(len(queries), 5)

    for q in queries[:5]:
        query_text = q.get("query", "")
        cached = get_cached_serp(query_text)
        if cached:
            queries_with_data += 1
            our_position = None
            for r in cached.get("organic_results", []):
                if url.rstrip("/") in r.get("url", "").rstrip("/"):
                    our_position = r["position"]
                    break

            competitors = []
            for r in cached.get("organic_results", [])[:5]:
                if url.rstrip("/") not in r.get("url", "").rstrip("/"):
                    competitors.append({
                        "position": r["position"],
                        "title": r["title"],
                        "snippet": r["snippet"][:200],
                        "url": r["url"],
                    })

            serp_data.append({
                "query": query_text,
                "impressions": q.get("impressions", 0),
                "our_position_gsc": q.get("position", 0),
                "our_position_serp": our_position,
                "competitors": competitors[:4],
                "serp_features": cached.get("serp_features", []),
                "spelling_suggestion": cached.get("spelling_suggestion"),
                "people_also_ask": cached.get("people_also_ask", []),
                "fetched_at": cached.get("fetched_at"),
            })

    if not serp_data:
        return None

    return {
        "queries_with_serp_data": queries_with_data,
        "queries_total": queries_total,
        "coverage": round(queries_with_data / max(queries_total, 1) * 100),
        "serp_results": serp_data,
    }
