#!/usr/bin/env python3
"""Product-line demand: start from WHAT YOU SELL, measure how many people search
for each product line, where you stand, and what winning it is worth.

Run on the server:

    /var/www/seo/venv/bin/python scripts/line_demand.py
    ... --top 60            (lines to list, default 60)
    ... --min-volume 50     (ignore lines under N searches/month, default 50)

HOW:
  1. YOUR PRODUCT LINES, from your own Magento catalog (enabled products only):
       a. every active category that holds products (brand, sale and bestseller
          categories skipped), e.g. "classroom rugs", "kids tables";
       b. every product type that appears in 2+ of your product names, e.g.
          "water table", "art cart", "name train", "step stool".
  2. DEMAND: Google Ads search volume and cost per click for each line (US,
     DataForSEO Keywords Data, one batched request), plus the "<line> for kids"
     variant when it is bigger.
  3. WHERE YOU STAND: your Google position and clicks for that exact search
     (Search Console, 90 days).
  4. YOUR OFFER: how many enabled products you have in the line, their average
     price, and their net margin (each product's brand or line margin from
     config, minus card fee), so the ads break-even return is shown too.
  4b. INTENT: a search is only counted when it names YOUR version of the product:
     "personalized step stool", "name puzzle stool", "wooden play kitchen",
     "classroom water table", "montessori ...". Plain "step stool" is mostly $10
     plastic stools: shown as 'generic' for context, never valued.
  5. VALUE: searches x click rate at a realistic goal (top 3 if you rank on
     pages 1-2, else bottom of page 1) x your organic conversion x average price
     x margin. Nothing from other stores' catalogs, ever.
  6. PAGE: the category page that should rank for it, or "NO PAGE: create one"
     when the line only exists as scattered products.

Read-only. Output: table + data/line_demand.csv.
"""
import argparse
import csv
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

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

TOP3_CTR, PAGE1_CTR = 0.10, 0.025
# How you differ from the generic, cheap version of a product. Only searches that
# carry one of these (when it applies to your products in the line) are valued.
QUALIFIERS = {
    "personalized": re.compile(r"(?i)personali[sz]|custom|\bname\b"),
    "name": re.compile(r"(?i)\bname\b"),
    "wooden": re.compile(r"(?i)\bwood|maple|birch|guidecraft|guide craft|whitney|maple landmark|tag toys"),
    "montessori": re.compile(r"(?i)montessori"),
    "classroom": re.compile(r"(?i)classroom|school|whitney|carpets? for kids|daycare|preschool"),
}
SKIP_CATEGORY = re.compile(r"(?i)\b(brand|bestseller|best seller|sale|new|clearance|default|root|"
                           r"gift card|featured|all products|shop by)\b")
NOISE_WORDS = {"set", "pc", "piece", "pieces", "kit", "pack", "with", "and", "for", "the", "of",
               "in", "x", "ft", "in.", "inch", "inches", "large", "small", "medium", "mini", "jr",
               "deluxe", "classic", "contemporary", "natural", "white", "black", "blue", "red",
               "green", "yellow", "pink", "purple", "orange", "grey", "gray", "maple", "birch",
               "kids", "kid", "children", "childrens", "toddler", "toddlers", "baby", "personalized",
               "personalised", "custom", "montessori", "wooden", "wood", "educational", "name",
               "classroom"}


# ---------------------------------------------------------------- pure logic
def clean_words(name, brand_words=()):
    """Lowercase words of a product name without brands, sizes, numbers or symbols."""
    words = re.findall(r"[a-z]+", (name or "").lower().replace("®", " ").replace("™", " "))
    return [w for w in words if w not in brand_words and len(w) > 1]


def product_types(products, brand_words, min_products=2):
    """Two-word product types shared by 2+ products (e.g. 'water table'), with the
    products that carry them. Pairs made only of filler words are dropped; a pair
    may include one descriptive word ('name train', 'step stool')."""
    found = defaultdict(set)
    for i, p in enumerate(products):
        w = clean_words(p["name"], brand_words)
        for a, b in zip(w, w[1:]):
            if a in NOISE_WORDS and b in NOISE_WORDS:
                continue
            if b in NOISE_WORDS - {"train", "stool"}:
                continue
            found[f"{a} {b}"].add(i)
    return {k: v for k, v in found.items() if len(v) >= min_products}


