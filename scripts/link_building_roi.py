#!/usr/bin/env python3
"""Link-building ROI check.

Compares organic Google Search Console performance for the link-building period
against the equal-length period immediately before it — at the SITE level (the
number that actually matters) and broken down by query, so you can see whether the
$/month on backlinks is lifting organic search or just producing nice reports.

Run on the SERVER where GSC credentials exist:

    python3 scripts/link_building_roi.py                 # last 90d vs prior 90d
    python3 scripts/link_building_roi.py --split 2026-07-01
                                                         # campaign = 2026-07-01..now,
                                                         # baseline = equal span before it

Reads config/defaults.json -> data_sources.gsc (property_url, credentials_path).
Read-only; makes no changes.
"""
import os
import sys


def _reexec_in_venv():
    """The systemd 'seo' service runs in a virtualenv; plain system python3 usually
    lacks pydantic/google libs. If our interpreter can't import pydantic, re-exec this
    script with the app's venv python so the user can just run `python3 scripts/...`."""
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
            os.execv(py, [py] + sys.argv)   # hand off; does not return


_reexec_in_venv()

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.data_sources.gsc_client import GSCClient   # noqa: E402

# Commercial intent terms to call out specifically (substring, case-insensitive).
COMMERCIAL = ["montessori", "name train", "name puzzle", "alphabet", "stacking",
              "building block", "shape sorter", "personalized", "personalised",
              "wooden toy", "puzzle", "lock box", "learning toy", "toddler toy"]


def _cfg():
    cfg = json.loads((ROOT / "config" / "defaults.json").read_text())
    return cfg.get("data_sources", {}).get("gsc", {})


def _totals(client, svc, start, end):
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": []}
    rows = svc.searchanalytics().query(siteUrl=client.site_url, body=body).execute().get("rows", [])
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    r = rows[0]
    return {"clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
            "ctr": r.get("ctr", 0.0), "position": r.get("position", 0.0)}


def _by_query(client, svc, start, end):
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": ["query"]}
    out = {}
    for r in client._query_all_rows(svc, body):
        out[r["keys"][0]] = {"clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0),
                             "position": r.get("position", 0.0)}
    return out


def _pct(a, b):
    return "n/a" if not b else f"{(a - b) / b * 100:+.0f}%"


def _row(label, base, camp, key, fmt="{:,.0f}"):
    b, c = base.get(key, 0), camp.get(key, 0)
    print(f"  {label:<16} {fmt.format(b):>14}   {fmt.format(c):>14}   {_pct(c, b):>8}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", help="Campaign start date YYYY-MM-DD (baseline = equal span before)")
    ap.add_argument("--days", type=int, default=90, help="Window length if --split omitted (default 90)")
    args = ap.parse_args()

    gsc = _cfg()
    client = GSCClient(site_url=gsc.get("property_url", ""),
                       credentials_path=gsc.get("credentials_path") or None)
    svc = client._get_service()
    if svc is None:
        sys.exit("GSC not configured: no credentials. Check config/defaults.json "
                 "-> data_sources.gsc.credentials_path / property_url.")

    end = date.today() - timedelta(days=3)          # GSC data lags ~3 days
    if args.split:
        split = date.fromisoformat(args.split)
    else:
        split = end - timedelta(days=args.days)
    span = end - split
    base_start, base_end = split - span, split
    camp_start, camp_end = split, end

    print(f"\nProperty: {client.site_url}")
    print(f"BASELINE (before links):  {base_start} -> {base_end}  ({span.days} days)")
    print(f"CAMPAIGN (with links):    {camp_start} -> {camp_end}  ({span.days} days)\n")

    bt, ct = _totals(client, svc, base_start, base_end), _totals(client, svc, camp_start, camp_end)
    print("SITE-WIDE ORGANIC                 baseline        campaign      change")
    _row("Clicks", bt, ct, "clicks")
    _row("Impressions", bt, ct, "impressions")
    print(f"  {'CTR':<16} {bt['ctr']*100:>13.2f}%   {ct['ctr']*100:>13.2f}%")
    print(f"  {'Avg position':<16} {bt['position']:>14.1f}   {ct['position']:>14.1f}   "
          f"{'(lower=better)'}")

    bq, cq = _by_query(client, svc, base_start, base_end), _by_query(client, svc, camp_start, camp_end)
    allq = set(bq) | set(cq)
    movers = []
    for q in allq:
        bc, cc = bq.get(q, {}).get("clicks", 0), cq.get(q, {}).get("clicks", 0)
        bi, ci = bq.get(q, {}).get("impressions", 0), cq.get(q, {}).get("impressions", 0)
        movers.append((q, cc - bc, ci - bi, bc, cc, bi, ci))

    print("\nTOP QUERY GAINERS (by clicks)        Δclicks   base→camp clicks   Δimpr")
    for q, dc, di, bc, cc, bi, ci in sorted(movers, key=lambda x: -x[1])[:12]:
        print(f"  {q[:38]:<38} {dc:>+7.0f}   {bc:>6.0f}→{cc:<6.0f}      {di:>+8.0f}")
    print("\nTOP QUERY LOSERS (by clicks)         Δclicks   base→camp clicks   Δimpr")
    for q, dc, di, bc, cc, bi, ci in sorted(movers, key=lambda x: x[1])[:8]:
        if dc >= 0:
            break
        print(f"  {q[:38]:<38} {dc:>+7.0f}   {bc:>6.0f}→{cc:<6.0f}      {di:>+8.0f}")

    print("\nCOMMERCIAL TERMS (your money queries)   base clk  camp clk   base imp  camp imp")
    hit = False
    for q, dc, di, bc, cc, bi, ci in sorted(movers, key=lambda x: -(x[6])):
        if any(t in q.lower() for t in COMMERCIAL) and (bi or ci):
            hit = True
            print(f"  {q[:36]:<36} {bc:>8.0f}  {cc:>8.0f}   {bi:>8.0f}  {ci:>8.0f}")
    if not hit:
        print("  (no impressions on the commercial terms list — tells you the links "
              "aren't lifting your buyer keywords)")

    # bottom line
    dclicks = ct["clicks"] - bt["clicks"]
    dimpr = ct["impressions"] - bt["impressions"]
    print("\nBOTTOM LINE")
    print(f"  Organic clicks:      {bt['clicks']:,.0f} -> {ct['clicks']:,.0f}  "
          f"({_pct(ct['clicks'], bt['clicks'])}, {dclicks:+,.0f})")
    print(f"  Organic impressions: {bt['impressions']:,.0f} -> {ct['impressions']:,.0f}  "
          f"({_pct(ct['impressions'], bt['impressions'])}, {dimpr:+,.0f})")
    verdict = ("LIFTING — links period is up on organic." if dclicks > 0 and dimpr > 0
               else "FLAT/DOWN — the spend is not showing up in organic search."
               if dclicks <= 0 and dimpr <= 0 else "MIXED — dig into the movers above.")
    print(f"  Read: {verdict}\n")


if __name__ == "__main__":
    main()
