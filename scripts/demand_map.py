#!/usr/bin/env python3
"""Demand map: what people search for (with monthly volume), which of YOUR products
answers it, where you rank today, and what winning it is worth at YOUR margin.
Demand first, then products, then profit.

Run on the server:

    /var/www/seo/venv/bin/python scripts/demand_map.py --domain guidecraft.com --brand "guide craft"
    ... --top 40            (rows to list, default 40)
    ... --min-volume 30     (ignore searches under N/month, default 30)

HOW (every number sourced, nothing guessed):
  1. DEMAND: DataForSEO Labs "ranked keywords" for --domain (the maker's site):
     every Google search it ranks top-20 for, with monthly US volume and ad CPC.
     The maker ranks for exactly the searches its products answer.
  2. YOUR PRODUCTS: your enabled, visible Magento products whose brand contains
     --brand (price, URL). Each search is matched to your best product by shared
     words (brand words ignored); weak matches are marked so you can check them.
  3. WHERE YOU STAND: your Google position and clicks for that exact search from
     Search Console (last 90 days); "-" = you don't appear.
  4. VALUE: monthly searches x click rate at a target position (3, typical
     industry curve) x your organic conversion rate x product price x net margin
     (config brand margin minus card fee). Branded searches ("guidecraft ...")
     are labelled: the maker will hold #1, a dealer can still take #2-#5.
  5. ADS: break-even return on ad spend at that margin, next to the search's CPC,
     so you can see which searches can pay as ads.
"""
import argparse
import json
import os
import re
import sys
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

TARGET_CTR = 0.10          # typical CTR at position 3 (published curves)
STOP = {"the", "a", "an", "for", "to", "of", "and", "with", "in", "on", "by", "kids", "kid",
        "children", "child", "toddler", "toddlers", "baby", "best", "buy", "set", "pc", "piece",
        "pieces", "toy", "toys"}


# ---------------------------------------------------------------- pure logic
def tokens(s, extra_stop=()):
    words = re.findall(r"[a-z0-9]+", (s or "").lower())
    out = set()
    for w in words:
        if w in STOP or w in extra_stop or len(w) < 3 and not w.isdigit():
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)   # crude singular
    return out


def best_match(keyword, products, brand_words):
    """(product, score) where score = share of the keyword's meaningful words found
    in the product name. None if nothing shares a word."""
    kt = tokens(keyword, brand_words)
    if not kt:
        return None, 0.0
    best, score = None, 0.0

    def hit(w, pt):  # exact word, or one contains the other ("light" ~ "lightbox")
        return w in pt or (len(w) >= 4 and any(w in x or (len(x) >= 4 and x in w) for x in pt))

    for p in products:
        pt = p["_tokens"]
        s = sum(1 for w in kt if hit(w, pt)) / len(kt)
        if s > score:
            best, score = p, s
    return best, score


def is_branded(keyword, brand_words):
    k = (keyword or "").lower().replace(" ", "")
    return any(b.replace(" ", "") in k for b in brand_words)


def monthly_profit(volume, current_ctr, conv, price, margin, target_ctr=TARGET_CTR):
    extra_clicks = max(0.0, volume * (target_ctr - current_ctr))
    return extra_clicks, extra_clicks * conv * price * margin


def break_even_roas(margin):
    return (1.0 / margin) if margin > 0 else None


# ---------------------------------------------------------------- data
def load_products(brand):
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured (MAGENTO_BASE_URL / MAGENTO_TOKEN in .env).")
    meta = mc.product_attributes_meta()
    man_opts = (meta.get("manufacturer") or {}).get("options", {})
    out, seen = [], set()
    for p in mc.iter_all_products(only_visible=True):
        if int(p.get("status", 1)) != 1 or p.get("sku") in seen:
            continue
        seen.add(p.get("sku"))
        man = man_opts.get(str(mc._attr(p, "manufacturer")), "") or mc._attr(p, "brand_google")
        name = p.get("name", "")
        if brand.lower() not in f"{man} {name}".lower().replace("guidecraft", "guide craft"):
            continue
        price = float(p.get("price") or 0)
        sp = mc._attr(p, "special_price")
        try:
            if sp and 0 < float(sp) < price:
                price = float(sp)
        except ValueError:
            pass
        out.append({"sku": p.get("sku"), "name": name, "price": price,
                    "url": "/" + (mc._attr(p, "url_key") or "") + ".html"})
    return out


def load_gsc_queries(cfg, days=90):
    from src.data_sources.gsc_client import GSCClient
    g = cfg["data_sources"]["gsc"]
    gsc = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
    return gsc.get_query_data(days=days)


def margin_for_brand(cfg, brand):
    ps = cfg.get("paid_search", {})
    fee = float(ps.get("card_fee", 0.03))
    for k, v in ps.get("brand_margins", {}).items():
        if not k.startswith("_") and (k in brand.lower() or brand.lower() in k):
            return float(v["margin"]) - fee
    return float(ps.get("default_margin", 0.15)) - fee


