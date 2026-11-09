#!/usr/bin/env python3
"""SERP diagnosis: for each money query, WHO holds page 1 and WHY you don't.
Turns "is it content or links?" into a per-query verdict with evidence.

Run on the server (needs SERPER_API_KEY in .env; DataForSEO optional):

    /var/www/seo/venv/bin/python scripts/serp_diagnosis.py
    ... --no-content       (skip fetching competitor pages for word counts)
    ... --file q.txt       (one "query | /your-url.html" per line instead of defaults)

For each query it checks, in order (first hit wins, the rest still printed):
  B. MARKETPLACE WALL  4+ of the top 10 are Amazon/Walmart/Target/Etsy/etc.
                       Organic page 1 is very hard; go long-tail or paid.
  I. INTENT MISMATCH   6+ of the top 10 are articles/guides, but your ranking
                       page is a category/product. Google wants a guide here;
                       a better product page will not move. Fix: a guide (or a
                       guide section on the category) that links to products.
  D. CANNIBALIZATION   Google shows a DIFFERENT page of yours than the target.
  C. CONTENT GAP       Your page has under half the words of the top 3 shops.
  F. AUTHORITY GAP     Your page has far fewer referring domains than page 1
                       (only when DataForSEO is configured). Needs backlinks.
  L. ON-PAGE / LINKS   None of the above: title/meta + internal links first.

Read-only. Uses the shared Serper cache/quota (14-day cache), so re-runs are free.
"""
import argparse
import os
import re
import statistics
import sys
import time
import urllib.request
from html import unescape
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

OWN = "alphabet-trains.com"
UA = "Mozilla/5.0 (compatible; AlphabetTrains-SEO-Crawler/1.0; +serp-diagnosis)"

# Commercial queries from the winnable sheet (blog-only informational queries
# left out on purpose: they bring readers, not buyers).
DEFAULT_TARGETS = [
    ("montessori toys", "/montessori-toys.html"),
    ("best toys for imaginative play", "/pretend-play-toys.html"),
    ("classroom rugs", "/rainbow-seating-rug-8x13.html"),
    ("montessori toys for 4 year olds", "/montessori-toys-4-year-olds.html"),
    ("personalized fairy tale book", "/my-very-own-fairy-tale-personalized-story-book.html"),
    ("personalized race car book", "/speedster-a-day-in-the-life-of-a-race-car-driver-from-chronicle-books.html"),
    ("personalized train book", "/personalized-books-for-kids.html"),
    ("best toys for building independence", "/montessori-toys-for-independence.html"),
    ("personalized goodnight book", "/goodnight-little-me-personalized-story-book.html"),
    ("my very own name book", "/my-very-own-name-personalized-story-book.html"),
    ("personalized coat rack for kids", "/ch-kids-personalized-coat-rack.html"),
    ("personalized toy box", "/personalized-toy-chest.html"),
    ("sorting toys", "/sorting-toys.html"),
    ("toys for sensory development", "/sensory-toys.html"),
    ("personalized big sister book", "/the-super-incredible-big-sister-book-medal.html"),
    ("alphabet carpet", "/alphabet-themed-carpets.html"),
    ("puppet theater for kids", "/puppet-theatre.html"),
    ("maple landmark name train", "/5-letter-name-train.html"),
]

MARKETPLACES = (
    "amazon.", "walmart.", "target.com", "etsy.", "ebay.", "wayfair.", "kohls.",
    "lakeshorelearning.", "kaplanco.", "schoolspecialty.", "michaels.", "costco.",
    "barnesandnoble.", "macys.", "nordstrom.", "crateandbarrel.", "potterybarnkids.",
    "potterybarn.", "lovevery.", "learningresources.", "melissaanddoug.",
    "fatbraintoys.", "discountschoolsupply.", "becker.", "hearthsong.",
)
EDITORIAL_HOSTS = (
    "reddit.", "quora.", "pinterest.", "youtube.", "wikipedia.", "nytimes.",
    "babylist.", "whattoexpect.", "parents.com", "verywellfamily.", "thebump.",
    "goodhousekeeping.", "today.com", "forbes.", "buzzfeed.", "medium.com",
    "healthline.", "motherly.", "scarymommy.", "romper.", "bustle.", "cnn.",
    "wirecutter", "familyeducation.", "montessorinature.", "thekavanaughreport.",
)
EDITORIAL_URL = re.compile(r"/(blog|blogs|article|articles|news|guide|guides|post|posts|"
                           r"stories|learn|resources|ideas)/", re.I)
EDITORIAL_TITLE = re.compile(r"\b(best|top \d+|\d+ (best|great|fun)|guide|ideas|how to|"
                             r"what (is|are)|review|reviews|vs\.?|tips)\b", re.I)


def classify_result(url: str, title: str) -> str:
    """'own' | 'marketplace' | 'editorial' | 'shop'. Pure, unit-tested."""
    host = (urlparse(url).netloc or "").lower()
    if OWN in host:
        return "own"
    if any(m in host for m in MARKETPLACES):
        return "marketplace"
    if any(e in host for e in EDITORIAL_HOSTS) or EDITORIAL_URL.search(url):
        return "editorial"
    if EDITORIAL_TITLE.search(title or "") and not re.search(r"/(products?|collections?|shop)/", url):
        return "editorial"
    return "shop"


