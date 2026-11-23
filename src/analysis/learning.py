"""What Governor learns from its own results.

Every graded change (data/seo_grades.json, written by the daily checks) has a TYPE:
intro_faq, retitle, links, new_page, assortment (products added or restocked).
For each type Governor keeps a success rate that starts neutral (50%) and moves with
every grade, so one lucky result can't swing it:

    rate = (worked + 0.5 x unclear + 1) / (graded + 2)

and turns it into a ranking factor between 0.5 and 1.5 (rate / 0.5). Start Here
multiplies each task's score by the factor for its type, and Page Content does the
same when choosing pages. With no grades every factor is 1.0: nothing changes until
there is evidence.

Control groups: when Governor changes a page it holds back a similar page for the
same 6 weeks and grades the DIFFERENCE, so December's lift for every page isn't
credited to Governor.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TYPES = ("intro_faq", "retitle", "links", "new_page", "assortment", "other")


def classify(change: str) -> str:
    """The type of a change from its description. Pure."""
    t = (change or "").lower()
    if t.startswith("new page") or "new category" in t:
        return "new_page"
    if "intro + faq" in t or "faq" in t or "intro" in t:
        return "intro_faq"
    if re.search(r"link", t) and not re.search(r"meta title|title \"", t):
        return "links"
    if re.search(r"meta title|title|h1", t):
        return "retitle"
    if re.search(r"restock|refill|added|products|carousel", t):
        return "assortment"
    return "other"


def rates(grades: dict) -> dict:
    """{type: {worked, not, unclear, graded, rate, factor}} from seo_grades.json. Pure."""
    out = {k: {"worked": 0, "not": 0, "unclear": 0} for k in TYPES}
    for g in (grades or {}).values():
        v = g.get("verdict")
        if v not in ("WORKED", "DID NOT WORK", "UNCLEAR"):
            continue
        k = g.get("type") or classify(g.get("change", ""))
        k = k if k in out else "other"
        out[k]["worked" if v == "WORKED" else "not" if v == "DID NOT WORK" else "unclear"] += 1
    for k, r in out.items():
        n = r["worked"] + r["not"] + r["unclear"]
        rate = (r["worked"] + 0.5 * r["unclear"] + 1) / (n + 2)
        r.update(graded=n, rate=round(rate, 3), factor=round(min(1.5, max(0.5, rate / 0.5)), 2))
    return out


def factors(path=None) -> dict:
    """{type: factor} from the saved grades; all 1.0 when there are none."""
    p = Path(path) if path else ROOT / "data" / "seo_grades.json"
    try:
        grades = json.loads(p.read_text())
    except (OSError, ValueError):
        return {k: 1.0 for k in TYPES}
    return {k: v["factor"] for k, v in rates(grades).items()}


def task_type(task: dict) -> str | None:
    """Which learned type a Start Here task belongs to (None = not learned). Pure."""
    cat = task.get("category")
    if cat == "demand":
        return {"retitle": "retitle", "links": "links", "content": "intro_faq",
                "new_page": "new_page"}.get(task.get("action_type"))
    return {"ctr": "retitle", "winnable": "retitle", "striking": "retitle", "interlink": "links",
            "orphan": "links", "content": "new_page"}.get(cat)


def choose_control(candidates: list, treated: dict):
    """A comparable page Governor will NOT change for the same window: same template
    and product line, closest in value. None if there isn't one. Pure."""
    same = [c for c in candidates if c.get("key") != treated.get("key") and not c.get("create")
            and c.get("template") == treated.get("template") and c.get("line") == treated.get("line")]
    if not same:
        same = [c for c in candidates if c.get("key") != treated.get("key") and not c.get("create")
                and c.get("template") == treated.get("template")]
    if not same:
        return None
    v = treated.get("value") or 0
    return min(same, key=lambda c: abs((c.get("value") or 0) - v))
