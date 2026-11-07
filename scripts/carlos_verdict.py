#!/usr/bin/env python3
"""Carlos link-building verdict — the one memo that settles the renewal decision.

Answers the question the vendor report could NOT (it had no GSC/GA4/sales data):
for each commercial category, did the ranking gains turn into (a) real organic
CLICKS and (b) actual organic REVENUE — or did traffic rise with no money, which
would confirm that the bottleneck is conversion, not rankings?

For a baseline window and a campaign window it prints, PER CATEGORY:
  GSC   : organic clicks, impressions, avg position   (did rank -> clicks happen?)
  GA4   : organic sessions, purchases, revenue, conv% (did clicks -> money happen?)
and a plain-English verdict per category + a site-wide bottom line.

Categories are read from config -> paid_search.product_lines (url_patterns), so this
stays data-driven; "name_puzzles" (the report's headline win) is added explicitly
because it is not yet its own config line.

Run on the SERVER where credentials.json exists:

    venv/bin/python scripts/carlos_verdict.py                # recent 180d vs prior 180d
    venv/bin/python scripts/carlos_verdict.py --split 2026-05-01
                                                             # campaign = 2026-05-01..now,
                                                             # baseline = equal span before
    venv/bin/python scripts/carlos_verdict.py --days 90      # tighter 90d windows

Read-only. Makes no changes to GSC, GA4, Magento, or the site.

IMPORTANT retention caveat the script also prints: GSC keeps ~16 months and GA4
event data is typically 2 or 14 months. A true "before Carlos started" baseline
(pre-May-2025) is usually OUTSIDE both windows, so this measures the campaign's
TRAJECTORY within the retained window, not a pure pre/post. Where a window predates
retention the rows come back empty and the script says so rather than inventing them.
"""
import os
import sys


