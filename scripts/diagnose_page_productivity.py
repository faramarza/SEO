#!/usr/bin/env python3
"""Page-productivity audit — the "132 of 1,500 pages" question, answered from
ground truth. Run on the server where the GSC credentials live:

    python3 scripts/diagnose_page_productivity.py            # last 90 days
    python3 scripts/diagnose_page_productivity.py --days 28
    python3 scripts/diagnose_page_productivity.py --show 60  # longer dead list

WHY this exists, and why it does NOT read latest_evaluation.json:
    The evaluation only ever builds page-assets from GSC's TOP 100 pages by
    impressions (run_agentic_evaluation.py). So the cached eval literally cannot
    see your long tail — it never looked. Ahrefs "Top Pages" is worse for this: it
    only lists pages IT observes ranking, not your real inventory (that's why it
    said ~132 when you have ~1,500). The only ground truth for "which of MY pages
    earn anything" is Google's own data — GSC Search Analytics, pulled for EVERY
    page (paginated to the full 25k/row cap), cross-referenced against your sitemap
    to surface the pages that exist but earn nothing.

WHAT the buckets mean (all from real numbers, nothing modelled):
    PRODUCTIVE          clicks >= 1          — actually bringing you organic visits
    RANKING · NO CLICKS impressions >= 30,   — Google shows it, nobody clicks:
                        clicks == 0            a title/position/intent problem, NOT a dead page
    BARELY VISIBLE      1..29 impressions     — shows so rarely it's effectively invisible
    ZERO VISIBILITY     in sitemap, 0 impr.   — exists but earns NOTHING: dead weight
                                               (either not indexed, or indexed and ranking
                                                for nothing — practically the same outcome)

HONEST LIMITS stated up front:
    • GSC "indexed vs not" is NOT in the Search Analytics API. A page can be indexed
      and still get 0 impressions. So ZERO VISIBILITY means "earns no search
      visibility", which is the thing that matters — it does NOT claim "deindexed".
      The precise indexed count lives in GSC's Pages/Index report (UI only).
    • GSC omits pages with 0 impressions entirely, so ZERO VISIBILITY can only be
      found by diffing the sitemap against GSC. No sitemap → that bucket is skipped
      (and we say so), but the productive/ranking/barely buckets are still exact.
    • GSC data lags ~3 days; we end the window there. URLs are normalized
      (scheme/host/trailing-slash) so sitemap and GSC rows match.

Read-only. Pulls from GSC + fetches your sitemap. Modifies nothing.
"""

import argparse
import gzip
import json
import sys
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RANKING_MIN_IMPR = 30  # impressions floor to call something "ranking but not clicked"


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "defaults.json") as f:
        return json.load(f)


def _norm(url: str) -> str:
    """Match key: host + path, lowercased, no scheme/query/fragment/trailing slash."""
    try:
        p = urlparse((url or "").strip())
        host = (p.netloc or "").lower().replace("www.", "")
        path = (p.path or "/").rstrip("/") or "/"
        return f"{host}{path}"
    except Exception:
        return (url or "").strip().lower().rstrip("/")


