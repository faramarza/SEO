"""Near-duplicate / thin-variation detector — the CORRECT way.

Measures shared PASSAGES between pages via word n-gram shingling (Broder
shingling, the technique search engines actually use for near-duplicate
detection), NOT single-word vocabulary overlap. Two distinct pages on the
same topic share ~0-3% of their 8-word sequences; templated pages that repeat
sentences with a variable swapped ("montessori toys for 3-year-olds" vs
"…4-year-olds") share far more. This finds clusters of such pages so they can
be consolidated.

This is a legitimate, standard method and close to how Google's near-dup
detection historically works — it is NOT Google's exact (proprietary,
semantic-ML) system, and the threshold is a calibrated heuristic, not an
official cutoff. Reported honestly as such in the UI.

Data comes from fetching the live pages (full body text, nav/header/footer
stripped) — never a 200-word preview, so the numbers are real.
"""

import fcntl
import json
import re
from datetime import datetime
from pathlib import Path

STORE_PATH = Path(__file__).parent.parent.parent / "data" / "duplication.json"

SHINGLE_N = 8              # words per passage-shingle
# % of 8-word passages two pages must share to count as templated. Distinct
# same-topic pages sit near 0-3%; this catches real repeated boilerplate.
DUP_THRESHOLD = 12.0
MIN_WORDS = 120           # ignore pages too short to judge


def load_store(path: Path = STORE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"clusters": [], "settings": {"threshold": DUP_THRESHOLD}}


def save_store(store: dict, path: Path = STORE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2))
        tmp.replace(path)


# ----------------------------------------------------------------- shingles
def page_words(html: str) -> list:
    """Visible body words, lowercased — nav/header/footer/script/style removed
    so shared chrome doesn't masquerade as duplicate content."""
    html = re.sub(r"(?is)<(script|style|nav|header|footer|noscript)[^>]*>.*?</\1>",
                  " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.findall(r"[a-z]+", text.lower())


def shingles(words, n: int = SHINGLE_N) -> set:
    if len(words) < n:
        return set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def overlap_pct(s1: set, s2: set) -> float:
    """Jaccard % over the two shingle sets."""
    if not s1 or not s2:
        return 0.0
    union = len(s1 | s2)
    return (len(s1 & s2) / union * 100) if union else 0.0


# ----------------------------------------------------------------- clustering
class _UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def find_clusters(pages, threshold: float = DUP_THRESHOLD):
    """pages: [{"url","shingles"(set),"impressions","title"}]. Returns clusters
    of pages that share > threshold passages, each with the pairwise overlaps.

    Candidate pairs are generated via an inverted shingle index (only pages
    that share at least one passage are ever compared) — the scalable way,
    so this stays fast even over hundreds of pages.
    """
    # Inverted index: shingle -> [page indexes]. Only pages sharing a shingle
    # become candidate pairs.
    index = {}
    for i, p in enumerate(pages):
        for sh in p["shingles"]:
            index.setdefault(sh, []).append(i)
    candidates = set()
    for plist in index.values():
        if len(plist) > 1:
            for a in range(len(plist)):
                for b in range(a + 1, len(plist)):
                    candidates.add((plist[a], plist[b]))

    edges = []
    for i, j in candidates:
        pct = overlap_pct(pages[i]["shingles"], pages[j]["shingles"])
        if pct >= threshold:
            edges.append((pct, i, j))

    if not edges:
        return []
    uf = _UnionFind(range(len(pages)))
    for _, i, j in edges:
        uf.union(i, j)
    # Group page indexes by cluster root
    groups = {}
    involved = {i for _, i, j in edges} | {j for _, i, j in edges}
    for i in involved:
        groups.setdefault(uf.find(i), set()).add(i)

    clusters = []
    for members in groups.values():
        members = sorted(members, key=lambda i: -(pages[i].get("impressions") or 0))
        pair_overlaps = [
            {"a": pages[i]["url"], "b": pages[j]["url"], "pct": round(pct, 1)}
            for pct, i, j in sorted(edges, reverse=True)
            if i in members and j in members]
        pillar = pages[members[0]]                # highest-impression page = keep
        clusters.append({
            "pillar": {"url": pillar["url"], "title": pillar.get("title", ""),
                       "impressions": pillar.get("impressions", 0)},
            "consolidate": [
                {"url": pages[i]["url"], "title": pages[i].get("title", ""),
                 "impressions": pages[i].get("impressions", 0)}
                for i in members[1:]],
            "max_overlap": max((o["pct"] for o in pair_overlaps), default=0),
            "pair_overlaps": pair_overlaps[:20],
            "page_count": len(members),
        })
    # Worst (most templated, most demand) first.
    clusters.sort(key=lambda c: (-c["max_overlap"],
                                 -(c["pillar"]["impressions"] or 0)))
    return clusters


def run_report(store, scanned, fetched, cluster_count):
    store["last_scan"] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "pages_scanned": scanned, "pages_fetched": fetched,
        "clusters_found": cluster_count,
    }
    return store
