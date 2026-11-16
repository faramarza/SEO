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
  5. Searches you already show up for in Search Console are added too (proven demand),
     plus gift searches for the personalized products. Your own brand is left out.
  6. Each search gets: your Google position, the page that should own it, the product
     price and its value at #3. CHEAP and INFO searches are set aside.
  7. GOOGLE DECIDES: every search that is listed or planned was looked up on Google
     Shopping. Under 30% kids' products -> dropped; complete products much cheaper
     than yours -> PRICEY. Each product line is checked first, so no line is skipped.
     Unvetted searches are never listed.
  8. Page plan, grouped by owning page, at most 4 per line first; pages changed in the
     last 6 weeks wait. --why "search" shows every step for one search.
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

from scripts.line_demand import _load_dotenv, load_catalog, search_volumes, valid_keyword, value  # noqa: E402
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
                       r"baby chair|infant chair|baby seat|infant seat|chairs? (for )?(an )?(infant|baby)|pottery barn|melissa|doug|lakeshore|"
                       r"ikea|kidkraft|step2|lovevery|amtrak|minecraft|lego|hair|hairstyles?|dress|"
                       r"dresses|dna|lipid|carbohydrate|nucleic|concrete|garage|bundle branch|rbbb|jam level|"
                       r"blind ?box(es)?|blind toy|collectibles?|funko|pop ?mart|squishmallows?|figures?|"
                       r"anime|adult|adults|1000 piece|500 piece|online|status|schedule|tickets?|"
                       r"thomas|tank engine|coal|steam|model train|embossers?|medical|cardiac|"
                       r"squishy|fidget|stress|toy story|disney|pixar|marvel|paw patrol|bluey|cocomelon|"
                       r"frozen|barbie|pokemon|star wars|sesame|peppa|costumes?|halloween|"
                       r"little tikes|fisher ?price|vtech|hape|playmobil|skip hop|green toys|b\. ?toys|"
                       r"battat|janod|djeco|grimms?|delta children|hey bear|leapfrog|crayola|radio flyer)\b",
                       re.I)
# Gift searches: people shopping FOR a child without naming the product ("personalized
# toddler gifts", "first birthday gift"). They belong to the personalized products.
GIFT_SEEDS = ("personalized gifts for kids", "personalized baby gifts", "personalized toddler gifts",
              "first birthday gift", "1st birthday gifts", "gifts for 1 year old", "gifts for 2 year old",
              "gifts for 3 year old", "toddler gifts", "baby gifts", "name gifts for kids",
              "personalized christmas gifts for kids", "baptism gifts", "new baby gifts",
              "gifts for grandchildren", "personalized gifts for grandchildren")
GIFT_WORD = re.compile(r"\b(gifts?|presents?)\b", re.I)
GIFT_FOR_CHILD = re.compile(r"\b(kids?|child|children'?s?|toddlers?|baby|babies|infants?|newborns?|boys?|"
                            r"girls?|grand(son|daughter|child|children|kids?)s?|nephew|niece|"
                            r"birthday|christening|baptism|([1-9]|1[0-2]) ?(year|yr)s?[- ]?old|\d+ ?months?[- ]?old|"
                            r"1st birthday|first birthday|first christmas|1st christmas)\b", re.I)
GIFT_OFF = re.compile(r"\b(cards?|baskets?|wrap|wrapping|bags?|boxes|box|registry|cash|money|"
                      r"mom|moms|mother|dad|dads|father|men|women|him|her|husband|wife|boyfriend|"
                      r"girlfriend|teachers?|coworkers?|boss|adults?|parents|nurse|shower|"
                      r"delivery|near me|anniversary|wedding|retirement|graduation|valentines?|"
                      r"employees?|clients?|couples?|friends?|sister|brother|grandparents?|grandma|grandpa)\b", re.I)


def rival_brand(q, rivals):
    """'i see me personalized book' is a search for iseeme.com: unwinnable. Pure."""
    flat = re.sub(r"[^a-z]", "", q.lower())
    return any(r and len(r) > 4 and r in flat for r in rivals)


def gift_search(keyword):
    """A search for a gift for a child, not for an adult and not gift wrap. Pure."""
    return bool(GIFT_WORD.search(keyword) and GIFT_FOR_CHILD.search(keyword)
                and not GIFT_OFF.search(keyword) and not OFF_TOPIC.search(keyword))


def is_personalized(product, cat_names=()):
    return bool(re.search(r"personali[sz]ed|\bname\b", " ".join([product.get("name", ""), *cat_names]), re.I))


CACHE = ROOT / "data" / "product_demand_cache.json"
SUGGEST_URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_suggestions/live"


# ---------------------------------------------------------------- seeds (pure)
def name_words(name):
    words = re.findall(r"[a-z]+", (name or "").lower().replace("®", " ").replace("™", " "))
    return [w for w in words if w not in NOISE and len(w) > 1]


MATCH_SYN = {"carpet": "rug", "box": "chest", "boxe": "chest", "childrens": "kids", "children": "kids",
             "child": "kids", "kid": "kids", "personalised": "personalized"}


def plural_key(s):
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in s.split())


def match_key(s):
    """Plural- and synonym-insensitive form: 'classroom carpets' == 'classroom rug',
    'wooden toy box' == 'wooden toy chest'."""
    return " ".join(MATCH_SYN.get(w, w) for w in plural_key(s.lower()).split())