# ---------------------------------------------------------------- GSC ----------
def pull_gsc_pages(config: dict, days: int) -> dict[str, dict]:
    """Every page GSC recorded in the window, paginated to the full cap."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    gsc = config["data_sources"]["gsc"]
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / gsc["credentials_path"]),
        scopes=['https://www.googleapis.com/auth/webmasters.readonly'],
    )
    service = build('searchconsole', 'v1', credentials=creds)
    property_url = gsc["property_url"]

    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=days)

    pages: dict[str, dict] = {}
    start_row = 0
    PAGE = 25000
    while True:
        body = {
            'startDate': start.isoformat(),
            'endDate': end.isoformat(),
            'dimensions': ['page'],
            'rowLimit': PAGE,
            'startRow': start_row,
        }
        resp = service.searchanalytics().query(siteUrl=property_url, body=body).execute()
        rows = resp.get('rows', [])
        if not rows:
            break
        for r in rows:
            url = r['keys'][0]
            pages[_norm(url)] = {
                'url': url,
                'clicks': r.get('clicks', 0) or 0,
                'impressions': r.get('impressions', 0) or 0,
                'ctr': r.get('ctr', 0.0) or 0.0,
                'position': r.get('position', 0.0) or 0.0,
            }
        start_row += len(rows)
        if len(rows) < PAGE:
            break
    return pages


# ------------------------------------------------------------- SITEMAP ---------
def _fetch(url: str, timeout=25) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; AlphabetTrains-SEO-Crawler/1.0; +page-audit)"})
    data = urllib.request.urlopen(req, timeout=timeout).read()
    if url.endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    return data


def _sitemap_locs_from_robots(base: str) -> list[str]:
    try:
        txt = _fetch(base.rstrip("/") + "/robots.txt").decode("utf-8", "replace")
        return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)
    except Exception:
        return []


def collect_sitemap_urls(base: str) -> set[str]:
    """Walk sitemap(s) — robots.txt hints + common locations, following indexes."""
    candidates = _sitemap_locs_from_robots(base) + [
        base.rstrip("/") + "/sitemap.xml",
        base.rstrip("/") + "/sitemap_index.xml",
        base.rstrip("/") + "/pub/sitemap.xml",
    ]
    seen_maps: set[str] = set()
    urls: set[str] = set()
    queue = list(dict.fromkeys(candidates))
    while queue:
        loc = queue.pop(0)
        if loc in seen_maps:
            continue
        seen_maps.add(loc)
        try:
            body = _fetch(loc).decode("utf-8", "replace")
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body, re.I)
        if not locs:
            continue
        # A sitemap index points at more sitemaps (they end in .xml/.gz).
        if "<sitemapindex" in body.lower():
            for loc in locs:
                if loc not in seen_maps:
                    queue.append(loc)
        else:
            for loc in locs:
                urls.add(_norm(loc))
    return urls


# -------------------------------------------------------------- MAIN -----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--show", type=int, default=40, help="rows of dead-weight to list")
    args = ap.parse_args()

    config = load_config()
    base = config.get("base_url") or config.get("site", {}).get("base_url") or ""
    gsc_prop = config["data_sources"]["gsc"].get("property_url", "")
    if not base:
        # GSC property is either a full URL ("https://host/") or a Domain property
        # ("sc-domain:host"). Derive the site root from whichever we were given —
        # no hardcoded host.
        m = re.search(r"https?://[^/]+", gsc_prop)
        if m:
            base = m.group(0)
        elif gsc_prop.startswith("sc-domain:"):
            base = "https://" + gsc_prop.split(":", 1)[1].strip()
        else:
            print("Could not determine site root from config (no base_url, and GSC "
                  f"property is {gsc_prop!r}). Pass one or add base_url to config.")
            sys.exit(1)

    print(f"Window: last {args.days} days (GSC, ending ~3 days ago)")
    print(f"Site:   {base}\n")

    gsc = pull_gsc_pages(config, args.days)
    if not gsc:
        print("GSC returned no pages — check credentials/property. Aborting.")
        sys.exit(1)

    productive = {k: v for k, v in gsc.items() if v['clicks'] >= 1}
    ranking_noclick = {k: v for k, v in gsc.items()
                       if v['clicks'] == 0 and v['impressions'] >= RANKING_MIN_IMPR}
    barely = {k: v for k, v in gsc.items()
              if v['clicks'] == 0 and 1 <= v['impressions'] < RANKING_MIN_IMPR}

    total_clicks = sum(v['clicks'] for v in gsc.values())
    total_impr = sum(v['impressions'] for v in gsc.values())

    print("=" * 68)
    print("PAGES GOOGLE SHOWED AT LEAST ONCE (ground truth from GSC)")
    print("=" * 68)
    n = len(gsc)
    def pct(x): return f"{100*x/n:.0f}%" if n else "-"
    print(f"  Pages with any impression : {n}")
    print(f"  ├─ PRODUCTIVE (≥1 click)  : {len(productive):>5}  ({pct(len(productive))})   "
          f"→ {total_clicks:,} clicks/window")
    print(f"  ├─ RANKING · NO CLICKS    : {len(ranking_noclick):>5}  ({pct(len(ranking_noclick))})   "
          f"→ shown but never clicked (title/position/intent)")
    print(f"  └─ BARELY VISIBLE (<{RANKING_MIN_IMPR} impr): {len(barely):>5}  ({pct(len(barely))})")
    print(f"  Total impressions in window: {total_impr:,}")

    # --- sitemap diff → zero-visibility dead weight -------------------------
    print("\n" + "=" * 68)
    print("YOUR ACTUAL INVENTORY vs WHAT EARNS ANYTHING (sitemap diff)")
    print("=" * 68)
    sitemap = collect_sitemap_urls(base)
    if not sitemap:
        print("  No sitemap found (tried robots.txt + common paths).")
        print("  → Can't enumerate ZERO-VISIBILITY pages without an inventory.")
        print("    The buckets above are still exact for pages that DID appear.")
    else:
        gsc_keys = set(gsc.keys())
        zero_vis = sorted(sitemap - gsc_keys)
        earning = sitemap & set(productive.keys())
        print(f"  Pages in sitemap (real inventory) : {len(sitemap)}")
        print(f"  ├─ earn ≥1 click                  : {len(earning):>5}  ({100*len(earning)/len(sitemap):.0f}%)")
        print(f"  ├─ get impressions but 0 clicks   : "
              f"{len((sitemap & gsc_keys) - set(productive.keys())):>5}")
        print(f"  └─ ZERO VISIBILITY (0 impressions): {len(zero_vis):>5}  "
              f"({100*len(zero_vis)/len(sitemap):.0f}%)  ← dead weight")
        print(f"\n  Sample of ZERO-VISIBILITY pages (exist, earn nothing) — first {args.show}:")
        for u in zero_vis[:args.show]:
            print(f"    {u}")
        if len(zero_vis) > args.show:
            print(f"    … and {len(zero_vis) - args.show} more")

    # --- biggest wasted-impression pages (ranking, no clicks) ---------------
    print("\n" + "=" * 68)
    print("TOP 'SHOWN BUT NOT CLICKED' PAGES (fix titles/intent, not new links)")
    print("=" * 68)
    top_wasted = sorted(ranking_noclick.values(), key=lambda v: -v['impressions'])[:args.show]
    if top_wasted:
        print(f"  {'impr':>7} {'pos':>5}  page")
        for v in top_wasted:
            p = _norm(v['url']).split("/", 1)[-1]
            print(f"  {v['impressions']:>7} {v['position']:>5.1f}  /{p[:58]}")
    else:
        print("  (none)")

    print("\nDone. Read-only — nothing was modified.")


if __name__ == "__main__":
    main()
