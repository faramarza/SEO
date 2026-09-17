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
# marketplaces (their authority is the platform), social/UGC, and Q&A/reference
# mega-sites whose referring-domain totals are so large they poison a gap
# estimate (you don't out-link Quora — you don't compete with it at all).
NON_EMULABLE = ("amazon.", "etsy.", "walmart.", "target.com", "ebay.",
                "wayfair.", "temu.", "aliexpress.", "pinterest.", "youtube.",
                "facebook.", "instagram.", "reddit.", "wikipedia.", "tiktok.",
                "quora.", "wikihow.", "medium.com", "linkedin.", "yelp.",
                "nytimes.", "goodhousekeeping.", "parents.com", "verywell",
                "healthline.", "webmd.", "britannica.")


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


# URL patterns that are never worth a link-building target: author bylines,
# tag/category listing pages, pagination, search, and feeds.
_NON_TARGET_PAT = re.compile(
    r"/(author|tag|tags|category|categories|page|search|feed|rss)/", re.I)


def _worth_targeting(url: str) -> bool:
    return bool(url) and not _NON_TARGET_PAT.search(url)


def build_targets(striking_rows, include_all_levers=False):
    """Group striking rows by PAGE → one target per page with all its
    keywords. This is the 'which pages, which keywords' list; the gap fill
    adds the numbers. Author/tag/listing pages are dropped — you don't build
    links to a byline page.

    By default only pages whose lever is EXTERNAL (backlinks are the last
    lever left) are returned — the true link targets. With
    include_all_levers=True, EVERY striking-distance page is returned, each
    tagged with its diagnosed lever (`lever`: external / internal / on_page)
    so nothing is hidden and the operator sees the whole board and why each
    page is or isn't primarily a link play."""
    by_url = {}
    for r in striking_rows or []:
        lever = r.get("lever")
        if not include_all_levers and lever != "external":
            continue
        url = r.get("url", "")
        if not url or not _worth_targeting(url):
            continue
        t = by_url.setdefault(url, {"url": url,
                                    "asset_type": r.get("asset_type", "other"),
                                    "keywords": [], "total_impressions": 0,
                                    "_levers": set()})
        t["keywords"].append({"query": r.get("query", ""),
                              "position": r.get("position", 0),
                              "impressions": r.get("impressions", 0) or 0,
                              "upside_clicks": r.get("upside_clicks", 0) or 0,
                              "lever": lever})
        t["total_impressions"] += r.get("impressions", 0) or 0
        if lever:
            t["_levers"].add(lever)
    for t in by_url.values():
        t["keywords"].sort(key=lambda k: -k["impressions"])
        # Page's primary lever: EXTERNAL if any keyword needs backlinks (that's
        # the link opportunity); otherwise the cheaper fix its top keyword has.
        levers = t.pop("_levers")
        t["lever"] = ("external" if "external" in levers
                      else (t["keywords"][0].get("lever") or "on_page"))
        t["levers"] = sorted(levers)
    return sorted(by_url.values(), key=lambda t: -t["total_impressions"])


# Bump when the verdict model changes — old measurements re-measure instead
# of displaying conclusions the current model would not draw.
# v4: ONE number anchored on the MEDIAN (typical) competitor, not a min-to-max
# range that spanned six orders of magnitude and read as noise. Plus a
# mixed-signals verdict when the ranking pages' link counts are wildly
# dispersed — then link count doesn't decide that SERP and no honest target
# exists (it's a content/relevance play).
# v5: distinguish a reachable gap from a wall — a domain gap larger than a
# small store can realistically earn is "out of reach", not a number to chase.
# v6: attach the raw worksheet numbers (you / page1 / links_needed) to every
# measurement so the outreach worksheet always shows a number, whatever the
# verdict — old v5 rows re-measure to gain those fields.
# v7: STANDARD FORMULA. Links needed = median referring domains of the top
# ranking PAGES minus your PAGE's (Ahrefs-KD method), page-level throughout.
# Removed the domain-total fallback that produced "you out-link them but don't
# rank" — when the ranking pages have ~no links it's now an honest
# content/relevance verdict, not a bogus domain comparison.
MODEL_VERSION = 7


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# At or below this median PAGE-level referring-domain count, the ranking pages
# essentially have no backlinks, so links aren't what's sorting the SERP — the
# honest verdict is content/relevance, not a link target.
PAGE_LINKS_MEANINGFUL = 5

# A realistic ceiling on new referring domains a small store can earn through
# deliberate outreach in a reasonable horizon (~a year of real effort). A gap
# larger than this is not a to-do — it's a wall, and the honest verdict is
# "compete on long-tail and content, not links."
REACHABLE_DOMAINS = 60