def verdict(kinds, own_urls, target_path, own_words=None, comp_words=None,
            own_rd=None, comp_rd=None):
    """Return (code, reason). Pure, unit-tested."""
    mk = kinds.count("marketplace")
    ed = kinds.count("editorial")
    target_is_shop_page = "/blog/" not in target_path
    if mk >= 4:
        return "B", f"{mk}/10 are marketplaces/big retailers"
    if ed >= 6 and target_is_shop_page:
        return "I", f"{ed}/10 are articles/guides; Google wants a guide, you offer a shop page"
    if own_urls and not any(urlparse(u).path.rstrip("/") == target_path.rstrip("/")
                            for u in own_urls):
        return "D", "Google shows your " + ", ".join(urlparse(u).path for u in own_urls) \
            + f" instead of {target_path}"
    if own_words and comp_words:
        med = statistics.median(comp_words)
        if own_words < 0.5 * med:
            return "C", f"your page ~{own_words} words vs top shops ~{int(med)}"
    if own_rd is not None and comp_rd:
        med = statistics.median(comp_rd)
        if med >= 5 and own_rd < med / 3:
            return "F", f"your page {own_rd} referring domains vs page-1 median {int(med)}"
    return "L", "page 1 is beatable on-page: fix title/meta and add internal links first"


def _fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def word_count(html: str) -> int:
    """Rough main-content word count: drop scripts/styles/nav/header/footer."""
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    return len(re.findall(r"[A-Za-z]{2,}", text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help='lines of "query | /your-path.html"')
    ap.add_argument("--no-content", action="store_true", help="skip word-count fetches")
    args = ap.parse_args()

    targets = DEFAULT_TARGETS
    if args.file:
        targets = []
        for line in Path(args.file).read_text().splitlines():
            if "|" in line:
                q, p = line.split("|", 1)
                targets.append((q.strip(), p.strip()))

    from src.data_sources import serp_client
    if not os.environ.get("SERPER_API_KEY"):
        sys.exit("SERPER_API_KEY not set (in /var/www/seo/.env). It powers Click Yield; "
                 "add it there and re-run.")
    try:
        from src.data_sources import dataforseo_client as dfs
        use_dfs = dfs.is_configured()
    except Exception:
        dfs, use_dfs = None, False

    print(f"SERP DIAGNOSIS  {len(targets)} queries  (Serper quota left today: "
          f"{serp_client.get_remaining_quota()})"
          + ("  [DataForSEO authority check ON]" if use_dfs else
             "  [authority check OFF: DataForSEO not configured]"))
    print("=" * 78)
    tally = {}
    for q, path in targets:
        res = serp_client.fetch_serp(q)
        if not res:
            print(f"\n\"{q}\": no SERP (quota exhausted or API error). Stopping.")
            break
        top = res.get("organic_results", [])[:10]
        kinds = [classify_result(r["url"], r.get("title", "")) for r in top]
        own_urls = [r["url"] for r, k in zip(top, kinds) if k == "own"]
        own_pos = next((r["position"] for r, k in zip(top, kinds) if k == "own"), None)

        own_words = comp_words = None
        if not args.no_content:
            try:
                own_words = word_count(_fetch(f"https://www.{OWN}{path}"))
            except Exception:
                try:
                    own_words = word_count(_fetch(f"https://{OWN}{path}"))
                except Exception:
                    own_words = None
            comp_words = []
            for r, k in zip(top, kinds):
                if k == "shop" and len(comp_words) < 3:
                    try:
                        comp_words.append(word_count(_fetch(r["url"])))
                    except Exception:
                        pass
                    time.sleep(0.5)
            comp_words = [w for w in comp_words if w > 50] or None

        own_rd = comp_rd = None
        if use_dfs:
            o = dfs.fetch_backlinks_summary(f"{OWN}{path}")
            own_rd = o.get("referring_domains") if o.get("available") else None
            comp_rd = []
            for r, k in zip(top, kinds):
                if k in ("shop", "editorial") and len(comp_rd) < 5:
                    c = dfs.fetch_backlinks_summary(r["url"])
                    if c.get("available"):
                        comp_rd.append(c.get("referring_domains", 0))
            comp_rd = comp_rd or None

        code, why = verdict(kinds, own_urls, path, own_words, comp_words, own_rd, comp_rd)
        tally[code] = tally.get(code, 0) + 1
        mix = {k: kinds.count(k) for k in ("marketplace", "editorial", "shop", "own")}
        print(f"\n[{code}] \"{q}\"  -> {path}")
        print(f"    you: {'#' + str(own_pos) if own_pos else 'not in top 10'}   page 1 mix: "
              f"{mix['marketplace']} marketplace, {mix['editorial']} articles, "
              f"{mix['shop']} shops" + (f", {mix['own']} yours" if mix["own"] else ""))
        print(f"    WHY: {why}")
        if own_words or comp_words:
            print(f"    words: yours {own_words or '?'}  vs top shops {comp_words or '?'}")
        if own_rd is not None or comp_rd:
            print(f"    referring domains: yours {own_rd}  vs page 1 {comp_rd}")
        for r, k in zip(top[:10], kinds):
            print(f"      {r['position']:>2}. {k[:4]:<5} {urlparse(r['url']).netloc[:30]:<31}"
                  f"{(r.get('title') or '')[:42]}")
        if res.get("serp_features"):
            print(f"    features: {', '.join(res['serp_features'])}")

    names = {"B": "marketplace wall", "I": "intent mismatch (needs a guide)",
             "D": "cannibalization", "C": "content gap", "F": "authority gap (backlinks)",
             "L": "on-page + internal links"}
    print("\n" + "=" * 78)
    print("SUMMARY: " + ", ".join(f"{names[k]} {v}" for k, v in sorted(tally.items())))


if __name__ == "__main__":
    main()
