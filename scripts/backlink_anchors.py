#!/usr/bin/env python3
"""Backlink anchors: which of YOUR pages other sites link to, and with what anchor
text. Answers "do my links carry the words people search for, and do they point at
the pages that should rank?"

Run on the server (DataForSEO credentials in .env):

    /var/www/seo/venv/bin/python scripts/backlink_anchors.py
    ... --limit 1000          (backlinks to fetch, default 1000, one per referring domain)
    ... --page classroom-rugs (only links pointing at URLs containing this text)

Read-only. One DataForSEO Backlinks request.
"""
import argparse
import collections
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

BRAND = re.compile(r"(?i)alphabet[\s-]*trains?|alphabettrains")
GENERIC = re.compile(r"(?i)^(click here|here|this|website|link|read more|more|visit|shop|"
                     r"source|homepage|home|site|learn more|this site|view|see more)$")


def anchor_kind(anchor, page_words=()):
    """brand | url | generic | empty | keyword (shares a word with the target page) | other."""
    a = (anchor or "").strip()
    if not a:
        return "empty/image"
    if BRAND.search(a):
        return "brand"
    if re.search(r"(?i)https?://|www\.|\.com", a):
        return "url"
    if GENERIC.match(a):
        return "generic"
    words = set(re.findall(r"[a-z]{4,}", a.lower()))
    if words & set(page_words):
        return "keyword"
    return "other"


def page_words(path):
    return {w for w in re.findall(r"[a-z]{4,}", path.lower()) if w not in {"html", "kids"}}


def fetch(limit):
    import httpx
    from src.data_sources import dataforseo_client as dfs
    if not dfs.is_configured():
        sys.exit("DataForSEO not configured (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD in .env).")
    body = [{"target": "alphabet-trains.com", "limit": max(1, min(limit, 1000)),
             "mode": "one_per_domain", "order_by": ["domain_from_rank,desc"],
             "include_subdomains": True}]
    r = httpx.post("https://api.dataforseo.com/v3/backlinks/backlinks/live",
                   headers={"Authorization": f"Basic {dfs._auth_token()}",
                            "Content-Type": "application/json"}, json=body, timeout=120.0)
    if r.status_code != 200:
        sys.exit(f"DataForSEO backlinks error {r.status_code}: {r.text[:200]}")
    task = (r.json().get("tasks") or [{}])[0]
    if (task.get("status_code") or 0) >= 40000:
        sys.exit(f"DataForSEO backlinks: {task.get('status_message')}")
    items = ((task.get("result") or [{}])[0].get("items")) or []
    return [{"from": (it.get("domain_from") or "").replace("www.", ""),
             "to": urlparse(it.get("url_to") or "").path or "/",
             "anchor": (it.get("anchor") or "").strip(),
             "dofollow": bool(it.get("dofollow")),
             "rank": it.get("domain_from_rank") or 0} for it in items]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--page", default=None)
    args = ap.parse_args()

    links = fetch(args.limit)
    if args.page:
        links = [l for l in links if args.page.lower() in l["to"].lower()]
    if not links:
        sys.exit("No backlinks returned.")

    by_page = collections.defaultdict(list)
    for l in links:
        by_page[l["to"]].append(l)
    kinds = collections.Counter(anchor_kind(l["anchor"], page_words(l["to"])) for l in links)

    print("=" * 100)
    print(f"BACKLINK ANCHORS  {len(links)} referring domains (one link each), alphabet-trains.com")
    print("=" * 100)
    print("anchor types overall: " + ", ".join(f"{k} {v} ({v*100/len(links):.0f}%)"
                                              for k, v in kinds.most_common()))
    print(f"\nWHERE THEY POINT (top 25 pages)\n   {'page':<52}{'domains':>8}{'dofollow':>9}  "
          f"{'keyword anchors':>15}")
    for path, ls in sorted(by_page.items(), key=lambda x: -len(x[1]))[:25]:
        kw = sum(1 for l in ls if anchor_kind(l["anchor"], page_words(path)) == "keyword")
        print(f"   {path[:51]:<52}{len(ls):>8}{sum(l['dofollow'] for l in ls):>9}  {kw:>15}")

    money = ["classroom-rugs", "wooden-blocks", "personalized-books", "wooden-name-puzzles",
             "personalized-step-stools", "name-trains", "montessori-toys", "classroom-furniture",
             "kids-tables", "personalized-baby-blankets"]
    print("\nYOUR MONEY PAGES: links and the anchors used")
    for m in money:
        ls = [l for l in links if m in l["to"]]
        anchors = collections.Counter((l["anchor"] or "(image/empty)").lower()[:40] for l in ls)
        print(f"   {m:<28}{len(ls):>4} domains   " +
              (", ".join(f'"{a}" x{c}' for a, c in anchors.most_common(5)) if ls else "none"))
    print("\nTop anchor texts overall:")
    for a, c in collections.Counter((l["anchor"] or "(image/empty)").lower()[:50]
                                    for l in links).most_common(15):
        print(f"   {c:>4}  {a}")


if __name__ == "__main__":
    main()
