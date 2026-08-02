#!/usr/bin/env python3
"""Winnable-page action sheet — the do-this-today list for the category pages that
rank at position 5–20 for GENERIC (non-brand) demand and earn 0 clicks.

Run on the server (venv Python — needs the Google libs):

    /var/www/seo/venv/bin/python scripts/winnable_action_sheet.py
    ... --days 90 --pages 12 --min-impr 150 --no-verify   (skip live link-checks)

WHAT IT DOES, and why it's grounded rather than guessed:
  1. Pulls GSC (web) page+query, and PROGRAMMATICALLY selects "winnable" pages —
     non-brand query, position 4–20, real impressions, ~0 clicks — printing WHY
     each qualified so you can audit the call. Brand/exact-product-name pages
     (the reseller trap) are excluded on purpose.
  2. Fetches each winnable page LIVE to read its ACTUAL <title>, meta description
     and <h1> — so the rewrite builds on your real words, never invented ones.
     If the winning query is already in the title, it says so and shifts the
     recommendation to position/internal-links instead of a pointless retitle.
  3. Finds INTERNAL-LINK SOURCES with no crawl: other pages that already rank
     (in GSC) for queries sharing terms with the target — i.e. topically-relevant
     pages whose link would be editorially natural — and (unless --no-verify)
     lightly fetches them to skip any that ALREADY link to the target.

Polite by construction: only targets + a capped handful of candidate sources are
fetched, throttled, with a browser UA. Read-only. Modifies nothing.
"""

import argparse
import json
import re
import sys
import time
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MAX_TITLE = 60
UA = "Mozilla/5.0 (compatible; AlphabetTrains-SEO-Crawler/1.0; +action-sheet)"

# Brand / navigational tokens = the reseller trap. A query containing any of these
# is a brand's own name; you can't win it. Derived from your /shop-by-brands/ set.
BRAND_TOKENS = {
    "iseeme", "i see me", "guidecraft", "guide craft", "storytime", "storytime toys",
    "melissa", "doug", "hape", "guide-craft",
}
STOP = {"the", "a", "an", "for", "to", "of", "and", "with", "your", "you", "in",
        "on", "best", "top", "buy", "shop", "kids", "kid", "toddler", "toddlers",
        "baby", "babies", "toys", "toy", "gift", "gifts"}


def load_config():
    with open(PROJECT_ROOT / "config" / "defaults.json") as f:
        return json.load(f)


def _service(config):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    gsc = config["data_sources"]["gsc"]
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / gsc["credentials_path"]),
        scopes=['https://www.googleapis.com/auth/webmasters.readonly'])
    return build('searchconsole', 'v1', credentials=creds), gsc["property_url"]


def _q(service, prop, start, end, dims, page=None, row_limit=25000):
    body = {'startDate': start, 'endDate': end, 'dimensions': dims,
            'type': 'web', 'rowLimit': row_limit}
    if page:
        body['dimensionFilterGroups'] = [{'filters': [
            {'dimension': 'page', 'operator': 'equals', 'expression': page}]}]
    return service.searchanalytics().query(siteUrl=prop, body=body).execute().get('rows', [])


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _extract(html):
    def tag(pat):
        m = re.search(pat, html, re.I | re.S)
        return unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""
    title = tag(r"<title[^>]*>(.*?)</title>")
    h1 = tag(r"<h1[^>]*>(.*?)</h1>")
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                  html, re.I | re.S)
    meta = unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""
    return title, meta, h1


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 2}


def _is_brand(query):
    ql = query.lower()
    return any(b in ql for b in BRAND_TOKENS)


