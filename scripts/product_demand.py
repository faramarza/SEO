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
                       r"dresses|dna|lipid|carbohydrate|nucleic|concrete|garage|bundle branch|rbbb|jam level|"
                       r"blind ?box(es)?|blind toy|collectibles?|funko|pop ?mart|squishmallows?|figures?|"
                       r"anime|adult|adults|1000 piece|500 piece)\b",
                       re.I)
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
                       r"playroom|teacher|alphabet|abc|personali[sz]ed|ages? \d|\d\+ ?(years?|months?))\b",
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
    prices = [p for _, p in items if p]
    med = sorted(prices)[len(prices) // 2] if prices else None
    share = (sum(1 for t, _ in items if KID_WORDS.search(t or "")) / len(items)) if items else None
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


def named_category(search, cats):
    """The active category whose whole name is inside the search ('montessori toys'
    -> Montessori Toys, 'classroom carpet' -> Classroom Rugs). Longest name wins."""
    st = norm_tokens(search)
    best = None
    for c in cats.values():
        name = c.get("name", "")
        if not c.get("active") or not c.get("url_key") or SKIP_CAT.search(name):
            continue
        ct = norm_tokens(name)
        if ct and ct <= st and (best is None or len(ct) > len(norm_tokens(best["name"]))):
            best = c
    return best


def owner_page(idx, products, cats, cat_sizes, search=""):
    """Which page should own a search: a category named in the search; else a single
    product's page; else the most specific active category holding >= half the
    matched products; else None (new page)."""
    named = named_category(search, cats) if search else None
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
    return (f"Catalog > Products > search \"{sku or name}\" > open it (Scope: alphabettrains) > "
            f"Search Engine Optimization")


def find_link_sources(posts, page_url, search, n=3):
    """Blog posts that talk about the topic and don't link to the page yet, with the
    exact sentence to find and what to paste. Pure (posts = [{name, url_key, content}])."""
    words = [w for w in norm_tokens(search) if len(w) > 3] or list(norm_tokens(search))
    out = []
    for p in posts:
        html = p.get("content") or ""
        if page_url and page_url in html:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", html))
        sents = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if 40 < len(x.strip()) < 260]
        best = None
        for sn in sents:
            hits = sum(1 for w in words if w in norm_tokens(sn))
            if hits and (best is None or hits > best[0]):
                best = (hits, sn)
        if best:
            out.append({"post": p.get("name", ""), "slug": p.get("url_key", ""), "hits": best[0],
                        "find": best[1][-90:],
                        "paste": f' See our <a href="{page_url}">{search}</a>.'})
    out.sort(key=lambda x: -x["hits"])
    return out[:n]


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
        return "AI Overview: Google shows no AI summary for these searches, so nothing to do there."
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


def how_steps(g, posts):
    """Numbered, idiot-proof steps for one page task."""
    top = g.get("phrase") or g["searches"][0]["search"]
    if g["action"] == "new_page":
        return [
            f"Catalog > Categories > select the parent (e.g. Classroom Rugs or Montessori Toys) > "
            f"Add Subcategory. Category Name: \"{top.title()}\". Enable: Yes. Include in Menu: Yes. Anchor: Yes.",
            f"Search Engine Optimization: URL Key \"{re.sub(r'[^a-z0-9]+', '-', top)}\", "
            f"Meta Title \"{suggest_title(top)}\".",
            f"Save. Then import {g.get('import_file', 'the product file')} (System > Data Transfer > Import, "
            f"Products, Add/Update) to fill it with the matching products.",
            "On the server: bin/magento indexer:reindex && bin/magento cache:flush, then tell Claude \"check\".",
        ]
    if g["action"] == "retitle":
        return [
            admin_path(g["kind"], g.get("page_name", ""), g.get("sku", "")) + ".",
            f"Meta Title: replace with  {suggest_title(top, g.get('page_name', ''))}",
            "If \"Use Default Value\" is ticked next to Meta Title, untick it first. Save.",
            "On the server: bin/magento cache:flush, then tell Claude \"check\".",
        ]
    steps = []
    srcs = find_link_sources(posts, g.get("page") or "", top)
    if not srcs:
        return ["No blog post discusses this yet. Tell Claude \"find link spots for " + top +
                "\" and it will search categories and CMS blocks instead."]
    for i, s_ in enumerate(srcs, 1):
        steps.append(f"Link {i}: blog post \"{s_['post']}\" (/blog/{s_['slug']}). Open it in the blog admin, "
                     f"switch to HTML view, find the sentence ending: \"...{s_['find']}\"  and paste right "
                     f"after it:  {s_['paste']}")
    steps.append("Save each post, then on the server: bin/magento cache:flush, and tell Claude \"check\".")
    return steps


