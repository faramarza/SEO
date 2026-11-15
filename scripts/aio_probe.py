#!/usr/bin/env python3
"""Show exactly what Google's results page contains for a search (via DataForSEO),
so "no AI summary" can be checked instead of trusted.

    /var/www/seo/venv/bin/python scripts/aio_probe.py "what is a montessori toy" "baby gifts"

Prints, per search, the block types on the page in order (organic, ai_overview,
shopping, people_also_ask, ...). One paid lookup per search, not cached. Read-only.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.line_demand import _load_dotenv  # noqa: E402

_load_dotenv()


def main():
    import httpx
    from src.data_sources import dataforseo_client as dfs
    queries = sys.argv[1:] or ["what is a montessori toy", "best gifts for 1 year old"]
    for q in queries:
        body = [{"keyword": q, "location_code": dfs.LOCATION_CODE, "language_code": dfs.LANGUAGE_CODE,
                 "device": "desktop", "load_async_ai_overview": True}]
        r = httpx.post(dfs._ENDPOINT, json=body, timeout=60.0,
                       headers={"Authorization": f"Basic {dfs._auth_token()}", "Content-Type": "application/json"})
        task = (r.json().get("tasks") or [{}])[0]
        print(f'\n"{q}": HTTP {r.status_code}, task {task.get("status_code")} {task.get("status_message")}')
        res = (task.get("result") or [{}])[0] or {}
        items = res.get("items") or []
        print(f"  item types on the page: {res.get('item_types')}")
        print("  in order: " + ", ".join(str(it.get("type")) for it in items[:25]))
        aio = [it for it in items if "ai_overview" in str(it.get("type"))]
        print(f"  AI Overview: {'YES' if aio else 'none returned'}")
        if aio:
            refs = aio[0].get("references") or []
            print("  quotes: " + ", ".join(sorted({x.get('domain', '') for x in refs})[:8]))


if __name__ == "__main__":
    main()
