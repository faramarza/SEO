#!/usr/bin/env python3
"""Pick the 8 products for the "Where to Start" row on /montessori-toys.html,
from REAL sales and REAL margins, not taste.

Run on the server:

    /var/www/seo/venv/bin/python scripts/montessori_top_picks.py
    ... --days 365          (GA4 sales window, default 365)
    ... --n 8               (how many picks, default 8)
    ... --brand-cap 3       (max picks from one brand, default 3)
    ... --catalog-csv f.csv (read products from a Magento export instead of the API)

Ranking = estimated PROFIT earned in the window:
    GA4 item revenue x margin
where margin comes from data/product_margins.json (per-SKU, from your
pricesheets) when present, else the brand margin in config (Guide Craft 49.6%,
Whitney Brothers 34.7%, Tag Toys 16.8%, ...), else the config default.
Products with no sales are ranked after sellers by margin x price, so a new
high-margin item can still fill an empty slot.

Only products that are ENABLED, VISIBLE, IN STOCK and in a Montessori category
qualify. Items under --min-margin (default 15%) are excluded: a hero slot should
not push a product that barely makes money.

Output: a ranked table, the comma-separated SKU list to paste into the Page
Builder "Products" element (condition: SKU is one of ...), and
data/montessori_top_picks.csv. Read-only toward Magento and GA4.
"""
import argparse
import csv
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

OUT_CSV = ROOT / "data" / "montessori_top_picks.csv"
OUT_HTML = ROOT / "data" / "montessori_where_to_start.html"

# Magento Catalog Products List widget: renders real product cards (image, price,
# Add to Cart) inside the category description, so the hierarchy is untouched:
# nothing is assigned to the Montessori Toys category itself.
_WIDGET = (
    '{{widget type="Magento\\CatalogWidget\\Block\\Product\\ProductsList" '
    'show_pager="0" products_count="%(n)d" sort_order="position_by_sku" '
    'template="Magento_CatalogWidget::product/widget/content/grid.phtml" '
    'conditions_encoded="^[`1`:^[`type`:`Magento||CatalogWidget||Model||Rule||'
    'Condition||Combine`,`aggregator`:`all`,`value`:`1`,`new_child`:``^],'
    '`1--1`:^[`type`:`Magento||CatalogWidget||Model||Rule||Condition||Product`,'
    '`attribute`:`sku`,`operator`:`()`,`value`:`%(skus)s`^]^]"}}'
)


_GRID_CSS = '.mt-start-wrap .block-products-list,.mt-start-wrap .block-content,.mt-start-wrap .products-grid{width:100%!important;max-width:100%!important;margin:0!important;padding:0!important}.mt-start-wrap ol.product-items{display:grid!important;grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:24px!important;margin:0 0 1.5rem!important;padding:0!important;list-style:none!important}.mt-start-wrap ol.product-items>li.product-item{width:auto!important;max-width:none!important;margin:0!important;padding:0!important;float:none!important;flex:none!important}.mt-start-wrap .product-item-info{width:100%!important;max-width:none!important}.mt-start-wrap .product-item-name{word-break:normal!important;overflow-wrap:break-word;hyphens:none}@media (max-width:768px){.mt-start-wrap ol.product-items{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:16px!important}}'


def section_html(picks):
    """The 'Where to Start' block in the page's own mt-hub style. The subtitle
    only says 'buy most' when every pick really has sales; otherwise it makes no
    popularity claim."""
    skus = ", ".join(r["sku"] for r in picks)
    if picks and all(r["purchased"] > 0 for r in picks):
        sub = ("Not sure where to start? These are the Montessori toys our customers "
               "buy most, each one built around a real skill, from fine motor control "
               "to early logic.")
    else:
        sub = ("Not sure where to start? These hand-picked wooden classics are a great "
               "first Montessori toy, each one built around a real skill, from fine "
               "motor control to early logic.")
    return (
        "<style>.mt-start-wrap{max-width:1200px;margin:2.5rem auto}"
        ".mt-start-sub{text-align:center;color:var(--mt-muted);max-width:720px;"
        "margin:0 auto 1.25rem;font-family:'Poppins',sans-serif}"
        # The theme's widget grid ignores the page width inside a category
        # description (all 8 cards squeezed into one row on phones). Force a
        # 4-column grid on desktop and 2 columns on mobile. Tested live.
        + _GRID_CSS + "</style>"
        "<section class=\"mt-start-wrap\" id=\"where-to-start\" "
        "aria-labelledby=\"mt-start-heading\">"
        "<h2 id=\"mt-start-heading\" style=\"text-align:center;margin-bottom:.5rem;\">"
        "Where to Start: Our Most-Loved Montessori Toys</h2>"
        f"<p class=\"mt-start-sub\">{sub}</p>"
        + _WIDGET % {"n": len(picks), "skus": skus}
        + "<div class=\"cta-btn-center\"><a href=\"#shop-by-age\" "
        "class=\"cta-button action primary\">Shop Montessori Toys by Age</a></div>"
        "</section>\n"
    )


