#!/usr/bin/env python3
"""Which of your blog posts should link to a page? Finds posts that talk about the
topic, says whether they already link to the page, and ranks them by how much
Google shows them (a link from a seen page passes more).

    /var/www/seo/venv/bin/python scripts/link_sources.py --target alphabet-themed-carpets \
        --word alphabet --word "abc" --word letter

Read-only. Lists Mirasvit blog posts via Magento REST; GSC for impressions.
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


BRAND = re.compile(r"alphabet[- ]?trains?(\.com)?", re.I)


def clean(html):
    """Visible text only: drop <script>/<style> (schema JSON) and the store name."""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    return BRAND.sub(" ", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))


def text_of(html):
    return clean(html).lower()


def score_post(content, words, target):
    """-> (mentions per word, already_links)."""
    t = text_of(content)
    hits = {w: len(re.findall(r"\b" + re.escape(w.lower()) + r"s?\b", t)) for w in words}
    links = target.lower() in (content or "").lower()
    return hits, links


def sentences_with(content, words, n=2):
    t = clean(content)
    out = []
    for s in re.split(r"(?<=[.!?])\s+", t):
        if any(re.search(r"\b" + re.escape(w) + r"s?\b", s, re.I) for w in words):
            out.append(s.strip()[:220])
        if len(out) >= n:
            break
    return out


def list_posts(mc):
    page, out = 1, []
    while True:
        q = (f"/blog/posts?searchCriteria[pageSize]=100&searchCriteria[currentPage]={page}")
        data = mc._req("GET", q) or {}
        items = data.get("items") or []
        out += items
        if not items or page * 100 >= int(data.get("total_count") or 0):
            return out
        page += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="text in the target URL, e.g. alphabet-themed-carpets")
    ap.add_argument("--word", action="append", required=True, help="topic word (repeatable)")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured")
    posts = list_posts(mc)

    impr = collections.Counter()
    try:
        cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
        from src.data_sources.gsc_client import GSCClient
        g = cfg["data_sources"]["gsc"]
        gsc = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
        for r in gsc.get_query_page_rows(days=args.days):
            slug = urlparse(r["page"]).path.rstrip("/").split("/")[-1]
            impr[slug] += r["impressions"]
    except Exception as e:
        print(f"(GSC unavailable: {e}; ranking by mentions only)")

    rows = []
    for p in posts:
        content = p.get("content") or ""
        hits, links = score_post(content, args.word, args.target)
        if sum(hits.values()) == 0:
            continue
        rows.append({"id": p.get("entity_id") or p.get("post_id") or p.get("id"), "name": p.get("name", ""), "slug": p.get("url_key", ""),
                     "hits": hits, "links": links, "impr": impr.get(p.get("url_key", ""), 0),
                     "examples": sentences_with(content, args.word)})
    rows.sort(key=lambda r: (r["links"], -r["impr"], -sum(r["hits"].values())))

    print(f"{len(posts)} blog posts; {len(rows)} mention {args.word}; "
          f"{sum(r['links'] for r in rows)} already link to '{args.target}'\n")
    for r in rows[:args.top]:
        state = "ALREADY LINKS" if r["links"] else "no link yet"
        print(f"post {r['id']}  /blog/{r['slug']}   {r['impr']:,} impr/{args.days}d   {state}")
        print(f"   {r['name'][:90]}")
        print("   mentions: " + ", ".join(f"{w} {n}" for w, n in r["hits"].items() if n))
        for s in r["examples"]:
            print(f"   > {s}")
        print()


if __name__ == "__main__":
    main()