def category_lines(categories, products):
    """{line name: (url, product indexes)} for active categories that hold products."""
    by_cat = defaultdict(set)
    for i, p in enumerate(products):
        for cid in p.get("cats", []):
            by_cat[cid].add(i)
    out = {}
    for cid, idx in by_cat.items():
        c = categories.get(cid)
        if not c or not c.get("active") or SKIP_CATEGORY.search(c["name"]) or not c.get("url_key"):
            continue
        name = re.sub(r"\s+", " ", c["name"].strip().lower())
        if len(name) < 4:
            continue
        prev = out.get(name)
        if not prev or len(idx) > len(prev[1]):
            out[name] = (f"/{c['url_key']}.html", idx)
    return out


def line_qualifiers(prods, cat_names=()):
    """Qualifiers that truly describe YOUR products in a line (majority of them, or
    the category itself), e.g. ['personalized', 'name'] for name puzzle stools."""
    texts = [f"{p.get('name', '')} {p.get('brand', '')}" for p in prods]
    out = []
    for q, rx in QUALIFIERS.items():
        share = sum(1 for t in texts if rx.search(t)) / max(1, len(texts))
        if share >= 0.5 or any(rx.search(c or "") for c in cat_names):
            out.append(q)
    return out


def qualified_seeds(line, quals):
    """'step stool' + ['personalized','name'] -> ['personalized step stool',
    'name step stool', 'step stool for kids'...]; never the bare generic line."""
    out = []
    for q in quals:
        if q in line.split():
            out.append(line)
        else:
            out.append(f"{q} {line}")
    return list(dict.fromkeys(out))


def singular_key(line):
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in line.split())


def covering_page(idx, cat_lines, min_share=0.5):
    """The category page that already holds most of a product type's items
    (e.g. name puzzle stools -> /personalized-step-stools.html), else None."""
    best, share = None, 0.0
    for name, (url, cidx) in cat_lines.items():
        sh = len(set(idx) & set(cidx)) / max(1, len(idx))
        # prefer the most specific page (fewest products) among equal coverage
        if sh > share or (sh == share and best and len(cidx) < best[1]):
            best, share = (url, len(cidx)), sh
    return best[0] if best and share >= min_share else None


def value(volume, my_pos, my_clicks, my_impr, conv, price, margin):
    target = TOP3_CTR if (my_pos and my_pos <= 20) else PAGE1_CTR
    cur = (my_clicks / my_impr) if my_impr else 0.0
    extra = max(0.0, volume * (target - cur))
    return extra, extra * conv * price * margin


# ---------------------------------------------------------------- data
def _magento():
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured (MAGENTO_BASE_URL / MAGENTO_TOKEN in .env).")
    return mc


def load_catalog(mc):
    cats = {}
    for c in (mc._req("GET", "/categories/list?searchCriteria[pageSize]=3000") or {}).get("items") or []:
        cats[int(c["id"])] = {"name": c.get("name", ""), "active": bool(c.get("is_active", True)),
                              "url_key": mc._attr(c, "url_key")}
    meta = mc.product_attributes_meta()
    man_opts = (meta.get("manufacturer") or {}).get("options", {})
    products, seen = [], set()
    for p in mc.iter_all_products(only_visible=True):
        if int(p.get("status", 1)) != 1 or p.get("sku") in seen:
            continue
        seen.add(p.get("sku"))
        ext = p.get("extension_attributes") or {}
        price = float(p.get("price") or 0)
        sp = mc._attr(p, "special_price")
        try:
            if sp and 0 < float(sp) < price:
                price = float(sp)
        except ValueError:
            pass
        products.append({
            "sku": p.get("sku"), "name": p.get("name", ""), "price": price,
            "brand": man_opts.get(str(mc._attr(p, "manufacturer")), "") or mc._attr(p, "brand_google"),
            "url": "/" + (mc._attr(p, "url_key") or "") + ".html",
            "cats": [int(l.get("category_id", 0)) for l in ext.get("category_links") or []],
        })
    return cats, products