# What a product IS. A search must name one of these to count.
KINDS = ("rug", "carpet", "mat", "mirror", "box", "train", "puzzle", "stool", "table", "chair", "cart",
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
    # "toy" may describe ("toy chest"); other kind words may not.
    desc = [x for x in w if x not in QUALIFIERS and (x in ("toy", "toys") or
                                                     x not in {plural_key(k) for k in KINDS})]
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
        # A stool in "Wooden Name Puzzles" must not take the puzzle searches.
        ck = head_kind(c)
        if ck and ck not in ("toy",) and ck != match_key(kind):
            continue
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


def head_kind(text):
    """The LAST product-kind word in a phrase: 'wooden toy chest' -> chest."""
    kinds = {match_key(k) for k in KINDS if " " not in k}
    words = [match_key(w) for w in re.findall(r"[a-z]+", (text or "").lower())]
    for w in reversed(words):
        if w in kinds:
            return w
    return None


def match_products(keyword, seed_products, products):
    """Products a search belongs to: those whose seeds appear inside it. Seeds
    naming the same thing the search ends on win ('wooden toy chest' belongs to
    toy chests, not to every 'wooden toy'); then the longest seed wins."""
    k = f" {match_key(keyword)} "
    head = head_kind(keyword)
    cands = [(seed, idx) for seed, idx in seed_products.items() if f" {match_key(seed)} " in k]
    same = [(s_, i) for s_, i in cands if head_kind(s_) == head]
    if head and not same:
        # "name puzzle stool": no stool seed fits, but puzzle seeds do. Never hand a
        # stool search to puzzles; use products of the searched kind whose name has
        # every word of the search, or none at all.
        want = norm_tokens(keyword) - norm_tokens(" ".join(QUALIFIERS) + " for")
        hits = {i for i, p in enumerate(products or [])
                if match_key(kind_of(p.get("name", "")) or "") == head
                and want <= norm_tokens(p.get("name", ""))}
        return hits
    cands = same or cands
    best, best_len = set(), 0
    for seed, idx in cands:
        if len(seed) > best_len:
            best, best_len = set(idx), len(seed)
        elif len(seed) == best_len:
            best |= idx
    return best


# ---------------------------------------------------------------- price fit
# "toy" alone is NOT proof: blind boxes, Funko, collectibles all say "toy".
KID_WORDS = re.compile(r"\b(kids?|child|children'?s?|toddlers?|baby|babies|infants?|montessori|"
                       r"classroom|preschool|daycare|school|nursery|educational|learning|"
                       r"playroom|teacher|alphabet|abc|personali[sz]ed|ages? \d|\d\+ ?(years?|months?)|"
                       r"\d+ ?(year|yr|month)s?[- ]?old|newborn|boys?|girls?|birthday|grand(son|daughter|kids?))\b",
                       re.I)


def shopping_check(query, cache):
    """(median price, share of products that are for kids/classrooms) from what
    Google Shopping shows for the search. Google decides what a search means:
    'bundle branch block' shows medical items, 'color block hair' hair products.
    Raw titles/prices are cached 30 days, so a better kids-rule never needs a
    paid re-lookup. (None, None) when unavailable."""
    hit = cache.get("shopitems|" + query)
    if hit and (time.time() - hit.get("t", 0)) < 30 * 86400:
        items = [tuple(x) for x in hit["items"]]
        SHOP_STATUS["cached"] += 1
    else:
        items = _shopping_items(query)
        if items is None:
            return None, None
        cache["shopitems|" + query] = {"t": time.time(), "items": items}
    prices = sorted(p for _, p in items if p)
    med = prices[len(prices) // 2] if prices else None
    # Upper quartile: cheap parts (single letter cars at $7) drag the median down,
    # so compare your price with what complete products cost.
    upper = prices[(len(prices) * 3) // 4] if prices else None
    share = (sum(1 for t, _ in items if kid_item(t, query)) / len(items)) if items else None
    return med, share, upper


def kid_item(title, query):
    """A Shopping result is a kids' product if its title says so, or if the search
    itself is a kids' search ('name train', 'montessori ...') and the title is that
    same product ('Name Trains in Bright Colors'). Pure."""
    if KID_WORDS.search(title or ""):
        return True
    if not _has(query, INTENT):
        return False
    want = {match_key(w) for w in query.lower().split()}
    have = {match_key(w) for w in re.findall(r"[a-z]+", (title or "").lower())}
    have |= {match_key(w) for w in re.findall(r"[a-z]+", re.sub(r"(?i)name(?=[a-z])", "name ", title or "").lower())}
    return want <= have


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


INTENT_URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/search_intent/live"


def search_intents(keywords, cache):
    """{keyword: (label, probability)} from DataForSEO Labs search intent (cached 30
    days; one request per 1000). Empty dict when unavailable."""
    out, todo = {}, []
    for k in keywords:
        hit = cache.get("intent|" + k)
        if hit and (time.time() - hit.get("t", 0)) < 30 * 86400:
            out[k] = tuple(hit["v"])
        else:
            todo.append(k)
    if not todo:
        return out
    try:
        import httpx
        from src.data_sources import dataforseo_client as dfs
        for i in range(0, len(todo), 1000):
            r = httpx.post(INTENT_URL, headers={"Authorization": f"Basic {dfs._auth_token()}",
                                                "Content-Type": "application/json"},
                           json=[{"keywords": todo[i:i + 1000], "language_code": "en"}], timeout=120.0)
            task = (r.json().get("tasks") or [{}])[0]
            if (task.get("status_code") or 0) >= 40000:
                print(f"(search intent unavailable: {task.get('status_message')})")
                return out
            for it in ((task.get("result") or [{}])[0] or {}).get("items") or []:
                ki = it.get("keyword_intent") or {}
                v = (ki.get("label") or "", float(ki.get("probability") or 0))
                out[(it.get("keyword") or "").lower()] = v
                cache["intent|" + (it.get("keyword") or "").lower()] = {"t": time.time(), "v": list(v)}
    except Exception as e:
        print(f"(search intent unavailable: {e})")
    return out


def navigational(intent):
    """True when Google reads the search as looking for one business or site. Pure."""
    return bool(intent) and intent[0] == "navigational" and intent[1] >= 0.5


SHOP_STATUS = {"checked": 0, "priced": 0, "cached": 0, "error": ""}


def price_flag(my_price, median, ratio=2.5):
    """'PRICEY' when you charge more than `ratio` x what shoppers see. Pure."""
    if not median or not my_price:
        return ""
    return "PRICEY" if my_price > ratio * median else ""


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- page plan
SKIP_CAT = re.compile(r"brand|bestseller|best seller|sale|new|clearance|featured|top selling|"
                      r"latest|gift|shop by|default|alphabetstore", re.I)
SYN = {"rugs": "rug", "carpets": "rug", "carpet": "rug", "personalised": "personalized",
       "toys": "toy", "box": "chest", "boxes": "chest", "chests": "chest", "childrens": "kids",
       "children": "kids", "child": "kids", "kid": "kids"}
STOP_T = {"and", "for", "the", "of", "a", "with", "in", "to"}


def norm_tokens(text):
    return {SYN.get(w, plural_key(w)) for w in re.findall(r"[a-z]+|\d+", (text or "").lower())
            if w not in STOP_T}


def category_sizes(products, cats):
    """Sellable products per category, counting its subcategories too (Montessori
    Toys holds its products in 'Montessori Toys for 2 Year Olds' etc.). Pure."""
    sizes = defaultdict(int)
    for p in products:
        seen = set()
        for c in p.get("cats", []):
            seen.add(c)
            for a in str((cats.get(c) or {}).get("path") or "").split("/"):
                if a.isdigit() and int(a) in cats:
                    seen.add(int(a))
        for c in seen:
            sizes[c] += 1
    return sizes


MIN_CATEGORY_PRODUCTS = 3   # a category with fewer sellable products is a dead page


def named_category(search, cats, cat_sizes=None):
    """The active category whose whole name is inside the search ('montessori toys'
    -> Montessori Toys, 'classroom carpet' -> Classroom Rugs). Longest name wins.
    Gift categories are skipped, except for gift searches."""
    st = norm_tokens(search)
    gift = gift_search(search)
    best = None
    for cid, c in cats.items():
        name = c.get("name", "")
        if not c.get("active") or not c.get("url_key"):
            continue
        # A leftover category (its brand went away, like Toy Boxes) can't own a search.
        if cat_sizes and cat_sizes.get(cid, 0) < MIN_CATEGORY_PRODUCTS:
            continue
        if SKIP_CAT.search(name) and not (gift and re.search(r"gift", name, re.I)
                                          and not re.search(r"card|certificate", name, re.I)):
            continue
        ct = norm_tokens(name)
        if ct and ct <= st and (best is None or len(ct) > len(norm_tokens(best["name"]))):
            best = c
    return best


def owner_page(idx, products, cats, cat_sizes, search=""):
    """Which page should own a search: a category named in the search; else a single
    product's page; else the most specific active category holding >= half the
    matched products; else None (new page)."""
    named = named_category(search, cats, cat_sizes) if search else None
    if named:
        return f"/{named.get('url_path') or named['url_key']}.html", named["name"], "category"
    idx = list(idx)
    if len(idx) == 1:
        p = products[idx[0]]
        return p["url"], p["name"], "product"
    counts = defaultdict(int)
    for i in idx:
        for c in products[i].get("cats", []):
            counts[c] += 1
    best = None
    for c, n in counts.items():
        cat = cats.get(c) or {}
        if not cat.get("active") or not cat.get("url_key") or SKIP_CAT.search(cat.get("name", "")):
            continue
        if n / len(idx) >= 0.5:
            key = (cat_sizes.get(c, 9999), -n)
            if best is None or key < best[0]:
                best = (key, c)
    if best:
        cat = cats[best[1]]
        return f"/{cat.get('url_path') or cat['url_key']}.html", cat["name"], "category"
    return None, None, "new"


def natural_phrase(searches):
    """The best-formed way to say a group's search: one that ends in what the product
    is ('large classroom carpet', not 'classroom rug large'). Pure."""
    for x in searches:
        if head_kind(x["search"]) and match_key(x["search"].split()[-1]) == head_kind(x["search"]):
            return x["search"]
    return searches[0]["search"]


def page_action(searches, title, h1, phrase=None):
    """What to do for one page, from its live title/H1 and its searches. Pure."""
    top = dict(searches[0], search=phrase or searches[0]["search"])
    covered = norm_tokens(top["search"]) <= norm_tokens(f"{title} {h1}")
    ranked = [x for x in searches if x.get("you")]
    best_pos = min((x["you"] for x in ranked), default=None)
    if not covered:
        return "retitle", (f"Title and H1 don't say \"{top['search']}\". Rewrite the meta title as one "
                           f"plain phrase leading with it (no | or - separators, under 60 chars) and "
                           f"use it in the H1 and first sentence.")
    if best_pos and best_pos <= 20:
        return "links", (f"Title already covers \"{top['search']}\" and you're at #{best_pos:.0f}: add "
                         f"2-3 internal links with that anchor from related blog posts and categories.")
    return "content", (f"Title covers \"{top['search']}\" but Google doesn't show you yet: add a short "
                       f"intro paragraph using the searches below, plus 2-3 internal links.")


def live_title_h1(url):
    """(title, h1), or (None, None) if the page can't be fetched or is a 404."""
    import requests
    try:
        r = requests.get("https://alphabet-trains.com" + url, timeout=25,
                         headers={"User-Agent": "Mozilla/5.0 (product demand)"})
        s = r.text
        if r.status_code != 200 or "page was not found" in s.lower()[:3000]:
            return None, None
    except Exception:
        return None, None
    g = lambda pat: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", (re.search(pat, s, re.S | re.I) or [None, ""])[1])).strip()
    return g(r"<title>(.*?)</title>"), g(r"<h1[^>]*>(.*?)</h1>")



# ---------------------------------------------------------------- step-by-step "how"
def suggest_title(search, page_name=""):
    """A plain, ready-to-paste meta title leading with the search (no separators)."""
    t = " ".join(w.capitalize() if w not in STOP_T else w for w in search.split())
    extras = [w for w in re.findall(r"[A-Za-z']+", page_name or "")
              if w.lower() in ("personalized", "wooden", "montessori", "classroom", "kids")
              and w.lower() not in search.lower()]
    if extras:
        cand = f"{extras[0].capitalize()} {t}"
        if len(cand) <= 60:
            t = cand
    for tail in (" for Kids",):   # never invent claims like "Made in USA"
        if len(t + tail) <= 60 and not re.search(r"kids|classroom|toddler|baby", t, re.I):
            return t + tail
    return t[:60]


def admin_path(kind, name, sku=""):
    if kind == "category":
        return (f"Catalog > Categories > \"{name}\" (Scope: alphabettrains) > Search Engine Optimization")
    return (f"Product \"{name}\" (SKU {sku}): Catalog > Products > type {sku} in the search box > "
            f"open the row with SKU {sku} (Scope: alphabettrains) > Search Engine Optimization"
            if sku else f"Product \"{name}\": Catalog > Products > search the name > open it "
            f"(Scope: alphabettrains) > Search Engine Optimization")


def description_renders(cat):
    """False when the theme shows a CMS block instead of the Description (Classroom
    Rugs: 'Static block and products'), so a link there would be invisible. Pure."""
    return not cat.get("landing_page") and (cat.get("display_mode") or "PRODUCTS") == "PRODUCTS"


def link_sources_from(posts, cats, page_url):
    """Blog posts and category descriptions to look for link spots in. Pure."""
    out = [dict(p, kind="post") for p in posts]
    for cid, c in cats.items():
        url = f"/{c.get('url_path') or c.get('url_key')}.html"
        if (c.get("active") and c.get("description") and url != page_url and description_renders(c)
                and not SKIP_CAT.search(c.get("name", ""))):
            out.append({"kind": "category", "id": cid, "name": c["name"], "url_key": c.get("url_key"),
                        "url": url, "content": c["description"], "scope": scope_for(c)})
    return out


def short_tail(sentence, n=45):
    """The last few words of a sentence, enough to find it with Ctrl+F. Pure."""
    if len(sentence) <= n:
        return sentence
    cut = sentence[-n:]
    return cut[cut.find(" ") + 1:] if " " in cut else cut


def plural_phrase(p):
    """'playroom rug' -> 'playroom rugs', 'toy box' -> 'toy boxes' (for "See our ..."). Pure."""
    w = p.split()
    if not w or w[-1].endswith("s") or not head_kind(w[-1]):
        return p
    w[-1] += "es" if re.search(r"(x|ch|sh)$", w[-1]) else "s"
    return " ".join(w)


_BLOCK = re.compile(r"(?is)<(p|li)\b[^>]*>(.*?)</\1>")
_BUTTON_AFTER = re.compile(r'(?is)^\s*(?:</?div[^>]*>\s*)*<a[^>]*class="[^"]*(?:cta|button|btn)', )


def paragraph_spots(html):
    """Paragraphs a link may be added to, in page order: [(text, find)].
    Rules, one paragraph at a time:
      - it already has a link -> skip (never two links side by side)
      - a "Shop ..." button follows it (promo box) -> skip
      - otherwise the link goes at the END of the paragraph, and `find` is its last
        few words exactly as they appear in HTML view (checked), so Ctrl+F finds it.
    Content with no <p>/<li> at all is treated as one paragraph. Pure."""
    import html as _h
    blocks = [(m.group(2), m.end()) for m in _BLOCK.finditer(html or "")]
    if not blocks and html and "<a" not in html.lower():
        blocks = [(html, len(html))]
    out = []
    for inner, end in blocks:
        if re.search(r"(?i)<a\b", inner) or _BUTTON_AFTER.match(html[end:end + 400]):
            continue
        text = _h.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner))).strip()
        if len(text) < 40:
            continue
        # Last words as written in the code (entities like &mdash; kept), with no tag
        # in between, so the owner can search for them in HTML view.
        raw = re.sub(r"\s+", " ", inner).strip()
        tail = re.split(r"<[^>]+>", raw)[-1].strip() or ""
        words = tail.split()
        find = ""
        for k in (6, 5, 4, 3, 2):
            if len(words) >= k:
                cand = " ".join(words[-k:])
                if cand in re.sub(r"\s+", " ", inner):
                    find = cand
                    break
        if find:
            out.append((text, find))
    return out