def _reexec_in_venv():
    """systemd 'seo' runs in a venv; plain python3 usually lacks google libs."""
    try:
        import google.analytics.data_v1beta  # noqa: F401
        return
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    cands = [os.path.join(root, v, "bin", "python") for v in ("venv", ".venv", "env")]
    try:
        import re
        import subprocess
        out = subprocess.run(["systemctl", "show", "-p", "ExecStart", "--value", "seo"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(/[^\s;]+)/bin/(?:gunicorn|python[0-9.]*)", out)
        if m:
            cands.insert(0, m.group(1) + "/bin/python")
    except Exception:
        pass
    cur = os.path.realpath(sys.executable)
    for py in cands:
        if os.path.exists(py) and os.path.realpath(py) != cur:
            os.execv(py, [py] + sys.argv)   # hand off; does not return


_reexec_in_venv()

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.data_sources.gsc_client import GSCClient   # noqa: E402
from src.data_sources.ga4_client import GA4Client   # noqa: E402


def _load_config() -> dict:
    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    ov = ROOT / "data" / "config_overrides.json"
    if ov.exists():
        try:
            merged = json.loads(ov.read_text())

            def _deep(a, b):
                for k, v in b.items():
                    if isinstance(v, dict) and isinstance(a.get(k), dict):
                        _deep(a[k], v)
                    else:
                        a[k] = v
            _deep(cfg, merged)
        except Exception:
            pass
    return cfg


def _categories(cfg: dict) -> list[tuple[str, str, list[str]]]:
    """(key, label, patterns) built from config product_lines, data-driven.
    Order matters: first matching category wins when a URL matches several, so
    the more specific lines are listed first. 'name_puzzles' is added because it
    is the report's headline category but not (yet) its own config line."""
    lines = cfg.get("paid_search", {}).get("product_lines", {})
    out: list[tuple[str, str, list[str]]] = []
    # report's headline win — explicit, flagged in the docstring/output
    out.append(("name_puzzles", "Personalized name puzzles",
                ["name-puzzle", "name_puzzle", "name puzzle", "puzzle"]))
    # everything the config defines, in a priority order that avoids collisions
    priority = ["name_trains", "stools", "montessori", "carpets", "books"]
    seen = {"name_puzzles"}
    for key in priority + [k for k in lines if k not in priority]:
        if key in seen or key not in lines:
            continue
        seen.add(key)
        line = lines[key]
        label = line.get("label", key.replace("_", " ").title())
        pats = [p.lower() for p in line.get("url_patterns", []) if p]
        if pats:
            out.append((key, label, pats))
    return out


def _bucket(url: str, cats: list[tuple[str, str, list[str]]]) -> str | None:
    u = (url or "").lower()
    for key, _label, pats in cats:
        if any(p in u for p in pats):
            return key
    return None


def _pct(a: float, b: float) -> str:
    return "  n/a" if not b else f"{(a - b) / b * 100:+.0f}%"


# ---------- GSC ----------
def _gsc_by_page(client, svc, start, end) -> dict:
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": ["page"]}
    out = {}
    for r in client._query_all_rows(svc, body):
        out[r["keys"][0]] = (r.get("clicks", 0), r.get("impressions", 0),
                             r.get("position", 0.0))
    return out


def _gsc_cat_totals(pagedata: dict, cats) -> dict:
    agg = {k: [0.0, 0.0, 0.0] for k, _l, _p in cats}  # clicks, impr, pos*impr
    for url, (c, i, p) in pagedata.items():
        k = _bucket(url, cats)
        if k is None:
            continue
        agg[k][0] += c
        agg[k][1] += i
        agg[k][2] += p * i
    return agg


# ---------- GA4 ----------
def _ga4_organic_by_landing(ga4: GA4Client, start: date, end: date) -> dict:
    """Organic-Search landing pages for an EXPLICIT window.
    Returns {landing_page: {sessions, purchases, revenue, add_to_carts}}."""
    client = ga4._get_client()
    if client is None:
        return {}
    try:
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Dimension, Metric, Filter,
            FilterExpression, OrderBy,
        )
        req = RunReportRequest(
            property=f"properties/{ga4.property_id}",
            date_ranges=[DateRange(start_date=start.isoformat(),
                                   end_date=end.isoformat())],
            dimensions=[Dimension(name="landingPage")],
            metrics=[
                Metric(name="sessions"),
                Metric(name="ecommercePurchases"),
                Metric(name="purchaseRevenue"),
                Metric(name="addToCarts"),
            ],
            dimension_filter=FilterExpression(filter=Filter(
                field_name="sessionDefaultChannelGroup",
                string_filter=Filter.StringFilter(
                    value="Organic Search",
                    match_type=Filter.StringFilter.MatchType.EXACT))),
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"),
                               desc=True)],
            limit=25000,
        )
        resp = client.run_report(req)
        out = {}
        for row in resp.rows:
            url = row.dimension_values[0].value
            m = {h.name: row.metric_values[i].value
                 for i, h in enumerate(resp.metric_headers)}
            out[url] = {
                "sessions": int(float(m.get("sessions", 0))),
                "purchases": int(float(m.get("ecommercePurchases", 0))),
                "revenue": float(m.get("purchaseRevenue", 0)),
                "add_to_carts": int(float(m.get("addToCarts", 0))),
            }
        return out
    except Exception as e:
        print(f"  (GA4 window {start}->{end} error: {e})")
        return {}


def _ga4_organic_totals(ga4: GA4Client, start: date, end: date) -> dict:
    """Site-wide organic totals for an explicit window (no landing-page breakdown)."""
    client = ga4._get_client()
    if client is None:
        return {}
    try:
        from google.analytics.data_v1beta.types import (
            RunReportRequest, DateRange, Metric, Filter, FilterExpression,
        )
        req = RunReportRequest(
            property=f"properties/{ga4.property_id}",
            date_ranges=[DateRange(start_date=start.isoformat(),
                                   end_date=end.isoformat())],
            metrics=[Metric(name="sessions"), Metric(name="ecommercePurchases"),
                     Metric(name="purchaseRevenue"), Metric(name="addToCarts")],
            dimension_filter=FilterExpression(filter=Filter(
                field_name="sessionDefaultChannelGroup",
                string_filter=Filter.StringFilter(
                    value="Organic Search",
                    match_type=Filter.StringFilter.MatchType.EXACT))),
        )
        resp = client.run_report(req)
        if not resp.rows:
            return {"sessions": 0, "purchases": 0, "revenue": 0.0}
        m = {h.name: resp.rows[0].metric_values[i].value
             for i, h in enumerate(resp.metric_headers)}
        return {"sessions": int(float(m.get("sessions", 0))),
                "purchases": int(float(m.get("ecommercePurchases", 0))),
                "revenue": float(m.get("purchaseRevenue", 0))}
    except Exception as e:
        print(f"  (GA4 totals {start}->{end} error: {e})")
        return {}


