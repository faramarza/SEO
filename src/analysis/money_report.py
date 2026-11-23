"""The weekly money report: what the business made this week, what the ads cost
against what they brought in, which Awin commissions are safe to approve, and which
data is going stale. Built every Monday (scripts/money_report.py) and shown on
Governor's "Money This Week" page. Read-only: it changes nothing anywhere.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta

from src.analysis.order_profit import order_profit, summarize

RETURN_DAYS = 30      # store return policy
DELIVERY_DAYS = 5     # typical delivery time before the return clock starts


def week_split(results: list, today: date) -> tuple[list, list]:
    """(this week, the week before) by order date: [today-7, today] and [today-14, today-7). Pure."""
    a, b = (today - timedelta(days=7)).isoformat(), (today - timedelta(days=14)).isoformat()
    this = [r for r in results if r.get("date", "") >= a]
    prev = [r for r in results if b <= r.get("date", "") < a]
    return this, prev


def totals(results: list) -> dict:
    live = [r for r in results if r.get("basis") != "canceled"]
    rev = sum(r["revenue"] for r in live)
    pro = sum(r["contribution"] for r in live)
    return {"orders": len(live), "revenue": round(rev, 2), "profit": round(pro, 2),
            "margin_pct": round(pro / rev * 100, 1) if rev else None}


def ads_line(results: list, sources: dict, spend) -> dict:
    """Profit from orders GA4 credits to Google Ads, against ad spend. Pure."""
    ads = [r for r in results if sources.get(str(r.get("number"))) == "Google Ads (google / cpc)"]
    rev = sum(r["revenue"] for r in ads)
    pro = sum(r["contribution"] for r in ads)
    out = {"orders": len(ads), "revenue": round(rev, 2), "profit_before_ads": round(pro, 2),
           "spend": None if spend is None else round(spend, 2)}
    if spend:
        out["net"] = round(pro - spend, 2)
        out["roas"] = round(rev / spend, 2)
    return out


def load_awin_rows(path) -> list:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return list(csv.DictReader(f))


def awin_due(rows: list, order_status: dict, today: date) -> list:
    """Pending Awin transactions with what to do now. order_status: {order number:
    magento status}. Approve once the return window has passed; decline refunds and
    cancellations; otherwise wait (with the date). Pure."""
    out = []
    for r in rows:
        if (r.get("commission_status") or "").strip() != "0":
            continue
        n = (r.get("extra") or "").strip()
        try:
            d = datetime.strptime((r.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        safe = d + timedelta(days=DELIVERY_DAYS + RETURN_DAYS)
        st = (order_status.get(n) or "").lower()
        if st in ("closed", "canceled"):
            action, why = "DECLINE", "order refunded or canceled (reason: Customer cancelled order / returned order)"
        elif not st:
            action, why = "CHECK", "order number not found in Magento"
        elif today >= safe:
            action, why = "APPROVE", f"return window passed ({safe.isoformat()})"
        else:
            action, why = "WAIT", f"approve after {safe.isoformat()}"
        out.append({"order": n, "transaction_id": r.get("id"), "publisher": r.get("site_name"),
                    "sale": r.get("sale_amount"), "commission": r.get("commission"),
                    "date": d.isoformat(), "action": action, "why": why})
    order = {"DECLINE": 0, "APPROVE": 1, "CHECK": 2, "WAIT": 3}
    return sorted(out, key=lambda x: (order[x["action"]], x["date"]))


def stale(last_dates: dict, today: date, max_days: int = 7) -> list:
    """['Carpets for Kids stock: last updated 12 days ago', ...] for anything older
    than max_days or never done. last_dates: {label: 'YYYY-MM-DD' or None}. Pure."""
    out = []
    for label, d in last_dates.items():
        if not d:
            out.append(f"{label}: never updated through Governor")
            continue
        age = (today - datetime.strptime(d[:10], "%Y-%m-%d").date()).days
        if age > max_days:
            out.append(f"{label}: last updated {age} days ago")
    return out
