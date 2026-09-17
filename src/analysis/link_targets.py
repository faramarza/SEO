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
MODEL_VERSION = 6


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _dispersed(vals):
    """True when the ranking pages' link counts are too spread out for link
    count to be what's sorting the SERP — e.g. a 2-link blog and a 9000-link
    site both ranking. No honest link target can come from such a SERP."""
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return False
    lo, hi = min(vals), max(vals)
    med = _median(vals) or 1
    return hi >= 20 * med and lo <= med / 4

# Below this, competitor PAGES effectively carry no direct links and rankings
# ride DOMAIN authority + internal links instead — the common case for
# category/product pages.
PAGE_LINKS_MEANINGFUL = 5

# A realistic ceiling on new referring domains a small store can earn through
# deliberate outreach in a reasonable horizon (~a year of real effort). A gap
# larger than this is not a to-do — it's a wall, and the honest verdict is
# "compete on long-tail and content, not links."
REACHABLE_DOMAINS = 60


def gap_verdict(own_rd, comp_rds, emulable_slots, total_slots,
                own_dom_rd=None, comp_dom_rds=None):
    """(verdict, gap_lo, gap_hi, explanation).

    Two-level model: when the ranking PAGES genuinely earn direct links,
    compare page-to-page. When they don't (deep pages usually don't — zeros
    across the board), the deciding variable is DOMAIN-level referring
    domains, theirs vs ours, and the verdict says to build links to the
    domain's most linkable pages rather than falsely declaring parity.
    Deliberately ranged, and deliberately allowed to conclude links are NOT
    the fix."""
    comp_dom_rds = comp_dom_rds or []
    if not comp_rds:
        if total_slots and emulable_slots == 0:
            return ("marketplace_locked", 0, 0,
                    "The top slots are marketplaces/platforms — no link count "
                    "displaces Amazon or Etsy. Cap expectations for this "
                    "keyword; win the remaining organic slots or its "
                    "long-tail variants instead.")
        return ("unknown", 0, 0, "No competitor link data yet.")

    med_page = _median(comp_rds)

    if med_page >= PAGE_LINKS_MEANINGFUL:
        # Their pages genuinely earn direct links — page-level comparison.
        if _dispersed(comp_rds):
            return ("mixed", 0, 0,
                    f"The pages ranking here span {min(comp_rds)}–"
                    f"{max(comp_rds)} referring domains — link count isn't "
                    "what's sorting this result, so there's no honest link "
                    "target. It's a content/relevance/intent play.")
        med = round(med_page)
        if own_rd >= med:
            return ("parity", 0, 0,
                    f"Your page ({own_rd} referring domains) already matches "
                    f"the typical page-1 competitor (~{med}). More links "
                    "won't move it — the gap is content/relevance. Spend "
                    "effort on-page.")
        gap = max(1, round(med - own_rd))
        if gap > REACHABLE_DOMAINS:
            return ("out_of_reach", 0, 0,
                    f"The pages ranking here have ~{med} referring domains vs "
                    f"your {own_rd} — a ~{gap}-domain gap that isn't "
                    "realistically closable for a store this size. Chase the "
                    "long-tail variants of this keyword instead, where the "
                    "competing pages are smaller.")
        v = "close" if gap <= 5 else "authority_gap"
        return (v, gap, gap,
                f"The typical page ranking here has ~{med} referring domains; "
                f"yours has {own_rd}. Aim for about {gap} more websites "
                "linking to this page. Necessary, not sufficient — content "
                "must stay competitive.")

    # Their pages carry ~no direct links — the ranking driver is the DOMAIN.
    if not comp_dom_rds or own_dom_rd is None:
        return ("unknown", 0, 0,
                "Competitor pages carry ~no direct links (normal for deep "
                "pages) — domain-level data needed and not yet measured.")
    if _dispersed(comp_dom_rds):
        return ("mixed", 0, 0,
                f"The sites ranking here span {min(comp_dom_rds)}–"
                f"{max(comp_dom_rds)} referring domains — domain strength "
                "isn't what's sorting this result, so there's no honest link "
                "target. It's a content/relevance play.")
    d_med = round(_median(comp_dom_rds))
    if own_dom_rd >= d_med:
        return ("parity", 0, 0,
                f"Your DOMAIN ({own_dom_rd} referring domains) already matches "
                f"the typical competitor here (~{d_med}). Links are NOT the "
                "constraint — the gap is content/relevance. Spend effort "
                "on-page.")
    g = max(1, round(d_med - own_dom_rd))
    if g > REACHABLE_DOMAINS:
        return ("out_of_reach", 0, 0,
                f"The sites ranking here are far larger domains (~{d_med} "
                f"referring domains vs your {own_dom_rd}). That ~{g}-domain "
                "gap isn't realistically closable with outreach for a store "
                "this size — this head term is won on domain scale you don't "
                "have. Don't spend links here: chase the long-tail variants "
                "of this keyword (where the competing pages are smaller) and "
                "make this page the best content for them.")
    return ("domain_gap", g, g,
            f"Their pages, like yours, have ~no direct links — rankings ride "
            f"DOMAIN authority. The typical competitor's site has ~{d_med} "
            f"referring domains vs your {own_dom_rd}. Aim for about {g} more "
            "websites linking to your SITE (any strong page — guides and "
            "linkable content work best), then funnel internal links here.")


