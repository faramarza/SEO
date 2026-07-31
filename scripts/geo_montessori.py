"""GEO (AI-citation readiness) report for your MONTESSORI pages, worst first.

  python3 scripts/geo_montessori.py

For every crawled page whose URL or title mentions "montessori", prints its GEO
score (how likely ChatGPT/Gemini/Perplexity/AI-Overviews are to cite it) and the
top concrete fixes to raise it. Prefers the scorecard computed during evaluation;
recomputes from the crawl if absent. Read-only; reads data/latest_evaluation.json.
"""

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

# Load geo_scorecard.py DIRECTLY by path (it only needs stdlib `re`). Importing it
# via the package (src.evaluators) would drag in pydantic-backed models through
# __init__.py and fail under the system Python — this way the script runs anywhere.
_geo_path = Path(__file__).resolve().parent.parent / "src" / "evaluators" / "geo_scorecard.py"
_spec = importlib.util.spec_from_file_location("geo_scorecard_standalone", _geo_path)
_gsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gsc)
evaluate_geo_readiness = _gsc.evaluate_geo_readiness

DATA = Path(__file__).resolve().parent.parent / "data" / "latest_evaluation.json"


def _is_montessori(r):
    url = (r.get("url") or "").lower()
    pm = r.get("page_metadata", {}) or {}
    title = (pm.get("title") or "").lower()
    return "montessori" in url or "montessori" in title


def _score(r):
    stored = r.get("geo_scorecard") or {}
    if stored.get("score") is not None and not stored.get("limited"):
        return stored
    pm = r.get("page_metadata", {}) or {}
    if not pm.get("has_crawl_data", True):
        return None
    return evaluate_geo_readiness(
        url=r.get("url", ""), asset_type=(r.get("asset_type") or "other"),
        title=pm.get("title", ""), meta_description=pm.get("meta_description", ""),
        headings=pm.get("headings", []) or [],
        content_preview=pm.get("content_preview", "") or "",
        body_html=pm.get("body_html", "") or "",
        above_fold_html=pm.get("above_fold_html", "") or "",
        word_count=pm.get("word_count", 0) or r.get("word_count", 0) or 0,
        schema_types=pm.get("schema_types", []) or [],
        has_crawl_data=True,
    )


def main():
    path_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = Path(path_args[0]) if path_args else DATA
    if not path.exists():
        print(f"No evaluation at {path}. Run an evaluation (with crawl) first.")
        sys.exit(1)
    results = json.loads(path.read_text()).get("results", [])
    scored = []
    for r in results:
        if not _is_montessori(r):
            continue
        sc = _score(r)
        if sc and sc.get("score") is not None:
            scored.append((r, sc))
    scored.sort(key=lambda x: x[1].get("score", 100))

    print(f"{len(scored)} Montessori pages scored for GEO / AI-citation readiness. "
          f"Lowest (weakest) first — these are the ones AI engines are least likely "
          f"to cite:\n")
    for r, sc in scored[:20]:
        p = urlparse(r.get("url", "")).path or r.get("url", "")
        print("=" * 72)
        print(f"{sc.get('score', '?')}/100 ({sc.get('grade', '?')})   {p}")
        if sc.get("verdict"):
            print(f"   {sc['verdict']}")
        findings = sorted(sc.get("findings", []), key=lambda f: -(f.get("points", 0)))
        for f in findings[:5]:
            print(f"   ✗ [{f.get('dimension', '')}] {f.get('label', '')}")
            if f.get("fix"):
                print(f"       FIX: {f['fix']}")
        print()

    if scored:
        avg = round(sum(s.get("score", 0) for _, s in scored) / len(scored), 1)
        print(f"Average Montessori-page GEO score: {avg}/100. "
              f"Fix the lowest scorers first; the FIX lines are the exact to-dos.")


if __name__ == "__main__":
    main()
