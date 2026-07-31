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
            impr = r.get("gsc_impressions", 0) or 0
            # Priority = how much traffic is at stake × how big the GEO gap is.
            priority = impr * (100 - sc.get("score", 100)) / 100.0
            scored.append((r, sc, impr, priority))

    # PRIORITIZED worklist: high traffic × low score first — fix these, don't
    # waste effort GEO-tuning a page nobody lands on.
    by_priority = sorted(scored, key=lambda x: -x[3])
    print(f"{len(scored)} Montessori pages scored for GEO / AI-citation readiness "
          f"(avg {round(sum(s[1]['score'] for s in scored)/len(scored),1) if scored else 0}/100).\n")
    print("── FIX THESE FIRST (high traffic × low GEO score) ──")
    print(f"{'impr/mo':>8}  {'GEO':>4}  page")
    for r, sc, impr, _ in by_priority[:20]:
        p = urlparse(r.get("url", "")).path or r.get("url", "")
        print(f"{impr:>8}  {sc.get('score','?'):>3}/100  {p}")

    print("\n── DETAIL: weakest pages + exact fixes ──\n")
    for r, sc, impr, _ in sorted(scored, key=lambda x: x[1].get("score", 100))[:20]:
        p = urlparse(r.get("url", "")).path or r.get("url", "")
        print("=" * 72)
        print(f"{sc.get('score', '?')}/100 ({sc.get('grade', '?')})   {impr} impr/mo   {p}")
        findings = sorted(sc.get("findings", []), key=lambda f: -(f.get("points", 0)))
        for f in findings[:5]:
            print(f"   ✗ [{f.get('dimension', '')}] {f.get('label', '')}")
            if f.get("fix"):
                print(f"       FIX: {f['fix']}")
        print()


if __name__ == "__main__":
    main()