def standard_gap(own_rd, comp_rds, emulable_slots=None, total_slots=None):
    """The recognized guesstimate — Ahrefs' Keyword-Difficulty method:

        links needed ≈ MEDIAN referring domains of the top ranking PAGES
                       − YOUR page's referring domains,   floored at 0.

    Page-level throughout (your page vs their pages, never domain totals). The
    median makes it robust to one giant outlier. Returns (verdict,
    links_needed, note). A guesstimate — links are necessary, not sufficient —
    and it is allowed to conclude links are NOT the lever."""
    if not comp_rds:
        if total_slots and emulable_slots == 0:
            return ("marketplace_locked", 0,
                    "The top slots are marketplaces/platforms — no link count displaces "
                    "Amazon or Etsy. Win the remaining organic slots or long-tail variants.")
        return ("unknown", 0, "No competitor link data yet.")
    med = round(_median(comp_rds))
    if med < PAGE_LINKS_MEANINGFUL:
        # The ranking pages themselves have almost no backlinks — links are not
        # what's sorting this SERP. Don't invent a number.
        return ("content_gap", 0,
                f"The pages ranking here have almost no backlinks (median ~{med} referring "
                "domains to the ranking page). Links aren't the lever for this term — it's "
                "relevance/content. Build a page that genuinely targets it.")
    if own_rd >= med:
        return ("parity", 0,
                f"Your page ({own_rd} referring domains) already matches the typical page-1 "
                f"page (~{med}). More links won't move it — the gap is content/relevance.")
    gap = max(1, med - own_rd)
    if gap > REACHABLE_DOMAINS:
        return ("out_of_reach", 0,
                f"The pages ranking here have a median ~{med} referring domains vs your "
                f"{own_rd} — a ~{gap}-domain gap that isn't realistically closable for a store "
                "this size. Chase easier long-tail variants instead.")
    v = "close" if gap <= 5 else "authority_gap"
    return (v, gap,
            f"Guesstimate (Ahrefs-KD method): the median page ranking here has ~{med} "
            f"referring domains; yours has {own_rd}. Aim for about {gap} more websites "
            "linking to THIS page. Necessary, not sufficient — content must stay competitive.")


# Verdicts where MORE links to the page would plausibly move it.
REACHABLE_VERDICTS = ("close", "authority_gap")

# Effort ordering: feasible-and-valuable first, don't-bother last.
_VERDICT_RANK = {"close": 0, "authority_gap": 1, "already_ranking": 2,
                 "unknown": 3, "pending": 3,
                 "content_gap": 4, "parity": 5, "out_of_reach": 6,
                 "marketplace_locked": 7}


def rank_targets(targets):
    return sorted(targets, key=lambda t: (
        _VERDICT_RANK.get(t.get("verdict", "unknown"), 2),
        -(t.get("total_impressions") or 0)))


def is_fresh(entry) -> bool:
    """Fresh = recent AND measured by the current verdict model — a model
    change invalidates old conclusions so they re-measure instead of
    displaying verdicts the current logic would not draw."""
    try:
        if entry.get("model") != MODEL_VERSION:
            return False
        return (datetime.now() - datetime.fromisoformat(entry["computed_at"])
                ) < timedelta(days=FRESH_DAYS)
    except (KeyError, ValueError, TypeError):
        return False


def _find_col(fieldnames, *aliases):
    for f in fieldnames or []:
        if (f or "").strip().lower() in aliases:
            return f
    return None


def _pending(note):
    return {"verdict": "pending", "note": note}


def _result(verdict, links_needed, note, own_rd, comps, page1):
    """One measurement, page-level throughout. `own_rd`/`you` = YOUR page's
    referring domains; `page1` = median referring domains of the ranking pages;
    `links_needed` = how many more (floored at 0). Domain totals are never the
    number here — that was the old apples-to-oranges bug."""
    return {"verdict": verdict, "gap_lo": links_needed, "gap_hi": links_needed,
            "note": note, "own_rd": own_rd, "you": own_rd, "page1": page1,
            "links_needed": links_needed, "scope": "page", "competitors": comps,
            "model": MODEL_VERSION,
            "computed_at": datetime.now().isoformat(timespec="seconds")}


