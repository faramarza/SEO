#!/usr/bin/env python3
"""Demand map: what people search for (with monthly volume), which of YOUR products
answers it, where you rank today, and what winning it is worth at YOUR margin.
Demand first, then products, then profit.

Run on the server:

    /var/www/seo/venv/bin/python scripts/demand_map.py --all
    ... --domain guidecraft.com --brand "guide craft"   (one source only)
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
        w = w[:-1] if w.endswith("s") and len(w) > 4 else w            # crude singular
        if w in STOP or w in extra_stop or len(w) < 3 and not w.isdigit():
            continue
        out.add(w)
    return out


PAGE1_CTR = 0.025         # typical CTR at position 8 (bottom of page 1)
MIN_GOOD_MATCH = 0.66   # two of three words


def is_head_term(keyword, brand_words=()):
    """One meaningful word ('blocks', 'wooden', 'sensory'): too broad to send
    buyers to one product and rarely winnable. Reported, never valued."""
    return len(tokens(keyword, brand_words)) < 2


def target_ctr(my_pos):
    """Realistic goal: already on pages 1-2 -> top 3; not ranking -> bottom of page 1."""
    return TARGET_CTR if (my_pos and my_pos <= 20) else PAGE1_CTR


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
def load_all_products():
    """Every enabled, visible product with its brand label, price and URL."""
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
        price = float(p.get("price") or 0)
        sp = mc._attr(p, "special_price")
        try:
            if sp and 0 < float(sp) < price:
                price = float(sp)
        except ValueError:
            pass
        out.append({"sku": p.get("sku"), "name": p.get("name", ""), "brand": man or "",
                    "price": price, "url": "/" + (mc._attr(p, "url_key") or "") + ".html"})
    return out


def brand_filter(products, brand):
    """Products of one brand ('*' = whole catalog). Matches the brand label or name."""
    if brand == "*":
        return products
    b = brand.lower().replace("guidecraft", "guide craft")
    return [p for p in products
            if b in f"{p['brand']} {p['name']}".lower().replace("guidecraft", "guide craft")]


def product_margin(p, cfg):
    """Net margin for one product: brand margin, else product line by URL, else
    default; minus the card fee."""
    ps = cfg.get("paid_search", {})
    fee = float(ps.get("card_fee", 0.03))
    label = f"{p.get('brand', '')} {p.get('name', '')}".lower().replace("guidecraft", "guide craft")
    for k, v in ps.get("brand_margins", {}).items():
        if not k.startswith("_") and k in label:
            return float(v["margin"]) - fee
    url = (p.get("url") or "").lower()
    for line in ps.get("product_lines", {}).values():
        if any(pat.lower() in url for pat in line.get("url_patterns", [])):
            return float(line["margin"]) - fee
    return float(ps.get("default_margin", 0.15)) - fee


def load_gsc_queries(cfg, days=90):
    from src.data_sources.gsc_client import GSCClient
    g = cfg["data_sources"]["gsc"]
    gsc = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
    return gsc.get_query_data(days=days)


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


def source_rows(src, demand, all_products, gsc, conv, cfg, min_volume):
    """Rows for one demand source: each search matched to a product of that brand
    (or the whole catalog), valued at that product's own margin."""
    domain, brand = src["domain"], src.get("brand", "*")
    brand_words = {domain.split(".")[0].lower()}
    if brand != "*":
        brand_words |= {brand.lower(), brand.lower().replace(" ", "")}
    stop_brand = set()
    for b in brand_words:
        stop_brand |= set(re.findall(r"[a-z0-9]+", b))
    products = brand_filter(all_products, brand) or all_products
    for p in all_products:
        p["_tokens"] = tokens(p["name"], stop_brand)
    rows = []
    for k in demand.get("keywords", []):
        if (k.get("volume") or 0) < min_volume:
            continue
        kw = k["keyword"]
        prod, score = best_match(kw, products, stop_brand)
        if score < MIN_GOOD_MATCH and products is not all_products:
            # the maker's brand has no fit; another product of yours may answer it
            p2, s2 = best_match(kw, all_products, stop_brand)
            if s2 > score:
                prod, score = p2, s2
        mine = gsc.get(kw.lower(), {})
        cur_ctr = (mine.get("clicks", 0) / mine["impressions"]) if mine.get("impressions") else 0.0
        head = is_head_term(kw, stop_brand)
        good = bool(prod) and score >= MIN_GOOD_MATCH and not head
        m = product_margin(prod, cfg) if prod else 0.0
        extra, profit = monthly_profit(k["volume"], cur_ctr, conv, prod["price"] if prod else 0, m,
                                       target_ctr=target_ctr(mine.get("position")))
        rows.append({"kw": kw, "vol": k["volume"], "cpc": k.get("cpc") or 0, "source": domain,
                     "branded": brand != "*" and is_branded(kw, brand_words),
                     "prod": prod if good else None, "cand": prod, "score": score, "margin": m,
                     "head": head, "difficulty": k.get("difficulty"),
                     "my_pos": mine.get("position"), "my_clicks": mine.get("clicks", 0),
                     "extra": extra, "profit": profit if good else 0.0})
    return rows