def find_link_sources(posts, page_url, search, n=3, anchors=None):
    """Blog posts and category descriptions with a paragraph that talks about the
    product and has no link yet: where to put the link and what to paste.
    One spot per post/category, best match first. Pure."""
    words = [w for w in norm_tokens(search) if len(w) > 3] or list(norm_tokens(search))
    # The paragraph must talk about the PRODUCT (rug/carpet, chest/box...), not just
    # share a word like "playroom".
    hk = head_kind(search)
    must = {SYN.get(hk, plural_key(hk))} if hk else set()
    out = []
    for p in posts:
        html = p.get("content") or ""
        if page_url and page_url in html:
            continue   # already links to the page
        best = None
        for text, find in paragraph_spots(html):
            st = norm_tokens(text)
            if must and not (must & st):
                continue
            hits = sum(1 for w in words if w in st)
            if hits and (best is None or hits > best[0]):
                best = (hits, find)
        if best:
            out.append({"post": p.get("name", ""), "slug": p.get("url_key", ""), "hits": best[0],
                        "kind": p.get("kind", "post"), "id": p.get("id"), "scope": p.get("scope", ""),
                        "url": p.get("url") or f"/blog/{p.get('url_key', '')}",
                        "find": best[1]})
    out.sort(key=lambda x: -x["hits"])
    out = out[:n]
    # Vary the link text over the searches this page should win.
    for i, x in enumerate(out):
        a = plural_phrase((anchors or [search])[i % len(anchors or [search])])
        x["paste"] = f' See our <a href="{page_url}">{a}</a>.'
    return out


