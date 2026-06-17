"""Moz Links API v2 client with local caching (30-day TTL).

Usage:
    client = MozClient()  # reads MOZ_API_TOKEN from env
    metrics = client.get_url_metrics("https://example.com/page")
    # Returns: {"domain_authority": 45, "page_authority": 32, ...}

Auth: Basic Auth via base64-encoded "access_id:secret_key" token.
Rate budget: 50 calls/month — aggressive caching is essential.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

# Cache lives alongside other data files
_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_CACHE_PATH = _DATA_DIR / "moz_cache.json"
_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

_MOZ_ENDPOINT = "https://lsapi.seomoz.com/v2/url_metrics"


class MozClient:
    """Thin wrapper around Moz Links API v2 with persistent JSON cache."""

    def __init__(self, api_token: Optional[str] = None):
        """
        Args:
            api_token: Base64-encoded "access_id:secret_key" for Basic Auth.
                       Falls back to MOZ_API_TOKEN env var.
        """
        self.api_token = api_token or os.environ.get("MOZ_API_TOKEN", "")
        self._cache = self._load_cache()

    # ── Public API ──────────────────────────────────────────────

    def test_connection(self) -> bool:
        """Verify credentials with a minimal API call."""
        if not self.api_token:
            return False
        try:
            resp = requests.post(
                _MOZ_ENDPOINT,
                headers=self._auth_headers(),
                json={"targets": ["https://moz.com"]},
                timeout=15,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_url_metrics(self, url: str) -> dict:
        """Get authority metrics for a single URL.

        Returns dict with keys:
            domain_authority, page_authority, spam_score,
            root_domains_to_page, external_pages_to_page,
            last_crawled (ISO str), cached (bool)

        Returns empty dict on failure.
        """
        cached = self._get_cached(url)
        if cached is not None:
            cached["cached"] = True
            return cached

        raw = self._fetch_url_metrics([url])
        if not raw:
            return {}

        metrics = self._extract_metrics(raw[0] if raw else {})
        if metrics:
            self._set_cached(url, metrics)
            metrics["cached"] = False
        return metrics

    def get_bulk_metrics(self, urls: list[str]) -> dict[str, dict]:
        """Get authority metrics for multiple URLs (up to 50).

        Returns {url: metrics_dict} for each URL.
        Uses cache where available; only fetches uncached URLs.
        """
        results = {}
        uncached_urls = []

        for u in urls:
            cached = self._get_cached(u)
            if cached is not None:
                cached["cached"] = True
                results[u] = cached
            else:
                uncached_urls.append(u)

        if uncached_urls:
            # Moz API allows up to 50 targets per call
            for batch_start in range(0, len(uncached_urls), 50):
                batch = uncached_urls[batch_start:batch_start + 50]
                raw_list = self._fetch_url_metrics(batch)
                for i, raw in enumerate(raw_list):
                    metrics = self._extract_metrics(raw)
                    if metrics:
                        batch_url = batch[i]
                        self._set_cached(batch_url, metrics)
                        metrics["cached"] = False
                        results[batch_url] = metrics

        return results

    # ── Internal ────────────────────────────────────────────────

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Basic {self.api_token}",
            "Content-Type": "application/json",
        }

    def _fetch_url_metrics(self, targets: list[str]) -> list[dict]:
        """POST to Moz API and return raw results list."""
        if not self.api_token:
            print("[MozClient] No API token configured")
            return []
        try:
            resp = requests.post(
                _MOZ_ENDPOINT,
                headers=self._auth_headers(),
                json={"targets": targets},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("results", [])
            else:
                print(f"[MozClient] API error {resp.status_code}: {resp.text[:200]}")
                return []
        except Exception as exc:
            print(f"[MozClient] Request failed: {exc}")
            return []

    @staticmethod
    def _extract_metrics(raw: dict) -> dict:
        """Pull relevant fields from a Moz API result object."""
        if not raw:
            return {}
        return {
            "domain_authority": raw.get("domain_authority", 0),
            "page_authority": raw.get("page_authority", 0),
            "spam_score": raw.get("spam_score", 0),
            "root_domains_to_page": raw.get("root_domains_to_page", 0),
            "external_pages_to_page": raw.get("external_pages_to_page", 0),
            "fetched_at": datetime.now().isoformat(),
        }

    # ── Cache management ────────────────────────────────────────

    def _load_cache(self) -> dict:
        """Load cache from disk. Returns empty dict on any error."""
        if _CACHE_PATH.exists():
            try:
                with open(_CACHE_PATH) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        """Persist cache to disk."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(_CACHE_PATH, "w") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as exc:
            print(f"[MozClient] Cache save failed: {exc}")

    def _get_cached(self, url: str) -> Optional[dict]:
        """Return cached metrics if fresh (within TTL), else None."""
        entry = self._cache.get(url)
        if not entry:
            return None
        fetched_at = entry.get("fetched_at", "")
        if not fetched_at:
            return None
        try:
            fetched_ts = datetime.fromisoformat(fetched_at).timestamp()
            if time.time() - fetched_ts > _CACHE_TTL_SECONDS:
                return None  # Expired
        except (ValueError, TypeError):
            return None
        # Return copy without mutating cache
        return dict(entry)

    def _set_cached(self, url: str, metrics: dict):
        """Store metrics in cache and persist."""
        self._cache[url] = dict(metrics)
        self._save_cache()
