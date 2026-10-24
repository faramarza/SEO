#!/usr/bin/env python3
"""Book vs Toy Search Console split — a ONE-SCREEN answer, not a data dump.

Buckets your organic queries into TOYS (montessori/name trains/puzzles/etc.) vs
BOOKS (I See Me personalized books) and reports, for each bucket:
  - clicks / impressions / avg position, recent 90 days vs the prior 90 days
  - whether BOOKS are getting clicks or just impressions (the "brand eats my
    clicks" test)
  - a short verdict line
Caps every list at 6 rows. Run on the server:
    venv/bin/python scripts/book_vs_toy_gsc.py
Read-only. GSC keeps ~16 months, so the 2-years-ago drop is before the window —
this shows the CURRENT state + the link-building-period trend.
"""
import os
import sys


def _reexec_in_venv():
    try:
        import pydantic  # noqa: F401
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
            os.execv(py, [py] + sys.argv)


_reexec_in_venv()

import json
import re
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.data_sources.gsc_client import GSCClient   # noqa: E402

BOOK_RE = re.compile(
    r"\b(book|storybook|story ?book|i ?see ?me|iseeme|goodnight little|christmas dream|"
    r"big boy|big sister|big brother|blast off|fairy ?tale|bedtime|lost my name|"
    r"personalized (?:book|story|storybook))\b", re.I)
TOY_RE = re.compile(
    r"\b(montessori|name train|name puzzle|wooden|stacking|building block|shape ?sorter|"
    r"busy ?board|step ?stool|stool|rug|alphabet train|blocks|\btoy|\btoys|puzzle|sorter|"
    r"\btrain|mirror|dollhouse|waldorf)\b", re.I)


def _bucket(q):
    if BOOK_RE.search(q):
        return "BOOK"
    if TOY_RE.search(q):
        return "TOY"
    return "OTHER"


def _by_query(client, svc, start, end):
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query"]}
    out = {}
    for r in client._query_all_rows(svc, body):
        out[r["keys"][0]] = (r.get("clicks", 0), r.get("impressions", 0), r.get("position", 0.0))
    return out


def _agg(qdict):
    """{bucket: [clicks, impressions, pos*impr, nqueries]}"""
    b = {k: [0, 0, 0.0, 0] for k in ("TOY", "BOOK", "OTHER")}
    for q, (c, i, p) in qdict.items():
        k = _bucket(q)
        b[k][0] += c
        b[k][1] += i
        b[k][2] += p * i
        b[k][3] += 1
    return b


def _pct(a, b):
    return "n/a" if not b else f"{(a - b) / b * 100:+.0f}%"


def main():
    gsc = json.loads((ROOT / "config" / "defaults.json").read_text()).get(
        "data_sources", {}).get("gsc", {})
    client = GSCClient(site_url=gsc.get("property_url", ""),
                       credentials_path=gsc.get("credentials_path") or None)
    svc = client._get_service()
    if svc is None:
        sys.exit("GSC not configured (check config/defaults.json -> data_sources.gsc).")

    end = date.today() - timedelta(days=3)
    split = end - timedelta(days=90)
    a = _agg(_by_query(client, svc, split, end))            # recent 90d
    b = _agg(_by_query(client, svc, split - timedelta(days=90), split))  # prior 90d

    print(f"\n{client.site_url}")
    print(f"PRIOR 90d: {split - timedelta(days=90)} → {split}")
    print(f"RECENT 90d: {split} → {end}\n")
    print(f"{'BUCKET':<7} {'clicks (prior→recent)':<26} {'impressions (prior→recent)':<30} {'avg pos'}")
    for k in ("TOY", "BOOK", "OTHER"):
        pc, pi = b[k][0], b[k][1]
        rc, ri = a[k][0], a[k][1]
        pp = (b[k][2] / pi) if pi else 0
        rp = (a[k][2] / ri) if ri else 0
        print(f"{k:<7} {pc:>6.0f} → {rc:<6.0f} ({_pct(rc, pc):>5})     "
              f"{pi:>8.0f} → {ri:<8.0f} ({_pct(ri, pi):>5})     {pp:>4.1f} → {rp:<4.1f}")

    # BOOK "brand eats my clicks" test (current state)
    rc, ri = a["BOOK"][0], a["BOOK"][1]
    rp = (a["BOOK"][2] / ri) if ri else 0
    ctr = (rc / ri * 100) if ri else 0
    print(f"\nBOOKS right now: {ri:,.0f} impressions, {rc:,.0f} clicks "
          f"(CTR {ctr:.2f}%, avg pos {rp:.1f})")
    print("  → " + ("Lots of impressions, almost no clicks = Google shows you but "
                    "shoppers pick the brand. Confirms the leak."
                    if ri > 500 and ctr < 1.0 else
                    "Books are still getting clicks — the leak is at checkout/preview, "
                    "not at the search result."))

    # TOY verdict (did the on-topic links move anything?)
    tpc, trc = b["TOY"][0], a["TOY"][0]
    print(f"\nTOYS over the link-building window: {tpc:,.0f} → {trc:,.0f} clicks ({_pct(trc, tpc)})")
    print("  → " + ("Flat/down despite on-topic links — links are not the lever for toys."
                    if trc <= tpc * 1.1 else
                    "Toy clicks rose — worth checking if the links contributed."))

    # tiny supporting evidence: up to 6 rows each
    recent = _by_query  # alias noop
    qa = _by_query(client, svc, split, end)
    books = sorted(((q, v[0], v[1]) for q, v in qa.items() if _bucket(q) == "BOOK"),
                   key=lambda x: -x[2])[:6]
    print("\nTop BOOK queries by impressions (clicks vs impressions):")
    for q, c, i in books:
        print(f"   {q[:46]:<46} {c:>4.0f} clk / {i:>6.0f} impr")

    # toy movers
    qb = _by_query(client, svc, split - timedelta(days=90), split)
    movers = []
    for q in set(qa) | set(qb):
        if _bucket(q) != "TOY":
            continue
        movers.append((q, qa.get(q, (0, 0, 0))[0] - qb.get(q, (0, 0, 0))[0]))
    movers.sort(key=lambda x: x[1])
    print("\nTOY terms that LOST the most clicks:")
    for q, d in movers[:6]:
        if d < 0:
            print(f"   {q[:46]:<46} {d:>+4.0f}")
    print()


if __name__ == "__main__":
    main()