def aio_check(searches, n=2):
    """Does Google show an AI Overview for the page's top searches, and does it quote
    you? Uses DataForSEO (cached, daily cap). [] when unavailable."""
    try:
        from src.data_sources import dataforseo_client as dfs
    except Exception:
        return []
    out = []
    for x in searches[:n]:
        r = dfs.fetch_aio_citation(x["search"], "alphabet-trains.com") or {}
        if not r.get("available"):
            continue
        out.append({"search": x["search"], "present": bool(r.get("ai_overview_present")),
                    "cited": bool(r.get("own_cited")),
                    "who": (r.get("competitors_cited") or [])[:3]})
    return out


def aio_step(aio):
    """One plain step from the AI Overview check. Pure."""
    shown = [a for a in aio if a["present"]]
    if not aio:
        return ""
    if not shown:
        return ""   # nothing to do: say nothing
    missing = [a for a in shown if not a["cited"]]
    if not missing:
        return ("AI Overview: Google's AI summary already quotes your page for \"" +
                shown[0]["search"] + "\". Keep that wording when you edit the page.")
    a = missing[0]
    who = ", ".join(a["who"]) if a["who"] else "other sites"
    return (f"AI Overview: Google shows an AI summary for \"{a['search']}\" and quotes {who}, not you. "
            f"Near the top of the page, add 2-3 short sentences that plainly answer what the "
            f"searcher wants (what it is, sizes, ages, who it's for), and 2-3 FAQ questions with "
            f"one-sentence answers. The AI quotes pages that state facts plainly.")


def intro_paragraph(phrase, prods):
    """A plain intro using only catalog facts: count, brands, price range. Pure."""
    if not prods:
        return ""
    brands = sorted({(p.get("brand") or "").strip() for p in prods if (p.get("brand") or "").strip()})
    lo, hi = min(p["price"] for p in prods), max(p["price"] for p in prods)
    if len(prods) == 1:
        return ""   # one product: a sentence on its own page won't win a broad search
    by = (" from " + (", ".join(brands[:-1]) + " and " + brands[-1] if len(brands) > 1 else brands[0])
          if brands and len(brands) <= 4 else "")
    return f"<p>Shop {len(prods)} {plural_phrase(phrase)}{by}.</p>"   # no prices: they change


def open_line(kind, name, sku="", scope="alphabettrains"):
    """The one 'Open:' line for any page. Pure."""
    if kind == "category":
        return f'Open: Catalog > Categories > "{name}" (Scope: {scope})'
    if kind == "post":
        return f'Open: blog admin > Posts > "{name}", and switch the content to HTML view'
    if sku:
        return f'Open: Catalog > Products > type {sku} in the search box > open "{name}" (Scope: {scope})'
    return f'Open: Catalog > Products > search "{name}" > open it (Scope: {scope})'


DONE_LINE = 'Done: run bin/magento cache:flush, then click "I did it".'


