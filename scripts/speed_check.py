#!/usr/bin/env python3
"""Real-visitor speed (Chrome UX Report, phones, 28-day p75) for your key pages.
This is the speed data Google actually uses for Core Web Vitals, unlike a single
lab test. Needs GOOGLE_API_KEY (CrUX API) in .env.

    /var/www/seo/venv/bin/python scripts/speed_check.py
    ... --url https://alphabet-trains.com/some-page.html   (extra pages)

Thresholds (Google): LCP good <= 2.5s, poor > 4s; INP good <= 200ms, poor > 500ms;
CLS good <= 0.1, poor > 0.25. "origin" = page has too little traffic for its own
data, so the whole-site figure is shown.
"""
import argparse
import os
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

PAGES = ["/", "/name-trains.html", "/5-letter-name-train.html", "/7-letter-name-train.html",
         "/personalized-step-stools.html", "/ch-personalized-step-stool.html",
         "/montessori-toys.html", "/classroom-rugs.html", "/wooden-name-puzzles.html"]


def fmt(v, unit):
    if v is None:
        return "-"
    return f"{v/1000:.2f}s" if unit == "s" else (f"{v:.0f}ms" if unit == "ms" else f"{v:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[])
    args = ap.parse_args()
    from src.data_sources.crux_client import CrUXClient
    c = CrUXClient()
    if not c.api_key:
        sys.exit("GOOGLE_API_KEY not set in .env (a Google Cloud API key with the Chrome UX "
                 "Report API enabled).")
    urls = [f"https://alphabet-trains.com{p}" for p in PAGES] + args.url
    print(f"{'page':<42}{'level':<8}{'LCP':>8}{'INP':>8}{'CLS':>7}{'FCP':>8}{'TTFB':>8}  rating")
    for u in urls:
        r = c.get_cwv(u)
        if not r:
            print(f"{u.replace('https://alphabet-trains.com', '')[:41]:<42}no data")
            continue
        print(f"{u.replace('https://alphabet-trains.com', '')[:41]:<42}{r['level']:<8}"
              f"{fmt(r['lcp_ms'], 's'):>8}{fmt(r['inp_ms'], 'ms'):>8}{fmt(r['cls'], ''):>7}"
              f"{fmt(r['fcp_ms'], 's'):>8}{fmt(r['ttfb_ms'], 'ms'):>8}  {r['overall_rating']}")
    print("\nPhones, 75th percentile of real Chrome visitors over 28 days. Good: LCP <= 2.5s, "
          "INP <= 200ms, CLS <= 0.1.")


if __name__ == "__main__":
    main()