def search_volumes(keywords):
    """{keyword: {volume, cpc, competition}} via DataForSEO Keywords Data (Google Ads),
    up to 1000 keywords per request."""
    import httpx
    from src.data_sources import dataforseo_client as dfs
    if not dfs.is_configured():
        sys.exit("DataForSEO not configured (DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD in .env).")
    out = {}
    kws = [k for k in dict.fromkeys(keywords) if 2 < len(k) <= 80 and len(k.split()) <= 10]
    for i in range(0, len(kws), 1000):
        body = [{"keywords": kws[i:i + 1000], "location_code": dfs.LOCATION_CODE,
                 "language_code": dfs.LANGUAGE_CODE}]
        r = httpx.post("https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
                       headers={"Authorization": f"Basic {dfs._auth_token()}",
                                "Content-Type": "application/json"}, json=body, timeout=120.0)
        if r.status_code != 200:
            sys.exit(f"DataForSEO search volume error {r.status_code}: {r.text[:200]}")
        task = (r.json().get("tasks") or [{}])[0]
        if (task.get("status_code") or 0) >= 40000:
            sys.exit(f"DataForSEO search volume: {task.get('status_message')}")
        for it in task.get("result") or []:
            out[(it.get("keyword") or "").lower()] = {
                "volume": it.get("search_volume") or 0, "cpc": it.get("cpc") or 0,
                "competition": it.get("competition") or ""}
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--min-volume", type=int, default=50)
    ap.add_argument("--check", nargs="+", metavar="SEARCH",
                    help='only look up these searches: volume, CPC, your Google position, e.g. '
                         '--check "kids wooden table" "preschool tables"')
    args = ap.parse_args()

    if args.check:
        cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
        from scripts.demand_map import load_gsc_queries
        kws = [k.strip().lower() for k in args.check if k.strip()]
        vols = search_volumes(kws)
        gsc = load_gsc_queries(cfg)
        print(f"{'search':<40}{'vol/mo':>9}{'cpc':>7}{'you':>6}{'clicks':>8}{'impr':>8}")
        for k in sorted(kws, key=lambda k: -(vols.get(k) or {}).get("volume", 0)):
            v, g = vols.get(k) or {}, gsc.get(k) or {}
            pos = f"{g['position']:.0f}" if g.get("position") else "-"
            print(f"{k[:39]:<40}{(v.get('volume') or 0):>9,}{(v.get('cpc') or 0):>7.2f}{pos:>6}"
                  f"{g.get('clicks', 0):>8}{g.get('impressions', 0):>8}")
        print("\nyou = your average Google position (Search Console, 90 days); '-' = not showing.")
        return

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    from scripts.demand_map import product_margin, load_gsc_queries, organic_conversion, norm_brand

    mc = _magento()
    cats, products = load_catalog(mc)
    brand_words = set()
    for p in products:
        brand_words |= set(re.findall(r"[a-z]+", (p["brand"] or "").lower()))
    brand_words |= {"guidecraft", "whitney", "brothers", "tag", "iseeme", "cubbyhole", "maple",
                    "landmark", "carpets", "carpet", "cfk"} - {"carpet"}

    lines = {}  # line -> {"kind", "url", "idx"}
    cat_lines = category_lines(cats, products)
    for name, (url, idx) in cat_lines.items():
        lines[name] = {"kind": "category", "url": url, "idx": idx}
    have = {singular_key(n) for n in lines}
    for name, idx in product_types(products, brand_words).items():
        if singular_key(name) in have:
            continue
        have.add(singular_key(name))
        lines[name] = {"kind": "product type", "idx": idx,
                       "url": covering_page(idx, cat_lines)}

    seeds = []
    for name, l in lines.items():
        prods = [products[i] for i in l["idx"]]
        catn = [name] if l["kind"] == "category" else []
        l["quals"] = line_qualifiers(prods, catn)
        qs = qualified_seeds(name, l["quals"])
        l["seeds"] = list(dict.fromkeys(qs + [singular_key(x) for x in qs]))  # "name trains" + "name train"
        seeds += l["seeds"] + [name]
    vols = search_volumes(seeds)
    gsc = load_gsc_queries(cfg)
    conv, conv_s, conv_o = organic_conversion(cfg)

    rows = []
    for name, l in lines.items():
        generic = (vols.get(name) or {}).get("volume", 0)
        if not l["seeds"]:
            continue  # nothing distinguishes your version: generic demand is not yours
        best_kw = max(l["seeds"], key=lambda k: (vols.get(k) or {}).get("volume", 0))
        v = vols.get(best_kw) or {"volume": 0, "cpc": 0}
        if (v.get("volume") or 0) < args.min_volume:
            continue
        prods = [products[i] for i in l["idx"]]
        price = statistics.median([p["price"] for p in prods if p["price"] > 0] or [0])
        margin = statistics.median([product_margin(p, cfg) for p in prods])
        mine = gsc.get(best_kw, {}) or gsc.get(name, {})
        extra, profit = value(v["volume"], mine.get("position"), mine.get("clicks", 0),
                              mine.get("impressions", 0), conv, price, margin)
        page = l["url"]
        brands = sorted({norm_brand(p["brand"]) for p in prods})
        rows.append({"line": name, "search": best_kw, "vol": v["volume"], "cpc": v["cpc"],
                     "generic": generic,
                     "kind": l["kind"], "n": len(prods), "price": price, "margin": margin,
                     "be": (1 / margin) if margin > 0 else None, "pos": mine.get("position"),
                     "clicks": mine.get("clicks", 0), "extra": extra, "profit": profit,
                     "page": page, "brands": ", ".join(brands)[:40]})
    rows.sort(key=lambda r: -r["profit"])

    print("=" * 128)
    print(f"YOUR PRODUCT LINES vs WHAT PEOPLE SEARCH FOR   ({len(products)} enabled products, "
          f"{len(lines)} lines, US Google searches/month)")
    print("=" * 128)
    print(f"value = extra clicks at a realistic goal x {conv*100:.2f}% organic conversion "
          f"({conv_o}/{conv_s}) x median price x median net margin, per month")
    print("only searches for YOUR version (personalized / name / wooden / montessori / classroom, "
          "when it describes your products) are valued;\n'generic' = the plain search, shown "
          "for context only (mostly cheaper mass-market products).")
    print(f"\n{'your version (search)':<34}{'vol/mo':>8}{'generic':>9}{'cpc':>6}{'you':>5}{'clk':>5}{'items':>6}"
          f"{'price':>7}{'margin':>7}{'ads BE':>7}{'+$/mo':>7}  page")
    for r in rows[:args.top]:
        you = f"{r['pos']:.0f}" if r["pos"] else "-"
        be = f"{r['be']:.1f}" if r["be"] else "-"
        page = r["page"] or "NO PAGE: create one"
        print(f"{r['search'][:33]:<34}{r['vol']:>8,}{r['generic']:>9,}{r['cpc']:>6.2f}{you:>5}{r['clicks']:>5}{r['n']:>6}"
              f"{r['price']:>7.0f}{r['margin']*100:>6.0f}%{be:>7}"
              f"{r['profit']:>7,.0f}  {page}")
    no_page = [r for r in rows if not r["page"]]
    if no_page:
        print(f"\nLINES WITH DEMAND BUT NO CATEGORY PAGE ({len(no_page)}): the products exist, "
              "Google has no page to rank")
        for r in sorted(no_page, key=lambda r: -r["vol"])[:15]:
            print(f"   {r['search']:<36}{r['vol']:>8,}/mo  {r['n']} products  ({r['brands']})")

    out = ROOT / "data" / "line_demand.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["line", "search", "volume", "generic_volume", "cpc", "kind", "products", "median_price",
                    "net_margin", "ads_break_even", "your_position", "your_clicks",
                    "profit_per_month", "page", "brands"])
        for r in rows:
            w.writerow([r["line"], r["search"], r["vol"], r["generic"], r["cpc"], r["kind"], r["n"],
                        round(r["price"], 2), round(r["margin"], 3),
                        round(r["be"], 2) if r["be"] else "", r["pos"] or "", r["clicks"],
                        round(r["profit"], 2), r["page"] or "", r["brands"]])
    print(f"\nEvery line -> {out}")


if __name__ == "__main__":
    main()
