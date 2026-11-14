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


def _live_check(url, timeout=20):
    """(ok, reason). Skips pages that redirect away (closed TAG products now 301
    to replacements) or show Out of stock: no point optimizing what can't sell."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        r = urllib.request.urlopen(req, timeout=timeout)
        html = r.read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"could not fetch ({type(e).__name__})", ""
    if r.geturl().split("?")[0].rstrip("/") != url.split("?")[0].rstrip("/"):
        return False, f"redirects to {urlparse(r.geturl()).path}", ""
    if re.search(r'class="stock unavailable"', html):
        return False, "out of stock", ""
    return True, "", html


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


SYNONYMS = {"rug": "carpet", "rugs": "carpet", "carpets": "carpet", "personalised": "personalized",
            "toy": "toys", "book": "books", "storybook": "books", "olds": "old", "month": "months"}


def _norm_tokens(s):
    return {SYNONYMS.get(w, w) for w in _tokens(s)}


def _junk_query(q):
    """Searches not worth a title: quoted product codes, dimension-only strings."""
    letters = re.findall(r"[a-z]{3,}", q.lower())
    return '"' in q or len(letters) < 2


def _title_rewrite(query, current_title):
    """None if the title already covers the search (synonyms count: rug = carpet).
    Otherwise a NOTE, not a ready-made title: write it by hand as one plain phrase
    with the search words first, no "|" or dash separators (Google rewrites those)."""
    cur = (current_title or "").strip()
    if cur and _norm_tokens(query) <= _norm_tokens(cur):
        return None
    missing = sorted(_norm_tokens(query) - _norm_tokens(cur))
    return (f"title lacks {', '.join(missing)}: rewrite as one plain phrase leading with "
            f"\"{query}\" (no | or - separators, under 60 chars)")


def recently_changed(log_path, within_days=42):
    """{path: date} for pages in docs/seo_change_log.md changed in the last N days.
    Search Console data still reflects the OLD page, so don't re-advise them."""
    from datetime import date as _d, datetime as _dt
    out = {}
    try:
        text = open(log_path).read()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.search(r"(20\d\d-\d\d-\d\d)", line)
        if not m:
            continue
        try:
            d = _dt.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if (_d.today() - d).days > within_days:
            continue
        for path in re.findall(r"(/[a-z0-9][a-z0-9/_-]*\.html)", line):
            out.setdefault(path, m.group(1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--min-impr", type=int, default=150)
    ap.add_argument("--no-verify", action="store_true", help="skip live link-source checks")
    ap.add_argument("--max-sources", type=int, default=4)
    ap.add_argument("--min-pos", type=float, default=4.0,
                    help="lowest avg position to include (default 4)")
    ap.add_argument("--max-pos", type=float, default=20.0,
                    help="highest avg position to include (default 20; use 30 for page 3)")
    ap.add_argument("--max-page-clicks", type=int, default=1,
                    help="only pages with at most this many clicks (default 1; "
                         "raise it to include pages that already get some traffic)")
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
        if _is_brand(query) or "shop-by-brands" in url or _junk_query(query):
            continue
        impr = r.get('impressions', 0)
        pos = r.get('position', 0.0)
        clk = r.get('clicks', 0)
        if impr < args.min_impr or not (args.min_pos <= pos <= args.max_pos):
            continue
        cur = best.get(url)
        if not cur or impr > cur[0]:
            best[url] = (impr, clk, pos, query)

    # winnable page = has such a query AND the page overall earns ~0 clicks
    winnable = []
    for url, (impr, clk, pos, query) in best.items():
        page_clicks = pages.get(url, {}).get('clicks', 0)
        if page_clicks <= args.max_page_clicks:
            winnable.append((url, impr, pos, query, page_clicks))
    winnable.sort(key=lambda x: -x[1])
    kept, skipped, pages_html = [], [], {}
    for w in winnable:
        if len(kept) >= args.pages:
            break
        ok, why, html = _live_check(w[0])
        if ok:
            kept.append(w)
            pages_html[w[0]] = html
        else:
            skipped.append((urlparse(w[0]).path, why))
        time.sleep(0.3)
    winnable = kept
    if skipped:
        print(f"Skipped {len(skipped)} page(s) that can't sell today: "
              + "; ".join(f"{p} ({why})" for p, why in skipped[:15]))

    if not winnable:
        print("No winnable pages matched (non-brand query, pos 4–20, "
              f"≥{args.min_impr} impr, page ~0 clicks). Loosen --min-impr.")
        return

    recent = recently_changed(str(PROJECT_ROOT / "docs" / "seo_change_log.md"))
    print(f"Window {start} → {end}\n")
    print(f"WINNABLE PAGES: {len(winnable)} (non-brand demand, position "
          f"{args.min_pos:g}-{args.max_pos:g}, <= {args.max_page_clicks} page clicks)")
    print("=" * 74)

    for rank, (url, impr, pos, query, pclk) in enumerate(winnable, 1):
        path = urlparse(url).path or url
        try:
            title, meta, _h1 = _extract(pages_html.get(url) or _fetch(url))
        except Exception as e:
            title, meta, _h1 = "", "", ""
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
        changed = recent.get(path)
        rw = _title_rewrite(query, title)
        if changed:
            print(f"   ⏸ CHANGED {changed}: per the change log. Search Console still shows the old page; "
                  "do NOT retitle, judge it after 4-6 weeks.")
        elif rw is None:
            print("   ✓ TITLE OK   : search words already in the title (synonyms count). "
                  "The gap is position: internal links below.")
        else:
            print(f"   → TITLE      : {rw}")

        # --- meta recommendation ---
        if not meta:
            print(f"   → META (add) : lead with \"{query}\" + one real, specific hook "
                  f"(selection, age fit, your curation) — no invented stats.")
        elif query.lower() not in meta.lower():
            print(f"   → META edit  : front-load \"{query}\" into your existing description.")
            print(f"                  current: {meta[:90]}{'…' if len(meta) > 90 else ''}")
        else:
            print("   ✓ META OK    : query already present.")

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
