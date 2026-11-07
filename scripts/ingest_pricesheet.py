#!/usr/bin/env python3
"""Ingest a vendor pricesheet and compute REAL margins for a product line.

Replaces a guessed per-category margin with one derived from actual dealer cost
vs your sell price (MAP). Writes data/product_margins.json (the per-SKU detail
and summary) and, with --write-override, updates data/config_overrides.json so
Paid Search and the break-even math use the real number immediately.

    # inspect only (prints summary, writes data/product_margins.json):
    python3 scripts/ingest_pricesheet.py path/to/pricelist.xlsx --line carpets

    # also set the live margin used by the engine:
    python3 scripts/ingest_pricesheet.py path/to/pricelist.xlsx --line carpets --write-override

--line is the config product_lines key (carpets, montessori, name_trains, ...).
--sell map|msrp chooses the sell-price basis (default map). --sheet picks the
worksheet index for multi-tab workbooks (default 0).

Read-only toward the pricesheet; the only writes are to data/ (never config/).
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.analysis.margins import (  # noqa: E402
    parse_pricesheet, compute_margins, recommended_line_margin,
)

MARGINS_PATH = ROOT / "data" / "product_margins.json"
OVERRIDES_PATH = ROOT / "data" / "config_overrides.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pricesheet", help="Path to .xlsx or .csv pricesheet")
    ap.add_argument("--line", required=True,
                    help="config product_lines key this sheet is for (e.g. carpets)")
    ap.add_argument("--sell", choices=["map", "msrp", "store"], default="map",
                    help="sell-price basis: map/msrp from the sheet, or 'store' to "
                         "use YOUR actual Magento prices (default map)")
    ap.add_argument("--sheet", type=int, default=0, help="worksheet index (default 0)")
    ap.add_argument("--ship-model", choices=["none", "carpets", "flat"], default="none",
                    help="'carpets' uses the Carpets for Kids size/zone freight card; "
                         "'flat' uses --shipping-* params; 'none' = gross margin")
    ap.add_argument("--zone", default="avg",
                    help="carpets ship zone 1-7 or 'avg' across zones (default avg)")
    ap.add_argument("--no-surcharge", action="store_true",
                    help="carpets: exclude the temporary +$60 XL seasonal surcharge")
    ap.add_argument("--shipping-flat", type=float, default=0.0,
                    help="flat model: shipping COGS per unit, flat $ (default 0)")
    ap.add_argument("--shipping-per-lb", type=float, default=0.0,
                    help="flat model: shipping COGS per pound, times ship weight")
    ap.add_argument("--xl-surcharge", type=float, default=0.0,
                    help="flat model: extra $ on XL rugs (e.g. 60)")
    ap.add_argument("--write-override", action="store_true",
                    help="update data/config_overrides.json with the median margin")
    ap.add_argument("--allow-gross", action="store_true",
                    help="permit --write-override with NO shipping model (stores a "
                         "gross, shipping-ignorant margin; normally refused for freight items)")
    args = ap.parse_args()

    try:
        records, colmap = parse_pricesheet(args.pricesheet, sheet_index=args.sheet)
    except Exception as e:
        sys.exit(f"Could not read pricesheet: {e}")
    if not records:
        sys.exit("No priced SKUs found (need SKU + cost + MAP/MSRP columns with values).")

    ship_fn = None
    if args.ship_model == "carpets":
        from src.analysis.shipping_carpets import make_ship_fn
        ship_fn = make_ship_fn(zone=args.zone, season_surcharge=not args.no_surcharge)

    price_map = None
    if args.sell == "store":
        try:
            from src.data_sources.magento_client import MagentoClient, MagentoError
        except Exception as e:
            sys.exit(f"Cannot import Magento client for --sell store: {e}")
        mc = MagentoClient()
        if not mc.configured:
            sys.exit("--sell store needs Magento creds (MAGENTO_BASE_URL/MAGENTO_TOKEN "
                     "in .env or the service env). Run this on the server.")
        skus = [r["sku"] for r in records]
        try:
            price_map = mc.prices_for_skus(skus)
        except MagentoError as e:
            sys.exit(f"Magento price read failed: {e}")
        matched = sum(1 for r in records if price_map.get(r["sku"]))
        print(f"Matched {matched}/{len(skus)} pricesheet SKUs to live Magento prices.")
        if matched == 0:
            sys.exit("No SKUs matched your Magento catalog. The pricesheet SKU column "
                     "may not equal your Magento SKUs; tell me how they map and I'll "
                     "add a translation.")

    res = compute_margins(records, sell_basis=args.sell,
                          shipping_flat=args.shipping_flat,
                          shipping_per_lb=args.shipping_per_lb,
                          xl_surcharge=args.xl_surcharge, ship_fn=ship_fn,
                          price_map=price_map)
    s = res["summary"]
    median = recommended_line_margin(s)
    has_ship = args.ship_model != "none" and (
        ship_fn is not None or any([args.shipping_flat, args.shipping_per_lb, args.xl_surcharge]))

    print(f"\nPricesheet: {args.pricesheet}")
    print(f"Line:       {args.line}")
    print(f"Columns detected: " + ", ".join(f"{k}=col{v}" for k, v in colmap.items()))
    print(f"Priced SKUs: {s['n']}  (XL rugs: {s.get('xl_skus', 0)})")
    if args.ship_model == "carpets":
        print(f"Shipping model: Carpets for Kids rate card, zone={args.zone}, "
              f"XL surcharge {'OFF' if args.no_surcharge else 'ON (+$60)'}  "
              f"(avg ${s.get('avg_shipping', 0)}/unit)")
    elif has_ship:
        print(f"Shipping model: flat ${args.shipping_flat}/unit + "
              f"${args.shipping_per_lb}/lb + ${args.xl_surcharge} on XL  "
              f"(avg ${s.get('avg_shipping', 0)}/unit)")
    else:
        print("Shipping model: NONE (numbers below are GROSS, before shipping). "
              "Use --ship-model carpets for net rug margins.")
    if s["n"]:
        label = "NET" if has_ship else "GROSS"
        print(f"{label} margin on {s['sell_basis'].upper()} (before 3% card fee): "
              f"median {s['median']*100:.1f}%  mean {s['mean']*100:.1f}%  "
              f"range {s['min']*100:.1f}% to {s['max']*100:.1f}%")
        if s.get("price_sources"):
            print("Price sources: " + ", ".join(f"{k}={v}" for k, v in
                                                 s["price_sources"].items())
                  + ("  (fallback = SKU not in Magento, used MAP/MSRP)"
                     if s["price_sources"].get("fallback") else ""))
        be = (1.0 / (median - 0.03)) if median and median > 0.03 else None
        if be:
            print(f"Implied break-even ROAS at the median margin: {be:.1f} "
                  f"(1 / (margin - card_fee 0.03))")

    # write per-SKU detail + summary (merge by line)
    store = _load_json(MARGINS_PATH)
    store.setdefault("lines", {})[args.line] = {
        "generated_at": date.today().isoformat(),
        "source_file": Path(args.pricesheet).name,
        "sell_basis": s.get("sell_basis", args.sell),
        "summary": s,
        "recommended_margin": median,
        "skus": res["skus"],
    }
    MARGINS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARGINS_PATH.write_text(json.dumps(store, indent=2))
    print(f"\nWrote per-SKU margins + summary -> {MARGINS_PATH}")

    if args.write_override:
        if not median:
            sys.exit("No median margin to write (no priced SKUs).")
        if not has_ship and not args.allow_gross:
            sys.exit("Refusing to write a GROSS margin as the live number: rugs carry "
                     "real shipping COGS. Re-run with a shipping model "
                     "(--shipping-flat / --shipping-per-lb / --xl-surcharge), or pass "
                     "--allow-gross to override deliberately.")
        ov = _load_json(OVERRIDES_PATH)
        ov.setdefault("paid_search", {}).setdefault("product_lines", {}) \
          .setdefault(args.line, {})["margin"] = round(median, 4)
        OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
        OVERRIDES_PATH.write_text(json.dumps(ov, indent=2))
        print(f"Updated live margin: paid_search.product_lines.{args.line}.margin "
              f"= {round(median, 4)} -> {OVERRIDES_PATH}")
        print("The engine will use this on the next request (config_overrides is "
              "deep-merged by load_config).")
    else:
        print("\n(Run again with --write-override to set this as the live margin.)")


if __name__ == "__main__":
    main()
