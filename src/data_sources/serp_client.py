"""
Google Custom Search API client for SERP competitor analysis.

Fetches actual search results for target queries to understand:
- What competitor titles/descriptions look like
- Which SERP features are present (shopping, featured snippets, etc.)
- How the site's listing compares to competitors

Free tier: 100 queries/day.
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
        with open(SERP_CACHE_PATH) as f:
            return json.load(f)
    return {"queries": {}, "daily_usage": {}}


def _save_cache(cache: dict):
    SERP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SERP_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
        f.write("\n")


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


def fetch_serp(query: str, api_key: str = "", cx: str = "") -> Optional[dict]:
    """Fetch SERP results for a query via Google Custom Search API.

    Returns dict with organic results, SERP features detected, and metadata.
    Returns None if quota exhausted or API error.
    """
    import httpx

    if not api_key:
        api_key = os.environ.get("GOOGLE_CSE_API_KEY", "")
    if not cx:
        cx = os.environ.get("GOOGLE_CSE_CX", "")

    if not api_key or not cx:
        return None

    cache = _load_cache()
    today = _today()
    daily_count = cache.get("daily_usage", {}).get(today, 0)

    if daily_count >= DAILY_LIMIT:
        return None

    # Check if we already have recent data (< 7 days old)
    query_key = query.lower().strip()
    existing = cache.get("queries", {}).get(query_key)
    if existing:
        fetched_at = existing.get("fetched_at", "")
        if fetched_at:
            try:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).days
                if age_days < 7:
                    return existing
            except (ValueError, TypeError):
                pass

    try:
        resp = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": api_key,
                "cx": cx,
                "q": query,
                "num": 10,
                "gl": "us",
                "hl": "en",
            },
            timeout=15.0,
        )

        if resp.status_code == 429:
            return None
        if resp.status_code != 200:
            print(f"[SERP] API error {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
    except Exception as e:
        print(f"[SERP] Request failed for '{query}': {e}")
        return None

    # Parse results
    organic_results = []
    for item in data.get("items", []):
        organic_results.append({
            "position": len(organic_results) + 1,
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
            "url": item.get("link", ""),
            "display_url": item.get("formattedUrl", ""),
        })

    # Detect SERP features from search information
    search_info = data.get("searchInformation", {})
    spelling = data.get("spelling", {})

    serp_features = []
    # Check for rich snippets in results
    for item in data.get("items", []):
        pagemap = item.get("pagemap", {})
        if pagemap.get("product"):
            serp_features.append("product_rich_snippet")
        if pagemap.get("review") or pagemap.get("aggregaterating"):
            serp_features.append("review_stars")
        if pagemap.get("videoobject"):
            serp_features.append("video")
        if pagemap.get("recipe"):
            serp_features.append("recipe")
    serp_features = list(set(serp_features))

    result = {
        "query": query,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_results": int(search_info.get("totalResults", 0)),
        "organic_results": organic_results,
        "serp_features": serp_features,
        "spelling_suggestion": spelling.get("correctedQuery"),
    }

    # Update cache
    if "queries" not in cache:
        cache["queries"] = {}
    if "daily_usage" not in cache:
        cache["daily_usage"] = {}

    cache["queries"][query_key] = result
    cache["daily_usage"][today] = daily_count + 1
    _save_cache(cache)

    return result


def fetch_serp_batch(queries: list[str], api_key: str = "", cx: str = "") -> list[dict]:
    """Fetch SERP results for multiple queries, respecting daily quota.

    Returns list of results (may be shorter than input if quota runs out).
    Sleeps 200ms between requests to be polite.
    """
    results = []
    for q in queries:
        if get_remaining_quota() <= 0:
            break
        result = fetch_serp(q, api_key, cx)
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
    queries_total = min(len(queries), 5)  # Top 5 queries

    for q in queries[:5]:
        query_text = q.get("query", "")
        cached = get_cached_serp(query_text)
        if cached:
            queries_with_data += 1
            # Find our position in SERP
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