def how_steps(g, posts):
    """Every task in the same simple pattern: Open, Go to, Find, Paste, Save.
    One short line each. [] when there is nothing concrete to do."""
    top = g.get("phrase") or g["searches"][0]["search"]
    name = g.get("page_name", "")
    if g["action"] == "new_page":
        return [
            "Open: Catalog > Categories > click the parent category > Add Subcategory",
            f'Category Name: {top.title()}. Enable Category: Yes. Include in Menu: Yes.',
            f"Go to: Search Engine Optimization. URL Key: {re.sub(r'[^a-z0-9]+', '-', top)}. "
            f"Meta Title: {suggest_title(top)}",
            "Save.",
            f"Open: System > Data Transfer > Import. Entity Type: Products. Import Behavior: Add/Update. "
            f"Select File: {g.get('import_file', '')}. Click Check Data, then Import.",
            'Done: run bin/magento indexer:reindex && bin/magento cache:flush, then click "I did it".',
        ]
    if g["action"] == "retitle":
        return [
            open_line(g["kind"], name, g.get("sku", "")),
            "Go to: Search Engine Optimization",
            f'Meta Title: if "Use Default Value" is ticked, untick it. Replace the text with: '
            f"{suggest_title(top, name)}",
            "Save.",
            DONE_LINE,
        ]
    steps = []
    if g["action"] == "content" and g.get("intro") and g["kind"] == "category":
        scope = "All Store Views" if "All Store Views" in (g.get("scope") or "") else "alphabettrains"
        steps += [open_line("category", name, scope=scope),
                  "Go to: Content > Description > Show / Hide Editor",
                  f"Paste at the very top: {g['intro']}",
                  "Save."]
    anchors, seen = [], set()
    for x in [{"search": top}] + list(g.get("searches") or []):
        k = frozenset(norm_tokens(x["search"]))
        if k not in seen:
            seen.add(k)
            anchors.append(x["search"])
    srcs = find_link_sources(posts, g.get("page") or "", top, anchors=anchors[:3])
    # For Governor's "Add these links for me" button: where, after what, what to add.
    # Only when EVERY spot is a category: Magento's blog API won't save a post's
    # text on its own (Nov 16: "Post name is required"), so blog spots stay by hand.
    g["links"] = ([{"url": x["url"], "name": x["post"], "find": x["find"], "paste": x["paste"].strip()}
                   for x in srcs] if srcs and all(x.get("kind") == "category" for x in srcs) else [])
    for i, s_ in enumerate(srcs, 1):
        head = f"Link {i} of {len(srcs)}. " if len(srcs) > 1 else ""
        if s_.get("kind") == "category":
            scope = "All Store Views" if "All Store Views" in (s_.get("scope") or "") else "alphabettrains"
            steps += [head + open_line("category", s_["post"], scope=scope),
                      "Go to: Content > Description > Show / Hide Editor"]
        else:
            steps.append(head + open_line("post", s_["post"]))
        steps += [f"Find (Ctrl+F): {s_['find']}",
                  f"Paste right after it (type one space first): {s_['paste'].strip()}",
                  "Save."]
    if not steps:
        return []   # nothing concrete to do: the plan drops this page
    steps.append(DONE_LINE)
    return steps


# Product lines, so one big line (rugs: high price, huge volume) can't crowd the
# others out of the plan. First match wins, checked against the page URL and the search.
LINES = (("Name trains", re.compile(r"train", re.I)),
         ("Stools", re.compile(r"stool", re.I)),
         ("Puzzles", re.compile(r"puzzle", re.I)),
         ("Rugs", re.compile(r"rug|carpet|\bmats?\b", re.I)),
         ("Furniture", re.compile(r"table|chair|shel|bookcase|cabinet|bench|cubb|locker|easel|cart|"
                                  r"chest|toy[- ]box|rack|storage|furniture|desk", re.I)),
         ("Books", re.compile(r"book|story", re.I)),
         ("Montessori", re.compile(r"montessori", re.I)))


def line_of(url, search=""):
    """Which product line a search belongs to: its own words first, then the page. Pure."""
    if gift_search(search or ""):
        return "Gifts"
    for text in (search or "", url or ""):
        for name, pat in LINES:
            if pat.search(text):
                return name
    return "Toys and other"


def vet_rows(rows, check, top=60, budget=150, per_line=5, line_tries=15):
    """Google Shopping vetting. `check(r)` looks one search up and returns True when it
    passes. Each product line first: walk its searches by value until `per_line` pass
    (or `line_tries` tried), so junk at the top of a line can't hide its real
    searches; then the rest by value, until `top` pass or `budget` lookups are spent.
    Returns the number of lookups. Pure apart from `check`."""
    checks, passed_total = 0, 0
    by_line = defaultdict(list)
    for r in rows:
        if not r.get("flag"):
            by_line[line_of("", r["search"])].append(r)
    done = set()
    for rs in by_line.values():
        passed = tried = 0
        for r in rs:
            if passed >= per_line or tried >= line_tries or checks >= budget:
                break
            tried += 1
            checks += 1
            done.add(id(r))
            ok = bool(check(r))
            passed += ok
            passed_total += ok
    for r in rows:
        if passed_total >= top or checks >= budget:
            break
        if r.get("flag") or id(r) in done:
            continue
        checks += 1
        passed_total += bool(check(r))
    return checks


def balanced(groups, max_pages=25, per_line=4):
    """Best pages by value, at most `per_line` per product line first, then fill. Pure."""
    ranked = sorted(groups, key=lambda g: -g["value"])
    out, n = [], defaultdict(int)
    for g in ranked:
        if n[g["line"]] < per_line:
            n[g["line"]] += 1
            out.append(g)
    for g in ranked:
        if len(out) >= max_pages:
            break
        if g not in out:
            out.append(g)
    return sorted(out[:max_pages], key=lambda g: -g["value"])


def gift_picks(idx, products, per_line=8):
    """For a new gift page: up to `per_line` products from each personalized line,
    cheapest first, leaving out lines on hold (stools: 2-3 month lead time). Pure."""
    by = defaultdict(list)
    for i in idx:
        ln = line_of(products[i]["url"], products[i]["name"])
        if ln != "Stools" and not re.search(r"coat rack|tag toys", products[i]["name"], re.I):
            by[ln].append(i)
    out = []
    for ln in sorted(by):
        out += sorted(by[ln], key=lambda i: products[i]["price"])[:per_line]
    return out


def ranking_owner(search, rank_pages, products, cats, cat_sizes=None):
    """(url, name, kind) of YOUR page that Google already shows for the search (most
    impressions, not a blog post), or None. Pure."""
    url = rank_pages.get(search)
    # Blog posts and brand pages are never the page to optimize for a product search.
    if not url or "/blog" in url or "/shop-by-brands/" in url or "brand" in url:
        return None
    for cid, c in cats.items():
        if c.get("active") and f"/{c.get('url_path') or c.get('url_key')}.html" == url:
            if cat_sizes and cat_sizes.get(cid, 0) < MIN_CATEGORY_PRODUCTS:
                return None
            return url, c["name"], "category"
    for p in products:
        if p["url"] == url:
            return url, p["name"], "product"
    return None


