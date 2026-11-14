#!/usr/bin/env python3
"""Shopper's-eye check: look at key product pages the way a buyer does and flag
what analytics never shows.

    /var/www/seo/venv/bin/python scripts/shopper_eye_check.py
    ... --url https://alphabet-trains.com/some-product.html   (extra pages)
    ... --no-browser        (skip the popup check)

Checks, per page:
  1. ADD-ONS TICKED BY DEFAULT: checkboxes in the page that start checked (gift
     sets, upsells), and which of those are made-to-order (long waits).
  2. RETURN WINDOW: the "N days" on the product page vs. your Return Policy page
     for that product type.
  3. STOCK vs MADE-TO-ORDER: "In Stock" badges on pages that also say the item
     is custom made / takes weeks.
  4. POPUPS (needs Playwright + Chromium): anything covering more than a third
     of the screen within ~8 seconds, on desktop and phone.
Read-only.
"""
import argparse
import html as htmllib
import re
import sys

import requests

SITE = "https://alphabet-trains.com"
PAGES = ["/3-letter-name-train.html", "/4-letter-name-train.html", "/5-letter-name-train.html",
         "/6-letter-name-train.html", "/7-letter-name-train.html", "/10-letter-name-train.html",
         "/alphabet-train-set.html", "/name-trains.html", "/ch-personalized-step-stool.html",
         "/regular-personalized-name-stool.html", "/wooden-name-puzzles.html",
         "/classroom-rugs.html", "/rainbow-seating-rug-8x13.html"]
POLICY = "/return-policy"
MADE_TO_ORDER = re.compile(r"name puzzle stool|coat rack|interlocking|name puzzle|step stool|toy chest",
                           re.I)
POLICY_CATEGORY = [  # (product-page pattern, policy line label)
    (re.compile(r"name[- ]?train", re.I), r"name trains?"),
    (re.compile(r"rug|carpet", re.I), r"carpets? and rugs"),
    (re.compile(r"chair|table|cabinet|furniture", re.I), r"furniture"),
]
IGNORE_CHECKBOX = re.compile(r"remember|consent|disabled|newsletter|privacy", re.I)


def visible_text(page):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)))


def prechecked_addons(page):
    """Checked checkboxes that look like add-ons -> [label]."""
    out = []
    for m in re.finditer(r"<input\b[^>]*type=\"checkbox\"[^>]*>", page, re.I):
        tag = m.group(0)
        if not re.search(r"\bchecked\b", tag) or IGNORE_CHECKBOX.search(tag):
            continue
        after = page[m.end():m.end() + 1500]
        alt = re.search(r'alt="([^"]+)"', after)
        label = htmllib.unescape(alt.group(1)) if alt else re.sub(r"\s+", " ",
                                                                  visible_text(after))[:60]
        out.append(label.strip())
    return out


def bundle_made_to_order(page):
    """Made-to-order products offered in a gift set/upsell block, ticked or not."""
    out = []
    for m in re.finditer(r'<input\b[^>]*class="panth-addon-check"[^>]*>', page):
        alt = re.search(r'alt="([^"]+)"', page[m.end():m.end() + 1500])
        name = htmllib.unescape(alt.group(1)).strip() if alt else ""
        if name and MADE_TO_ORDER.search(name):
            out.append(name)
    return out


def return_days(text):
    return sorted({int(n) for n in re.findall(r"return[^.]{0,60}?(\d{1,3})\s*days", text, re.I)})


def policy_days(policy_text, url):
    for pat, label in POLICY_CATEGORY:
        if pat.search(url):
            m = re.search(label + r"[^.]{0,80}?(\d{1,3})\s*days", policy_text, re.I)
            if m:
                return int(m.group(1))
    m = re.search(r"all other items[^.]{0,80}?(\d{1,3})\s*days", policy_text, re.I)
    return int(m.group(1)) if m else None


def stock_conflict(text):
    instock = re.search(r"(\d[\d,]*\s+total units currently in stock|\bin stock\b)", text, re.I)
    custom = re.search(r"(custom[- ]made|made to order|\d+\s*weeks? or longer|production time)",
                       text, re.I)
    return (instock.group(0), custom.group(0)) if instock and custom else None


def page_findings(url, page, policy_text):
    text = visible_text(page)
    f = []
    ticked = prechecked_addons(page)
    if ticked:
        slow = [t for t in ticked if MADE_TO_ORDER.search(t)]
        f.append(f"ADD-ONS TICKED BY DEFAULT: {len(ticked)} ({'; '.join(ticked[:8])})"
                 + (f"  <- made-to-order among them: {'; '.join(slow)}" if slow else ""))
    slow_offered = bundle_made_to_order(page)
    if slow_offered:
        f.append("MADE-TO-ORDER IN GIFT SET (long waits, Christmas risk): " + "; ".join(slow_offered))
    days, pol = return_days(text), policy_days(policy_text, url)
    if days and pol and pol not in days:
        f.append(f"RETURN WINDOW MISMATCH: page says {days} days, Return Policy says {pol} days")
    c = stock_conflict(text)
    if c:
        f.append(f"STOCK vs MADE-TO-ORDER: shows '{c[0]}' but also '{c[1]}'")
    return f


def overlays(urls):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {u: ["(popup check skipped: Playwright not installed)"] for u in urls}
    js = """() => {const W=innerWidth,H=innerHeight,o=[];for(const e of document.querySelectorAll('body *')){
      const c=getComputedStyle(e);if(!['fixed','absolute'].includes(c.position)||c.display==='none'||
      c.visibility==='hidden'||+c.opacity===0)continue;const r=e.getBoundingClientRect();
      if(r.width*r.height>0.35*W*H&&+c.zIndex>10)o.push((e.innerText||'').replace(/\\s+/g,' ').slice(0,80))}
      return o.slice(0,3)}"""
    res = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        for u in urls:
            res[u] = []
            for name, opts in (("desktop", {"viewport": {"width": 1366, "height": 850}}),
                               ("phone", p.devices["iPhone 13"])):
                ctx = b.new_context(**opts)
                pg = ctx.new_page()
                try:
                    pg.goto(u, wait_until="domcontentloaded", timeout=60000)
                    pg.wait_for_timeout(8000)
                    for t in pg.evaluate(js):
                        res[u].append(f"POPUP ({name}, within 8s) covers >35% of screen: '{t}'")
                except Exception as e:
                    res[u].append(f"(popup check failed on {name}: {e})")
                ctx.close()
        b.close()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    urls = [SITE + p for p in PAGES] + args.url
    get = lambda u: requests.get(u, timeout=30, headers={"User-Agent": "Mozilla/5.0 (shopper check)"}).text
    policy_text = visible_text(get(SITE + POLICY))
    pops = {} if args.no_browser else overlays(urls)
    total = 0
    for u in urls:
        try:
            f = page_findings(u, get(u), policy_text) + pops.get(u, [])
        except Exception as e:
            f = [f"(could not load: {e})"]
        total += sum(1 for x in f if not x.startswith("("))
        print(f"\n{u.replace(SITE, '')}")
        for x in f or ["OK"]:
            print(f"   - {x}")
    print(f"\n{total} issue(s) found.")


if __name__ == "__main__":
    sys.exit(main())