# Effort ordering: feasible-and-valuable first, don't-bother last.
_VERDICT_RANK = {"close": 0, "authority_gap": 1, "domain_gap": 2,
                 "unknown": 3, "pending": 3,
                 "mixed": 4, "parity": 5, "out_of_reach": 6,
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


def _display(own_rd, comp_rds, own_dom_rd=None, comp_dom_rds=None):
    """The raw numbers to ALWAYS show on the outreach worksheet, whatever the
    verdict: (you, page1, links_needed, scope). `you` and `page1` are referring
    domains — yours vs the median page-1 competitor — measured at whichever
    level actually decides the ranking (page-level when the ranking pages carry
    real direct links, else domain-level). `links_needed` = max(0, page1-you):
    how many more referring domains to MATCH the page-1 sites. This is the
    honest proxy for 'links to reach page 1' — necessary, not a guarantee."""
    med_page = _median(comp_rds) if comp_rds else 0
    if comp_rds and med_page >= PAGE_LINKS_MEANINGFUL:
        you, page1, scope = own_rd, round(med_page), "page"
    elif comp_dom_rds and own_dom_rd is not None:
        you, page1, scope = own_dom_rd, round(_median(comp_dom_rds)), "domain"
    else:
        you, page1, scope = own_rd, round(med_page), "page"
    return you, page1, max(0, page1 - you), scope


def _result(verdict, lo, hi, note, own_rd, comps, own_dom_rd=None,
            comp_rds=None, comp_dom_rds=None):
    r = {"verdict": verdict, "gap_lo": lo, "gap_hi": hi, "note": note,
         "own_rd": own_rd, "competitors": comps, "model": MODEL_VERSION,
         "computed_at": datetime.now().isoformat(timespec="seconds")}
    if own_dom_rd is not None:
        r["own_domain_rd"] = own_dom_rd
    # Always-present raw numbers for the worksheet (independent of verdict).
    if comps is not None:
        crds = comp_rds if comp_rds is not None else [c.get("rd", 0) for c in comps]
        cdoms = comp_dom_rds
        if cdoms is None:
            dl = [c.get("domain_rd") for c in comps if c.get("domain_rd") is not None]
            cdoms = dl or None
        you, page1, need, scope = _display(own_rd, crds, own_dom_rd, cdoms)
        r.update(you=you, page1=page1, links_needed=need, scope=scope)
    return r


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

    comps = sorted(comps, key=lambda c: c.get("position") or 99)[:3]
    if own_rd is None:
        return _pending("Your page isn't in that SERP export — export a SERP overview where "
                        "your page appears (or note its referring domains from Site Explorer).")
    if not comps:
        return _pending("No emulable competitors in the export (all marketplaces/social).")
    comp_rds = [c["rd"] for c in comps]
    # Ahrefs 'Domains' is page-level referring domains — use the page path of
    # gap_verdict directly (real per-page counts, no domain fallback needed).
    v, lo, hi, note = gap_verdict(own_rd, comp_rds, len(comps), len(rows))
    return _result(v, lo, hi, note, own_rd, comps)


# Verdicts where MORE links would actually move the ranking — the opposite of
# parity/out_of_reach/mixed/marketplace. These are the "point outreach here"
# opportunities, whether on a head term or (usually) a long-tail one.
REACHABLE_VERDICTS = ("close", "authority_gap", "domain_gap")


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
    organic = (serp.get("organic_results") or serp.get("organic") or [])[:8]
    own_dom = _domain(target["url"])
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
        comps.append({"url": u, "domain": d,
                      "position": r.get("position", 0)})
        if len(comps) >= 3:
            break
    result_comps = []
    # fetch_rd_fn returns EXACT totals: {"available","count","reason"} — the
    # summary endpoint, so large sites never saturate a cap.
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

    # When the TYPICAL competitor page carries ~no direct links (the deep-page
    # norm), rankings ride the DOMAIN — measure domain totals on both sides.
    # Domain lookups are cached in the client and shared across targets.
    own_dom_rd = comp_dom_rds = None
    med_page = sorted(comp_rds)[len(comp_rds) // 2] if comp_rds else 0
    if comp_rds and med_page < PAGE_LINKS_MEANINGFUL:
        own_res = fetch_rd_fn(own_dom)
        if not own_res.get("available"):
            return _pending(f"Domain lookup ran out of budget: {own_res.get('reason','')} — resumes next run.")
        own_dom_rd = int(own_res.get("count") or 0)
        comp_dom_rds = []
        for c in result_comps:
            res = fetch_rd_fn(c["domain"])
            if not res.get("available"):
                return _pending(f"Domain lookup ran out mid-target: {res.get('reason','')} — resumes next run.")
            c["domain_rd"] = int(res.get("count") or 0)
            comp_dom_rds.append(c["domain_rd"])

    v, lo, hi, note = gap_verdict(own_rd, comp_rds, len(comps), len(organic),
                                  own_dom_rd=own_dom_rd, comp_dom_rds=comp_dom_rds)
    return _result(v, lo, hi, note, own_rd, result_comps, own_dom_rd=own_dom_rd)