def merge_rows(rows):
    """One row per search: keep the most valuable match across sources."""
    best = {}
    for r in rows:
        k = r["kw"].lower()
        if k not in best or (r["profit"], r["vol"]) > (best[k]["profit"], best[k]["vol"]):
            best[k] = r
    return sorted(best.values(), key=lambda r: (-r["profit"], -r["vol"]))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="every source in config demand_sources (makers + category stores)")
    ap.add_argument("--domain", help="one source site, e.g. guidecraft.com")
    ap.add_argument("--brand", default="*", help='your brand name for it, e.g. "guide craft"; '
                    '"*" = match against the whole catalog')
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--min-volume", type=int, default=30)
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    if args.all:
        sources = [s for s in cfg.get("demand_sources", []) if isinstance(s, dict)]
    elif args.domain:
        sources = [{"domain": args.domain, "brand": args.brand}]
    else:
        sys.exit("Pass --all, or --domain (and --brand).")

    from src.data_sources import dataforseo_client as dfs
    all_products = load_all_products()
    gsc = load_gsc_queries(cfg)
    conv, conv_s, conv_o = organic_conversion(cfg)

    rows, skipped = [], []
    for src in sources:
        demand = dfs.fetch_ranked_keywords(src["domain"], limit=1000, max_position=20)
        if not demand.get("available"):
            skipped.append(f"{src['domain']}: {demand.get('reason')}")
            continue
        rows += source_rows(src, demand, all_products, gsc, conv, cfg, args.min_volume)
    if not rows:
        sys.exit("No demand data. " + " | ".join(skipped))
    rows = merge_rows(rows)

    print("=" * 118)
    print(f"DEMAND MAP  ({len(sources)} source sites, US monthly searches)  ->  your "
          f"{len(all_products)} products")
    print("=" * 118)
    for s in skipped:
        print(f"   skipped {s}")
    print(f"organic conversion used: {conv*100:.2f}% ({conv_o} orders / {conv_s} organic "
          f"product+category sessions, GA4 90 days); value = reaching #3 (10% click rate)")

    by_brand = {}
    for r in rows:
        key = (r["prod"]["brand"] or "(no brand)") if r["prod"] else "NO MATCHING PRODUCT"
        b = by_brand.setdefault(key, {"searches": 0, "vol": 0, "profit": 0.0, "margin": r["margin"]})
        b["searches"] += 1
        b["vol"] += r["vol"]
        b["profit"] += r["profit"]
    print(f"\nBY BRAND  {'brand':<28}{'searches':>9}{'volume/mo':>11}{'margin':>8}"
          f"{'ads break-even':>16}{'+$/mo at #3':>13}")
    for name, b in sorted(by_brand.items(), key=lambda x: -x[1]["profit"]):
        be = break_even_roas(b["margin"]) if name != "NO MATCHING PRODUCT" else None
        print(f"          {name[:27]:<28}{b['searches']:>9}{b['vol']:>11,}"
              f"{(b['margin']*100 if be else 0):>7.1f}%{(f'{be:.2f}' if be else '-'):>16}"
              f"{b['profit']:>13,.0f}")

    print(f"\nTOP {args.top} SEARCHES BY PROFIT POTENTIAL")
    print(f"{'search':<36}{'vol/mo':>7}{'cpc':>6}{'KD':>4}{'you':>5}{'clk':>5}{'+$/mo':>7}  "
          f"{'B':<2}{'your product':<40}{'brand':<16}")
    for r in rows[:args.top]:
        p = r["prod"]
        you = f"{r['my_pos']:.0f}" if r["my_pos"] else "-"
        kd = f"{r['difficulty']:.0f}" if isinstance(r.get("difficulty"), (int, float)) else "-"
        print(f"{r['kw'][:35]:<36}{r['vol']:>7,}{r['cpc']:>6.2f}{kd:>4}{you:>5}{r['my_clicks']:>5}"
              f"{r['profit']:>7,.0f}  {'B' if r['branded'] else ' ':<2}"
              f"{(p['name'][:39] if p else '-'):<40}{(p['brand'][:15] if p else ''):<16}")

    unmatched = sorted((r for r in rows if not r["prod"] and not r["head"]), key=lambda r: -r["vol"])
    print("\nHIGH DEMAND WITH NO GOOD PRODUCT MATCH (top 25 by volume): add a product, or a "
          "page that answers it")
    for r in unmatched[:25]:
        near = f"  nearest: {r['cand']['name'][:40]} ({r['score']*100:.0f}%)" if r["cand"] else ""
        print(f"   {r['kw'][:44]:<46}{r['vol']:>7,}/mo  cpc ${r['cpc']:.2f}  [{r['source']}]{near}")

    heads = sorted((r for r in rows if r["head"]), key=lambda r: -r["vol"])[:10]
    if heads:
        print("\nBROAD ONE-WORD SEARCHES (not valued: too broad to win or to send buyers to one "
              "product):")
        print("   " + ", ".join(f"{r['kw']} {r['vol']:,}"
                                + (f" (you #{r['my_pos']:.0f})" if r["my_pos"] else "") for r in heads))

    out = ROOT / "data" / "demand_map.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["search", "volume", "cpc", "source", "branded", "your_position", "your_clicks",
                    "product", "sku", "brand", "url", "match", "net_margin", "profit_at_3"])
        for r in rows:
            p = r["prod"] or {}
            w.writerow([r["kw"], r["vol"], r["cpc"], r["source"], int(r["branded"]),
                        r["my_pos"] or "", r["my_clicks"], p.get("name", ""), p.get("sku", ""),
                        p.get("brand", ""), p.get("url", ""), round(r["score"], 2),
                        round(r["margin"], 3), round(r["profit"], 2)])
    print(f"\nEvery search -> {out}")
    print("B = branded (the maker holds #1; aim for #2-#5). KD = keyword difficulty 0-100.\n"
          "+$/mo = gross profit at a realistic goal: top 3 if you already rank on pages 1-2, "
          "else bottom of page 1;\nonly for searches of 2+ words matched to your product at "
          ">= 67%. At your conversion and that product's net margin.")


if __name__ == "__main__":
    main()
