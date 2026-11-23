#!/usr/bin/env python3
"""Build the weekly money report (Governor page "Money This Week").

    /var/www/seo/venv/bin/python scripts/money_report.py        (builds and saves it)

Runs every Monday from cron; the page also has a "Rebuild now" button.
Read-only everywhere: Magento orders, GA4 sources, Google Ads spend (last 7 days),
the latest Awin export uploaded on the page. Saves data/money_report.json.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.sales_by_line import _load_dotenv, line_of  # noqa: E402

_load_dotenv()

from scripts.order_profit import fetch_orders, load_awin  # noqa: E402
from src.analysis import money_report as mr  # noqa: E402
from src.analysis.order_profit import order_profit, summarize  # noqa: E402

MONEY_DIR = ROOT / "data" / "money"
REPORT = ROOT / "data" / "money_report.json"
AWIN_FILE = MONEY_DIR / "awin_latest.csv"


def ad_spend(cfg, days=7):
    """(spend $, reason if unavailable) from the Google Ads API, read-only."""
    ads = cfg.get("data_sources", {}).get("google_ads", {})
    if not ads.get("customer_id"):
        return None, "Google Ads not configured"
    try:
        from src.data_sources.google_ads_client import GoogleAdsClient
        p = Path(ads.get("config_path", "google-ads.yaml"))
        c = GoogleAdsClient(credentials_path=str(p if p.is_absolute() else ROOT / p),
                            customer_id=ads.get("customer_id"),
                            login_customer_id=ads.get("login_customer_id"),
                            brand_terms=ads.get("brand_terms", []))
        camps = c.fetch_campaign_summary(days=days)
        if not camps:
            return None, c._last_error or "no campaign data"
        return sum(x.cost for x in camps.values()), None
    except Exception as e:
        return None, str(e)


def order_statuses(mc, numbers):
    out = {}
    for n in numbers:
        q = ("/orders?searchCriteria[filterGroups][0][filters][0][field]=increment_id"
             f"&searchCriteria[filterGroups][0][filters][0][value]={quote(str(n))}"
             "&searchCriteria[pageSize]=1&fields=items[increment_id,status]")
        try:
            for o in (mc._req("GET", q) or {}).get("items") or []:
                out[str(o.get("increment_id"))] = o.get("status") or ""
        except Exception:
            pass
    return out


def last_stock_updates():
    d = ROOT / "data" / "stock_updates"
    last = {"Guidecraft stock": None, "Whitney Brothers stock": None, "Carpets for Kids stock": None}
    names = {"guidecraft": "Guidecraft stock", "whitney": "Whitney Brothers stock", "carpets": "Carpets for Kids stock"}
    for p in d.glob("*.json") if d.exists() else []:
        try:
            x = json.loads(p.read_text())
        except Exception:
            continue
        if x.get("status") == "applied" and x.get("vendor") in names:
            k = names[x["vendor"]]
            if not last[k] or x["applied"] > last[k]:
                last[k] = x["applied"]
    return last


def build(today=None):
    today = today or date.today()
    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    try:
        carpets = json.loads((ROOT / "data" / "product_margins.json").read_text())["lines"]["carpets"]["skus"]
    except Exception:
        carpets = {}
    awin_rows = mr.load_awin_rows(AWIN_FILE) if AWIN_FILE.exists() else []
    commissions = load_awin(AWIN_FILE) if AWIN_FILE.exists() else {}
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient()
    if not mc.configured:
        raise RuntimeError("Magento not configured")
    start = today - timedelta(days=14)
    orders = fetch_orders(mc, start.isoformat(), (today + timedelta(days=1)).isoformat())
    results = [dict(order_profit(o, cfg, carpets, commissions), date=o["date"], items=o["items"]) for o in orders]
    this, prev = mr.week_split(results, today)
    notes = []
    try:
        from scripts.sales_source import ga4_sources, label
        sources = {k: label(v) for k, v in ga4_sources(today - timedelta(days=7), today).items()}
    except Exception as e:
        sources, _ = {}, notes.append(f"GA4 sources unavailable: {e}")
    spend, why = ad_spend(cfg, 7)
    if spend is None:
        notes.append(f"Google Ads spend unavailable: {why}")
    if not awin_rows:
        notes.append("No Awin export uploaded yet: commissions are not subtracted and the approval list is empty.")
    live = [r for r in this if r["basis"] != "canceled"]
    by_line = summarize(live, lambda r: "Refunded" if r["basis"] == "refunded" else
                        (line_of(max(r["items"], key=lambda i: i["revenue"])["name"]) if r["items"] else "Other"))
    pending = [r for r in awin_rows if (r.get("commission_status") or "").strip() == "0"]
    statuses = order_statuses(mc, sorted({(r.get("extra") or "").strip() for r in pending}))
    plan_file = ROOT / "data" / "product_demand_plan.json"
    plan_date = None
    if plan_file.exists():
        try:
            plan_date = json.loads(plan_file.read_text()).get("generated", "")[:10]
        except Exception:
            pass
    report = {
        "built": datetime.now().isoformat(timespec="minutes"),
        "week": [(today - timedelta(days=7)).isoformat(), today.isoformat()],
        "this_week": mr.totals(this), "last_week": mr.totals(prev),
        "by_line": [{"line": k, "orders": n, "revenue": rv, "profit": c, "margin_pct": m}
                    for k, n, rv, c, m in by_line],
        "ads": mr.ads_line(this, sources, spend),
        "awin": mr.awin_due(awin_rows, statuses, today),
        "awin_file_date": datetime.fromtimestamp(AWIN_FILE.stat().st_mtime).date().isoformat() if AWIN_FILE.exists() else None,
        "stale": mr.stale({**last_stock_updates(),
                           "Awin export": datetime.fromtimestamp(AWIN_FILE.stat().st_mtime).date().isoformat()
                           if AWIN_FILE.exists() else None,
                           "Demand plan (Start Here)": plan_date}, today,
                          max_days=7) ,
        "notes": notes,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1))
    return report


if __name__ == "__main__":
    r = build()
    t = r["this_week"]
    print(f"Money report built: {t['orders']} orders, ${t['revenue']:,.2f} paid, ${t['profit']:,.2f} profit before ads.")
    for n in r["notes"]:
        print("  note:", n)
