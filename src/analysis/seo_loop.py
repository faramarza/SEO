"""SEO loop — unattended select → act → record → verify → learn.

Design (agreed with the operator, Sept 2026):
- SELECT from the same data the Governor already trusts: CTR-recovery rows
  (page 1 only — below it a perfect title earns nothing) and winnable rows
  whose position drifted 3+ places down. Exclusions: recently-changed pages,
  duplicate-loser URLs, brand queries, system pages, pages with revenue in
  the window (the pages you can least afford to break — we use the 28-day
  revenue window as a conservative stand-in for "ordered in the last 7 days"),
  and anything reverted in the last 60 days.
- ACT is meta_title + meta_description ONLY, via the structurally-scoped
  Magento client. One treatment per experiment.
- VERIFY at 28 days (Google needs to recrawl and re-render the snippet; 14 is
  too short), normalized by the site-wide trend, with ASYMMETRIC thresholds:
  keep on decent evidence, revert only on confirmed, sustained harm — a wrong
  keep costs little, a wrong revert costs the experiment AND adds title churn
  Google penalizes.
- LEARN: the last outcomes are summarized into the rewrite prompt.

State lives in data/seo_loop.json (same JSON-file architecture as the rest of
the tool). The web layer owns scheduling, LLM generation, and Magento calls;
this module owns the decisions.
"""

import json
import fcntl
from datetime import datetime, timedelta
from pathlib import Path

from src.analysis.site_benchmarks import achievable_ctr
from src.analysis.growth_playbook import _is_system_page

STATE_PATH = Path(__file__).parent.parent.parent / "data" / "seo_loop.json"

# Selection tunables (env-overridable via the web layer if ever needed).
MIN_IMPRESSIONS = 200
CTR_UNDERPERFORMANCE = 0.70    # actual < 70% of the site's achievable CTR
DRIFT_POSITIONS = 3.0          # defend pages sliding 3+ spots
RECENT_CHANGE_DAYS = 21        # don't touch pages changed recently
REVERT_EXCLUDE_DAYS = 60
VERIFY_AFTER_DAYS = 28
WRITES_PER_WEEK = 5
WRITES_PER_DAY = 1

# Verify thresholds — deliberately asymmetric.
KEEP_CTR_LIFT = 1.10           # normalized CTR ratio to call it a win
KEEP_POS_GAIN = 0.8            # or position improved by this many spots
REVERT_POS_LOSS = 2.0          # revert path needs a real slide...
REVERT_CONFIRM_DAYS = 7        # ...confirmed again a week later

BRAND_HINTS = ("alphabet train", "alphabet-trains", "alphabettrains")


# ----------------------------------------------------------------- state i/o
def load_state(path: Path = STATE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"settings": {"enabled": False, "mode": "shadow"}, "experiments": []}


