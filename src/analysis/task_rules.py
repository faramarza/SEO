"""Rules every Governor task list must follow, learned from working with Frank.

  1. Don't re-advise a page changed in the last 6 weeks (docs/seo_change_log.md):
     Search Console still reflects the old page.
  2. Supplier-brand searches ("iseeme", "guidecraft", "maple landmark", ...) and
     /shop-by-brands/ pages: the maker holds #1, a dealer can't win them.
  3. Lines on hold or low value get little or no priority (books made ~$400 in a
     year; stools are on hold until the maker's lead time is fixed).
  4. Closed brands (TAG Toys) and pages renamed "...discontinued" never get tasks.
"""
import json
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPPLIER_BRANDS = ("iseeme", "i see me", "guidecraft", "guide craft", "maple landmark",
                   "carpets for kids", "carpetsforkids", "whitney brothers", "whitney bros",
                   "cubby hole", "cubbyhole", "hollow woodworks", "tag toys", "plan toys",
                   "plantoys", "hape", "melissa", "tegu")
# (pattern in URL or query, weight). 0 = drop the task.
LINE_WEIGHTS = (
    (re.compile(r"tag-toys|tag toys|discontinued", re.I), 0.0),
    (re.compile(r"stool|coat-rack|coat rack", re.I), 0.0),          # on hold (lead time)
    (re.compile(r"book|story", re.I), 0.25),                         # ~$400/yr line
)


def recently_changed(within_days=42, log_path=None):
    path = Path(log_path) if log_path else ROOT / "docs" / "seo_change_log.md"
    out = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.search(r"(20\d\d-\d\d-\d\d)", line)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if (date.today() - d).days > within_days:
            continue
        for p in re.findall(r"(/[a-z0-9][a-z0-9/_-]*\.html)", line):
            out.setdefault(p, m.group(1))
    # Changes Governor applied itself (Page Content approvals), when reading the real log.
    if log_path is None:
        import json as _json
        gov = ROOT / "data" / "governor_changes.json"
        try:
            for c in _json.loads(gov.read_text()):
                d = datetime.strptime(c["date"], "%Y-%m-%d").date()
                if (date.today() - d).days <= within_days:
                    for p in (c.get("pages") or []) + ((c.get("control") or {}).get("pages") or []):
                        out.setdefault(p, c["date"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return out


def _path(url):
    m = re.match(r"https?://[^/]+(/.*)?$", url or "")
    p = (m.group(1) if m else url) or "/"
    return p.split("?")[0]


def task_verdict(url, query, recent=None):
    """(weight, reason). weight 0 = drop; 0<w<1 = demote; 1 = keep as is."""
    recent = recent if recent is not None else recently_changed()
    path, q = _path(url), (query or "").lower()
    if url and is_dead_page(url):
        return 0.0, "closed, discontinued or redirected page"
    if path in recent:
        return 0.0, f"changed {recent[path]}: wait for results"
    if "/shop-by-brands/" in path or any(re.search(r"\b" + re.escape(b) + r"\b", q) for b in SUPPLIER_BRANDS):
        return 0.0, "supplier brand search: the maker holds #1"
    for pat, w in LINE_WEIGHTS:
        if pat.search(path) or pat.search(q):
            return w, "line on hold or low value" if w < 1 else ""
    return 1.0, ""


def apply(tasks, recent=None):
    """Filter/demote a list of task dicts (keys: url, metric.query/primary_keyword,
    lever_score). Returns the kept tasks; dropped ones are listed in the result's
    `dropped` attribute for transparency."""
    recent = recent if recent is not None else recently_changed()
    kept, dropped = [], []
    for t in tasks:
        q = (t.get("metric") or {}).get("query") or t.get("primary_keyword") or ""
        w, why = task_verdict(t.get("url", ""), q, recent)
        if w == 0:
            dropped.append((t.get("url"), q, why))
            continue
        if w < 1:
            t["lever_score"] = (t.get("lever_score") or 0) * w
            t["rule_note"] = why
        kept.append(t)
    return kept, dropped


_REDIRECTED = None


def redirected_paths():
    """Old URLs that now 301 elsewhere (closed TAG products), from the redirect files."""
    global _REDIRECTED
    if _REDIRECTED is None:
        import csv
        out = set()
        for f in (ROOT / "docs" / "imports").glob("*replacements*.csv"):
            try:
                for r in csv.DictReader(open(f)):
                    if r.get("old_url"):
                        out.add(r["old_url"].strip())
            except OSError:
                pass
        _REDIRECTED = out
    return _REDIRECTED


CLOSED = re.compile(r"tag[- ]toys|discontinued", re.I)


def is_dead_page(url, title=""):
    """True for pages no task may link to: closed brands, discontinued, redirected."""
    return bool(CLOSED.search(f"{url} {title}")) or _path(url) in redirected_paths()