def _title_rewrite(query, current_title):
    """Honest front-load: put the searcher's exact words first, KEEP the page's real
    title. If the query is already there, don't fake a change."""
    q = query.strip()
    cur = (current_title or "").strip()
    if cur and q.lower() in cur.lower():
        return None  # already present — retitle won't help; fix position/links
    lead = q[:1].upper() + q[1:]
    merged = f"{lead} | {cur}" if cur else lead
    if len(merged) <= MAX_TITLE:
        return merged
    # keep the whole query; trim the tail of the old title at a word boundary
    room = MAX_TITLE - len(lead) - 3
    if room <= 8:
        return lead[:MAX_TITLE].rstrip()
    tail = cur[:room]
    if " " in tail and not cur[room:room + 1].isspace():
        tail = tail.rsplit(" ", 1)[0]  # don't cut mid-word
    return f"{lead} | {tail.rstrip()}".rstrip(" |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--min-impr", type=int, default=150)
    ap.add_argument("--no-verify", action="store_true", help="skip live link-source checks")
    ap.add_argument("--max-sources", type=int, default=4)
    args = ap.parse_args()

    config = load_config()
    service, prop = _service(config)
    end = (date.today() - timedelta(days=3)).isoformat()
    start = (date.today() - timedelta(days=3 + args.days)).isoformat()

    # page-level to find zero-click pages; page+query to classify winnable queries.
    pages = {r['keys'][0]: r for r in _q(service, prop, start, end, ['page'])}
    pq = _q(service, prop, start, end, ['page', 'query'])

    # best NON-BRAND query per page, within winnable position band
    best = {}  # url -> (impr, clicks, pos, query)
    src_index = {}  # url -> set(tokens) of everything it ranks for (for link-source matching)
    for r in pq:
        url, query = r['keys'][0], r['keys'][1]
        src_index.setdefault(url, set()).update(_tokens(query))
        if _is_brand(query) or "shop-by-brands" in url:
            continue
        impr = r.get('impressions', 0); pos = r.get('position', 0.0); clk = r.get('clicks', 0)
        if impr < args.min_impr or not (4.0 <= pos <= 20.0):
            continue
        cur = best.get(url)
        if not cur or impr > cur[0]:
            best[url] = (impr, clk, pos, query)

    # winnable page = has such a query AND the page overall earns ~0 clicks
    winnable = []
    for url, (impr, clk, pos, query) in best.items():
        page_clicks = pages.get(url, {}).get('clicks', 0)
        if page_clicks <= 1:
            winnable.append((url, impr, pos, query, page_clicks))
    winnable.sort(key=lambda x: -x[1])
    winnable = winnable[:args.pages]

    if not winnable:
        print("No winnable pages matched (non-brand query, pos 4–20, "
              f"≥{args.min_impr} impr, page ~0 clicks). Loosen --min-impr.")
        return

    print(f"Window {start} → {end}\n")
    print(f"WINNABLE PAGES: {len(winnable)} (non-brand demand, position 4–20, ~0 clicks)")
    print("=" * 74)

    for rank, (url, impr, pos, query, pclk) in enumerate(winnable, 1):
        path = urlparse(url).path or url
        try:
            title, meta, h1 = _extract(_fetch(url))
        except Exception as e:
            title, meta, h1 = "", "", ""
            fetch_err = f"(could not fetch page: {type(e).__name__})"
        else:
            fetch_err = ""
        time.sleep(0.4)

        print(f"\n{rank}. {path}")
        print(f"   TARGET QUERY : \"{query}\"  ({impr} impr/{args.days}d @ pos {pos:.1f}, {pclk} clicks)")
        if fetch_err:
            print(f"   {fetch_err}")
        print(f"   CURRENT TITLE: {title or '(none found)'}")

        # --- title recommendation ---
        rw = _title_rewrite(query, title)
        if rw is None:
            print(f"   ✓ TITLE OK   : query already in title — do NOT retitle. "
                  f"The gap is POSITION/authority → internal links below.")
        else:
            print(f"   → NEW TITLE  : {rw}   ({len(rw)} chars)")

        # --- meta recommendation ---
        if not meta:
            print(f"   → META (add) : lead with \"{query}\" + one real, specific hook "
                  f"(selection, age fit, your curation) — no invented stats.")
        elif query.lower() not in meta.lower():
            print(f"   → META edit  : front-load \"{query}\" into your existing description.")
            print(f"                  current: {meta[:90]}{'…' if len(meta) > 90 else ''}")
        else:
            print(f"   ✓ META OK    : query already present.")

        # --- internal-link sources (topical overlap, crawl-free) ---
        qtok = _tokens(query)
        cands = []
        for surl, stok in src_index.items():
            if surl == url or "shop-by-brands" in surl:
                continue
            overlap = qtok & stok
            if len(overlap) >= 1:
                # prefer blog/hub pages (natural to link FROM) and stronger overlap
                weight = len(overlap) + (1 if "/blog/" in surl else 0)
                cands.append((weight, surl, overlap))
        cands.sort(key=lambda x: -x[0])

        target_path = urlparse(url).path
        picked = []
        for weight, surl, overlap in cands:
            if len(picked) >= args.max_sources:
                break
            already = False
            if not args.no_verify:
                try:
                    shtml = _fetch(surl)
                    already = target_path in shtml
                    time.sleep(0.4)
                except Exception:
                    already = False
            if already:
                continue
            picked.append((surl, overlap))

        if picked:
            print(f"   → ADD INTERNAL LINKS (anchor = \"{query}\"), from:")
            for surl, overlap in picked:
                sp = urlparse(surl).path
                print(f"        • {sp}   (shares: {', '.join(sorted(overlap))})")
        else:
            print(f"   → INTERNAL LINKS: no clean topical source found in GSC set; "
                  f"link from homepage + the montessori-toys hub with anchor \"{query}\".")

    print("\n" + "=" * 74)
    print("HOW TO USE: titles/meta are reversible 5-min edits. Internal links are the")
    print("real lever — they move these pages from page 2 to page 1 using authority you")
    print("ALREADY have (no new backlinks). Do the internal links first.")
    print("\nRead-only — nothing modified.")


if __name__ == "__main__":
    main()
