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
                       r"tattoo|meme|lyrics|song|movie|game ?of|dining|coffee|console|folding|"
                       r"foldable|fold|picnic|legs?|topper|floating|patio|office|bedroom|tv|"
                       r"kitchen cabinets?|cabinets? in kitchen|kitchen shelf|high ?chair|bouncer|"
                       r"swing|gaming|electronic|ipad|samsung|amazon fire|recliner|outdoor furniture|"
                       r"baby chair|infant chair|baby seat|infant seat|pottery barn|melissa|doug|lakeshore|"
                       r"ikea|kidkraft|step2|lovevery|amtrak|minecraft|lego|hair|hairstyles?|dress|"
                       r"dresses|dna|lipid|carbohydrate|nucleic|concrete|garage|bundle branch|rbbb|jam level)\b",
                       re.I)
CACHE = ROOT / "data" / "product_demand_cache.json"
SUGGEST_URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_suggestions/live"


# ---------------------------------------------------------------- seeds (pure)
def name_words(name):
    words = re.findall(r"[a-z]+", (name or "").lower().replace("®", " ").replace("™", " "))
    return [w for w in words if w not in NOISE and len(w) > 1]


def plural_key(s):
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in s.split())


# What a product IS. A search must name one of these to count.
KINDS = ("rug", "carpet", "mat", "mirror", "train", "puzzle", "stool", "table", "chair", "cart",
         "shelf", "bookshelf", "bookcase", "cabinet", "block", "blocks", "book", "storybook", "easel",
         "bench", "sorter", "stacker", "tower", "truck", "car", "kitchen", "house", "playhouse",
         "cube", "locker", "cubby", "display", "rack", "chest", "lightbox", "light table",
         "toy", "toys", "game", "sensory table", "drum", "tracker", "board", "rocker")
# Proof the searcher shops for a child or a classroom (else "usa map", "tow truck").
INTENT = ("montessori", "baby", "infant", "toddler", "kid", "kids", "child", "children", "childrens",
          "classroom", "preschool", "daycare", "school", "nursery", "teacher",
          "personalized", "personalised", "name", "toy", "toys", "tummy", "sensory", "learning",
          "educational", "play", "playroom", "alphabet", "abc", "letter", "circle time", "seating",
          "stem", "girls", "boys")


def _has(text, words):
    t = f" {plural_key(text.lower())} "
    return any(f" {plural_key(w)} " in t for w in words)


def kind_of(name, categories=()):
    """The noun saying what the product is, from its name, else its categories."""
    for text in [name, *categories]:
        words = [plural_key(w) for w in re.findall(r"[a-z]+", (text or "").lower())]
        for w in reversed(words):
            if w in {plural_key(k) for k in KINDS}:
                return w
    return None


# Kinds that are children's products by themselves ("wooden blocks", "name train").
KID_KINDS = ("block", "blocks", "puzzle", "toy", "toys", "sorter", "stacker", "playhouse", "tracker",
             "train", "storybook", "easel")


def qualified(keyword):
    """A search counts only if it names a product kind AND shows child/classroom
    intent (or the kind is a children's product by itself)."""
    return _has(keyword, KINDS) and (_has(keyword, INTENT) or _has(keyword, KID_KINDS))


def product_seeds(name, categories=()):
    """Ways to describe one product, always ending in what it IS:
    'Carpet for kids USA Map 8ft x 12ft' -> usa map rug, map rug, kids rug,
    classroom rug... ; 'Montessori Tummy Time Mirror' -> tummy time mirror,
    montessori mirror, baby mirror... Pure, unit-tested."""
    kind = kind_of(name, categories)
    if not kind:
        return set()
    kinds = {kind, "rug"} if kind == "carpet" else {kind}
    w = [x for x in name_words(name) if (x not in BRAND_WORDS or x in QUALIFIERS)
         and plural_key(x) != kind]
    desc = [x for x in w if x not in QUALIFIERS and x not in {plural_key(k) for k in KINDS}]
    seeds = set()
    for k in kinds:
        if desc:
            seeds.add(f"{desc[-1]} {k}")
            if len(desc) >= 2:
                seeds.add(f"{desc[-2]} {desc[-1]} {k}")
        for q in QUALIFIERS:
            seeds.add(f"{q} {k}")
    for c in categories:
        c = re.sub(r"\s+", " ", (c or "").lower()).strip()
        if 3 < len(c) <= 40 and not re.search(r"brand|sale|best|new|featured|shop", c):
            seeds.add(c)
    return {x for x in seeds if len(x.split()) >= 2 and qualified(x) or (len(x.split()) >= 2 and _has(x, KINDS) and len(x.split()) >= 3)}


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