def _cfg():
    return json.loads((ROOT / "config" / "defaults.json").read_text())


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def eff_price(price, special):
    try:
        p = float(price or 0)
    except ValueError:
        p = 0.0
    try:
        s = float(special or 0)
    except ValueError:
        s = 0.0
    return s if 0 < s < p else p


def margin_for(sku, brand, sku_margins, brand_margins, default):
    """(margin, source). Per-SKU pricesheet margin wins, then brand, then default."""
    if sku in sku_margins:
        return sku_margins[sku], "sku"
    b = norm(brand).replace("guidecraft", "guide craft")
    for key, v in brand_margins.items():
        if key.startswith("_"):
            continue
        if key in b or (b and b in key):
            return float(v["margin"]), "brand"
    return default, "default"


DEFAULT_EXCLUDE = r"name[- ]?train|letter[- ]?train|alphabet[- ]train"


def rank(products, sales, sku_margins, brand_margins, default_margin,
         n=8, brand_cap=3, min_margin=0.15, exclude=DEFAULT_EXCLUDE,
         stock_check=None):
    """Pure ranking, unit-tested. products: dicts with sku, name, brand, price,
    categories, enabled, in_stock. sales: {normalized item name: {revenue,
    purchased, added_to_cart}}. stock_check(sku) -> (in_stock, qty) is called for
    any would-be pick whose stock is unknown; sold-out items are skipped and
    marked, never promoted. Returns (picks, ranked_all)."""
    ranked, seen = [], set()
    for p in products:
        if p["sku"] in seen:
            continue  # the paged product API can return a SKU twice
        seen.add(p["sku"])
        if not (p["enabled"] and p["in_stock"]):
            continue
        if not any("montessori" in c.lower() for c in p["categories"]):
            continue
        if exclude and re.search(exclude, f"{p['name']} {p['sku']} {p.get('url_key', '')}", re.I):
            continue  # name trains have their own page/audience and an unconfirmed margin
        m, src = margin_for(p["sku"], p["brand"], sku_margins, brand_margins, default_margin)
        if m < min_margin:
            continue
        s = sales.get(norm(p["name"]), {})
        rev = float(s.get("revenue", 0) or 0)
        bestseller = any("bestseller" in c.lower() for c in p["categories"])
        ranked.append({**p, "margin": m, "margin_source": src,
                       "revenue": rev, "purchased": int(s.get("purchased", 0) or 0),
                       "added_to_cart": int(s.get("added_to_cart", 0) or 0),
                       "profit": round(rev * m, 2),
                       "unit_profit": round(p["price"] * m, 2),
                       "bestseller_cat": bestseller})
    # sellers first by profit; then non-sellers: bestseller category, add-to-carts,
    # then profit per unit sold.
    ranked.sort(key=lambda r: (r["profit"] > 0, r["profit"], r["bestseller_cat"],
                               r["added_to_cart"], r["unit_profit"]), reverse=True)
    picks, per_brand = [], {}
    for r in ranked:
        b = norm(r["brand"]) or "(none)"
        if per_brand.get(b, 0) >= brand_cap:
            continue
        if r.get("stock_known") is False and stock_check:
            ok, qty = stock_check(r["sku"])
            r["qty"] = qty
            r["stock_known"] = ok is not None
            if ok is None:
                r["stock_status"] = "UNCHECKED"
            elif not ok:
                r["stock_status"] = "OUT"
                continue
            else:
                r["stock_status"] = "in stock"
        picks.append(r)
        per_brand[b] = per_brand.get(b, 0) + 1
        if len(picks) >= n:
            break
    return picks, ranked


