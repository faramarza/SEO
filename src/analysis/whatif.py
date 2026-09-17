"""What-if keyword tester — for a page whose head term is a wall, explore
whether a DIFFERENT, more specific keyword is winnable.

Given a page, we (a) suggest candidate keywords by combining the store's niche
vocabulary with the page's own core phrase and by surfacing the real queries
the page already gets impressions for, and (b) measure any candidate on demand
with the SAME engine Link Targets uses (SERP → page-1 sites' referring domains
→ links needed). The operator can ignore any suggestion and add their own — the
suggestions are only a starting point, never locked in.

No numbers are invented: a candidate has no verdict until it's measured, and
'links needed' is the same honest 'domains to match the page-1 sites' figure.
"""

import fcntl
import json
import re
from datetime import datetime
from pathlib import Path

from src.analysis import link_targets as lt

STORE_PATH = Path(__file__).parent.parent.parent / "data" / "whatif.json"

# The store's niche vocabulary — the modifiers that turn a generic head term
# ("lock box") into a term this brand can actually win ("montessori lock box").
# Editable via the store's `modifiers`, but these are sensible defaults for
# Alphabet Trains.
DEFAULT_MODIFIERS = [
    "montessori", "wooden", "personalized", "name", "classroom",
    "educational", "handmade", "custom", "toddler", "preschool",
]
SUFFIX_PATTERNS = ["for toddlers", "for 2 year olds", "for preschoolers",
                   "for kids", "for classrooms"]
# Generic lead words to strip so we get the page's core noun phrase.
_GENERIC_LEAD = {"best", "top", "the", "a", "an", "cheap", "buy", "good",
                 "kids", "kid", "childrens", "children's"}


def _norm(q):
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _slug_phrase(url):
    """A readable phrase from the last path segment: /little-lock-box.html →
    'little lock box'."""
    seg = re.sub(r"\.(html?|php|aspx)$", "", (url or "").rstrip("/").rsplit("/", 1)[-1])
    seg = seg.replace("-", " ").replace("_", " ")
    return _norm(seg)


def core_phrase(head_query, url):
    """The core noun phrase to build variants around: the head query with a
    generic lead word dropped, or the URL slug if there's no query."""
    words = _norm(head_query).split()
    while words and words[0] in _GENERIC_LEAD:
        words = words[1:]
    return " ".join(words) if words else _slug_phrase(url)


def suggest_candidates(url, gsc_queries, head_query, modifiers=None):
    """Editable starting list: the page's real GSC queries (grounded) first,
    then generated niche variants of its core phrase (ideas to try). Deduped,
    each tagged with its source so the operator knows which is real."""
    modifiers = modifiers or DEFAULT_MODIFIERS
    core = core_phrase(head_query, url)
    out, seen = [], set()

    def add(q, source):
        q = _norm(q)
        if q and q not in seen and len(q) > 2:
            seen.add(q)
            out.append({"query": q, "source": source})

    for q in gsc_queries or []:
        add(q, "your Search Console")
    for m in modifiers:
        if m in core:
            continue
        add(f"{m} {core}", "suggested")
    for suf in SUFFIX_PATTERNS:
        if suf.rsplit(" ", 1)[-1] in core:
            continue
        add(f"{core} {suf}", "suggested")
    return out


def _own_position(serp, own_domain):
    """Your page's own rank for the query in the fetched SERP, or None."""
    if not serp:
        return None
    organic = (serp.get("organic_results") or serp.get("organic") or [])
    for r in organic:
        if lt._domain(r.get("url", "")) == own_domain:
            return r.get("position")
    return None


def measure_candidate(url, query, fetch_serp_fn, fetch_rd_fn):
    """Measure ONE candidate keyword for a page with the Link Targets engine,
    plus whether the page already ranks for it. Returns the same result shape
    as link_targets (you / page1 / links_needed / verdict / note) so the UI is
    consistent, with an added own_position."""
    target = {"url": url, "keywords": [{"query": query}]}
    res = lt.compute_target(target, fetch_serp_fn, fetch_rd_fn, keyword=query)
    res["query"] = _norm(query)
    pos = _own_position(fetch_serp_fn(query), lt._domain(url))
    res["own_position"] = pos
    # Sanity guard: if the page ALREADY ranks on page 1, links are provably not
    # the barrier — you're there on your current links. Never show a
    # links-needed number that contradicts the ranking (the classic "you rank
    # #9 but need 52,000 links" nonsense, caused by one giant domain poisoning
    # the page-1 median).
    if pos is not None and pos <= 10 and res.get("verdict") != "pending":
        res["verdict"] = "already_ranking"
        res["links_needed"] = 0
        res["note"] = (f"You already rank #{int(round(pos))} for this on your current links — "
                       "links aren't the barrier. It's an on-page / relevance nudge to climb, "
                       "not a link-building job.")
    return res


# ── Store ────────────────────────────────────────────────────────────────
def load_store(path: Path = STORE_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("pages", {})
    return data


def save_store(store: dict, path: Path = STORE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2))
        tmp.replace(path)


def _page(store, url):
    return store["pages"].setdefault(
        url, {"removed": [], "added": [], "measurements": {}})


def page_candidates(store, url, gsc_queries, head_query):
    """Merge auto-suggestions with the operator's edits: drop what they've
    ignored, add what they've typed, attach any measurement. This is what the
    UI renders."""
    p = _page(store, url)
    removed = {_norm(q) for q in p.get("removed", [])}
    rows = []
    seen = set()
    for c in suggest_candidates(url, gsc_queries, head_query):
        if c["query"] in removed:
            continue
        seen.add(c["query"])
        rows.append({**c, "measurement": p["measurements"].get(c["query"])})
    for q in p.get("added", []):
        q = _norm(q)
        if q in removed or q in seen:
            continue
        seen.add(q)
        rows.append({"query": q, "source": "you",
                     "measurement": p["measurements"].get(q)})
    # Measured-and-winnable first (fewest links), then unmeasured, then walls.
    def key(r):
        m = r.get("measurement")
        if not m or m.get("verdict") == "pending":
            return (1, 0)
        ln = m.get("links_needed")
        return (0, ln if ln is not None else 1e9)
    return sorted(rows, key=key)