# ---------------------------------------------------------------- price fit
KID_WORDS = re.compile(r"\b(kids?|child|children'?s?|toddlers?|baby|babies|infants?|montessori|"
                       r"classroom|preschool|daycare|school|nursery|toys?|educational|learning|"
                       r"girls?|boys?|playroom|play|teacher|stem|alphabet|abc|personali[sz]ed)\b", re.I)


def shopping_check(query, cache):
    """(median price, share of products that are for kids/classrooms) from what
    Google Shopping shows for the search. Google decides what a search means:
    'bundle branch block' shows medical items, 'color block hair' hair products.
    (None, None) when unavailable. Cached 30 days."""
    hit = cache.get("shop2|" + query)
    if hit and (time.time() - hit.get("t", 0)) < 30 * 86400:
        return hit.get("median"), hit.get("kid_share")
    items = _shopping_items(query)
    if items is None:
        return None, None
    prices = [p for _, p in items if p]
    med = sorted(prices)[len(prices) // 2] if prices else None
    share = (sum(1 for t, _ in items if KID_WORDS.search(t or "")) / len(items)) if items else None
    cache["shop2|" + query] = {"t": time.time(), "median": med, "kid_share": share}
    return med, share


def _shopping_items(query):
    import os
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        SHOP_STATUS["error"] = "SERPER_API_KEY not set"
        return None
    import requests
    try:
        r = requests.post("https://google.serper.dev/shopping", timeout=30,
                          headers={"X-API-KEY": key, "Content-Type": "application/json"},
                          json={"q": query, "gl": "us", "num": 20})
        SHOP_STATUS["checked"] += 1
        if r.status_code != 200:
            SHOP_STATUS["error"] = f"Serper shopping HTTP {r.status_code}: {r.text[:120]}"
            return None
        out = []
        for it in (r.json().get("shopping") or []):
            m = re.search(r"([\d,]+\.?\d*)", str(it.get("price") or ""))
            out.append((it.get("title") or "", float(m.group(1).replace(",", "")) if m else None))
        if out:
            SHOP_STATUS["priced"] += 1
        return out
    except Exception as e:
        SHOP_STATUS["error"] = f"Serper shopping failed: {e}"
        return None


def shopping_median(query, cache):
    """Median price Google Shopping shows for a search (Serper, cached 30 days).
    None when unavailable. Used to flag searches where shoppers expect far less."""
    import os
    key = os.environ.get("SERPER_API_KEY")
    hit = cache.get("shop|" + query)
    if hit and (time.time() - hit.get("t", 0)) < 30 * 86400:
        return hit.get("median")
    if not key:
        SHOP_STATUS["error"] = "SERPER_API_KEY not set"
        return None
    import requests
    try:
        r = requests.post("https://google.serper.dev/shopping", timeout=30,
                          headers={"X-API-KEY": key, "Content-Type": "application/json"},
                          json={"q": query, "gl": "us", "num": 20})
        SHOP_STATUS["checked"] += 1
        if r.status_code != 200:
            SHOP_STATUS["error"] = f"Serper shopping HTTP {r.status_code}: {r.text[:120]}"
            return None
        prices = []
        for it in (r.json().get("shopping") or []):
            m = re.search(r"([\d,]+\.?\d*)", str(it.get("price") or ""))
            if m:
                prices.append(float(m.group(1).replace(",", "")))
    except Exception as e:
        SHOP_STATUS["error"] = f"Serper shopping failed: {e}"
        return None
    if prices:
        SHOP_STATUS["priced"] += 1
    med = sorted(prices)[len(prices) // 2] if prices else None
    cache["shop|" + query] = {"t": time.time(), "median": med}
    return med


SHOP_STATUS = {"checked": 0, "priced": 0, "error": ""}


def price_flag(my_price, median, ratio=2.5):
    """'PRICEY' when you charge more than `ratio` x what shoppers see. Pure."""
    if not median or not my_price:
        return ""
    return "PRICEY" if my_price > ratio * median else ""


# ---------------------------------------------------------------- main
def variant_key(r):
    """Same search typed differently ('classroom carpet rugs', 'classrooms carpet') ->
    one key: identical volume + same word set (plural-insensitive). Pure."""
    words = frozenset(plural_key(w) for w in re.findall(r"[a-z]+", r["search"])
                      if w not in {"and", "for", "the", "of", "in", "with"})
    return (r["volume"], words)


def collapse_variants(rows):
    groups = {}
    for r in rows:
        k = variant_key(r)
        cur = groups.get(k)
        if cur is None:
            groups[k] = dict(r, variants=1)
        else:
            cur["variants"] += 1
            if (r.get("you") or 999) < (cur.get("you") or 999) or len(r["search"]) < len(cur["search"]) \
                    and not cur.get("you"):
                keep = cur["variants"]
                groups[k] = dict(r, variants=keep)
    return list(groups.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--expand", type=int, default=80)
    ap.add_argument("--min-volume", type=int, default=30)
    ap.add_argument("--price-check", type=int, default=150,
                    help="check Google Shopping prices for the top N searches (Serper, cached)")
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
        if OFF_TOPIC.search(kw) or not qualified(kw):
            continue
        idx = match_products(kw, seed_products, products)
        if not idx:
            continue
        prods = [products[i] for i in idx]
        mine = gsc.get(kw, {})
        price = sorted(x["price"] for x in prods)[len(prods) // 2]
        p = min(prods, key=lambda x: abs(x["price"] - price))
        margin = product_margin(p, cfg)
        _, profit = value(it["volume"], mine.get("position"), mine.get("clicks", 0),
                          mine.get("impressions", 0), conv, price, margin)
        flag = "CHEAP" if CHEAP_INTENT.search(kw) else ("INFO" if is_informational(kw) else "")
        rows.append({"search": kw, "volume": it["volume"], "cpc": it.get("cpc", 0),
                     "you": round(mine["position"], 1) if mine.get("position") else None,
                     "clicks": mine.get("clicks", 0), "products": len(prods),
                     "example": p["name"][:40], "price": round(price, 2),
                     "profit": 0 if flag else profit, "flag": flag, "url": p["url"]})
    rows = collapse_variants(rows)
    rows.sort(key=lambda r: -r["profit"])
    # Google decides: walk down the list, ask Google Shopping what each search shows,
    # keep only searches where most products are for kids/classrooms, and flag
    # PRICEY ones. Stop once `top` searches are verified or the budget is spent.
    verified, checks = [], 0
    for r in rows:
        if len(verified) >= args.top or checks >= args.price_check:
            break
        if r["flag"]:
            continue
        med, share = shopping_check(r["search"], cache)
        checks += 1
        r["shop_median"], r["kid_share"] = med, share
        if share is not None and share < 0.3:
            r["flag"], r["profit"] = "NOTKIDS", 0
            continue
        if price_flag(r["price"], med):
            r["flag"], r["profit"] = "PRICEY", 0
        r["verified"] = share is not None
        verified.append(r)
    CACHE.write_text(json.dumps(cache))
    rows.sort(key=lambda r: -r["profit"])
    print(f"price check: {SHOP_STATUS['checked']} searches looked up, {SHOP_STATUS['priced']} had "
          f"Shopping results; dropped {sum(1 for r in rows if r['flag'] == 'NOTKIDS')} that Google shows "
          f"as not for kids, {sum(1 for r in rows if r['flag'] == 'PRICEY')} PRICEY" + (f"  [PROBLEM: {SHOP_STATUS['error']}]" if SHOP_STATUS["error"] else ""))

    print(f"\n{'search':<38}{'vol/mo':>8}{'you':>6}{'clk':>5}{'+$/mo':>7}  {'flag':<6}{'price':>8}  example product")
    show = [r for r in rows if r.get("verified")] or rows
    if not any(r.get("verified") for r in rows):
        print("(Google Shopping check unavailable: showing UNVERIFIED searches)")
    for r in show[:args.top]:
        you = f"{r['you']:.0f}" if r["you"] else "-"
        print(f"{r['search'][:37]:<38}{r['volume']:>8,}{you:>6}{r['clicks']:>5}{r['profit']:>7,.0f}  "
              f"{r['flag']:<6}{r['price']:>8.2f}  {r['example']} (+{r['products'] - 1})"
              + (f"  [{r['variants']} ways typed]" if r.get("variants", 1) > 1 else ""))
    out = ROOT / "data" / "product_demand.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}) if rows else ["search"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nAll {len(rows)} searches -> {out}")
    print("you = your Google position (Search Console, 90 days). +$/mo = profit at #3 at your "
          "conversion and margin. CHEAP = shoppers want the cheap version; INFO = readers, not buyers;\n"
          "PRICEY = Google Shopping shows this for under 40% of your price (shoppers expect a cheaper item).")


if __name__ == "__main__":
    main()