def build_page_plan(rows, products, cats, min_profit=10.0, max_pages=25, posts=None, rank_pages=None):
    """Group the good searches by the page that should own them, with one action each.
    Owner: a category named in the search; else the page Google already ranks for it;
    else the matched products' page or category; else a new page."""
    cat_sizes = category_sizes(products, cats)
    groups = {}
    for r in rows:
        if r.get("flag") or not r.get("verified") or r.get("profit", 0) < min_profit:
            continue
        # A gift search belongs to a gift page named for it, not to whichever line has
        # the most personalized products (books would win every time).
        gift = gift_search(r["search"])
        owner = None if named_category(r["search"], cats, cat_sizes) else ranking_owner(
            r["search"], rank_pages or {}, products, cats, cat_sizes)
        url, name, kind = owner or owner_page([] if gift else (r.get("_idx") or []), products, cats,
                                              cat_sizes, r["search"])
        # New pages: all gift searches share one gift page; others group by their words.
        key = url or ("NEW:gifts" if gift else "NEW:" + " ".join(sorted(norm_tokens(r["search"]))))
        g = groups.setdefault(key, {"page": url, "page_name": name or r["search"].title(),
                                    "kind": kind, "searches": [], "value": 0.0, "_idx": []})
        g["_idx"] = sorted(set(g["_idx"]) | set(r.get("_idx") or []))
        g["searches"].append({"search": r["search"], "volume": r["volume"], "you": r.get("you"),
                              "variants": r.get("variants", 1)})
        g["value"] += r.get("profit", 0)
    from src.analysis import task_rules
    recent = task_rules.recently_changed()
    waiting = []
    for g in list(groups.values()):
        g["searches"].sort(key=lambda x: -x["volume"])
        g["line"] = line_of(g["page"], g["searches"][0]["search"])
        w, why = task_rules.task_verdict(g["page"] or "", g["searches"][0]["search"], recent)
        if w == 0:
            g["action"], g["step"] = "wait", why
            g["value"] = round(g["value"], 2)
            g.pop("_idx", None)
            waiting.append(g)
    plan = balanced([g for g in groups.values() if g.get("action") != "wait"], max_pages)
    for g in plan:
        if g["kind"] == "new":
            g["action"] = "new_page"
            g["step"] = (f"No page targets \"{g['searches'][0]['search']}\". Create a category page for it "
                         f"with the matching products (like the Preschool & Daycare Rugs page).")
        else:
            title, h1 = live_title_h1(g["page"])
            if title is None:
                g["action"], g["step"] = "skip", f"Could not open {g['page']} (not found): no task."
                continue
            g["title"], g["h1"] = title, h1
            g["action"], g["step"] = page_action(g["searches"], title, h1, natural_phrase(g["searches"]))
        g["value"] = round(g["value"], 2)
        if g["kind"] == "product":
            hit = next((p for p in products if p["url"] == g["page"]), None)
            g["sku"] = hit["sku"] if hit else ""
        if g["action"] == "new_page":
            slug = re.sub(r"[^a-z0-9]+", "_", g["searches"][0]["search"])
            path = ROOT / "docs" / "imports" / f"new_page_{slug}.csv"
            idx = g.get("_idx") or []
            if gift_search(g["searches"][0]["search"]):
                idx = gift_picks(idx, products)
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["sku", "categories"])
                for i in idx:
                    w.writerow([products[i]["sku"], f"alphabetstore/{g['searches'][0]['search'].title()}"])
            g["import_file"] = f"docs/imports/{path.name}"
        g["phrase"] = natural_phrase(g["searches"])
        if g["action"] == "content":
            g["intro"] = intro_paragraph(g["phrase"], [products[i] for i in (g.get("_idx") or [])])
            own = next((c for c in cats.values() if f"/{c.get('url_path') or c.get('url_key')}.html" == g["page"]), None)
            if own is not None and own.get("store_copy") is not None:
                g["scope"] = scope_for(own)
        g["aio"] = aio_check(g["searches"])
        g["how"] = how_steps(g, link_sources_from(posts or [], cats, g.get("page")))
        step = aio_step(g["aio"])
        if step:
            g["how"].append(step)
        g.pop("_idx", None)
    # A task with no concrete step is not a task.
    return [g for g in plan if g.get("action") != "skip" and g.get("how")], waiting


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


def row_status(r, in_plan, waiting_why, min_profit=10.0):
    """Why a search is or isn't in the plan, in plain words. Pure."""
    meaning = {"CHEAP": "shoppers want the cheap version", "INFO": "readers, not buyers",
               "NOTKIDS": "Google shows mostly non-kids products", "PRICEY": "Google shows much cheaper items",
               "NAV": "people are looking for one business or site"}
    if r.get("flag"):
        return "left out: " + meaning.get(r["flag"], r["flag"])
    if waiting_why:
        return ("WAIT: page " if waiting_why.startswith("changed") else "left out: ") + waiting_why
    if in_plan:
        return "IN PLAN"
    if r.get("profit", 0) < min_profit:
        if r.get("you") and r["you"] <= 3:
            return f"fine: you're already #{r['you']:.0f} and getting the clicks"
        return "left out: under $10/mo to gain"
    if not r.get("verified"):
        return "not price-checked yet (outside this run's check budget)"
    return "left out: lower value than the pages in the plan"


def print_line_check(rows, plan, waiting, per_line=4):
    """Per product line: its biggest searches and why each is or isn't in the plan."""
    in_plan = {x["search"] for g in plan for x in g["searches"]}
    wait = {x["search"]: g["step"] for g in waiting for x in g["searches"]}
    by_line = defaultdict(list)
    for r in rows:
        by_line[line_of("", r["search"])].append(r)
    print("\nLINE CHECK (each product line's biggest buyable searches, and why they are or aren't in the plan)")
    for name in ["Gifts"] + [n for n, _ in LINES] + ["Toys and other"]:
        allr = by_line.get(name, [])
        # Only searches Google Shopping confirmed as kids' products, or that you already
        # rank for; unvetted big numbers ("coal engine train") would only mislead.
        good = sorted((r for r in allr if not r.get("flag") and (r.get("verified") or r.get("you"))),
                      key=lambda r: (r.get("you") is None, -r["volume"]))[:per_line]
        junk = sum(1 for r in allr if r.get("flag"))
        print(f"  {name}:" + ("" if good else " no buyable searches found")
              + (f"  ({junk} searches ignored: not for kids, cheap, or much cheaper items on Google)"
                 if junk else ""))
        for r in good:
            you = f"#{r['you']:.0f}" if r.get("you") else "not ranked"
            print(f"      {r['search'][:34]:<35}{r['volume']:>7,}/mo  {you:<11}"
                  f"{row_status(r, r['search'] in in_plan, wait.get(r['search']))}")


OWNER_CTX = {}


def read_store_descriptions(mc, cats):
    """Replace each category's description with what YOUR store view shows, and note
    whether the store view has its own copy (decides which Scope to edit in)."""
    for cid, c in cats.items():
        if not (c.get("active") and c.get("description")):
            continue
        try:
            sc = mc.scoped_description("category", cid)
        except Exception:
            sc = None
        if sc:
            c["description"], c["store_copy"] = sc["store"], sc["override"]


