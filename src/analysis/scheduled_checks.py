"""Checks Governor runs by itself every day (scripts/scheduled_checks.py). Each one
alerts only when there is something to do, at most once per issue:

  1. ADS      last week's real ROAS (money report) under the pause rule -> alert
  2. AWIN     pending commissions now APPROVE or DECLINE -> alert with the list
  3. STALE    a vendor stock file, the Awin export or Start Here over a week old
  4. SEO      every change in docs/seo_change_log.md, once its check date has
              passed, graded WORKED / DID NOT WORK / UNCLEAR from Search Console
              (impression-weighted position and clicks per day, 28 days before the
              change vs the last 28 days). Judged by position first: December
              lifts clicks and sales for every store.
Pure functions here; the runner does the I/O.
"""
from __future__ import annotations

import re
from datetime import date, datetime

ADS_PAUSE_ROAS = 3.0      # the rule agreed for Nov 30: under 3 -> pause Name Trains PMAX-Real


def parse_change_log(text: str) -> list:
    """Rows of the change log table -> [{id, date, check, pages, queries, change}]. Pure."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if len(cells) < 7 or not re.fullmatch(r"\d+[a-z]?", cells[0]):
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[1]) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[6]):
            continue
        pages = re.findall(r"/[a-z0-9][a-z0-9/_-]*\.html", cells[2])
        queries = []
        for part in re.split(r";", cells[4]):
            part = re.sub(r"\([^)]*\)", "", part).strip().lower()
            for q in part.split("/"):
                q = q.strip(" .")
                if q and not q.startswith("supports") and len(q) > 3:
                    queries.append(q)
        out.append({"id": cells[0], "date": cells[1], "check": cells[6], "pages": pages,
                    "queries": queries, "change": cells[3][:120]})
    return out


def _agg(rows, pages, queries, days):
    sel = [r for r in rows if any(r["page"].endswith(p) for p in pages)
           and (not queries or any(q == r["query"] or q in r["query"] for q in queries))]
    imp = sum(r["impressions"] for r in sel)
    pos = (sum(r["position"] * r["impressions"] for r in sel) / imp) if imp else None
    return {"position": round(pos, 1) if pos else None, "clicks_per_day": round(sum(r["clicks"] for r in sel) / days, 2),
            "impressions": imp}


def grade(entry: dict, before_rows: list, after_rows: list, days: int = 28) -> dict:
    """WORKED / DID NOT WORK / UNCLEAR / NO DATA for one change. Rows: GSC page x
    query rows {page, query, clicks, impressions, position}. Pure."""
    if not entry["pages"]:
        return {"verdict": "NO DATA", "why": "no page in the log entry"}
    b = _agg(before_rows, entry["pages"], entry["queries"], days)
    a = _agg(after_rows, entry["pages"], entry["queries"], days)
    if not a["impressions"] and not b["impressions"]:
        return {"verdict": "NO DATA", "why": "Google showed these pages for none of the target searches", "before": b, "after": a}
    if b["position"] is None:
        verdict = "WORKED" if a["position"] else "UNCLEAR"
        why = f"newly showing at #{a['position']}" if a["position"] else "no position before or after"
    elif a["position"] is None:
        verdict, why = "DID NOT WORK", f"was #{b['position']}, no longer showing"
    else:
        gain = b["position"] - a["position"]
        clicks_up = a["clicks_per_day"] >= b["clicks_per_day"] * 1.3 and a["clicks_per_day"] >= 0.2
        if gain >= 2 or (gain >= 0 and clicks_up):
            verdict = "WORKED"
        elif gain <= -2:
            verdict = "DID NOT WORK"
        else:
            verdict = "UNCLEAR"
        why = f"#{b['position']} -> #{a['position']}, clicks/day {b['clicks_per_day']} -> {a['clicks_per_day']}"
    return {"verdict": verdict, "why": why, "before": b, "after": a}


def due_entries(entries: list, graded: dict, today: date) -> list:
    """Change log entries whose check date has passed and that are not graded yet. Pure."""
    return [e for e in entries if e["id"] not in graded
            and datetime.strptime(e["check"], "%Y-%m-%d").date() <= today]


def ads_alert(report: dict):
    """Alert text when last week's ads ran under the pause rule, else None. Pure."""
    a = (report or {}).get("ads") or {}
    if not a.get("spend"):
        return None
    if a.get("roas") is not None and a["roas"] < ADS_PAUSE_ROAS:
        return (f"Google Ads last week: spent ${a['spend']:,.2f}, ad orders made ${a['profit_before_ads']:,.2f} "
                f"before ads, net ${a['net']:,.2f} (real ROAS {a['roas']}). Rule: under {ADS_PAUSE_ROAS} -> pause "
                f"Name Trains PMAX-Real. See Money This Week.")
    return None


def awin_alert(report: dict):
    items = [x for x in (report or {}).get("awin") or [] if x["action"] in ("APPROVE", "DECLINE")]
    if not items:
        return None, None
    key = ",".join(sorted(f"{x['action']}:{x['order']}" for x in items))
    txt = "Awin: " + "; ".join(f"{x['action'].lower()} order {x['order']} ({x['publisher']}, ${x['commission']})"
                               for x in items) + ". Commission > Validate Pending Transactions."
    return txt, key