def build_page_plan(rows, products, cats, min_profit=10.0, max_pages=25, posts=None):
    """Group the good searches by the page that should own them, with one action each."""
    cat_sizes = defaultdict(int)
    for p in products:
        for c in p.get("cats", []):
            cat_sizes[c] += 1
    groups = {}
    for r in rows:
        if r.get("flag") or not r.get("verified") or r.get("profit", 0) < min_profit:
            continue
        url, name, kind = owner_page(r.get("_idx") or [], products, cats, cat_sizes, r["search"])
        key = url or ("NEW:" + r["search"])
        g = groups.setdefault(key, {"page": url, "page_name": name or r["search"].title(),
                                    "kind": kind, "searches": [], "value": 0.0, "_idx": []})
        g["_idx"] = sorted(set(g["_idx"]) | set(r.get("_idx") or []))
        g["searches"].append({"search": r["search"], "volume": r["volume"], "you": r.get("you"),
                              "variants": r.get("variants", 1)})
        g["value"] += r.get("profit", 0)
    plan = sorted(groups.values(), key=lambda g: -g["value"])[:max_pages]
    for g in plan:
        g["searches"].sort(key=lambda x: -x["volume"])
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
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["sku", "categories"])
                for i in idx:
                    w.writerow([products[i]["sku"], f"alphabetstore/{g['searches'][0]['search'].title()}"])
            g["import_file"] = f"docs/imports/{path.name}"
        g["phrase"] = natural_phrase(g["searches"])
        g["aio"] = aio_check(g["searches"])
        g["how"] = how_steps(g, posts or [])
        step = aio_step(g["aio"])
        if step:
            g["how"].append(step)
        g.pop("_idx", None)
    return [g for g in plan if g.get("action") != "skip"]


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
    skip = {x.lower().strip() for x in cfg.get("demand_skip", []) if isinstance(x, str)}
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
        if OFF_TOPIC.search(kw) or not qualified(kw) or kw in skip:
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
                     "profit": 0 if flag else profit, "flag": flag, "url": p["url"],
                     "_idx": sorted(idx)})
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
    print(f"price check: {SHOP_STATUS['checked']} looked up on Google Shopping now, "
          f"{SHOP_STATUS['cached']} from the saved copy (under 30 days old); dropped {sum(1 for r in rows if r['flag'] == 'NOTKIDS')} that Google shows "
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
    plan = build_page_plan(rows, products, cats, posts=posts)
    (ROOT / "data" / "product_demand_plan.json").write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(), "pages": plan}, indent=1))
    print(f"\nPAGE PLAN ({len(plan)} pages) -> data/product_demand_plan.json, also on Governor's Start Here")
    for g in plan[:15]:
        top = ", ".join(f"{x['search']} ({x['volume']:,}" + (f", #{x['you']:.0f})" if x.get("you") else ")")
                        for x in g["searches"][:3])
        print(f"  ${g['value']:>6,.0f}/mo  {g['action']:<9} {(g['page'] or 'NEW PAGE')[:45]:<46} {top}")
    print("you = your Google position (Search Console, 90 days). +$/mo = profit at #3 at your "
          "conversion and margin. CHEAP = shoppers want the cheap version; INFO = readers, not buyers;\n"
          "PRICEY = Google Shopping shows this for under 40% of your price (shoppers expect a cheaper item).")


if __name__ == "__main__":
    main()
