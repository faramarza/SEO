"""Page content that sells: intros, FAQs and gift guides for the pages buyers search
for, drafted by AI from YOUR catalog, approved by you, inserted by Governor with Undo.

Where it goes (one rule for every page):
  - a short INTRO (2-3 sentences) at the very top of the page description,
  - the rest (guide sections with product links, then FAQs) at the BOTTOM,
so products stay near the top. Each part sits between invisible markers, so Undo
removes exactly Governor's text and a rewrite replaces only its own block.
Templates:
  "category"  existing category page: intro + FAQs
  "guide"     gift / "best" searches, and new pages: intro + 2-3 sections linking
              real products + FAQs
The AI only writes words. Product names, prices and links are filled in from the
catalog, and every product it picks must be one we gave it (in stock, enabled).
Pure functions; the web layer does the AI call and the Magento write.
"""
from __future__ import annotations

import html
import re

INTRO = ("<!-- governor-intro:start -->", "<!-- governor-intro:end -->")
BODY = ("<!-- governor-body:start -->", "<!-- governor-body:end -->")

# Claims the AI may not make unless the catalog says so.
BANNED = re.compile(r"(#\s?1\b|number one|guarantee|cheapest|lowest price|award|certified|non-?toxic|"
                    r"organic|eco-?friendly|safest|best[- ]selling|top[- ]rated|free shipping|"
                    r"made in (the )?u\.?s\.?a?\.?)", re.I)
YOUNG = re.compile(r"\b(baby|babies|infant|newborn|1[- ]year|one[- ]year|2[- ]year|two[- ]year|"
                   r"under 3|first birthday|1st birthday|12 months|18 months)\b", re.I)


def min_age(product: dict, rules: dict) -> int:
    """Minimum age from config rules {'name train': 3} matched on the product name. Pure."""
    n = (product.get("name") or "").lower()
    return max([age for k, age in (rules or {}).items() if k in n] or [0])


def system_prompt() -> str:
    return ("You write short, plain, factual copy for a family-run online toy store. Never invent "
            "facts, awards, safety claims, materials, origins or prices. Only use what the product list "
            "says. No superlatives about the store. Write for parents and teachers. Return ONLY JSON.")


def user_prompt(target: dict, products: list, template: str) -> str:
    """The instructions + the catalog facts the AI may use. Pure."""
    facts = "\n".join(f"- sku {p['sku']}: {p['name']} (${p['price']:.2f})"
                      + (f", ages {p['min_age']}+" if p.get("min_age") else "") for p in products)
    searches = ", ".join(f"\"{s['search']}\"" for s in target.get("searches", [])[:5])
    base = (f"Page: {target.get('page_name') or target['searches'][0]['search'].title()}\n"
            f"People search for: {searches}\n"
            f"Products on this page (use only these):\n{facts}\n\n")
    if template == "category":
        return base + ("Write JSON: {\"intro\": 2-3 sentences (max 60 words) that plainly answer what "
                       "this page offers and who it is for, using the main search phrase once; "
                       "\"faqs\": 4 items [{\"q\": a question a buyer asks, \"a\": one or two factual "
                       "sentences}]}. Do not mention prices in the intro.")
    return base + ("Write JSON: {\"intro\": 2-3 sentences (max 60 words) that directly answer the search; "
                   "\"sections\": 2-3 items [{\"heading\": short heading by age, occasion or budget, "
                   "\"items\": 2-4 [{\"sku\": a sku from the list, \"why\": one sentence why it fits}]}]; "
                   "\"faqs\": 4 items [{\"q\", \"a\"}]; \"meta_title\": under 60 characters, no | or - ; "
                   "\"meta_description\": under 155 characters}. Respect the ages: never put a product "
                   "in a section for children younger than its age.")


def _clean(text: str) -> str:
    """Plain text, no HTML, sentences with banned claims removed. Pure."""
    t = re.sub(r"<[^>]+>", "", str(text or "")).strip()
    keep = [s for s in re.split(r"(?<=[.!?])\s+", t) if s and not BANNED.search(s)]
    return " ".join(keep).strip()


