#!/usr/bin/env python3
"""Magento sales reality check, did total orders actually hold up?

The Carlos verdict found GA4 ORGANIC revenue zeroing out across categories while
organic sessions rose. That pattern usually means GA4 purchase tracking (or its
channel attribution) broke in the window, not that sales collapsed. Magento is
the source of truth for orders, so this settles it:

  - If total orders held roughly steady (seasonality aside) through the campaign
    window, the business did NOT collapse -> the GA4 organic drop is a MEASUREMENT
    artifact, and the honest read is "I can't measure organic ROI right now."
  - If total orders really fell hard, sales genuinely declined (still not something
    more backlinks fix).

Prints a month-by-month order count + revenue trend, plus the same baseline vs
campaign 180-day windows the Carlos verdict uses.

Run on the SERVER where Magento creds exist (.env or the service environment):

    venv/bin/python scripts/magento_sales_check.py
    venv/bin/python scripts/magento_sales_check.py --months 15

READ-ONLY: issues only GET /orders. Writes nothing to Magento.

Note: Magento orders are ALL channels (it has no traffic-source attribution), so
this proves whether TOTAL sales held up. It cannot isolate organic by itself; it
is the denominator that tells you the GA4 organic figure is an artifact, not a
real business event.
"""
import os
import sys


def _reexec_in_venv():
    try:
        import httpx  # noqa: F401
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

import argparse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv():
    """Minimal .env loader (no dependency): KEY=VALUE lines, does not overwrite
    vars already set in the process environment."""
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
from src.data_sources.magento_client import MagentoClient, MagentoError  # noqa: E402


def _first_of_month(y: int, m: int) -> date:
    return date(y, m, 1)


def _add_month(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _iso(d: date) -> str:
    return d.isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=14,
                    help="How many months of trend to show (default 14)")
    ap.add_argument("--include-canceled", action="store_true",
                    help="Count canceled orders too (default excludes them)")
    args = ap.parse_args()
    exclude_canceled = not args.include_canceled

    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured: set MAGENTO_BASE_URL and MAGENTO_TOKEN "
                 "in .env or the service environment. (httpx must also be installed.)")

    today = date.today()

    print(f"\n{'='*66}")
    print("MAGENTO SALES REALITY CHECK (all channels, source of truth)")
    print(f"{'='*66}")
    print(f"As of {today}. Canceled orders "
          f"{'INCLUDED' if args.include_canceled else 'excluded'}.\n")

    # --- monthly trend ---
    # Start N months back, on the 1st.
    y, m = today.year, today.month
    back = []
    for _ in range(args.months):
        back.append((y, m))
        y = y - 1 if m == 1 else y
        m = 12 if m == 1 else m - 1
    back.reverse()

    print("MONTH-BY-MONTH ORDERS")
    print(f"  {'month':<10}{'orders':>10}{'revenue':>16}")
    monthly = {}
    try:
        for (yy, mm) in back:
            start = _first_of_month(yy, mm)
            ny, nm = _add_month(yy, mm)
            end = _first_of_month(ny, nm)       # exclusive
            if start > today:
                continue
            r = mc.order_revenue(_iso(start), _iso(end), exclude_canceled=exclude_canceled)
            monthly[(yy, mm)] = r
            cap = " (capped)" if r["capped"] else ""
            print(f"  {start.strftime('%Y-%m'):<10}{r['count']:>10,}"
                  f"{r['base_revenue']:>15,.0f}{cap}")
    except MagentoError as e:
        sys.exit(f"\nMagento read failed: {e}\n(If this is a 401/403, the integration "
                 "token may be Catalog-only and lack Sales/Orders read access.)")

    # --- baseline vs campaign 180d windows (match carlos_verdict) ---
    from datetime import timedelta
    camp_end = today
    camp_start = today - timedelta(days=180)
    base_end = camp_start
    base_start = camp_start - timedelta(days=180)
    # baseline [base_start, base_end), campaign [camp_start, camp_end)
    rb = mc.order_revenue(_iso(base_start), _iso(base_end), exclude_canceled=exclude_canceled)
    rc = mc.order_revenue(_iso(camp_start), _iso(camp_end), exclude_canceled=exclude_canceled)

    def _pct(a, b):
        return "n/a" if not b else f"{(a - b) / b * 100:+.0f}%"

    print("\n180-DAY WINDOWS (same split as the Carlos verdict)")
    print(f"  BASELINE {base_start} -> {base_end}:   "
          f"{rb['count']:,} orders, ${rb['base_revenue']:,.0f}")
    print(f"  CAMPAIGN {camp_start} -> {camp_end}:   "
          f"{rc['count']:,} orders, ${rc['base_revenue']:,.0f}")
    print(f"  Change:  orders {_pct(rc['count'], rb['count'])}, "
          f"revenue {_pct(rc['base_revenue'], rb['base_revenue'])}")

    # --- verdict ---
    print("\nWHAT THIS TELLS YOU")
    held = rb["count"] and rc["count"] >= rb["count"] * 0.8
    if rc["count"] == 0 and rb["count"] == 0:
        print("  No orders found in either window. Likely the token lacks Sales/Orders")
        print("  read scope, or the store is on a different Magento instance.")
    elif held:
        print("  Total orders held up (within ~20% of baseline). The business did NOT")
        print("  collapse, so the GA4 organic-revenue drop is a MEASUREMENT artifact,")
        print("  not a real sales event. Conclusion for Carlos stands: you can't yet")
        print("  measure organic ROI, so don't fund more links until tracking is fixed.")
    else:
        print("  Total orders fell materially vs baseline. Some of the decline is real,")
        print("  not just a tracking glitch. Dig into WHICH months/products drove it")
        print("  (seasonality, the step-stool warning, vendor lead times) before")
        print("  attributing anything to organic or to link building either way.")
    print("  Reminder: these are ALL-channel orders. Magento has no traffic source, so")
    print("  this is the denominator, not an organic-only figure.\n")


if __name__ == "__main__":
    main()