def save_state(state: dict, path: Path = STATE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- selection
def _is_brand(q):
    ql = (q or "").lower()
    return any(b in ql for b in BRAND_HINTS)


def _excluded_urls(state) -> dict:
    """url -> reason, for every exclusion the loop itself creates."""
    out = {}
    now = datetime.now()
    for e in state.get("experiments", []):
        url = e.get("url", "")
        st = e.get("status")
        if st in ("proposed", "applied", "suspect"):
            out[url] = "open experiment"
        elif st == "reverted":
            try:
                until = datetime.fromisoformat(e.get("reverted_at", "")) + \
                    timedelta(days=REVERT_EXCLUDE_DAYS)
                if now < until:
                    out[url] = f"reverted — excluded until {until.date()}"
            except (ValueError, TypeError):
                out[url] = "reverted recently"
        else:  # kept/neutral/failed — still respect the recent-change window
            try:
                changed = datetime.fromisoformat(e.get("applied_at") or e.get("created_at", ""))
                if now - changed < timedelta(days=RECENT_CHANGE_DAYS):
                    out[url] = "changed recently"
            except (ValueError, TypeError):
                pass
    return out


def select_candidates(ctr_rows, winnable_rows, state, revenue_by_url=None,
                      dup_losers=None, extra_excluded=None, limit=WRITES_PER_WEEK):
    """Rank the pages worth an unattended meta rewrite this week.

    ctr_rows: find_ctr_recovery rows (already page-1-only, impressions-floored
    upstream — re-checked here). winnable_rows: click-yield winnable pages with
    trend/position_delta for the drift arm. revenue_by_url: url -> 28d revenue
    (pages with any revenue are untouchable). Returns candidate dicts, best
    first, each with a selection reason — never more than `limit`.
    """
    revenue_by_url = revenue_by_url or {}
    dup_losers = dup_losers or set()
    excluded = dict(extra_excluded or {})
    excluded.update(_excluded_urls(state))

    cands = {}

    def blocked(url, query=""):
        if not url or url in cands:
            return "dup"
        if url in excluded:
            return excluded[url]
        if url in dup_losers:
            return "duplicate-variant URL (will be redirected)"
        if _is_system_page(url):
            return "system page"
        if _is_brand(query):
            return "brand query"
        if (revenue_by_url.get(url) or 0) > 0:
            return "page had revenue in the window — hands off"
        return None

    # Arm 1: page-1 CTR gap (impressions × gap is the prize).
    for r in ctr_rows or []:
        url, q = r.get("url", ""), r.get("query", "")
        impr = r.get("impressions", 0) or 0
        pos = r.get("position", 99) or 99
        if impr < MIN_IMPRESSIONS or pos > 10:
            continue
        if blocked(url, q):
            continue
        expected = achievable_ctr(pos)
        actual = (r.get("actual_ctr", 0) or 0) / 100.0
        if expected <= 0 or actual >= expected * CTR_UNDERPERFORMANCE:
            continue
        gap = expected - actual
        cands[url] = {
            "url": url, "query": q, "arm": "ctr_gap",
            "asset_type": r.get("asset_type", "other"),
            "position": pos, "impressions": impr,
            "ctr": round(actual * 100, 2), "expected_ctr": round(expected * 100, 2),
            "score": round(impr * gap, 2),
            "reason": (f"Page 1 (#{pos}) with {impr} impressions clicking at "
                       f"{actual:.1%} vs {expected:.1%} achievable."),
        }

    # Arm 2: defend drifting pages (position slid DRIFT_POSITIONS+).
    for r in winnable_rows or []:
        url, q = r.get("url", ""), r.get("query", "")
        impr = r.get("impressions", 0) or 0
        delta = r.get("position_delta")
        if r.get("trend") != "down" or delta is None or abs(delta) < DRIFT_POSITIONS:
            continue
        if impr < MIN_IMPRESSIONS:
            continue
        if blocked(url, q):
            continue
        cands[url] = {
            "url": url, "query": q, "arm": "drift",
            "asset_type": r.get("asset_type", "other"),
            "position": r.get("position_now28") or r.get("position", 0),
            "impressions": impr,
            "score": round(impr * min(abs(delta) / 10.0, 1.0), 2),
            "reason": (f"Slid ~{abs(delta):.0f} spots in 28d "
                       f"(#{r.get('position_prev28', '?')}→#{r.get('position_now28', '?')}) "
                       f"on {impr} impressions — defend before it leaves page 2."),
        }

    ranked = sorted(cands.values(), key=lambda c: -c["score"])
    return ranked[:limit]


# --------------------------------------------------------------- experiments
def open_writes_this_week(state) -> int:
    monday = (datetime.now() - timedelta(days=datetime.now().weekday())).date()
    n = 0
    for e in state.get("experiments", []):
        try:
            if (e.get("applied_at")
                    and datetime.fromisoformat(e["applied_at"]).date() >= monday):
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def writes_today(state) -> int:
    today = datetime.now().date()
    n = 0
    for e in state.get("experiments", []):
        try:
            if (e.get("applied_at")
                    and datetime.fromisoformat(e["applied_at"]).date() == today):
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def new_experiment(candidate, entity, rewrite, hypothesis, baseline) -> dict:
    return {
        "id": f"loop-{datetime.now().strftime('%Y%m%d%H%M%S')}-{abs(hash(candidate['url'])) % 10000:04d}",
        "url": candidate["url"],
        "query": candidate.get("query", ""),
        "arm": candidate.get("arm", ""),
        "selection_reason": candidate.get("reason", ""),
        "entity": {k: entity[k] for k in ("entity_type", "sku", "category_id", "name")
                   if k in entity},
        "before": {"meta_title": entity.get("meta_title", ""),
                   "meta_description": entity.get("meta_description", "")},
        "after": {"meta_title": rewrite.get("meta_title", ""),
                  "meta_description": rewrite.get("meta_description", "")},
        "hypothesis": hypothesis,
        "baseline": baseline,      # page + site metrics at proposal time
        "status": "proposed",      # proposed → applied → kept/neutral/reverted/failed
        "created_at": _now_iso(),
        "applied_at": None,
        "check_at": None,
        "verdicts": [],
    }


def mark_applied(exp):
    exp["status"] = "applied"
    exp["applied_at"] = _now_iso()
    exp["check_at"] = (datetime.now() + timedelta(days=VERIFY_AFTER_DAYS)) \
        .date().isoformat()


# -------------------------------------------------------------------- verify
def _trend_factor(baseline_site, current_site):
    """Site-wide impressions ratio, to normalize a seasonal tide out of the
    page verdict. Clamped so a broken site total can't swing a verdict 5x."""
    b = (baseline_site or {}).get("impressions", 0) or 0
    c = (current_site or {}).get("impressions", 0) or 0
    if b <= 0 or c <= 0:
        return 1.0
    return max(0.5, min(2.0, c / b))


def verify_experiment(exp, current_page, current_site):
    """Verdict for one experiment past its check date.

    current_page: {impressions, clicks, ctr (fraction), position} for the same
    28d window shape as the baseline. Returns (verdict, detail) where verdict ∈
    keep | neutral | revert | suspect | inconclusive. 'suspect' schedules a
    confirmation check REVERT_CONFIRM_DAYS out instead of reverting on one bad
    reading.
    """
    base = exp.get("baseline", {}).get("page", {})
    b_ctr = base.get("ctr", 0) or 0
    b_pos = base.get("position", 0) or 0
    c_ctr = current_page.get("ctr", 0) or 0
    c_pos = current_page.get("position", 0) or 0
    c_impr = current_page.get("impressions", 0) or 0

    if c_impr <= 0:
        return "inconclusive", "No impressions in the verification window."

    tf = _trend_factor(exp.get("baseline", {}).get("site"), current_site)
    # Normalize page CTR by the site tide (a site-wide CTR dip shouldn't be
    # blamed on this rewrite). Position isn't tide-adjusted — rank is relative.
    ctr_ratio = ((c_ctr / b_ctr) / tf) if b_ctr > 0 else (2.0 if c_ctr > 0 else 1.0)
    pos_delta = (b_pos - c_pos) if (b_pos and c_pos) else 0.0  # >0 = improved

    detail = (f"CTR {b_ctr:.2%}→{c_ctr:.2%} (site-normalized ratio {ctr_ratio:.2f}), "
              f"position #{b_pos:.1f}→#{c_pos:.1f}, {c_impr} impressions, "
              f"site trend ×{tf:.2f}.")

    if ctr_ratio >= KEEP_CTR_LIFT or pos_delta >= KEEP_POS_GAIN:
        return "keep", "Improved beyond the noise floor. " + detail
    if pos_delta <= -REVERT_POS_LOSS:
        if exp.get("status") == "suspect":
            return "revert", "Position loss CONFIRMED on the second check. " + detail
        return "suspect", ("Position slid beyond the floor — scheduling a "
                           f"confirmation check in {REVERT_CONFIRM_DAYS} days "
                           "before reverting (one bad reading isn't proof). " + detail)
    if exp.get("status") == "suspect":
        return "neutral", "Earlier slide did NOT confirm — keeping. " + detail
    return "neutral", "Flat within noise — keeping (reverting costs churn). " + detail


def outcomes_digest(state, limit=20) -> str:
    """Compact history of recent verdicts for the rewrite prompt, so the
    generator stops repeating the kind of rewrite that keeps losing."""
    done = [e for e in state.get("experiments", [])
            if e.get("status") in ("kept", "neutral", "reverted")]
    done.sort(key=lambda e: e.get("applied_at") or "", reverse=True)
    lines = []
    for e in done[:limit]:
        lines.append(f"- [{e['status'].upper()}] “{e.get('query', '')}” "
                     f"({e.get('arm')}): \"{e['after'].get('meta_title', '')}\" — "
                     f"{(e.get('verdicts') or [{}])[-1].get('detail', '')[:120]}")
    return "\n".join(lines)