def validate(draft: dict, products: list, template: str) -> tuple[dict, list]:
    """Clean the AI draft against the catalog. Returns (clean draft, [problems fixed]). Pure."""
    by_sku = {p["sku"]: p for p in products}
    fixes, out = [], {"intro": _clean(draft.get("intro")), "faqs": [], "sections": []}
    if out["intro"] != re.sub(r"<[^>]+>", "", str(draft.get("intro") or "")).strip():
        fixes.append("removed a claim from the intro")
    for f in (draft.get("faqs") or [])[:5]:
        q, a = _clean(f.get("q")), _clean(f.get("a"))
        if q and a:
            out["faqs"].append({"q": q, "a": a})
        else:
            fixes.append("dropped an FAQ with an unsupported claim")
    if template == "guide":
        for s in (draft.get("sections") or [])[:3]:
            head = _clean(s.get("heading"))
            items = []
            for it in (s.get("items") or [])[:4]:
                p = by_sku.get(str(it.get("sku")))
                if not p:
                    fixes.append(f"dropped unknown product {it.get('sku')}")
                    continue
                if YOUNG.search(head) and (p.get("min_age") or 0) >= 3:
                    fixes.append(f"moved out {p['name']} (ages {p['min_age']}+) from \"{head}\"")
                    continue
                items.append({"sku": p["sku"], "why": _clean(it.get("why"))})
            if head and items:
                out["sections"].append({"heading": head, "items": items})
        out["meta_title"] = re.sub(r"[|\-–]", " ", _clean(draft.get("meta_title")))[:60].strip()
        out["meta_description"] = _clean(draft.get("meta_description"))[:155]
    return out, fixes


def render(draft: dict, products: list) -> tuple[str, str]:
    """(intro html, body html). Names, prices and links come from the catalog. Pure."""
    by_sku = {p["sku"]: p for p in products}
    e = html.escape
    intro = f"<p>{e(draft['intro'])}</p>" if draft.get("intro") else ""
    parts = []
    for s in draft.get("sections") or []:
        lis = "".join(f'<li><a href="{e(by_sku[i["sku"]]["url"])}">{e(by_sku[i["sku"]]["name"])}</a>'
                      f' (${by_sku[i["sku"]]["price"]:.2f}): {e(i["why"])}</li>' for i in s["items"])
        parts.append(f"<h2>{e(s['heading'])}</h2><ul>{lis}</ul>")
    if draft.get("faqs"):
        parts.append("<h2>Frequently asked questions</h2>" +
                     "".join(f"<h3>{e(f['q'])}</h3><p>{e(f['a'])}</p>" for f in draft["faqs"]))
    return intro, "".join(parts)


def _strip(desc: str, marks) -> str:
    return re.sub(re.escape(marks[0]) + r".*?" + re.escape(marks[1]), "", desc or "", flags=re.S)


def merge(current: str, intro_html: str, body_html: str) -> str:
    """Put the intro at the top and the body at the bottom of the existing description,
    replacing any earlier Governor blocks; the owner's own text is untouched. Pure."""
    base = _strip(_strip(current or "", INTRO), BODY).strip()
    top = f"{INTRO[0]}{intro_html}{INTRO[1]}" if intro_html else ""
    bottom = f"{BODY[0]}{body_html}{BODY[1]}" if body_html else ""
    return "\n".join(x for x in (top, base, bottom) if x)


def remove(current: str) -> str:
    """The description without Governor's blocks (Undo). Pure."""
    return _strip(_strip(current or "", INTRO), BODY).strip()


def pick_targets(plan_pages: list, done: set, limit: int = 2) -> list:
    # (each page's value is weighted by how well that kind of change has worked here)
    """This week's pages: highest value first, gift and new pages as guides, skip pages
    already given content (`done`). Pure."""
    from src.analysis.learning import factors
    f = factors()
    kind = lambda g: "new_page" if g.get("action") == "new_page" else "intro_faq"
    out = []
    for g in sorted(plan_pages, key=lambda x: -(x.get("value") or 0) * f.get(kind(x), 1.0)):
        key = g.get("page") or "NEW:" + g["searches"][0]["search"]
        if key in done:
            continue
        gift = g.get("line") == "Gifts"
        if g.get("action") == "new_page":
            out.append(dict(g, key=key, template="guide", create=True))
        elif g.get("kind") == "category":
            out.append(dict(g, key=key, template="guide" if gift else "category", create=False))
        if len(out) >= limit:
            break
    return out