def display_order(picks):
    """Order shown on the page: the best seller (most units) first, then the rest
    from lowest to highest price, so the row opens with an easy first purchase
    instead of the most expensive set."""
    if not picks:
        return []
    lead = max(picks, key=lambda r: (r["purchased"], r["profit"]))
    rest = sorted((r for r in picks if r is not lead), key=lambda r: r["price"])
    return [lead] + rest


# ---------------------------------------------------------------- loaders
def _parse_additional(s):
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def products_from_csv(path):
    csv.field_size_limit(10 ** 9)
    out = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if (r.get("store_view_code") or "").strip():
                continue  # store-view override rows repeat the product
            add = _parse_additional(r.get("additional_attributes"))
            cats = [c.split("/")[-1].strip() for c in (r.get("categories") or "").split(",") if c]
            qty = float(r.get("qty") or 0)
            out.append({
                "sku": r["sku"], "name": r.get("name", ""),
                "brand": add.get("manufacturer") or add.get("brand_google") or "",
                "price": eff_price(r.get("price"), r.get("special_price")),
                "categories": cats,
                "enabled": str(r.get("product_online", "1")) == "1"
                           and "Not Visible" not in (r.get("visibility") or ""),
                "in_stock": str(r.get("is_in_stock", "1")) == "1" and qty > 0,
                "url_key": r.get("url_key", ""),
            })
    return out


def _magento():
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured (MAGENTO_BASE_URL / MAGENTO_TOKEN in .env). "
                 "Or pass --catalog-csv with a product export.")
    return mc


def magento_stock_check(mc):
    """READ-ONLY per-SKU stock lookup. (in_stock, qty); (None, None) if unreadable.
    In stock = flagged in stock AND (qty > 0 or stock not managed)."""
    from urllib.parse import quote

    def check(sku):
        try:
            st = mc._req("GET", f"/stockItems/{quote(sku, safe='')}") or {}
        except Exception:
            return None, None
        qty = float(st.get("qty") or 0)
        managed = bool(st.get("manage_stock", True))
        return bool(st.get("is_in_stock")) and (qty > 0 or not managed), qty
    return check


def products_from_magento(mc):
    cats = {}
    data = mc._req("GET", "/categories/list?searchCriteria[pageSize]=2000") or {}
    for c in data.get("items") or []:
        cats[int(c["id"])] = c.get("name", "")
    meta = mc.product_attributes_meta()
    man_opts = (meta.get("manufacturer") or {}).get("options", {})
    out = []
    for p in mc.iter_all_products(only_visible=True):
        ext = p.get("extension_attributes") or {}
        names = [cats.get(int(l.get("category_id", 0)), "") for l in ext.get("category_links") or []]
        stock = ext.get("stock_item") or {}
        man = mc._attr(p, "manufacturer")
        brand = man_opts.get(str(man), "") or mc._attr(p, "brand_google")
        out.append({
            "sku": p.get("sku", ""), "name": p.get("name", ""), "brand": brand,
            "price": eff_price(p.get("price"), mc._attr(p, "special_price")),
            "categories": [n for n in names if n],
            "enabled": int(p.get("status", 1)) == 1,
            # list endpoint may omit stock_item; unknown is treated as in stock
            # and flagged so a sold-out item is not silently promoted.
            "in_stock": bool(stock.get("is_in_stock", True)),
            "stock_known": bool(stock),
            "url_key": mc._attr(p, "url_key"),
        })
    return out


def sales_from_ga4(cfg, days):
    from src.data_sources.ga4_client import GA4Client
    g = cfg["data_sources"]["ga4"]
    ga = GA4Client(property_id=str(g["property_id"]),
                   credentials_path=str(ROOT / g["credentials_path"]))
    raw = ga.get_item_data(days=days)
    return {norm(k): v for k, v in raw.items()}


