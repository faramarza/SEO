#!/usr/bin/env python3
"""Why do your top pages rank #1 and get ZERO clicks? Query-level ground truth.

Run on the server (venv Python — it needs the Google libs):

    /var/www/seo/venv/bin/python scripts/diagnose_zero_click_queries.py
    ... --days 90 --pages 15 --min-impr 300

For each high-impression / near-zero-click page it answers the three questions
that decide what (if anything) to fix — WITHOUT guessing:

  1. IMAGE vs WEB.   It pulls the page's impressions under type='web' AND
     type='image' separately. If the impressions are mostly IMAGE, the page is
     showing as a thumbnail in an image pack — clicks go to Google's image viewer,
     not your site. That is NOT fixable with a title; stop blaming the snippet.

  2. COMMODITY vs DEMAND.  It lists the actual web queries the page ranks for.
     If they're the exact product name ("super mom personalized book") — a
     resold catalog item a dozen sites carry — you rank but lose the click to the
     brand/Amazon. That's a positioning problem, not a title problem. If they're
     broad, high-intent terms with real volume, a better snippet genuinely wins.

  3. POSITION REALITY.  avg position per query — "position 1.0" across a page can
     hide that it's #1 for a zero-demand exact-name query and #20 for the term
     that matters.

Read-only. Only GSC reads. Nothing is modified.
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "defaults.json") as f:
        return json.load(f)


def _service(config):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    gsc = config["data_sources"]["gsc"]
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / gsc["credentials_path"]),
        scopes=['https://www.googleapis.com/auth/webmasters.readonly'],
    )
    return build('searchconsole', 'v1', credentials=creds), gsc["property_url"]


def _query(service, prop, start, end, dimensions, search_type="web",
           page_filter=None, row_limit=25000):
    body = {
        'startDate': start, 'endDate': end,
        'dimensions': dimensions, 'type': search_type,
        'rowLimit': row_limit,
    }
    if page_filter:
        body['dimensionFilterGroups'] = [{
            'filters': [{'dimension': 'page', 'operator': 'equals',
                         'expression': page_filter}]
        }]
    return service.searchanalytics().query(siteUrl=prop, body=body).execute().get('rows', [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pages", type=int, default=15, help="how many wasted pages to inspect")
    ap.add_argument("--min-impr", type=int, default=300)
    ap.add_argument("--top-queries", type=int, default=6)
    args = ap.parse_args()

    config = load_config()
    service, prop = _service(config)
    end = (date.today() - timedelta(days=3)).isoformat()
    start = (date.today() - timedelta(days=3 + args.days)).isoformat()

    # 1) find the wasted pages: high web impressions, (near) zero clicks
    print(f"Window {start} → {end}  (type=web)\n")
    rows = _query(service, prop, start, end, ['page'], "web")
    wasted = sorted(
        [{'url': r['keys'][0], 'impr': r.get('impressions', 0), 'clicks': r.get('clicks', 0),
          'pos': r.get('position', 0.0)}
         for r in rows
         if r.get('impressions', 0) >= args.min_impr and r.get('clicks', 0) == 0],
        key=lambda x: -x['impr'])[:args.pages]

    if not wasted:
        print(f"No pages with ≥{args.min_impr} web impressions and 0 clicks. "
              f"(Lower --min-impr to widen.)")
        return

    print(f"Inspecting {len(wasted)} pages (≥{args.min_impr} web impr, 0 web clicks):\n")

    for w in wasted:
        url = w['url']
        path = urlparse(url).path or url
        # image-search impressions for the same page
        img_rows = _query(service, prop, start, end, ['page'], "image", page_filter=url)
        img_impr = sum(r.get('impressions', 0) for r in img_rows)
        img_clicks = sum(r.get('clicks', 0) for r in img_rows)
        # web queries for this page
        q_rows = _query(service, prop, start, end, ['query'], "web", page_filter=url)
        q_rows = sorted(q_rows, key=lambda r: -r.get('impressions', 0))[:args.top_queries]

        share = ""
        denom = w['impr'] + img_impr
        if denom:
            share = f"  ({100*img_impr/denom:.0f}% of impressions are IMAGE)"
        print("─" * 72)
        print(f"{path}")
        print(f"  web: {w['impr']:>6} impr / {w['clicks']} clicks @ pos {w['pos']:.1f}"
              f"   |   image: {img_impr:>6} impr / {img_clicks} clicks{share}")
        if not q_rows:
            print("  (no query rows returned — likely anonymized long-tail)")
            continue
        print(f"  {'impr':>6} {'clk':>4} {'pos':>5}  query")
        for r in q_rows:
            print(f"  {r.get('impressions',0):>6} {r.get('clicks',0):>4} "
                  f"{r.get('position',0.0):>5.1f}  {r['keys'][0][:52]}")

    print("\n" + "─" * 72)
    print("READING IT:")
    print("  • >60% IMAGE impressions      → thumbnail in image pack; a title won't help.")
    print("  • queries = exact product name → commodity resale; you rank but the brand/")
    print("                                   Amazon wins the click. Reposition, don't retitle.")
    print("  • broad high-intent queries    → real demand; better title/snippet WILL win clicks.")
    print("\nRead-only — nothing modified.")


if __name__ == "__main__":
    main()
