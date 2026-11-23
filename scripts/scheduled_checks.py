#!/usr/bin/env python3
"""Daily checks: alert in Governor's notifications (the bell) only when something
needs doing. Cron, once a day:

    /var/www/seo/venv/bin/python scripts/scheduled_checks.py

Reads data/money_report.json (built Mondays) and Search Console; grades SEO changes
from docs/seo_change_log.md once their check date passes (results kept in
data/seo_grades.json and shown on the Money This Week page). Read-only toward
Magento, Ads and Awin.
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.sales_by_line import _load_dotenv  # noqa: E402

_load_dotenv()

from src.analysis import scheduled_checks as sc  # noqa: E402

DATA = ROOT / "data"
NOTES = DATA / "notifications.json"
SENT = DATA / "check_alerts_sent.json"
GRADES = DATA / "seo_grades.json"


def _load(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def notify(kind, key, message, severity="warning"):
    """Add one bell notification, once per (kind, key)."""
    sent = _load(SENT, {})
    if sent.get(kind) == key:
        return False
    notes = _load(NOTES, [])
    notes.insert(0, {"type": kind, "severity": severity, "message": message,
                     "timestamp": datetime.now(timezone.utc).isoformat(), "read": False})
    NOTES.write_text(json.dumps(notes[:100], indent=2) + "\n")
    sent[kind] = key
    SENT.write_text(json.dumps(sent, indent=1))
    return True


def grade_seo(today, gsc_rows):
    entries = sc.parse_change_log((ROOT / "docs" / "seo_change_log.md").read_text())
    entries += _load(DATA / "governor_changes.json", [])    # Page Content approvals
    graded = _load(GRADES, {})
    new = []
    for e in sc.due_entries(entries, graded, today):
        changed = datetime.strptime(e["date"], "%Y-%m-%d").date()
        before = gsc_rows(changed - timedelta(days=28), changed - timedelta(days=1))
        after = gsc_rows(today - timedelta(days=31), today - timedelta(days=4))   # GSC lags ~3 days
        g = dict(sc.grade(e, before, after), id=e["id"], pages=e["pages"], change=e["change"],
                 graded=today.isoformat())
        graded[e["id"]] = g
        new.append(g)
    GRADES.write_text(json.dumps(graded, indent=1))
    return new


def main(today=None, gsc_rows=None):
    today = today or date.today()
    report = _load(DATA / "money_report.json", {})
    week = (report.get("week") or ["", ""])[1]
    msg = sc.ads_alert(report)
    if msg:
        notify("ads_check", week, msg)
    txt, key = sc.awin_alert(report)
    if txt:
        notify("awin_check", key, txt, severity="info")
    if report.get("stale"):
        notify("stale_check", week + "|" + "|".join(report["stale"]),
               "Out of date: " + "; ".join(report["stale"]) + ".", severity="info")
    if gsc_rows is None:
        cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
        from src.data_sources.gsc_client import GSCClient
        g = cfg["data_sources"]["gsc"]
        client = GSCClient(site_url=g["property_url"], credentials_path=str(ROOT / g["credentials_path"]))
        gsc_rows = client.page_query_range
    new = grade_seo(today, gsc_rows)
    if new:
        counts = {v: sum(1 for x in new if x["verdict"] == v) for v in ("WORKED", "DID NOT WORK", "UNCLEAR", "NO DATA")}
        lines = "; ".join(f"#{x['id']} {x['verdict'].lower()} ({x['why']})" for x in new)
        notify("seo_grades", ",".join(x["id"] for x in new),
               f"SEO results are in: {counts['WORKED']} worked, {counts['DID NOT WORK']} did not, "
               f"{counts['UNCLEAR']} unclear. {lines}", severity="info")
    print(f"checks done {today}: ads={'alert' if msg else 'ok'}, awin={'alert' if txt else 'ok'}, "
          f"seo graded {len(new)}")


if __name__ == "__main__":
    main()
