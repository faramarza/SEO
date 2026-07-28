"""Site-specific benchmarks derived from the site's OWN data, replacing
industry-average assumptions (CTR curves, conversion rate, AOV) and hardcoded
thresholds. Everything here is measured from the latest evaluation + GA4, with
sane fallbacks when the site has too little data to be trusted.

Pure/simple functions over the evaluation `results` list so it's testable and
usable from both the web layer and the workflow. Results are cached per
evaluation timestamp so repeated calls in one request are cheap.
"""

import json
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent.parent / "data"

# Industry-average organic CTR by position — used ONLY as a fallback where the
# site itself has too little data at a given position to be reliable.
_FALLBACK_CTR = {
    1: 0.27, 2: 0.16, 3: 0.11, 4: 0.08, 5: 0.06,
    6: 0.05, 7: 0.04, 8: 0.032, 9: 0.028, 10: 0.025,
}
# Minimum impressions at a position before we trust the site's own CTR there.
_MIN_IMPR_FOR_CTR = 150

_CACHE = {"ts": None, "data": None}


def _fallback_ctr(pos: int) -> float:
    p = int(round(pos))
    if p in _FALLBACK_CTR:
        return _FALLBACK_CTR[p]
    if p <= 10:
        return 0.025
    return max(0.002, 0.02 / (p - 9))


def site_ctr_by_position(results: list) -> dict:
    """Actual impression-weighted CTR at each integer SERP position, from every
    page's GSC queries. Returns {position: {ctr, impressions, clicks, source}}
    where source is 'site' (enough data) or 'fallback' (industry benchmark)."""
    agg = {}  # pos -> [impr, clicks]
    for r in results:
        for q in r.get("top_queries", []) or []:
            pos = q.get("position", 0) or 0
            if pos <= 0:
                continue
            p = int(round(pos))
            impr = q.get("impressions", 0) or 0
            clicks = q.get("clicks", 0) or 0
            slot = agg.setdefault(p, [0, 0])
            slot[0] += impr
            slot[1] += clicks
    curve = {}
    for p in range(1, 31):
        impr, clicks = agg.get(p, [0, 0])
        if impr >= _MIN_IMPR_FOR_CTR and impr > 0:
            curve[p] = {"ctr": round(clicks / impr, 4), "impressions": impr,
                        "clicks": clicks, "source": "site"}
        else:
            curve[p] = {"ctr": round(_fallback_ctr(p), 4), "impressions": impr,
                        "clicks": clicks, "source": "fallback"}
    return curve


def expected_ctr(position: float, curve: dict = None) -> float:
    """Expected CTR at a (possibly fractional) position, from the site curve
    when available, else the industry fallback."""
    if position is None or position <= 0:
        return 0.0
    p = int(round(position))
    if curve and p in curve:
        return curve[p]["ctr"]
    if curve and p > 30:
        # extrapolate from the deepest measured slot
        deep = curve.get(30) or {}
        return deep.get("ctr", _fallback_ctr(p))
    return _fallback_ctr(p)


def site_expected_ctr(position: float) -> float:
    """Expected CTR at a position from the site's OWN curve (cached, read from
    latest_evaluation), with per-position industry fallback baked in."""
    bm = compute_site_benchmarks()
    curve = bm.get("ctr_by_position") if bm.get("available") else None
    return expected_ctr(position, curve)


def site_conversion_and_aov(results: list) -> dict:
    """Site conversion rate and AOV from GA4 across all pages, plus per
    asset_type where there's enough volume. Falls back to config-ish defaults
    when GA4 is too sparse."""
    tot_sessions = tot_purchases = tot_revenue = 0.0
    by_type = {}
    for r in results:
        s = r.get("ga4_sessions", 0) or 0
        p = r.get("ga4_purchases", 0) or 0
        rev = r.get("ga4_revenue", 0) or 0
        tot_sessions += s
        tot_purchases += p
        tot_revenue += rev
        t = (r.get("asset_type") or "other").lower()
        e = by_type.setdefault(t, [0.0, 0.0, 0.0])
        e[0] += s
        e[1] += p
        e[2] += rev

    cvr = (tot_purchases / tot_sessions) if tot_sessions >= 100 and tot_purchases > 0 else None
    aov = (tot_revenue / tot_purchases) if tot_purchases >= 5 and tot_revenue > 0 else None

    type_cvr = {}
    for t, (s, p, rev) in by_type.items():
        if s >= 100 and p > 0:
            type_cvr[t] = round(p / s, 4)

    return {
        "site_cvr": round(cvr, 4) if cvr is not None else None,
        "site_aov": round(aov, 2) if aov is not None else None,
        "cvr_by_type": type_cvr,
        "sessions": int(tot_sessions),
        "purchases": round(tot_purchases, 1),
        "revenue": round(tot_revenue, 2),
    }


def impression_percentiles(results: list) -> dict:
    """Percentiles of per-page GSC impressions, so demand thresholds can be set
    from the site's own distribution rather than fixed numbers."""
    vals = sorted((r.get("gsc_impressions", 0) or 0) for r in results
                  if (r.get("gsc_impressions", 0) or 0) > 0)
    if len(vals) < 20:
        return {}

    def pct(q):
        i = min(len(vals) - 1, int(q * len(vals)))
        return vals[i]

    return {"p50": pct(0.50), "p75": pct(0.75), "p90": pct(0.90),
            "p95": pct(0.95), "max": vals[-1], "n": len(vals)}


def compute_site_benchmarks(results: list = None) -> dict:
    """All site benchmarks in one dict. Reads latest_evaluation when `results`
    isn't supplied; caches per evaluation timestamp."""
    ts = None
    if results is None:
        path = DATA_PATH / "latest_evaluation.json"
        if not path.exists():
            return {"available": False}
        try:
            with open(path) as f:
                ev = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"available": False}
        ts = ev.get("timestamp")
        if _CACHE["ts"] == ts and _CACHE["data"] is not None:
            return _CACHE["data"]
        results = ev.get("results", [])

    conv = site_conversion_and_aov(results)
    out = {
        "available": True,
        "ctr_by_position": site_ctr_by_position(results),
        "impression_percentiles": impression_percentiles(results),
        **conv,
    }
    if ts is not None:
        _CACHE["ts"] = ts
        _CACHE["data"] = out
    return out
