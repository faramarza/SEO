#!/usr/bin/env python3
"""Which Magento category serves a URL, and which saved copy of its description
the storefront reads. Read-only, through Governor's Magento API token.

    /var/www/seo/venv/bin/python scripts/category_check.py montessori-toys
    ... montessori-toys --find mt-start-wrap   (also report whether each copy
                                                contains this text)

For every category whose url_key OR name matches, prints the description as
saved for All Store Views (scope 'all') and for each store view, with its
length and whether it contains --find. A store view that shows a different
length from 'all' has its own copy: that copy is what the storefront shows.
"""
import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url_key", help="e.g. montessori-toys (no .html)")
    ap.add_argument("--find", default="mt-start-wrap")
    args = ap.parse_args()

    from src.data_sources.magento_client import MagentoClient, MagentoError
    mc = MagentoClient()
    if not mc.configured:
        sys.exit("Magento not configured (MAGENTO_BASE_URL / MAGENTO_TOKEN in .env).")

    key = args.url_key.strip().strip("/").removesuffix(".html")
    data = mc._req("GET", "/categories/list?searchCriteria[pageSize]=3000") or {}
    cats = data.get("items") or []
    hits = [c for c in cats
            if mc._attr(c, "url_key") == key
            or key.replace("-", " ") in (c.get("name") or "").lower()]
    if not hits:
        sys.exit(f"No category with url_key '{key}' or a matching name.")

    try:
        views = [v for v in (mc._req("GET", "/store/storeViews") or [])
                 if v.get("code") and v.get("code") != "admin"]
    except MagentoError as e:
        views = []
        print(f"(could not list store views: {e})")
    scopes = ["all"] + [v["code"] for v in views]

    for c in sorted(hits, key=lambda c: int(c["id"])):
        cid = c["id"]
        print(f"\nCATEGORY {cid}  \"{c.get('name')}\"  url_key={mc._attr(c, 'url_key')}  "
              f"active={c.get('is_active')}  parent={c.get('parent_id')}")
        for sc in scopes:
            try:
                full = mc._req("GET", f"/categories/{quote(str(cid))}", scope=sc) or {}
            except MagentoError as e:
                print(f"   scope {sc:<16} error: {e}")
                continue
            desc = mc._attr(full, "description") or ""
            uk = mc._attr(full, "url_key")
            has = "YES" if args.find and args.find in desc else "no"
            print(f"   scope {sc:<16} url_key={uk:<28} description {len(desc):>6} chars   "
                  f"contains '{args.find}': {has}")
    print("\nThe storefront URL /<url_key>.html is served by the ACTIVE category with that "
          "url_key in the\nstore view; its store-view description wins over 'all' when they differ.")


if __name__ == "__main__":
    main()