def sku_margins_from_file(cfg=None):
    """Per-SKU margins: data/product_margins.json (pricesheet ingests) plus
    config paid_search.sku_margins (hand-entered from vendor invoices)."""
    out = {}
    for sku, rec in ((cfg or {}).get("paid_search", {}).get("sku_margins") or {}).items():
        if not sku.startswith("_") and isinstance(rec, dict) and rec.get("margin") is not None:
            out[str(sku)] = float(rec["margin"])
    p = ROOT / "data" / "product_margins.json"
    if not p.exists():
        return out
    try:
        d = json.loads(p.read_text())
    except Exception:
        return out
    for line in (d.get("lines") or {}).values():
        for sku, rec in (line.get("skus") or {}).items():
            if rec.get("margin") is not None:
                out[str(sku)] = float(rec["margin"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--brand-cap", type=int, default=3)
    ap.add_argument("--min-margin", type=float, default=0.15)
    ap.add_argument("--catalog-csv", default=None)
    ap.add_argument("--no-ga4", action="store_true", help="rank without sales data")
    ap.add_argument("--exclude", default=DEFAULT_EXCLUDE,
                    help="regex on name/SKU/url_key to leave out (default: name trains; "
                         "pass '' to include everything)")
    args = ap.parse_args()

    cfg = _cfg()
    ps = cfg.get("paid_search", {})
    brand_margins = ps.get("brand_margins", {})
    default_margin = float(ps.get("default_margin", 0.15))

    mc = None if args.catalog_csv else _magento()
    products = products_from_csv(args.catalog_csv) if args.catalog_csv else products_from_magento(mc)
    sales = {} if args.no_ga4 else sales_from_ga4(cfg, args.days)
    if not args.no_ga4 and not sales:
        print("WARNING: GA4 returned no item sales; ranking by margin and bestseller tag only.")
    picks, ranked = rank(products, sales, sku_margins_from_file(cfg), brand_margins,
                         default_margin, n=args.n, brand_cap=args.brand_cap,
                         min_margin=args.min_margin, exclude=args.exclude,
                         stock_check=magento_stock_check(mc) if mc else None)

    print(f"Products read: {len(products)}  ({'catalog CSV' if args.catalog_csv else 'Magento API, sorted paging'})")
    print(f"Montessori candidates (enabled, in stock, margin >= {args.min_margin:.0%}): "
          f"{len(ranked)}   GA4 items with sales data: {len(sales)}")
    out = [r for r in ranked if r.get("stock_status") == "OUT"]
    unchecked = [r for r in picks if r.get("stock_status") == "UNCHECKED"]
    if out:
        print(f"Skipped as SOLD OUT (checked live): " + ", ".join(r["sku"] for r in out))
    if unchecked:
        print("WARNING: could not read stock for " + ", ".join(r["sku"] for r in unchecked)
              + " (token may lack Inventory access). Check these by hand.")
    print(f"\n{'#':>2} {'SKU':<14}{'name':<44}{'brand':<17}{'price':>8}{'margin':>8}"
          f"{'sold':>5}{'revenue':>9}{'profit':>8}  stock")
    for i, r in enumerate(ranked[:25], 1):
        tag = "*" if r in picks else ("x" if r.get("stock_status") == "OUT" else " ")
        guess = "?" if r["margin_source"] == "default" else " "
        qty = r.get("qty")
        stock = r.get("stock_status", "")
        if qty is not None and stock:
            stock = f"{stock} ({qty:g})"
        print(f"{i:>2}{tag}{r['sku'][:13]:<14}{r['name'][:43]:<44}{r['brand'][:16]:<17}"
              f"{r['price']:>8.2f}{r['margin']*100:>6.1f}%{guess}{r['purchased']:>5}"
              f"{r['revenue']:>9.0f}{r['profit']:>8.0f}  {stock}")
    print("\n* = picked (brand cap applied, stock verified)   x = sold out, skipped")
    print("? = margin is the config DEFAULT (no cost data for that brand): treat its "
          "profit as a guess.")

    print("\nPAGE BUILDER > Products element > Condition: SKU is one of:")
    shown = display_order(picks)
    print("  " + ", ".join(r["sku"] for r in shown))
    print("  (display order: best seller first, then lowest to highest price)")

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(section_html(shown))
    print(f"\nReady-to-paste section (widget + heading + CTA) -> {OUT_HTML}")
    print("Paste it into the Montessori Toys description just ABOVE "
          "<div class=\"mt-what-wrap\"> (right under the hero).")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "picked", "sku", "name", "brand", "price", "margin",
                    "margin_source", "purchased", "revenue", "est_profit", "url_key"])
        for i, r in enumerate(ranked, 1):
            w.writerow([i, int(r in picks), r["sku"], r["name"], r["brand"], r["price"],
                        r["margin"], r["margin_source"], r["purchased"], r["revenue"],
                        r["profit"], r["url_key"]])
    print(f"\nFull ranking -> {OUT_CSV}")


if __name__ == "__main__":
    main()
