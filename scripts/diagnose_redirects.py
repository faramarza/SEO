#!/usr/bin/env python3
"""Does your entry redirect strip the gclid / marketing params?

Run on the server (plain python3 is fine — only uses the standard library):
    python3 scripts/diagnose_redirects.py

WHY: your GA4 data stream is set to http://www.alphabet-trains.com, but the site
runs on https://alphabet-trains.com — so every click (ad OR organic) hits a redirect
chain (http->https and/or www->non-www). If that chain DROPS the query string, the
gclid / utm / referrer are gone before GA4's tag loads, and the session becomes
'(not set)' for EVERY channel. That matches the 14 clean-URL '(not set)' orders and
'google/organic' showing 0 orders.

This appends a test gclid to each entry variant, follows every redirect hop, and
reports whether the gclid survives to the final page — and if not, exactly which
hop dropped it. Read-only: makes a handful of GET requests, changes nothing.
"""

import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse, parse_qs

TEST = "gcltest12345"                       # stand-in for a real gclid
PATHS = ["/", "/name-trains.html", "/montessori-toys.html"]
VARIANTS = [
    "http://alphabet-trains.com",
    "http://www.alphabet-trains.com",
    "https://www.alphabet-trains.com",
    "https://alphabet-trains.com",
]
UA = "Mozilla/5.0 (compatible; AlphabetTrains-redirect-check/1.0; +attribution audit)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None  # never auto-follow — we walk the chain ourselves


def walk(start_url, max_hops=8):
    """Return (final_url, [(url, status, location), ...]) following redirects manually."""
    opener = urllib.request.build_opener(_NoRedirect)
    hops = []
    cur = start_url
    for _ in range(max_hops):
        req = urllib.request.Request(cur, method="GET", headers={"User-Agent": UA})
        try:
            resp = opener.open(req, timeout=20)
            hops.append((cur, resp.status, None))
            return cur, hops
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            hops.append((cur, e.code, loc))
            if e.code in (301, 302, 303, 307, 308) and loc:
                cur = urljoin(cur, loc)
            else:
                return cur, hops
        except Exception as ex:
            hops.append((cur, f"err:{type(ex).__name__}: {ex}", None))
            return cur, hops
    return cur, hops


def has_gclid(url):
    q = parse_qs(urlparse(url).query)
    return any(TEST in v for vals in q.values() for v in vals) or TEST in url


def main():
    print("Testing whether the entry redirect preserves the query string (gclid).\n")
    any_drop = False
    for path in PATHS:
        print("=" * 78)
        print(f"PATH: {path}")
        print("=" * 78)
        for base in VARIANTS:
            start = f"{base}{path}?gclid={TEST}&utm_source=redirecttest&utm_medium=cpc"
            final, hops = walk(start)
            kept = has_gclid(final)
            # find the hop where gclid disappeared
            drop_at = None
            for u, status, loc in hops:
                if loc and not has_gclid(urljoin(u, loc)) and has_gclid(u):
                    drop_at = (u, loc)
                    break
            flag = "OK  ✓ gclid kept" if kept else "LOST ✗ gclid DROPPED"
            if not kept:
                any_drop = True
            print(f"\n  {base}{path}")
            for u, status, loc in hops:
                if loc:
                    print(f"    {status} → {loc[:70]}")
                else:
                    print(f"    {status}  (final)")
            print(f"    → {flag}")
            if not kept and drop_at:
                print(f"      dropped at: {drop_at[0][:50]} → {drop_at[1][:50]}")

    print("\n" + "=" * 78)
    if any_drop:
        print("VERDICT: at least one entry path DROPS the gclid on redirect. That is the")
        print("site-wide attribution leak — the marketing params (and often the referrer)")
        print("are gone before GA4 loads, so sessions land in '(not set)'/'(direct)'.")
        print("FIX: make the redirect PRESERVE the query string (301 to the SAME path +")
        print("full ?query), and collapse it to a SINGLE hop (http/www → https/non-www in")
        print("one redirect, not a chain). In Magento/nginx this is the rewrite rule that")
        print("appends $query_string / $args to the redirect target.")
    else:
        print("VERDICT: the redirect preserves the gclid on every path. So the redirect is")
        print("NOT the leak — the site-wide '(not set)' is coming from elsewhere (tag timing,")
        print("consent, or the GA4 config). Tell me and we'll look next there.")
    print("\nRead-only — nothing modified.")


if __name__ == "__main__":
    main()
