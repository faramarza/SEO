"""Link Targets — surgical 'where to point outreach' analysis.

For every page the tool has diagnosed as authority-blocked (striking-distance
rows whose only lever left is external links), measure the ACTUAL company it
would need to keep: the referring-domain counts of the pages ranking top-5
for its keywords, versus its own. Output is a RANGE ("≈ 10–30 more referring
domains"), never a promise — links are necessary there, not sufficient — and
the verdict is allowed to say the opposite ("parity: your links match the
top 5; the gap is content/relevance") or "marketplace-locked" when the top
slots are Amazon/Etsy and no link count displaces them.

Budgets: 1 Serper SERP + up to 4 DataForSEO backlink lookups per page, both
already cached and daily-capped in their clients — the refresh job simply
stops when a budget runs out and resumes next run.
"""

import fcntl
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

STORE_PATH = Path(__file__).parent.parent.parent / "data" / "link_targets.json"
FRESH_DAYS = 30
RD_LOOKUP_LIMIT = 300          # counts above this display as "300+"

# Results whose ranking cannot be emulated by earning links to a shop page:
# marketplaces (their authority is the platform) and social/UGC platforms.
NON_EMULABLE = ("amazon.", "etsy.", "walmart.", "target.com", "ebay.",
                "wayfair.", "temu.", "aliexpress.", "pinterest.", "youtube.",
                "facebook.", "instagram.", "reddit.", "wikipedia.", "tiktok.")


def load_store(path: Path = STORE_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("targets", {})
    return data


def save_store(store: dict, path: Path = STORE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2))
        tmp.replace(path)


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", (url or "").lower())
    return m.group(1) if m else ""


def is_emulable(url: str) -> bool:
    d = _domain(url)
    return bool(d) and not any(n in d for n in NON_EMULABLE)


def build_targets(striking_rows):
    """Group authority-blocked striking rows by PAGE → one target per page
    with all its blocked keywords. This is the 'which pages, which keywords'
    list; the gap fill adds the numbers."""
    by_url = {}
    for r in striking_rows or []:
        if r.get("lever") != "external":
            continue
        url = r.get("url", "")
        if not url:
            continue
        t = by_url.setdefault(url, {"url": url,
                                    "asset_type": r.get("asset_type", "other"),
                                    "keywords": [], "total_impressions": 0})
        t["keywords"].append({"query": r.get("query", ""),
                              "position": r.get("position", 0),
                              "impressions": r.get("impressions", 0) or 0,
                              "upside_clicks": r.get("upside_clicks", 0) or 0})
        t["total_impressions"] += r.get("impressions", 0) or 0
    for t in by_url.values():
        t["keywords"].sort(key=lambda k: -k["impressions"])
    return sorted(by_url.values(), key=lambda t: -t["total_impressions"])


def gap_verdict(own_rd, comp_rds, emulable_slots, total_slots):
    """(verdict, gap_lo, gap_hi, explanation). comp_rds = referring-domain
    counts of the emulable top-ranking pages, own_rd = ours. Deliberately a
    range and deliberately allowed to conclude links are NOT the fix."""
    if not comp_rds:
        if total_slots and emulable_slots == 0:
            return ("marketplace_locked", 0, 0,
                    "The top slots are marketplaces/platforms — no link count "
                    "displaces Amazon or Etsy. Cap expectations for this "
                    "keyword; win the remaining organic slots or its "
                    "long-tail variants instead.")
        return ("unknown", 0, 0, "No competitor link data yet.")
    lo, hi = min(comp_rds), max(comp_rds)
    med = sorted(comp_rds)[len(comp_rds) // 2]
    if own_rd >= hi:
        return ("parity", 0, 0,
                f"Your page already has {own_rd} referring domains vs the top "
                f"pages' {lo}–{hi}. More links are NOT the constraint here — "
                "the gap is content/relevance/intent. Spend effort on-page.")
    gap_lo = max(1, lo - own_rd)
    gap_hi = max(gap_lo, hi - own_rd)
    if gap_hi <= 5:
        v = "close"
        note = (f"Small gap: top pages hold {lo}–{hi} referring domains "
                f"(median {med}) vs your {own_rd}. A handful of good links "
                "puts you in their company — highest-feasibility target.")
    else:
        v = "authority_gap"
        note = (f"Top-5 pages hold {lo}–{hi} referring domains (median {med}) "
                f"vs your {own_rd}. Roughly {gap_lo}–{gap_hi} more quality "
                "referring domains puts this page in their company — "
                "necessary, not sufficient: content must stay competitive.")
    return (v, gap_lo, gap_hi, note)


# Effort ordering: feasible-and-valuable first, don't-bother last.
_VERDICT_RANK = {"close": 0, "authority_gap": 1, "unknown": 2, "pending": 2,
                 "parity": 3, "marketplace_locked": 4}


def rank_targets(targets):
    return sorted(targets, key=lambda t: (
        _VERDICT_RANK.get(t.get("verdict", "unknown"), 2),
        -(t.get("total_impressions") or 0)))


def is_fresh(entry) -> bool:
    try:
        return (datetime.now() - datetime.fromisoformat(entry["computed_at"])
                ) < timedelta(days=FRESH_DAYS)
    except (KeyError, ValueError, TypeError):
        return False


def compute_target(target, fetch_serp_fn, fetch_rd_fn):
    """Fill one target with SERP + link-gap data. fetch_serp_fn(query) →
    serp dict or None; fetch_rd_fn(url) → {"available", "links", "reason"}.
    Returns the enriched target; on budget exhaustion marks it pending with
    the reason so the screen shows WHY instead of silently missing data."""
    kw = target["keywords"][0]["query"] if target.get("keywords") else ""
    serp = fetch_serp_fn(kw) if kw else None
    if not serp:
        target.update({"verdict": "pending",
                       "note": "SERP fetch unavailable (Serper quota or key) — "
                               "will retry on the next refresh."})
        return target
    organic = (serp.get("organic_results") or serp.get("organic") or [])[:8]
    own_dom = _domain(target["url"])
    comps, non_emulable = [], 0
    for r in organic:
        u = r.get("url", "")
        if not u or _domain(u) == own_dom:
            continue
        if not is_emulable(u):
            non_emulable += 1
            continue
        comps.append({"url": u, "domain": _domain(u),
                      "position": r.get("position", 0)})
        if len(comps) >= 3:
            break
    target["serp_features"] = serp.get("serp_features") or serp.get("features") or []
    target["non_emulable_slots"] = non_emulable
    target["competitors"] = []

    own = fetch_rd_fn(target["url"])
    if not own.get("available"):
        target.update({"verdict": "pending",
                       "note": f"Backlink data unavailable: {own.get('reason', '')}"})
        return target
    own_rd = len(own.get("links") or [])
    target["own_rd"] = own_rd
    target["own_rd_capped"] = own_rd >= RD_LOOKUP_LIMIT

    comp_rds = []
    for c in comps:
        res = fetch_rd_fn(c["url"])
        if not res.get("available"):
            target.update({"verdict": "pending",
                           "note": f"Backlink data ran out mid-target: "
                                   f"{res.get('reason', '')} — resumes next run."})
            return target
        c["rd"] = len(res.get("links") or [])
        c["rd_capped"] = c["rd"] >= RD_LOOKUP_LIMIT
        comp_rds.append(c["rd"])
        target["competitors"].append(c)

    v, lo, hi, note = gap_verdict(own_rd, comp_rds, len(comps), len(organic))
    target.update({"verdict": v, "gap_lo": lo, "gap_hi": hi, "note": note,
                   "computed_at": datetime.now().isoformat(timespec="seconds")})
    return target
