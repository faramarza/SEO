#!/usr/bin/env python3
"""Product-led demand: start from EVERYTHING you sell and find what people type
to look for it. One run covers the whole catalog; nothing to type per product.

    /var/www/seo/venv/bin/python scripts/product_demand.py
    ... --top 60            (searches to list, default 60)
    ... --expand 80         (seed phrases to expand into all their variants, default 80)
    ... --min-volume 30

HOW
  1. Your sellable products (no TAG, discontinued, disabled, out of stock).
  2. For each product, the ways to describe it ("seeds"):
       - its product type: the last 2-3 meaningful words ("tummy time mirror")
       - its main noun with your qualifiers ("montessori mirror", "baby mirror",
         "kids mirror", "classroom mirror", "personalized ...", "wooden ...")
       - its category names ("montessori mirrors")
  3. Monthly US volume for every seed (one bulk request), keep the real ones.
  4. The top seeds are EXPANDED: every real search that contains them, with volume
     (DataForSEO Labs keyword suggestions, cached 30 days).
  5. Each search gets: your Google position (Search Console), the page that should
     own it, the product price, its value at #3, and a CHEAP-INTENT flag when the
     searcher clearly wants a cheap version (diy, cheap, printable, ...).
Writes data/product_demand.csv. Read-only.
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.line_demand import _load_dotenv, load_catalog, search_volumes, value  # noqa: E402
from scripts.demand_map import product_margin, organic_conversion, load_gsc_queries, is_informational  # noqa: E402

_load_dotenv()

QUALIFIERS = ("montessori", "baby", "infant", "toddler", "kids", "classroom", "preschool",
              "daycare", "wooden", "personalized", "name", "sensory")
NOISE = {"set", "sets", "pc", "pcs", "piece", "pieces", "kit", "pack", "with", "and", "for", "the",
         "of", "in", "x", "ft", "inch", "inches", "large", "small", "medium", "mini", "jr", "deluxe",
         "classic", "contemporary", "natural", "white", "black", "blue", "red", "green", "yellow",
         "pink", "purple", "orange", "grey", "gray", "primary", "nature", "rectangle", "oval",
         "round", "square", "edition", "new", "letter", "letters", "high", "low", "by", "s"}
BRAND_WORDS = {"guidecraft", "guide", "craft", "whitney", "brothers", "maple", "landmark",
               "landmarks", "carpets", "carpet", "for", "kids", "iseeme", "cubby", "hole",
               "hollow", "woodworks", "tag", "toys", "kidsoft", "grippies", "powerclix", "plus"}
CHEAP_INTENT = re.compile(r"\b(cheap|diy|how to make|printable|free|dollar|ikea|amazon|walmart|"
                          r"target|used|second ?hand|plans?|pattern)\b", re.I)
OFF_TOPIC = re.compile(r"\b(car ?seat|car|bathroom|makeup|vanity|selfie|sunglass|wall decor|"
                       r"tattoo|meme|lyrics|song|movie|game ?of)\b", re.I)
CACHE = ROOT / "data" / "product_demand_cache.json"
SUGGEST_URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_suggestions/live"


# ---------------------------------------------------------------- seeds (pure)
def name_words(name):
    words = re.findall(r"[a-z]+", (name or "").lower().replace("®", " ").replace("™", " "))
    return [w for w in words if w not in NOISE and len(w) > 1]


def product_seeds(name, categories=()):
    """All reasonable ways to describe one product, e.g. 'Montessori Tummy Time
    Mirror' -> tummy time mirror, time mirror(dropped), montessori mirror, baby
    mirror, kids mirror, ... Pure, unit-tested."""
    w = [x for x in name_words(name) if x not in BRAND_WORDS or x in QUALIFIERS]
    core = [x for x in w if x not in QUALIFIERS]
    seeds = set()
    if core:
        head = core[-1]
        if len(core) >= 2:
            seeds.add(" ".join(core[-2:]))
        if len(core) >= 3:
            seeds.add(" ".join(core[-3:]))
        # the bare noun ("mirror", "table") is NOT a seed: it drags in bathroom
        # mirrors and dining tables. It only counts with a qualifier.
        for q in QUALIFIERS:
            seeds.add(f"{q} {head}")
            if len(core) >= 2:
                seeds.add(f"{q} {' '.join(core[-2:])}")
    for c in categories:
        c = re.sub(r"\s+", " ", (c or "").lower()).strip()
        if 3 < len(c) <= 40 and not re.search(r"brand|sale|best|new|featured|shop", c):
            seeds.add(c)
    return {s for s in seeds if len(s) > 3 and len(s.split()) >= 2 and s.split()[-1] not in NOISE}


def plural_key(s):
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in s.split())


# ---------------------------------------------------------------- expansion
def _cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def expand(seed, cache):
    """Every real search containing `seed`, with volume. Cached 30 days."""
    hit = cache.get(seed)
    if hit and (time.time() - hit.get("t", 0)) < 30 * 86400:
        return hit["items"]
    import httpx
    from src.data_sources import dataforseo_client as dfs
    body = [{"keyword": seed, "location_code": dfs.LOCATION_CODE, "language_code": dfs.LANGUAGE_CODE,
             "include_seed_keyword": True, "limit": 100,
             "order_by": ["keyword_info.search_volume,desc"],
             "filters": [["keyword_info.search_volume", ">=", 10]]}]
    try:
        r = httpx.post(SUGGEST_URL, headers={"Authorization": f"Basic {dfs._auth_token()}",
                                             "Content-Type": "application/json"},
                       json=body, timeout=90.0)
        task = (r.json().get("tasks") or [{}])[0]
        if (task.get("status_code") or 0) >= 40000:
            print(f"   (suggestions for '{seed}': {task.get('status_message')})")
            return []
        items = ((task.get("result") or [{}])[0] or {}).get("items") or []
    except Exception as e:
        print(f"   (suggestions for '{seed}' failed: {e})")
        return []
    out = [{"keyword": (it.get("keyword") or "").lower(),
            "volume": (it.get("keyword_info") or {}).get("search_volume") or 0,
            "cpc": (it.get("keyword_info") or {}).get("cpc") or 0} for it in items if it.get("keyword")]
    cache[seed] = {"t": time.time(), "items": out}
    return out


def match_products(keyword, seed_products, products):
    """Products a search belongs to: those whose seeds appear inside it."""
    k = f" {plural_key(keyword)} "
    best, best_len = set(), 0
    for seed, idx in seed_products.items():
        if f" {plural_key(seed)} " in k and len(seed) >= best_len:
            if len(seed) > best_len:
                best = set()
            best |= idx
            best_len = len(seed)
    return best


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--expand", type=int, default=80)
    ap.add_argument("--min-volume", type=int, default=30)
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    from scripts.line_demand import _magento
    mc = _magento()
    cats, products = load_catalog(mc)
    seed_products = defaultdict(set)
    for i, p in enumerate(products):
        names = [cats.get(c, {}).get("name", "") for c in p.get("cats", []) if cats.get(c, {}).get("active")]
        for s in product_seeds(p["name"], names):
            seed_products[s].add(i)
    print(f"{len(products)} sellable products -> {len(seed_products)} ways to describe them")

    vols = search_volumes(list(seed_products))
    real = sorted(((s, vols.get(s, {}).get("volume", 0)) for s in seed_products),
                  key=lambda x: -x[1])
    real = [(s, v) for s, v in real if v >= args.min_volume]
    print(f"{len(real)} of them are real searches (>= {args.min_volume}/mo); expanding the top {args.expand}")

    cache = _cache()
    found = {}
    for s, v in real:
        found[s] = {"keyword": s, "volume": v, "cpc": vols.get(s, {}).get("cpc", 0)}
    for s, _ in real[:args.expand]:
        for it in expand(s, cache):
            if it["volume"] >= args.min_volume:
                found.setdefault(it["keyword"], it)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache))

    gsc = load_gsc_queries(cfg)
    conv, _, _ = organic_conversion(cfg)
    rows = []
    for kw, it in found.items():
        if OFF_TOPIC.search(kw):
            continue
        idx = match_products(kw, seed_products, products)
        if not idx:
            continue
        prods = [products[i] for i in idx]
        p = max(prods, key=lambda x: x["price"])
        mine = gsc.get(kw, {})
        price = sorted(x["price"] for x in prods)[len(prods) // 2]
        margin = product_margin(p, cfg)
        _, profit = value(it["volume"], mine.get("position"), mine.get("clicks", 0),
                          mine.get("impressions", 0), conv, price, margin)
        flag = "CHEAP" if CHEAP_INTENT.search(kw) else ("INFO" if is_informational(kw) else "")
        rows.append({"search": kw, "volume": it["volume"], "cpc": it.get("cpc", 0),
                     "you": round(mine["position"], 1) if mine.get("position") else None,
                     "clicks": mine.get("clicks", 0), "products": len(prods),
                     "example": p["name"][:40], "price": round(price, 2),
                     "profit": 0 if flag else profit, "flag": flag, "url": p["url"]})
    rows.sort(key=lambda r: -r["profit"])

    print(f"\n{'search':<38}{'vol/mo':>8}{'you':>6}{'clk':>5}{'+$/mo':>7}  {'flag':<6}{'price':>8}  example product")
    for r in rows[:args.top]:
        you = f"{r['you']:.0f}" if r["you"] else "-"
        print(f"{r['search'][:37]:<38}{r['volume']:>8,}{you:>6}{r['clicks']:>5}{r['profit']:>7,.0f}  "
              f"{r['flag']:<6}{r['price']:>8.2f}  {r['example']} (+{r['products'] - 1})")
    out = ROOT / "data" / "product_demand.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["search"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nAll {len(rows)} searches -> {out}")
    print("you = your Google position (Search Console, 90 days). +$/mo = profit at #3 at your "
          "conversion and margin. CHEAP = shoppers want the cheap version; INFO = readers, not buyers.")


if __name__ == "__main__":
    main()
