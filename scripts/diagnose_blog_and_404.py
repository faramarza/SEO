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
from pathlib import Path
from urllib.parse import urlparse

DATA = Path(__file__).resolve().parent.parent / "data" / "latest_evaluation.json"


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
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA
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
    money = {_norm(r.get("url", "")) for r in results
             if (r.get("ga4_revenue") or 0) > 0
             and (r.get("asset_type") or "").lower() in ("product", "category")}
    print("=" * 70)
    print("1. BLOG → MONEY  (does each blog even point at a page that sells?)")
    print("=" * 70)
    blogs = [r for r in results
             if (r.get("asset_type") or "").lower() in ("blog", "article", "guide")]
    blogs.sort(key=lambda r: -(r.get("gsc_clicks") or 0))
    if not blogs:
        print("No blog/article pages found in the evaluation.\n")
    for r in blogs[:20]:
        outs = (r.get("page_metadata", {}) or {}).get("internal_outlinks", []) or []
        links_to_money = sorted({_norm(o.get("target_url", "")) for o in outs
                                 if _norm(o.get("target_url", "")) in money})
        clicks = r.get("gsc_clicks") or 0
        verdict = ("VANITY (no link to any revenue page)" if not links_to_money
                   else f"links to {len(links_to_money)} revenue page(s)")
        path = urlparse(r.get("url", "")).path
        print(f"  {clicks:>4} clk/mo  {path[:52]:52}  → {verdict}")
    print()

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
    if not dead:
        print("No internal links point to an uncrawled/dead target. ✓\n")
    else:
        ranked = sorted(dead.values(), key=lambda d: -len(d["sources"]))
        print(f"{len(ranked)} suspected dead internal target(s). Worst first:\n")
        for d in ranked[:25]:
            print(f"  ✗ {d['raw']}")
            print(f"    linked from {len(d['sources'])} page(s):")
            for src, anchor in d["sources"][:6]:
                a = f' "{anchor}"' if anchor else ""
                print(f"      • {urlparse(src).path or src}{a}")
            print()
        print("Note: a target can be absent because it 404s OR was simply not crawled\n"
              "(GSC-only URL, param page). Confirm the real 404s in GA4 (page_title =\n"
              "'Sorry This Page Was Not Found' → switch dimension to Page path), then the\n"
              "sources above tell you which pages to fix the links on.")


if __name__ == "__main__":
    main()
