#!/usr/bin/env python3
"""Mondays: draft page content for this week's 1-2 most valuable pages, find this
week's Reddit threads with draft replies, and ring the bell. Nothing is published or
posted: you approve on Page Content and post on Reddit yourself.

    /var/www/seo/venv/bin/python scripts/weekly_drafts.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.sales_by_line import _load_dotenv  # noqa: E402

_load_dotenv()


def main():
    from web.app import app, page_content_generate_next, reddit_find_and_draft
    from scripts.scheduled_checks import notify
    with app.app_context():
        res = page_content_generate_next(limit=2)
        threads, rd_errors = reddit_find_and_draft()
    for e in rd_errors:
        print("reddit: " + e)
    if threads:
        notify("reddit_threads", ",".join(t["id"] for t in threads),
               f"{len(threads)} Reddit thread(s) where your products fit, with draft replies. Open Reddit Helper.",
               severity="info")
    ok = [r for r in res if not r.get("error")]
    for r in res:
        print(("drafted: " + r["page_name"]) if not r.get("error") else ("skipped: " + r["error"]))
    if ok:
        notify("page_drafts", ",".join(r["id"] for r in ok),
               f"{len(ok)} page draft(s) ready to review: " + "; ".join(r["page_name"] for r in ok)
               + ". Open Page Content to read and approve.", severity="info")


if __name__ == "__main__":
    main()
