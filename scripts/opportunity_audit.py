#!/usr/bin/env python3
"""Site-wide SEO opportunity audit: what is moving each page up actually worth,
in dollars, from YOUR data. Ranks every page so effort goes where the money is,
and tells you whether organic search can move the business at all.

Run on the server:

    /var/www/seo/venv/bin/python scripts/opportunity_audit.py
    ... --days 90            (window, default 90)
    ... --top 30             (pages to list, default 30)
    ... --check-pages 25     (also fetch the top N shop pages live and flag
                              category pages that list no products)

METHOD (every input is measured, and printed so it can be audited):
  1. Search Console query x page rows for the window. Brand queries (config
     google_ads.brand_terms) are removed: you already win those.
  2. CLICK-THROUGH CURVE: your own non-brand clicks / impressions by rounded
     position (1..10, then 11-15, 16-20, 21-30). Buckets with too few
     impressions fall back to a published industry curve, and the output says
     which positions used which source.
  3. CONVERSION: GA4 organic landing-page purchases / sessions, pooled by page
     type (category, product, blog, home, other). Pooled because single pages
     have too few orders to estimate alone. If Magento is configured, GA4
     purchases are scaled up by (real Magento orders / GA4 purchases), since GA4
     misses some orders.
  4. ORDER VALUE: GA4 organic revenue / purchases, else config AOV.
  5. MARGIN: per-URL product-line margin from config (url_patterns), else
     default_margin; minus the card fee.
  6. SCENARIOS per query x page currently below the target:
        A  "bottom of page 1": position 8
        B  "top 3":            position 3
     extra clicks = impressions x (curve CTR at target - your CURRENT CTR)
     extra profit = extra clicks x conversion x order value x margin
  7. Flags: CANNIBAL when 2+ of your URLs share a query's impressions (each
     with >= 15%); NO-PRODUCTS when a category page lists no products
     (--check-pages).

Read-only. Output: tables on screen + data/opportunity_audit.csv.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
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

OUT_CSV = ROOT / "data" / "opportunity_audit.csv"

# Published fallback CTR by organic position (approximate blended desktop+mobile,
# in line with large public CTR studies such as Advanced Web Ranking / Sistrix).
# Used ONLY for positions where the site's own data is too thin.
FALLBACK_CTR = {1: 0.27, 2: 0.15, 3: 0.10, 4: 0.07, 5: 0.05, 6: 0.04, 7: 0.03,
                8: 0.025, 9: 0.02, 10: 0.017, 15: 0.01, 20: 0.006, 30: 0.003}
MIN_BUCKET_IMPR = 400
SCENARIOS = {"A": 8, "B": 3}
PAGE_TYPES = ("category", "product", "blog", "home", "other")


# ---------------------------------------------------------------- pure logic
def path_of(url: str) -> str:
    p = urlparse(url).path if "://" in url else url.split("?")[0]
    return p or "/"


def bucket(pos: float) -> int:
    r = max(1, int(round(pos)))
    if r <= 10:
        return r
    if r <= 15:
        return 15
    if r <= 20:
        return 20
    return 30


def build_ctr_curve(rows, min_impr=MIN_BUCKET_IMPR):
    """rows: dicts with clicks, impressions, position (non-brand). Returns
    ({bucket: ctr}, {bucket: 'site'|'fallback'}). Own data where the bucket has
    enough impressions, fallback otherwise; then forced non-increasing so a
    better position never predicts fewer clicks."""
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        b = bucket(r["position"])
        agg[b][0] += r["clicks"]
        agg[b][1] += r["impressions"]
    curve, src = {}, {}
    for b in sorted(FALLBACK_CTR):
        c, i = agg.get(b, (0, 0))
        if i >= min_impr:
            curve[b], src[b] = c / i, "site"
        else:
            curve[b], src[b] = FALLBACK_CTR[b], "fallback"
    # Non-increasing, enforced from the WORST position upward: a better position
    # is raised to at least the next-worse one. (Enforcing top-down let one noisy
    # bucket at #1 with 0 clicks zero out the whole curve.)
    nxt = 0.0
    for b in sorted(curve, reverse=True):
        curve[b] = max(curve[b], nxt)
        nxt = curve[b]
    return curve, src


def raw_ctr_table(rows):
    """{bucket: (impressions, clicks)} of the site's own non-brand data, printed
    so the curve can be audited."""
    agg = defaultdict(lambda: [0, 0])
    for r in rows:
        b = bucket(r["position"])
        agg[b][0] += r["impressions"]
        agg[b][1] += r["clicks"]
    return {b: tuple(v) for b, v in agg.items()}


def ctr_at(curve, pos):
    return curve[bucket(pos)]


def is_brand(query, brand_terms):
    q = (query or "").lower()
    return any(t.lower() in q for t in brand_terms)


def page_type(path, category_paths):
    if path in ("/", ""):
        return "home"
    if path.startswith("/blog"):
        return "blog"
    if path in category_paths:
        return "category"
    if path.endswith(".html"):
        return "product" if category_paths else "category"
    return "other"


def margin_for_path(path, product_lines, default_margin, card_fee):
    p = path.lower()
    for line in product_lines.values():
        if any(pat.lower() in p for pat in line.get("url_patterns", [])):
            return max(0.0, float(line["margin"]) - card_fee)
    return max(0.0, default_margin - card_fee)


def find_cannibals(rows, share=0.15, min_query_impr=100):
    """Paths that share a query with another of your paths, each holding >= share
    of that query's impressions, for queries with real volume (>= min_query_impr
    impressions in the window). Returns {path: set(queries)}."""
    by_q = defaultdict(list)
    for r in rows:
        by_q[r["query"]].append(r)
    out = defaultdict(set)
    for q, rs in by_q.items():
        tot = sum(r["impressions"] for r in rs)
        if tot < min_query_impr:
            continue
        big = [r for r in rs if tot and r["impressions"] / tot >= share]
        if len({r["path"] for r in big}) >= 2:
            for r in big:
                out[r["path"]].add(q)
    return out


def row_opportunity(r, curve, value_per_click):
    """Extra clicks and profit for each scenario for one query x page row."""
    cur_ctr = r["clicks"] / r["impressions"] if r["impressions"] else 0.0
    out = {}
    for name, target in SCENARIOS.items():
        if r["position"] <= target:
            out[name] = (0.0, 0.0)
            continue
        extra = max(0.0, r["impressions"] * (ctr_at(curve, target) - cur_ctr))
        out[name] = (extra, extra * value_per_click)
    return out


def snippet_gap(r, industry=FALLBACK_CTR):
    """Clicks lost at the CURRENT position versus a typical result at that
    position: impressions x (industry CTR - your CTR). Big gaps mean the
    listing is shown but not chosen (title/description, SERP features, intent)."""
    cur = r["clicks"] / r["impressions"] if r["impressions"] else 0.0
    return max(0.0, r["impressions"] * (industry[bucket(r["position"])] - cur))


# ---------------------------------------------------------------- data loads
def _cfg():
    return json.loads((ROOT / "config" / "defaults.json").read_text())


def load_gsc_rows(cfg, days):
    from src.data_sources.gsc_client import GSCClient
    g = cfg["data_sources"]["gsc"]
    gsc = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
    return gsc.get_query_page_rows(days=days)


def load_ga4_pages(cfg, days):
    from src.data_sources.ga4_client import GA4Client
    g = cfg["data_sources"]["ga4"]
    ga = GA4Client(property_id=str(g["property_id"]), credentials_path=str(ROOT / g["credentials_path"]))
    return ga.get_page_data(days=days, organic_only=True), ga


def load_category_paths():
    try:
        from src.data_sources.magento_client import MagentoClient
        mc = MagentoClient()
        if not mc.configured:
            return set(), None
        data = mc._req("GET", "/categories/list?searchCriteria[pageSize]=3000") or {}
        paths = set()
        for c in data.get("items") or []:
            uk = mc._attr(c, "url_key")
            if uk and c.get("is_active", True):
                paths.add(f"/{uk}.html")
        return paths, mc
    except Exception as e:
        print(f"(Magento categories unavailable: {e}; category vs product split is approximate)")
        return set(), None


def count_listed_products(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AlphabetTrains-SEO-Audit/1.0)"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    m = re.search(r'class="main"(.*?)class="page-footer"', html, re.S)
    main = m.group(1) if m else html
    return len(re.findall(r'class="product-item-info', main))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--check-pages", type=int, default=0)
    args = ap.parse_args()

    cfg = _cfg()
    brand_terms = cfg["data_sources"].get("google_ads", {}).get("brand_terms", [])
    ps = cfg.get("paid_search", {})
    card_fee = float(ps.get("card_fee", 0.03))
    default_margin = float(ps.get("default_margin", 0.15))
    aov_cfg = float(cfg.get("profit_model", {}).get("aov", 53.19))
    months = args.days / 30.0

    raw = load_gsc_rows(cfg, args.days)
    if not raw:
        sys.exit("No Search Console rows (credentials or property issue).")
    rows, brand_clicks = [], 0
    for r in raw:
        if is_brand(r["query"], brand_terms):
            brand_clicks += r["clicks"]
            continue
        rows.append({**r, "path": path_of(r["page"])})
    curve, src = build_ctr_curve(rows)

    ga_pages, ga = load_ga4_pages(cfg, args.days)
    cat_paths, mc = load_category_paths()

    # conversion + order value pooled by page type
    pool = defaultdict(lambda: {"sessions": 0, "orders": 0, "revenue": 0.0})
    for lp, d in ga_pages.items():
        t = page_type(path_of(lp), cat_paths)
        pool[t]["sessions"] += d["sessions"]
        pool[t]["orders"] += d["conversions"]
        pool[t]["revenue"] += d["revenue"]
    all_orders = sum(p["orders"] for p in pool.values())
    all_rev = sum(p["revenue"] for p in pool.values())
    aov = (all_rev / all_orders) if all_orders else aov_cfg

    capture = 1.0
    if mc is not None and ga is not None:
        try:
            end = date.today() - timedelta(days=1)
            start = end - timedelta(days=args.days)
            mag = mc.order_revenue(start.isoformat(), (end + timedelta(days=1)).isoformat())
            tot = ga.get_account_totals(days=args.days) or {}
            ga_buys = int(tot.get("purchases") or tot.get("ecommercePurchases") or 0)
            if ga_buys and mag.get("count"):
                capture = min(3.0, max(1.0, mag["count"] / ga_buys))
                print(f"Magento orders {mag['count']} vs GA4 purchases {ga_buys}: "
                      f"scaling GA4 conversions x{capture:.2f}")
        except Exception as e:
            print(f"(Magento order check skipped: {e})")

    conv = {}
    for t in PAGE_TYPES:
        p = pool.get(t, {"sessions": 0, "orders": 0})
        conv[t] = (p["orders"] * capture / p["sessions"]) if p["sessions"] else 0.0

    # per-row opportunity, aggregated per page
    cannibals = find_cannibals(rows)
    pages = defaultdict(lambda: {"impr": 0, "clicks": 0, "pos_w": 0.0, "top_q": ("", 0),
                                 "A_clicks": 0.0, "A_profit": 0.0, "B_clicks": 0.0, "B_profit": 0.0,
                                 "gap_clicks": 0.0, "gap_profit": 0.0})
    for r in rows:
        path = r["path"]
        t = page_type(path, cat_paths)
        m = margin_for_path(path, ps.get("product_lines", {}), default_margin, card_fee)
        vpc = conv[t] * aov * m
        opp = row_opportunity(r, curve, vpc)
        pg = pages[path]
        pg["type"], pg["margin"], pg["vpc"] = t, m, vpc
        pg["impr"] += r["impressions"]
        pg["clicks"] += r["clicks"]
        pg["pos_w"] += r["position"] * r["impressions"]
        if r["impressions"] > pg["top_q"][1]:
            pg["top_q"] = (r["query"], r["impressions"])
        for s in SCENARIOS:
            pg[f"{s}_clicks"] += opp[s][0]
            pg[f"{s}_profit"] += opp[s][1]
        g = snippet_gap(r)
        pg["gap_clicks"] += g
        pg["gap_profit"] += g * vpc

    # optional live check: category pages that list no products
    no_products = set()
    if args.check_pages:
        cands = sorted((p for p, d in pages.items() if d["type"] == "category"),
                       key=lambda p: -pages[p]["A_profit"])[:args.check_pages]
        base = cfg["data_sources"]["gsc"]["property_url"].replace("sc-domain:", "https://")
        for p in cands:
            try:
                if count_listed_products(base.rstrip("/") + p) == 0:
                    no_products.add(p)
            except Exception:
                pass
            time.sleep(0.5)

    # ---------------- report
    print("=" * 96)
    print(f"SEO OPPORTUNITY AUDIT  last {args.days} days  (non-brand Google search; brand "
          f"clicks excluded: {brand_clicks})")
    print("=" * 96)
    raw_t = raw_ctr_table(rows)
    print("\nYOUR CLICK-THROUGH BY POSITION (non-brand) vs a typical result")
    print(f"   {'pos':>5}{'impressions':>13}{'clicks':>8}{'your CTR':>10}{'typical':>9}{'used':>8}")
    for b in sorted(curve):
        i, c = raw_t.get(b, (0, 0))
        lab = f"#{b}" if b <= 10 else {15: "11-15", 20: "16-20", 30: "21+"}[b]
        print(f"   {lab:>5}{i:>13,}{c:>8,}{(c / i * 100 if i else 0):>9.2f}%"
              f"{FALLBACK_CTR[b]*100:>8.1f}%{curve[b]*100:>7.2f}%{'' if src[b]=='site' else '*'}")
    print("   used = your own CTR (made non-increasing); * = too few impressions, typical used")
    print("\nORGANIC CONVERSION BY PAGE TYPE (GA4 landing pages"
          + (f", x{capture:.2f} for orders GA4 misses" if capture > 1 else "") + ")")
    for t in PAGE_TYPES:
        p = pool.get(t)
        if p and p["sessions"]:
            print(f"   {t:<9} {p['sessions']:>7} sessions  {p['orders']:>4} orders  "
                  f"conv {conv[t]*100:.2f}%")
    print(f"   average organic order value: ${aov:.2f}"
          + ("" if all_orders else " (config AOV, no GA4 organic orders)"))

    tot_clicks = sum(d["clicks"] for d in pages.values())
    print("\nWHOLE-SITE UPSIDE (per month)")
    print(f"   today: {tot_clicks/months:,.0f} non-brand clicks/month")
    for s, target in SCENARIOS.items():
        c = sum(d[f"{s}_clicks"] for d in pages.values()) / months
        pr = sum(d[f"{s}_profit"] for d in pages.values()) / months
        print(f"   scenario {s} (every page reaches #{target}): +{c:,.0f} clicks/month, "
              f"+${pr:,.0f} gross profit/month")
    gc = sum(d["gap_clicks"] for d in pages.values()) / months
    gp = sum(d["gap_profit"] for d in pages.values()) / months
    print(f"   snippet gap (same positions, typical click rate): +{gc:,.0f} clicks/month, "
          f"+${gp:,.0f} gross profit/month")
    by_type = defaultdict(float)
    for d in pages.values():
        by_type[d["type"]] += d["A_profit"] / months
    print("   scenario A profit by page type: " + ", ".join(
        f"{t} ${v:,.0f}" for t, v in sorted(by_type.items(), key=lambda x: -x[1])))

    ranked = sorted(pages.items(), key=lambda kv: -kv[1]["A_profit"])
    print(f"\nTOP {args.top} PAGES BY PROFIT FROM REACHING #8 (scenario A), per month")
    print(f"   {'page':<46}{'type':<9}{'impr/mo':>8}{'pos':>6}{'clk/mo':>7}"
          f"{'+clk A':>7}{'+$ A':>7}{'+$ B':>7}{'gapclk':>7}  flags / top query")
    for path, d in ranked[:args.top]:
        pos = d["pos_w"] / d["impr"] if d["impr"] else 0
        flags = []
        if path in cannibals:
            flags.append(f"CANNIBAL({len(cannibals[path])}q)")
        if path in no_products:
            flags.append("NO-PRODUCTS")
        print(f"   {path[:45]:<46}{d['type']:<9}{d['impr']/months:>8,.0f}{pos:>6.1f}"
              f"{d['clicks']/months:>7,.0f}{d['A_clicks']/months:>7,.0f}"
              f"{d['A_profit']/months:>7,.0f}{d['B_profit']/months:>7,.0f}"
              f"{d['gap_clicks']/months:>7,.0f}  "
              f"{' '.join(flags)} \"{d['top_q'][0][:40]}\"")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "type", "impressions", "clicks", "avg_position", "margin_net",
                    "value_per_click", "extra_clicks_A", "extra_profit_A", "extra_clicks_B",
                    "extra_profit_B", "snippet_gap_clicks", "cannibal_queries", "no_products",
                    "top_query"])
        for path, d in ranked:
            w.writerow([path, d["type"], d["impr"], d["clicks"],
                        round(d["pos_w"] / d["impr"], 1) if d["impr"] else "",
                        round(d["margin"], 3), round(d["vpc"], 3),
                        round(d["A_clicks"], 1), round(d["A_profit"], 2),
                        round(d["B_clicks"], 1), round(d["B_profit"], 2),
                        round(d["gap_clicks"], 1),
                        "; ".join(sorted(cannibals.get(path, []))[:5]),
                        int(path in no_products), d["top_q"][0]])
    print(f"\nFull table (every page) -> {OUT_CSV}")
    print("Profit = extra clicks x conversion x order value x net margin, per month. "
          "Scenario B is a ceiling, not a forecast.")


if __name__ == "__main__":
    main()
