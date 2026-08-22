"""Chrome UX Report (CrUX) API client with local caching.

Fetches real-user Core Web Vitals data (LCP, FID/INP, CLS) for URLs.
The CrUX API is free but requires a Google API key.
Rate limit: 150 queries/minute.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_CACHE_PATH = _DATA_DIR / "crux_cache.json"
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

_CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"


class CrUXClient:
    """Thin wrapper around CrUX API with persistent JSON cache."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY", "")
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        if _CACHE_PATH.exists():
            try:
                with open(_CACHE_PATH) as f:
                    content = f.read().strip()
                    if not content:
                        return {}
                    return json.loads(content)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_cache(self):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self._cache, f, indent=2)
            f.write("\n")
        tmp.replace(_CACHE_PATH)

    def get_cwv(self, url: str) -> Optional[dict]:
        """Get Core Web Vitals for a URL. Returns cached data if fresh."""
        if not self.api_key:
            return None

        cache_key = url.lower().rstrip("/")
        cached = self._cache.get(cache_key)
        if cached:
            fetched_at = cached.get("fetched_at", 0)
            if time.time() - fetched_at < _CACHE_TTL_SECONDS:
                return cached

        try:
            resp = requests.post(
                _CRUX_ENDPOINT,
                params={"key": self.api_key},
                json={"url": url, "formFactor": "PHONE"},
                timeout=10,
            )

            if resp.status_code == 404:
                # No CrUX data for this URL - try origin level
                return self._get_origin_cwv(url)
            if resp.status_code != 200:
                return None

            data = resp.json()
            metrics = data.get("record", {}).get("metrics", {})

            result = {
                "url": url,
                "fetched_at": time.time(),
                "level": "url",
                "lcp_ms": self._extract_p75(metrics.get("largest_contentful_paint", {})),
                "inp_ms": self._extract_p75(metrics.get("interaction_to_next_paint", {})),
                "cls": self._extract_p75(metrics.get("cumulative_layout_shift", {})),
                "fcp_ms": self._extract_p75(metrics.get("first_contentful_paint", {})),
                "ttfb_ms": self._extract_p75(metrics.get("time_to_first_byte", {})),
            }

            # Assess each metric
            result["lcp_rating"] = self._rate_lcp(result["lcp_ms"])
            result["inp_rating"] = self._rate_inp(result["inp_ms"])
            result["cls_rating"] = self._rate_cls(result["cls"])
            result["overall_rating"] = self._overall_rating(result)

            self._cache[cache_key] = result
            self._save_cache()
            return result

        except Exception as e:
            print(f"[CrUX] Error fetching {url}: {e}")
            return None

    def _get_origin_cwv(self, url: str) -> Optional[dict]:
        """Fall back to origin-level CrUX data."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        cache_key = f"origin:{origin.lower()}"
        cached = self._cache.get(cache_key)
        if cached and time.time() - cached.get("fetched_at", 0) < _CACHE_TTL_SECONDS:
            return cached

        try:
            resp = requests.post(
                _CRUX_ENDPOINT,
                params={"key": self.api_key},
                json={"origin": origin, "formFactor": "PHONE"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            metrics = data.get("record", {}).get("metrics", {})

            result = {
                "url": url,
                "fetched_at": time.time(),
                "level": "origin",
                "lcp_ms": self._extract_p75(metrics.get("largest_contentful_paint", {})),
                "inp_ms": self._extract_p75(metrics.get("interaction_to_next_paint", {})),
                "cls": self._extract_p75(metrics.get("cumulative_layout_shift", {})),
                "fcp_ms": self._extract_p75(metrics.get("first_contentful_paint", {})),
                "ttfb_ms": self._extract_p75(metrics.get("time_to_first_byte", {})),
            }

            result["lcp_rating"] = self._rate_lcp(result["lcp_ms"])
            result["inp_rating"] = self._rate_inp(result["inp_ms"])
            result["cls_rating"] = self._rate_cls(result["cls"])
            result["overall_rating"] = self._overall_rating(result)

            self._cache[cache_key] = result
            self._save_cache()
            return result

        except Exception:
            return None

    def _extract_p75(self, metric: dict) -> Optional[float]:
        """Extract the 75th percentile value from a CrUX metric. CrUX returns some
        percentiles (notably CLS) as STRINGS (e.g. "0.05"); coerce to float so the
        `<=` rating comparisons don't raise TypeError and silently drop all CWV."""
        p75 = metric.get("percentiles", {}).get("p75")
        if p75 is None:
            return None
        try:
            return float(p75)
        except (TypeError, ValueError):
            return None

    def _rate_lcp(self, ms: Optional[float]) -> str:
        if ms is None:
            return "unknown"
        if ms <= 2500:
            return "good"
        if ms <= 4000:
            return "needs_improvement"
        return "poor"

    def _rate_inp(self, ms: Optional[float]) -> str:
        if ms is None:
            return "unknown"
        if ms <= 200:
            return "good"
        if ms <= 500:
            return "needs_improvement"
        return "poor"

    def _rate_cls(self, score: Optional[float]) -> str:
        if score is None:
            return "unknown"
        if score <= 0.1:
            return "good"
        if score <= 0.25:
            return "needs_improvement"
        return "poor"

    def _overall_rating(self, result: dict) -> str:
        ratings = [result.get("lcp_rating", "unknown"),
                   result.get("inp_rating", "unknown"),
                   result.get("cls_rating", "unknown")]
        known = [r for r in ratings if r != "unknown"]
        if not known:
            return "unknown"
        if "poor" in known:
            return "poor"
        if "needs_improvement" in known:
            return "needs_improvement"
        return "good"

    def get_cwv_batch(self, urls: list[str]) -> dict[str, dict]:
        """Fetch CWV for multiple URLs. Returns dict keyed by URL."""
        results = {}
        for url in urls:
            cwv = self.get_cwv(url)
            if cwv:
                results[url] = cwv
            time.sleep(0.1)  # Be polite
        return results