def scope_for(cat):
    """Edit where the store's text lives: its own store-view copy, or All Store Views. Pure."""
    if cat is None or cat.get("store_copy") is None:
        return ("Scope: alphabettrains. If \"Use Default Value\" is ticked under Description, "
                "switch Scope to All Store Views and edit there instead")
    return "Scope: alphabettrains" if cat["store_copy"] else "Scope: All Store Views (this page has one shared copy)"


def load_rank_pages(cfg, days=90):
    """{search: your page with the most impressions for it} from Search Console."""
    try:
        from src.data_sources.gsc_client import GSCClient
        g = cfg["data_sources"]["gsc"]
        rows = GSCClient(site_url=g["property_url"],
                         credentials_path=str(ROOT / g["credentials_path"])).get_query_page_rows(days=days)
    except Exception as e:
        print(f"(Search Console pages unavailable: {e})")
        return {}
    best = {}
    for r in rows:
        q, pg = (r.get("query") or "").lower(), re.sub(r"^https?://[^/]+", "", r.get("page") or "")
        if pg.endswith(".html") and (q not in best or r["impressions"] > best[q][1]):
            best[q] = (pg.split("?")[0], r["impressions"])
    return {q: v[0] for q, v in best.items()}


def explain(q, found, seed_products, products, rows, skip, gsc, cache, conv=0.0):
    """Print every filter one search goes through, so 'why isn't X in the plan' has
    a plain answer."""
    print(f'\nWHY: "{q}"')
    print(f"  1. One of the ways to describe your products? {'yes' if q in seed_products else 'no'}")
    it = found.get(q)
    print(f"  2. Real search volume: {it['volume']:,}/mo" if it else
          "  2. Not found as a real search (no volume, or never suggested). Stops here.")
    if not it:
        return
    print(f"  3. Off-topic word?  {'YES: ' + OFF_TOPIC.search(q).group(0) if OFF_TOPIC.search(q) else 'no'}")
    print(f"  4. Names a product kind + kids intent?  {'yes' if qualified(q) else 'no'}; "
          f"gift for a child?  {'yes' if gift_search(q) else 'no'}"
          + ("" if qualified(q) or gift_search(q) else "  -> DROPPED"))
    print(f"  5. In your skip list?  {'YES, dropped' if q in skip else 'no'}")
    idx = (set().union(*[seed_products[g] for g in GIFT_SEEDS]) if gift_search(q)
           else match_products(q, seed_products, products))
    print(f"  6. Matching products: {len(idx)}" + (f" (e.g. {products[min(idx)]['name']})" if idx else ""))
    mine = gsc.get(q, {})
    print(f"  7. Your Google position: {mine.get('position', 0):.1f}, clicks {mine.get('clicks', 0)}, "
          f"impressions {mine.get('impressions', 0)}" if mine else "  7. Your Google position: not ranking")
    r = next((x for x in rows if x["search"] == q), None)
    if r:
        extra = r["volume"] * (0.10 - (mine.get("clicks", 0) / mine["impressions"] if mine.get("impressions") else 0))
        print(f"  8. Price ${r['price']:.2f}, flag {r['flag'] or 'none'}. At #3: about {max(extra, 0):,.0f} more "
              f"clicks/mo x {conv:.2%} organic conversion x ${r['price']:.2f} x margin = ${r['profit']:,.0f}/mo")
    med, share, upper = shopping_check(q, cache)
    if share is None:
        print("  9. Google Shopping: unavailable")
    else:
        pf = price_flag(r["price"], upper) if r else ""
        print(f"  9. Google Shopping: median ${med or 0:.2f}, complete products about ${upper or 0:.2f}, "
              f"kids share {share:.0%} ({'DROPPED, under 30%' if share < 0.3 else 'ok'})"
              + (f", PRICEY vs your ${r['price']:.2f}" if pf else ""))
        for t, pr in (cache.get("shopitems|" + q) or {}).get("items", [])[:8]:
            print(f"       {'KID ' if kid_item(t, q) else '    '}${pr or 0:>7.2f}  {t[:70]}")
    from src.analysis import task_rules
    if r:
        cats, cat_sizes = OWNER_CTX.get("cats", {}), OWNER_CTX.get("cat_sizes", {})
        owner = None if named_category(q, cats, cat_sizes) else ranking_owner(
            q, OWNER_CTX.get("rank", {}), products, cats, cat_sizes)
        url, name, _ = owner or owner_page([] if gift_search(q) else (r.get("_idx") or []), products, cats,
                                           cat_sizes, q)
        print(f"     Google shows your page {OWNER_CTX.get('rank', {}).get(q) or '(none)'} for it; "
              f"Google's reading of the search: {search_intents([q], cache).get(q) or 'unknown'}")
        w, why = task_rules.task_verdict(url or "", q)
        print(f" 10. Page that should own it: {url or 'NEW PAGE'}; task rules: {why or 'ok'}")
    CACHE.write_text(json.dumps(cache))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--expand", type=int, default=80)
    ap.add_argument("--min-volume", type=int, default=30)
    ap.add_argument("--price-check", type=int, default=150,
                    help="check Google Shopping prices for the top N searches (Serper, cached)")
    ap.add_argument("--why", help='explain one search step by step, e.g. --why "name train"')
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    skip = {x.lower().strip() for x in cfg.get("demand_skip", []) if isinstance(x, str)}
    from scripts.line_demand import _magento
    mc = _magento()
    cats, products = load_catalog(mc)
    OWNER_CTX.update(cats=cats, cat_sizes=category_sizes(products, cats))
    seed_products = defaultdict(set)
    gift_idx = set()
    for i, p in enumerate(products):
        names = [cats.get(c, {}).get("name", "") for c in p.get("cats", []) if cats.get(c, {}).get("active")]
        for s in product_seeds(p["name"], names):
            seed_products[s].add(i)
        if is_personalized(p, names) and line_of(p["url"], p["name"]) != "Stools" \
                and not re.search(r"coat rack", p["name"], re.I):   # on hold: 2-3 month lead time
            gift_idx.add(i)
    for g in GIFT_SEEDS:
        seed_products[g] |= gift_idx
    print(f"{len(products)} sellable products -> {len(seed_products)} ways to describe them")

    gsc = load_gsc_queries(cfg)
    # Searches you already show up for are real demand by definition: look them up too,
    # so a product line never vanishes because no seed happened to produce its search.
    brand = [b.lower() for b in cfg.get("data_sources", {}).get("google_ads", {}).get("brand_terms", [])]
    rivals = [d["domain"].split(".")[0].lower() for d in cfg.get("demand_sources", []) if d.get("domain")]
    own_brand = lambda q: (any(b in q.replace("-", " ") or b in q for b in brand)
                           or rival_brand(q, rivals))
    proven = [q for q, d in gsc.items() if (d.get("impressions") or 0) >= 100 and not own_brand(q)
              and (qualified(q) or gift_search(q)) and not OFF_TOPIC.search(q)]
    vols = search_volumes(list(seed_products) + proven)
    real = sorted(((s, vols.get(s, {}).get("volume", 0)) for s in seed_products),
                  key=lambda x: -x[1])
    real = [(s, v) for s, v in real if v >= args.min_volume]
    real_proven = [(q, vols.get(q, {}).get("volume", 0)) for q in proven
                   if vols.get(q, {}).get("volume", 0) >= args.min_volume]
    print(f"{len(real)} of them are real searches (>= {args.min_volume}/mo); expanding the top {args.expand}")

    cache = _cache()
    found = {}
    for s, v in real + real_proven:
        found[s] = {"keyword": s, "volume": v, "cpc": vols.get(s, {}).get("cpc", 0)}
    for s, _ in real[:args.expand]:
        for it in expand(s, cache):
            if it["volume"] >= args.min_volume:
                found.setdefault(it["keyword"], it)
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache))

    conv, _, _ = organic_conversion(cfg)
    rank_pages = load_rank_pages(cfg)
    OWNER_CTX["rank"] = rank_pages
    rows = []
    for kw, it in found.items():
        gift = gift_search(kw)
        if kw in skip or own_brand(kw) or not gift and (OFF_TOPIC.search(kw) or not qualified(kw)):
            continue
        idx = set(gift_idx) if gift else match_products(kw, seed_products, products)
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
                     "profit": 0 if flag else profit, "flag": flag, "url": p["url"],
                     "_idx": sorted(idx)})
    if args.why:
        explain(args.why.lower().strip(), found, seed_products, products, rows, skip, gsc, cache, conv)
        return
    rows = collapse_variants(rows)
    rows.sort(key=lambda r: -r["profit"])
    # Google decides: walk down the list, ask Google Shopping what each search shows,
    # keep only searches where most products are for kids/classrooms, and flag
    # PRICEY ones. Stop once `top` searches are verified or the budget is spent.
    def vet(r):
        med, share, upper = shopping_check(r["search"], cache)
        r["shop_median"], r["kid_share"], r["shop_upper"] = med, share, upper
        if share is not None and share < 0.3:
            r["flag"], r["profit"] = "NOTKIDS", 0
        elif price_flag(r["price"], upper):
            r["flag"], r["profit"] = "PRICEY", 0
        r["verified"] = share is not None and not r["flag"]
        return r["verified"]

    intents = search_intents([r["search"] for r in rows if not r["flag"] and valid_keyword(r["search"])], cache)
    for r in rows:
        if not r["flag"] and navigational(intents.get(r["search"])):
            r["flag"], r["profit"] = "NAV", 0
    vet_rows(rows, vet, top=args.top, budget=args.price_check)
    CACHE.write_text(json.dumps(cache))
    rows.sort(key=lambda r: -r["profit"])
    print(f"price check: {SHOP_STATUS['checked']} looked up on Google Shopping now, "
          f"{SHOP_STATUS['cached']} from the saved copy (under 30 days old); dropped {sum(1 for r in rows if r['flag'] == 'NOTKIDS')} that Google shows "
          f"as not for kids, {sum(1 for r in rows if r['flag'] == 'PRICEY')} PRICEY" + (f"  [PROBLEM: {SHOP_STATUS['error']}]" if SHOP_STATUS["error"] else ""))

    print(f"\n{'search':<38}{'vol/mo':>8}{'you':>6}{'clk':>5}{'+$/mo':>7}  {'flag':<6}{'price':>8}  example product")
    # Everything Google Shopping looked at and found to be for kids (PRICEY included,
    # so you see where price is the problem). Unvetted searches are never listed.
    show = [r for r in rows if r.get("kid_share") is not None and r["flag"] != "NOTKIDS"]
    if not show:
        print("(Google Shopping check unavailable: nothing could be verified, see PROBLEM above)")
    for r in show[:args.top]:
        you = f"{r['you']:.0f}" if r["you"] else "-"
        print(f"{r['search'][:37]:<38}{r['volume']:>8,}{you:>6}{r['clicks']:>5}{r['profit']:>7,.0f}  "
              f"{r['flag']:<6}{r['price']:>8.2f}  {r['example']} (+{r['products'] - 1})"
              + (f"  [{r['variants']} ways typed]" if r.get("variants", 1) > 1 else ""))
    out = ROOT / "data" / "product_demand.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r if k != "_idx"}) if rows else ["search"])
        w.writeheader()
        w.writerows([{k: v for k, v in r.items() if k != "_idx"} for r in rows])
    print(f"\nAll {len(rows)} searches -> {out}")
    try:
        from scripts.link_sources import list_posts
        posts = list_posts(mc)
    except Exception as e:
        print(f"(blog posts unavailable for link suggestions: {e})")
        posts = []
    read_store_descriptions(mc, cats)
    plan, waiting = build_page_plan(rows, products, cats, posts=posts, rank_pages=rank_pages)
    (ROOT / "data" / "product_demand_plan.json").write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "pages": plan, "waiting": waiting},
        indent=1))
    print_line_check(rows, plan, waiting)
    print(f"\nPAGE PLAN ({len(plan)} pages, at most 4 per product line first) -> "
          f"data/product_demand_plan.json, also on Governor's Start Here")
    for g in plan:
        top = ", ".join(f"{x['search']} ({x['volume']:,}" + (f", #{x['you']:.0f})" if x.get("you") else ")")
                        for x in g["searches"][:3])
        print(f"  ${g['value']:>6,.0f}/mo  {g['line'][:11]:<12}{g['action']:<9} "
              f"{(g['page'] or 'NEW PAGE')[:45]:<46} {top}")
        for a in g.get("aio") or []:
            state = ("no AI summary" if not a["present"] else
                     "AI summary QUOTES YOU" if a["cited"] else
                     "AI summary quotes " + (", ".join(a["who"]) or "others") + ", not you")
            print(f"{'':>30}AI Overview for \"{a['search']}\": {state}")
        if g.get("aio") == []:
            print(f"{'':>30}AI Overview: not checked (DataForSEO unavailable or daily cap reached)")
    print("you = your Google position (Search Console, 90 days). +$/mo = profit at #3 at your "
          "conversion and margin. CHEAP = shoppers want the cheap version; INFO = readers, not buyers;\n"
          "PRICEY = complete products on Google Shopping cost under 40% of your price (shoppers expect a cheaper item).")


if __name__ == "__main__":
    main()
