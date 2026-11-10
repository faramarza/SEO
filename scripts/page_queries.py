#!/usr/bin/env python3
"""Everything Google shows for a group of your pages: every search they appear for,
how often people click, which of your pages competes for the same search, and
(optionally) what page 1 of Google looks like for the biggest searches.

Run on the server:

    /var/www/seo/venv/bin/python scripts/page_queries.py --match name-train --match name-trains
    ... --days 90           (window, default 90)
    ... --top 40            (searches to list, default 40)
    ... --serp 5            (also fetch Google page 1 for the top N searches, via Serper)

Read-only.
"""
import argparse
import collections
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

TYPICAL_CTR = {1: .27, 2: .15, 3: .10, 4: .07, 5: .05, 6: .04, 7: .03, 8: .025, 9: .02, 10: .017}


def typical(pos):
    p = max(1, int(round(pos)))
    return TYPICAL_CTR.get(p, .01 if p <= 15 else .005)


def group(rows, matches):
    """{query: [rows for matching pages]} plus totals."""
    out = collections.defaultdict(list)
    for r in rows:
        path = urlparse(r["page"]).path
        if any(m.lower() in path.lower() for m in matches):
            out[r["query"]].append({**r, "path": path})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", action="append", required=True,
                    help="text in the page address, e.g. name-train (repeatable)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--serp", type=int, default=0)
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    from src.data_sources.gsc_client import GSCClient
    g = cfg["data_sources"]["gsc"]
    gsc = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
    rows = gsc.get_query_page_rows(days=args.days)
    q = group(rows, args.match)
    months = args.days / 30.0

    pages = collections.defaultdict(lambda: [0, 0])
    for rs in q.values():
        for r in rs:
            pages[r["path"]][0] += r["impressions"]
            pages[r["path"]][1] += r["clicks"]
    print("=" * 104)
    print(f"SEARCHES FOR PAGES MATCHING {args.match}   last {args.days} days (per month)")
    print("=" * 104)
    print("\nPAGES")
    for path, (imp, clk) in sorted(pages.items(), key=lambda x: -x[1][0]):
        print(f"   {path[:60]:<62}{imp/months:>8,.0f} impr{clk/months:>7,.0f} clicks  CTR {clk/imp*100 if imp else 0:.1f}%")

    ranked = sorted(q.items(), key=lambda kv: -sum(r["impressions"] for r in kv[1]))
    print(f"\nTOP {args.top} SEARCHES   (typical = normal click rate at that position)")
    print(f"   {'search':<36}{'impr/mo':>8}{'clk/mo':>7}{'CTR':>7}{'typical':>8}{'pos':>6}  pages shown")
    for query, rs in ranked[:args.top]:
        imp = sum(r["impressions"] for r in rs)
        clk = sum(r["clicks"] for r in rs)
        pos = sum(r["position"] * r["impressions"] for r in rs) / imp if imp else 0
        pg = ", ".join(f"{r['path'].strip('/')[:28]}({r['impressions']})" for r in
                       sorted(rs, key=lambda r: -r["impressions"])[:3])
        flag = "  <- 2+ pages compete" if len(rs) > 1 else ""
        print(f"   {query[:35]:<36}{imp/months:>8,.0f}{clk/months:>7,.1f}{clk/imp*100 if imp else 0:>6.1f}%"
              f"{typical(pos)*100:>7.1f}%{pos:>6.1f}  {pg}{flag}")

    if args.serp:
        from src.data_sources import serp_client
        if not os.environ.get("SERPER_API_KEY"):
            print("\n(SERP skipped: SERPER_API_KEY not set)")
            return
        for query, _ in ranked[:args.serp]:
            res = serp_client.fetch_serp(query)
            if not res:
                print(f"\n\"{query}\": no SERP (quota or error)")
                continue
            print(f"\nGOOGLE PAGE 1 for \"{query}\"   features: {', '.join(res.get('serp_features', [])) or '-'}")
            for r in res.get("organic_results", [])[:10]:
                mine = "  <== YOU" if "alphabet-trains.com" in r["url"] else ""
                print(f"   {r['position']:>2}. {urlparse(r['url']).netloc[:26]:<27}{(r.get('title') or '')[:58]}{mine}")
                if mine:
                    print(f"       your snippet: {(r.get('snippet') or '')[:150]}")


if __name__ == "__main__":
    main()
