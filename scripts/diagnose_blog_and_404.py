"""Two on-demand diagnostics, run on the server where the data lives.

  python3 scripts/diagnose_blog_and_404.py

1. BLOG → MONEY structural check: for every blog/article page, does it actually
   link toward a product/category page that earns revenue? A traffic-heavy blog
   with zero links to money pages is structurally pure vanity — it can't assist a
   sale it never points at. (First-touch sales are already answered by GA: organic
   drove ~2 transactions/30d; this shows WHICH blogs are even positioned to help.)

2. BROKEN INTERNAL LINKS: the crawler only keeps pages that returned 200, so any
   internal link whose target isn't in that set is a suspected dead link — the
   thing sending visitors to "Page Not Found". Ranked by how many pages link to
   each dead target, so the worst offenders (like your 119-view 404) surface first.

Reads data/latest_evaluation.json. Read-only. Nothing is modified.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

DATA = Path(__file__).resolve().parent.parent / "data" / "latest_evaluation.json"


def _live_status(url: str):
    """HEAD the URL and return its immediate status ('404' / '301' / '200' / err),
    WITHOUT following redirects, so we can tell a true 404 from a stale-but-
    redirecting link from a false positive. Gentle: one request, browser UA."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={
        "User-Agent": "Mozilla/5.0 (compatible; AlphabetTrains-SEO-Crawler/1.0; +link-audit)"})
    try:
        resp = opener.open(req, timeout=15)
        return str(resp.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:
        return f"err:{type(e).__name__}"


def _norm(url: str) -> str:
    """Normalize for matching: drop scheme/query/fragment, lowercase, no trailing /."""
    try:
        p = urlparse(url.strip())
        path = (p.path or "/").rstrip("/") or "/"
        host = (p.netloc or "").lower()
        return f"{host}{path}".lower()
    except Exception:
        return (url or "").strip().lower().rstrip("/")


def main():
    path_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = Path(path_args[0]) if path_args else DATA
    if not path.exists():
        print(f"No evaluation found at {path}. Run an evaluation (with crawl) first.")
        sys.exit(1)
    data = json.loads(path.read_text())
    results = data.get("results", [])
    print(f"Loaded {len(results)} pages · evaluation {data.get('timestamp','?')}\n")

    # Domain(s) we consider internal.
    hosts = {urlparse(r.get("url", "")).netloc.lower() for r in results if r.get("url")}
    known = {_norm(r.get("url", "")) for r in results if r.get("url")}

    # ---- 1. BLOG -> MONEY structural check --------------------------------
    # Two sets: ANY product/category page (structural funnel), and the subset that
    # has recorded GA4 revenue. The revenue set is unreliable when attribution is
    # broken (almost nothing shows revenue), so we report BOTH and lead with the
    # honest structural signal: does the blog link toward commerce AT ALL?
    commerce = {_norm(r.get("url", "")) for r in results
                if (r.get("asset_type") or "").lower() in ("product", "category")}
    money = {_norm(r.get("url", "")) for r in results
             if (r.get("ga4_revenue") or 0) > 0
             and (r.get("asset_type") or "").lower() in ("product", "category")}
    print("=" * 70)
    print("1. BLOG → MONEY  (does each blog funnel toward commerce at all?)")
    print("=" * 70)
    blogs = [r for r in results
             if (r.get("asset_type") or "").lower() in ("blog", "article", "guide")]
    blogs.sort(key=lambda r: -(r.get("gsc_clicks") or 0))
    if not blogs:
        print("No blog/article pages found in the evaluation.\n")
    for r in blogs[:25]:
        outs = (r.get("page_metadata", {}) or {}).get("internal_outlinks", []) or []
        tgts = {_norm(o.get("target_url", "")) for o in outs}
        n_comm = len(tgts & commerce)
        n_money = len(tgts & money)
        clicks = r.get("gsc_clicks") or 0
        if n_comm == 0:
            verdict = "ORPHAN FROM COMMERCE (links to 0 product/category pages)"
        elif n_money == 0:
            verdict = f"links to {n_comm} product/cat page(s), 0 with recorded revenue"
        else:
            verdict = f"links to {n_comm} product/cat page(s), {n_money} earning revenue ✓"
        path = urlparse(r.get("url", "")).path
        print(f"  {clicks:>4} clk/mo  {path[:50]:50}  → {verdict}")
    print("\n  (Revenue counts are unreliable while attribution is broken — treat\n"
          "   'ORPHAN FROM COMMERCE' as the real signal: those blogs can't assist a\n"
          "   sale because they link to no product at all.)\n")

    # ---- 2. BROKEN INTERNAL LINKS -----------------------------------------
    print("=" * 70)
    print("2. BROKEN INTERNAL LINKS  (targets no successful page — likely 404)")
    print("=" * 70)
    dead = {}  # normalized target -> {"raw": url, "sources": [(src, anchor)]}
    for r in results:
        src = r.get("url", "")
        for o in (r.get("page_metadata", {}) or {}).get("internal_outlinks", []) or []:
            tgt = o.get("target_url", "")
            if not tgt:
                continue
            host = urlparse(tgt).netloc.lower()
            if host and host not in hosts:
                continue  # external link, skip
            n = _norm(tgt)
            if not n or n in known:
                continue  # resolves to a crawled 200 page — fine
            d = dead.setdefault(n, {"raw": tgt, "sources": []})
            d["sources"].append((src, (o.get("anchor_text") or "").strip()))
    verify = "--verify" in sys.argv
    if not dead:
        print("No internal links point to an uncrawled/dead target. ✓\n")
    else:
        ranked = sorted(dead.values(), key=lambda d: -len(d["sources"]))
        print(f"{len(ranked)} suspected dead internal target(s). Worst first"
              + (" (live-checked):\n" if verify else
                 " — re-run with --verify to confirm real 404s:\n"))
        for d in ranked[:30]:
            status = ""
            if verify:
                st = _live_status(d["raw"])
                time.sleep(0.3)  # gentle on the store
                label = {"404": "404 DEAD", "301": "301 redirect (stale link)",
                         "302": "302 redirect (stale link)", "200": "200 OK (false positive)"}.get(st, st)
                status = f"  [{label}]"
            print(f"  ✗ {d['raw']}{status}")
            print(f"    linked from {len(d['sources'])} page(s):")
            for src, anchor in d["sources"][:5]:
                a = f' "{anchor}"' if anchor else ""
                print(f"      • {urlparse(src).path or src}{a}")
            print()
        if not verify:
            print("Note: a target can be absent because it 404s OR was simply not crawled.\n"
                  "Run  python3 scripts/diagnose_blog_and_404.py --verify  to HEAD-check each\n"
                  "one live (404 DEAD / 301 stale-link / 200 false-positive).")


if __name__ == "__main__":
    main()
