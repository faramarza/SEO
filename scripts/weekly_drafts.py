#!/usr/bin/env python3
"""Mondays: draft page content for this week's 1-2 most valuable pages and ring the
bell. Nothing is published: you approve on Governor's Page Content page.

    /var/www/seo/venv/bin/python scripts/weekly_drafts.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.sales_by_line import _load_dotenv  # noqa: E402

_load_dotenv()


def main():
    from web.app import app, page_content_generate_next
    from scripts.scheduled_checks import notify
    with app.app_context():
        res = page_content_generate_next(limit=2)
    ok = [r for r in res if not r.get("error")]
    for r in res:
        print(("drafted: " + r["page_name"]) if not r.get("error") else ("skipped: " + r["error"]))
    if ok:
        notify("page_drafts", ",".join(r["id"] for r in ok),
               f"{len(ok)} page draft(s) ready to review: " + "; ".join(r["page_name"] for r in ok)
               + ". Open Page Content to read and approve.", severity="info")


if __name__ == "__main__":
    main()