def gap_from_ahrefs(target, csv_text, own_domain):
    """Compute the link gap for ONE keyword from an Ahrefs 'SERP overview'
    CSV export — the authoritative source the operator already owns. The
    export lists each ranking page with its referring-domain count ('Domains'
    column) and its URL. We read the competitors' counts and the operator's
    own page count straight from Ahrefs; DataForSEO is not involved. Returns
    the enriched target (verdict from the same median math, now on real data).
    """
    import csv
    import io
    # Accept whatever the operator pastes: a raw Ahrefs CSV (comma) OR a copy
    # from Excel/Numbers/Sheets (tab). Sniff, then fall back to trying both.
    sample = "\n".join((csv_text or "").splitlines()[:5])
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    rows, reader = [], None
    for d in (delim, "," if delim != "," else "\t"):
        try:
            reader = csv.DictReader(io.StringIO(csv_text), delimiter=d)
            rows = list(reader)
        except csv.Error:
            continue
        if reader.fieldnames and len(reader.fieldnames) > 1:
            break
    if not rows or not reader:
        return _pending("Couldn't read that paste — copy the whole CSV (header row included).")
    fn = reader.fieldnames or []
    c_url = _find_col(fn, "url", "target url", "page url")
    c_dom = _find_col(fn, "domains", "referring domains", "ref domains", "ref. domains")
    c_pos = _find_col(fn, "position", "pos", "#")
    if not c_url or not c_dom:
        return _pending("That CSV has no URL / Domains columns — export the SERP overview "
                        "(Keywords Explorer), not a different report.")

    own_rd = None
    comps, seen = [], set()
    for r in rows:
        url = (r.get(c_url) or "").strip()
        dom = _domain(url)
        try:
            rd = int(float((r.get(c_dom) or "0").replace(",", "") or 0))
        except ValueError:
            continue
        if not dom:
            continue
        if own_domain in dom:                     # our own ranking page
            own_rd = rd if own_rd is None else max(own_rd, rd)
            continue
        if not is_emulable(url) or dom in seen:   # skip marketplaces/dupes
            continue
        seen.add(dom)
        try:
            pos = int(float(r.get(c_pos) or 0)) if c_pos else 0
        except ValueError:
            pos = 0
        comps.append({"url": url, "domain": dom, "position": pos, "rd": rd})

    comps = sorted(comps, key=lambda c: c.get("position") or 99)[:10]
    if own_rd is None:
        return _pending("Your page isn't in that SERP export — export a SERP overview where "
                        "your page appears (or note its referring domains from Site Explorer).")
    if not comps:
        return _pending("No emulable competitors in the export (all marketplaces/social).")
    comp_rds = [c["rd"] for c in comps]
    # Ahrefs 'Domains' is page-level referring domains — exactly the input the
    # standard (Ahrefs-KD) formula wants, on real data.
    v, need, note = standard_gap(own_rd, comp_rds, len(comps), len(rows))
    page1 = round(_median(comp_rds)) if comp_rds else 0
    return _result(v, need, note, own_rd, comps, page1)


def is_budget_note(note) -> bool:
    """A pending result caused by running out of daily SERP/backlink budget
    (as opposed to a permanent problem) — the caller should stop and resume
    next run rather than keep burning calls."""
    n = (note or "").lower()
    return any(w in n for w in ("cap", "quota", "budget", "unavailable", "ran out"))


def best_opportunity(keyword_gaps):
    """Across a page's measured keywords, the single best REACHABLE link
    opportunity (feasible verdict, most impressions) — or None when every
    measured keyword is parity/out-of-reach/mixed. This is what lets a page
    read 'parity on the head term, but a reachable gap on a long-tail one'."""
    reach = [g for g in (keyword_gaps or [])
             if g.get("verdict") in REACHABLE_VERDICTS]
    if not reach:
        return None
    return sorted(reach, key=lambda g: (_VERDICT_RANK.get(g.get("verdict"), 9),
                                        -(g.get("impressions") or 0)))[0]


