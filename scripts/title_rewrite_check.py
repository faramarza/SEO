#!/usr/bin/env python3
"""Does Google show YOUR meta title, or rewrite it? Measured on your own pages.

For your top pages (by Search Console impressions) it takes each page's biggest
search, looks at Google page 1 (Serper), and compares the title Google shows
with the <title> on the live page. Then it groups the results by title style
(with a separator like | or -, or plain words) so the rule is decided by data.

    /var/www/seo/venv/bin/python scripts/title_rewrite_check.py
    ... --pages 30     (pages to test, default 30; one Serper search each, cached)
    ... --days 90

Read-only.
"""
import argparse
import collections
import html
import json
import os
import re
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

SEPARATOR = re.compile(r"\s[|\-:–]\s|\|")


def norm(t):
    t = html.unescape(t or "").lower()
    t = t.replace("...", " ").replace("…", " ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def style(title):
    return "separator" if SEPARATOR.search(title or "") else "plain"


def verdict(mine, shown):
    """kept / truncated / site-name-changed / rewritten."""
    m, s = norm(mine), norm(shown)
    if not m or not s:
        return "unknown"
    if m == s:
        return "kept"
    if (shown or "").rstrip().endswith(("...", "…")) and m.startswith(s):
        return "truncated"
    # Google often only swaps the part after the separator for the site name.
    first = norm(SEPARATOR.split(mine)[0]) if SEPARATOR.search(mine) else m
    if first and s.startswith(first):
        return "site-name-changed"
    if s.startswith(m):
        return "kept"
    return "rewritten"


def live_title(url):
    import requests
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (title check)"})
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        return html.unescape(m.group(1)).strip() if m else ""
    except Exception:
        return ""


def summarize(results):
    out = collections.defaultdict(collections.Counter)
    for r in results:
        out[r["style"]][r["verdict"]] += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    from src.data_sources.gsc_client import GSCClient
    from src.data_sources import serp_client
    if not os.environ.get("SERPER_API_KEY"):
        sys.exit("SERPER_API_KEY not set in .env")
    g = cfg["data_sources"]["gsc"]
    gsc = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
    rows = gsc.get_query_page_rows(days=args.days)

    by_page = collections.defaultdict(list)
    for r in rows:
        by_page[r["page"]].append(r)
    pages = sorted(by_page, key=lambda p: -sum(r["impressions"] for r in by_page[p]))

    results = []
    for page in pages:
        if len(results) >= args.pages:
            break
        # biggest search where the page is on page 1 (so it shows up in the SERP)
        cands = sorted((r for r in by_page[page] if r["position"] <= 10),
                       key=lambda r: -r["impressions"])
        if not cands:
            continue
        query = cands[0]["query"]
        serp = serp_client.fetch_serp(query) or {}
        path = urlparse(page).path
        shown = next((o.get("title") for o in serp.get("organic_results", [])
                      if urlparse(o.get("url", "")).path == path
                      and "alphabet-trains.com" in o.get("url", "")), None)
        if shown is None:
            continue
        mine = live_title(page)
        v = verdict(mine, shown)
        results.append({"path": path, "query": query, "mine": mine, "shown": shown,
                        "style": style(mine), "verdict": v})
        print(f"\n{path}   search: \"{query}\"   -> {v.upper()}  ({style(mine)})")
        print(f"   your title : {mine}")
        print(f"   Google shows: {shown}")

    print("\n" + "=" * 80)
    print("SUMMARY  (kept = Google shows your words; site-name-changed = only the part after")
    print("the separator was swapped for the site name; rewritten = Google wrote its own)")
    for st, c in sorted(summarize(results).items()):
        n = sum(c.values())
        print(f"   {st:<10} {n:>3} pages: " + ", ".join(f"{k} {v} ({v/n*100:.0f}%)" for k, v in c.most_common()))


if __name__ == "__main__":
    main()