def _ga4_cat_totals(landing: dict, cats) -> dict:
    agg = {k: {"sessions": 0, "purchases": 0, "revenue": 0.0, "atc": 0}
           for k, _l, _p in cats}
    for url, d in landing.items():
        k = _bucket(url, cats)
        if k is None:
            continue
        agg[k]["sessions"] += d["sessions"]
        agg[k]["purchases"] += d["purchases"]
        agg[k]["revenue"] += d["revenue"]
        agg[k]["atc"] += d["add_to_carts"]
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", help="Campaign start YYYY-MM-DD (baseline = equal span before)")
    ap.add_argument("--days", type=int, default=180,
                    help="Window length if --split omitted (default 180)")
    args = ap.parse_args()

    cfg = _load_config()
    cats = _categories(cfg)
    gsc_cfg = cfg.get("data_sources", {}).get("gsc", {})
    ga4_cfg = cfg.get("data_sources", {}).get("ga4", {})

    gsc = GSCClient(site_url=gsc_cfg.get("property_url", ""),
                    credentials_path=gsc_cfg.get("credentials_path") or None)
    svc = gsc._get_service()
    ga4 = GA4Client(property_id=str(ga4_cfg.get("property_id", "")),
                    credentials_path=ga4_cfg.get("credentials_path") or None)

    if svc is None:
        print("WARNING: GSC not configured (no credentials) — GSC section skipped.")
    if ga4._get_client() is None:
        print("WARNING: GA4 not configured (no credentials) — GA4 section skipped.")

    gsc_end = date.today() - timedelta(days=3)     # GSC lags ~3d
    if args.split:
        split = date.fromisoformat(args.split)
    else:
        split = gsc_end - timedelta(days=args.days)
    span = gsc_end - split
    base_start, base_end = split - span, split
    camp_start, camp_end = split, gsc_end

    print(f"\n{'='*74}")
    print("CARLOS LINK-BUILDING VERDICT — did rankings become clicks, and clicks money?")
    print(f"{'='*74}")
    print(f"BASELINE window: {base_start} -> {base_end}  ({span.days} days)")
    print(f"CAMPAIGN window: {camp_start} -> {camp_end}  ({span.days} days)")
    print("NOTE: GSC retains ~16 months, GA4 event data ~2-14 months. A true "
          "pre-May-2025\n      baseline is usually beyond retention — empty rows "
          "there are a retention gap,\n      not zero demand. This shows the "
          "campaign's TRAJECTORY in the retained window.\n")

    # ---- GSC ----
    gb = gcb = None
    if svc is not None:
        gb = _gsc_cat_totals(_gsc_by_page(gsc, svc, base_start, base_end), cats)
        gcb = _gsc_cat_totals(_gsc_by_page(gsc, svc, camp_start, camp_end), cats)
        print("GSC ORGANIC BY CATEGORY (baseline -> campaign)")
        print(f"  {'category':<24}{'clicks':>18}{'impressions':>22}{'avg pos':>12}")
        for key, label, _p in cats:
            bc, bi, bp = gb[key]
            cc, ci, cp = gcb[key]
            bpos = (bp / bi) if bi else 0
            cpos = (cp / ci) if ci else 0
            print(f"  {label[:24]:<24}"
                  f"{bc:>7.0f}->{cc:<6.0f}{_pct(cc, bc):>4}"
                  f"{bi:>9.0f}->{ci:<8.0f}{_pct(ci, bi):>4}"
                  f"{bpos:>6.1f}->{cpos:<4.1f}")
        print()

    # ---- GA4 ----
    ab = acb = None
    site_base = site_camp = None
    if ga4._get_client() is not None:
        ab = _ga4_cat_totals(_ga4_organic_by_landing(ga4, base_start, base_end), cats)
        acb = _ga4_cat_totals(_ga4_organic_by_landing(ga4, camp_start, camp_end), cats)
        site_base = _ga4_organic_totals(ga4, base_start, base_end)
        site_camp = _ga4_organic_totals(ga4, camp_start, camp_end)
        print("GA4 ORGANIC-SEARCH REVENUE BY CATEGORY (baseline -> campaign)")
        print(f"  {'category':<24}{'org sessions':>18}{'revenue $':>20}{'conv %':>16}")
        for key, label, _p in cats:
            b, c = ab[key], acb[key]
            bconv = (b["purchases"] / b["sessions"] * 100) if b["sessions"] else 0
            cconv = (c["purchases"] / c["sessions"] * 100) if c["sessions"] else 0
            print(f"  {label[:24]:<24}"
                  f"{b['sessions']:>7.0f}->{c['sessions']:<6.0f}{_pct(c['sessions'], b['sessions']):>4}"
                  f"{b['revenue']:>8,.0f}->{c['revenue']:<9,.0f}"
                  f"{bconv:>6.2f}->{cconv:<4.2f}")
        sb = site_camp or {}
        site_conv = (sb.get("purchases", 0) / sb.get("sessions", 1) * 100
                     if sb.get("sessions") else 0)
        print(f"\n  SITE-WIDE ORGANIC (campaign window): {sb.get('sessions', 0):,} sessions, "
              f"${sb.get('revenue', 0):,.0f} revenue, {sb.get('purchases', 0)} orders, "
              f"{site_conv:.2f}% conv")
        print()

    # ---- PER-CATEGORY VERDICT ----
    if svc is not None and ga4._get_client() is not None:
        print("VERDICT BY CATEGORY")
        site_conv = ((site_camp or {}).get("purchases", 0)
                     / (site_camp or {}).get("sessions", 1) * 100
                     if (site_camp or {}).get("sessions") else 0)
        for key, label, _p in cats:
            bc, bi, bp = gb[key]
            cc, ci, cp = gcb[key]
            bpos = (bp / bi) if bi else 0
            cpos = (cp / ci) if ci else 0
            c = acb[key]
            conv = (c["purchases"] / c["sessions"] * 100) if c["sessions"] else 0
            rank_up = bpos and cpos and cpos < bpos - 0.5
            clicks_up = cc > bc * 1.1 and cc >= 5
            selling = c["revenue"] >= 50 and c["purchases"] >= 1
            converting = conv >= max(0.5, site_conv * 0.5)

            if not (bi or ci):
                v = "NO DATA in retained window (demand unknown here, not zero)."
            elif selling and converting and (clicks_up or rank_up):
                v = (f"PAID OFF — rank/clicks up AND organic sells "
                     f"(${c['revenue']:,.0f}, {conv:.1f}% conv).")
            elif (clicks_up or rank_up) and not selling:
                v = ("RANK/CLICKS UP, NO MONEY — organic traffic rose but it isn't "
                     "converting.\n        -> confirms the bottleneck is CONVERSION, "
                     "not rankings. More links won't fix this.")
            elif selling and not (clicks_up or rank_up):
                v = (f"SELLS but organic traffic FLAT — ${c['revenue']:,.0f} from "
                     "organic without a link-driven lift.")
            else:
                v = "FLAT — no clear rank, click, or revenue movement from the links."
            print(f"  • {label}: {v}")
        print()

    print("HOW TO READ THIS")
    print("  The renewal question = 'did backlinks bring PROFITABLE customers?'")
    print("  - 'PAID OFF' categories are the case FOR continuing (targeted, if any).")
    print("  - 'RANK/CLICKS UP, NO MONEY' is the case AGAINST — it proves Frank's")
    print("    email right: the bottleneck is conversion, so fix the broken routes")
    print("    and product pages before buying more links.")
    print("  - Compare each category's organic conv% to the SITE-WIDE organic conv%")
    print("    above: well below site = a page/offer problem, not a traffic problem.\n")


if __name__ == "__main__":
    main()