def worksheet_rows(targets):
    """Flatten pages → one row per (page, keyword) for the outreach worksheet.
    EVERY candidate term is a row, whether measured yet or not, so the operator
    sees the whole board and its numbers. Measured rows carry you/page1/
    links_needed; unmeasured rows carry status so a blank is never ambiguous.
    Ranked: measured reachable opportunities (most search demand) first, then
    everything else by demand — the operator decides, the order just helps."""
    rows = []
    for t in targets or []:
        gaps_by_q = {(g.get("query") or "").lower(): g
                     for g in (t.get("keyword_gaps") or [])}
        for k in t.get("keywords") or []:
            q = k.get("query", "")
            g = gaps_by_q.get(q.lower())
            row = {"url": t["url"], "lever": t.get("lever", "external"),
                   "query": q, "position": k.get("position", 0),
                   "impressions": k.get("impressions", 0) or 0,
                   "you": None, "page1": None, "links_needed": None,
                   "scope": None, "verdict": None, "note": "", "status": "unmeasured"}
            if g and g.get("verdict") == "pending":
                row["status"] = "pending"
                row["note"] = g.get("note", "")
            elif g:
                row.update(status="measured", verdict=g.get("verdict"),
                           you=g.get("you"), page1=g.get("page1"),
                           links_needed=g.get("links_needed"),
                           scope=g.get("scope"), note=g.get("note", ""))
            rows.append(row)

    def _score(r):
        measured = r["status"] == "measured"
        reachable = measured and (r.get("links_needed") or 0) > 0 \
            and r.get("verdict") in REACHABLE_VERDICTS
        # 0 = reachable opportunity, 1 = other measured, 2 = not yet measured
        tier = 0 if reachable else (1 if measured else 2)
        return (tier, -(r.get("impressions") or 0))
    return sorted(rows, key=_score)


def compute_page_gaps(target, fetch_serp_fn, fetch_rd_fn, max_keywords=3):
    """Measure the link gap for a page's top `max_keywords` keywords, not just
    the head term — so long-tail queries (where competitors are small and a
    couple of links win) surface alongside the head-term verdict instead of
    being hidden behind it.

    Returns (gaps, budget_out): `gaps` is a per-keyword list of result dicts
    (each tagged with its query/impressions/position); `budget_out` is True
    when daily SERP/backlink budget ran out mid-page, so the caller stops and
    resumes next run with the remaining keywords still to do."""
    gaps, budget_out = [], False
    for k in (target.get("keywords") or [])[:max_keywords]:
        res = compute_target(target, fetch_serp_fn, fetch_rd_fn, keyword=k["query"])
        res["query"] = k.get("query", "")
        res["impressions"] = k.get("impressions", 0) or 0
        res["position"] = k.get("position", 0) or 0
        gaps.append(res)
        if res.get("verdict") == "pending" and is_budget_note(res.get("note")):
            budget_out = True
            break
    return gaps, budget_out


def compute_target(target, fetch_serp_fn, fetch_rd_fn, keyword=None):
    """DataForSEO-backed measurement for ONE keyword — returns a result dict
    for the 'dataforseo' source (same median math as the Ahrefs path).
    `keyword` defaults to the page's head term (keywords[0]) for backward
    compatibility. fetch_rd_fn(u) -> {"available","count","reason"}
    (referring-domain total, uncapped)."""
    kw = keyword or (target["keywords"][0]["query"] if target.get("keywords") else "")
    serp = fetch_serp_fn(kw) if kw else None
    if not serp:
        return _pending("SERP fetch unavailable (Serper quota or key) — will retry next run.")
    organic = (serp.get("organic_results") or serp.get("organic") or [])[:10]
    own_dom = _domain(target["url"])
    # Pre-filter the outliers the median shouldn't see at all: marketplaces,
    # social, and mega Q&A/reference sites (Amazon, Wikipedia, Quora…) are
    # excluded up front via is_emulable — you don't out-link them and their
    # link profiles would distort any estimate. Up to 6 emulable ranking pages.
    comps, non_emulable, seen_domains = [], 0, set()
    for r in organic:
        u = r.get("url", "")
        d = _domain(u)
        if not u or d == own_dom or d in seen_domains:
            continue
        if not is_emulable(u):
            non_emulable += 1
            continue
        seen_domains.add(d)
        comps.append({"url": u, "domain": d, "position": r.get("position", 0)})
        if len(comps) >= 6:
            break
    result_comps = []
    # fetch_rd_fn returns EXACT PAGE-level totals: {"available","count","reason"}.
    own = fetch_rd_fn(target["url"])
    if not own.get("available"):
        return _pending(f"Backlink data unavailable: {own.get('reason', '')}")
    own_rd = int(own.get("count") or 0)

    comp_rds = []
    for c in comps:
        res = fetch_rd_fn(c["url"])
        if not res.get("available"):
            return _pending(f"Backlink data ran out mid-target: {res.get('reason','')} — resumes next run.")
        c["rd"] = int(res.get("count") or 0)
        comp_rds.append(c["rd"])
        result_comps.append(c)

    # The standard Ahrefs-KD guesstimate on PAGE-level referring domains. The
    # median is inherently outlier-robust (one giant can't move it), and the
    # marketplaces/mega-sites were already removed above.
    v, need, note = standard_gap(own_rd, comp_rds, len(comps), len(organic))
    page1 = round(_median(comp_rds)) if comp_rds else 0
    return _result(v, need, note, own_rd, result_comps, page1)