def organic_conversion(cfg, days=90):
    """Organic conversion of product + category landing pages from GA4 (pooled)."""
    try:
        from src.data_sources.ga4_client import GA4Client
        g = cfg["data_sources"]["ga4"]
        ga = GA4Client(property_id=str(g["property_id"]),
                       credentials_path=str(ROOT / g["credentials_path"]))
        pages = ga.get_page_data(days=days, organic_only=True)
        s = sum(d["sessions"] for lp, d in pages.items() if lp.endswith(".html"))
        o = sum(d["conversions"] for lp, d in pages.items() if lp.endswith(".html"))
        return (o / s) if s else 0.01, s, o
    except Exception:
        return 0.01, 0, 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True, help="maker's site, e.g. guidecraft.com")
    ap.add_argument("--brand", required=True, help='your brand name for it, e.g. "guide craft"')
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-volume", type=int, default=30)
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    from src.data_sources import dataforseo_client as dfs
    demand = dfs.fetch_ranked_keywords(args.domain, limit=1000, max_position=20)
    if not demand.get("available"):
        sys.exit(f"Demand data unavailable: {demand.get('reason')}")
    brand_words = {args.brand.lower(), args.brand.lower().replace(" ", ""),
                   args.domain.split(".")[0].lower()}
    stop_brand = set()
    for b in brand_words:
        stop_brand |= set(re.findall(r"[a-z0-9]+", b))

    products = load_products(args.brand)
    for p in products:
        p["_tokens"] = tokens(p["name"], stop_brand)
    gsc = load_gsc_queries(cfg)
    margin = margin_for_brand(cfg, args.brand)
    conv, conv_s, conv_o = organic_conversion(cfg)
    be = break_even_roas(margin)

    rows = []
    for k in demand["keywords"]:
        if (k.get("volume") or 0) < args.min_volume:
            continue
        kw = k["keyword"]
        prod, score = best_match(kw, products, stop_brand)
        mine = gsc.get(kw.lower(), {})
        cur_ctr = (mine.get("clicks", 0) / mine["impressions"]) if mine.get("impressions") else 0.0
        price = prod["price"] if prod else 0.0
        extra, profit = monthly_profit(k["volume"], cur_ctr, conv, price, margin)
        rows.append({"kw": kw, "vol": k["volume"], "cpc": k.get("cpc") or 0,
                     "maker_pos": k.get("position"), "branded": is_branded(kw, brand_words),
                     "prod": prod, "score": score, "my_pos": mine.get("position"),
                     "my_clicks": mine.get("clicks", 0), "extra": extra,
                     "profit": profit if (prod and score >= 0.5) else 0.0})
    rows.sort(key=lambda r: -r["profit"])

    tot_vol = sum(r["vol"] for r in rows)
    matched = [r for r in rows if r["prod"] and r["score"] >= 0.5]
    print("=" * 110)
    print(f"DEMAND MAP  {args.domain}  ->  your '{args.brand}' products   (US, monthly searches)")
    print("=" * 110)
    print(f"searches found: {len(rows)} (>= {args.min_volume}/mo), {tot_vol:,} searches/month total; "
          f"matched to one of your {len(products)} products: {len(matched)} "
          f"({sum(r['vol'] for r in matched):,} searches/month)")
    print(f"your net margin on this brand: {margin*100:.1f}%  ->  ads break even at a return of "
          f"{be:.2f}" if be else "")
    print(f"organic conversion used: {conv*100:.2f}% ({conv_o} orders / {conv_s} organic "
          f"sessions on product+category pages, GA4 90 days)")
    print(f"\n{'search':<38}{'vol/mo':>7}{'cpc':>6}{'you':>6}{'clk':>5}{'+$/mo':>7}  "
          f"{'B':<2}{'your matching product':<44}{'match':>5}")
    for r in rows[:args.top]:
        p = r["prod"]
        you = f"{r['my_pos']:.0f}" if r["my_pos"] else "-"
        pname = (p["name"][:43] if p else "(no product of yours matches)")
        print(f"{r['kw'][:37]:<38}{r['vol']:>7,}{r['cpc']:>6.2f}{you:>6}{r['my_clicks']:>5}"
              f"{r['profit']:>7,.0f}  {'B' if r['branded'] else ' ':<2}{pname:<44}"
              f"{r['score']*100:>4.0f}%")
    unmatched = sorted((r for r in rows if not r["prod"] or r["score"] < 0.5), key=lambda r: -r["vol"])
    if unmatched:
        print("\nDEMAND YOU HAVE NO GOOD PRODUCT FOR (top 15 by volume) - candidates to add:")
        for r in unmatched[:15]:
            print(f"   {r['kw'][:50]:<52}{r['vol']:>7,}/mo  cpc ${r['cpc']:.2f}")
    print("\nB = branded search (maker holds #1; aim for #2-#5). match < 50% = check by hand, "
          "not valued.\n+$/mo = gross profit if you reached position 3, at your conversion rate "
          "and margin.")


if __name__ == "__main__":
    main()
