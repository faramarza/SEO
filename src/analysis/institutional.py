"""Institutional Prospecting Agent — data layer.

Builds and maintains a list of institutional buyers (Montessori/independent
preschools, licensed childcare centers, museums/specialty retail, federal
primes) for Alphabet Trains & Toys. Output is data and drafts; a human sends.

Spec constraints enforced here and in the web layer:
- Never invent a contact: emails only from a page actually retrieved from the
  org's own site (contact_confidence=verified) or a directory (directory).
  Empty fields are correct output.
- Organizations only; draft cap 15/run; no prices/discounts anywhere.
- Crawls respect robots.txt, wait >=1.5s between requests, and identify
  themselves with frank@alphabet-trains.com in the User-Agent.
- The Southeast ranks first (freight lands cheaper from FOB West Point, GA) —
  and that reasoning never appears in any outreach draft.
"""

import csv
import fcntl
import io
import json
import re
import time
from datetime import datetime
from pathlib import Path

STORE_PATH = Path(__file__).parent.parent.parent / "data" / "institutional.json"

DRAFTS_PER_RUN = 15
FETCH_DELAY_S = 1.5
USER_AGENT = ("AlphabetTrains-Institutional/1.0 "
              "(institutional outreach research; frank@alphabet-trains.com)")

SOUTHEAST = {"GA", "FL", "SC", "NC", "TN", "AL", "MS", "LA", "VA", "KY", "AR"}

ORG_TYPES = ("montessori_school", "childcare_center", "museum_retail", "federal_prime")
# Segment priority for ranking (spec: Montessori primary).
_SEGMENT_RANK = {"montessori_school": 0, "childcare_center": 1,
                 "museum_retail": 2, "federal_prime": 3}

STATUSES = ("new", "researched", "flagged_research", "drafted", "contacted",
            "replied", "opted_out", "customer", "suppressed")

# ── State source directory (Phase-1 seed: Southeast licensing portals). ──
# verified stays False until a health check or a successful import confirms
# the link — a plausible-but-dead URL is the same defect class as an invented
# contact. 'kind' tells the operator what to expect at the other end.
STATE_SOURCE_SEED = {
    "GA": {"agency": "GA DECAL (Dept. of Early Care and Learning)",
           "url": "https://families.decal.ga.gov/ChildCare/Search",
           "kind": "search portal — export per search"},
    "FL": {"agency": "FL DCF child care provider search",
           "url": "https://cares.myflfamilies.com/PublicSearch",
           "kind": "search portal — export per search"},
    "SC": {"agency": "SC DSS child care search",
           "url": "https://childcare.sc.gov/",
           "kind": "search portal"},
    "NC": {"agency": "NC DCDEE child care facility search",
           "url": "https://ncchildcare.ncdhhs.gov/childcaresearch",
           "kind": "search portal"},
    "TN": {"agency": "TN DHS child care lookup",
           "url": "https://tnmap.tn.gov/childcare/",
           "kind": "map/search portal"},
    "AL": {"agency": "AL DHR child care licensing",
           "url": "https://dhr.alabama.gov/child-care/",
           "kind": "search portal"},
    "MS": {"agency": "MS State Dept. of Health licensed facilities",
           "url": "https://msdh.ms.gov/page/30,0,183.html",
           "kind": "directory page"},
    "LA": {"agency": "LA Dept. of Education early childhood licensing",
           "url": "https://www.louisianabelieves.com/early-childhood",
           "kind": "directory page"},
    "VA": {"agency": "VA DOE child care search",
           "url": "https://www.doe.virginia.gov/early-childhood-care-education/child-care/search-for-child-care",
           "kind": "search portal"},
    "KY": {"agency": "KY CHFS service provider directory",
           "url": "https://prd.webapps.chfs.ky.gov/serviceproviderdirectory/",
           "kind": "search portal"},
    "AR": {"agency": "AR DHS find-child-care",
           "url": "https://humanservices.arkansas.gov/divisions-shared-services/early-childhood/find-child-care/",
           "kind": "directory page"},
}

# AMI school locator state-page slugs (enumerable, one page per state).
AMI_BASE = "https://amiusa.org/school-locator1/category/"
AMI_SLUGS = {
    "GA": "georgia", "FL": "florida", "SC": "south-carolina",
    "NC": "north-carolina", "TN": "tennessee", "AL": "alabama",
    "MS": "mississippi", "LA": "louisiana", "VA": "virginia",
    "KY": "kentucky", "AR": "arkansas",
    "TX": "texas", "NY": "new-york", "CA": "california", "OH": "ohio",
    "PA": "pennsylvania", "IL": "illinois", "MD": "maryland",
    "NJ": "new-jersey", "MI": "michigan", "WA": "washington",
    "CO": "colorado", "MA": "massachusetts", "AZ": "arizona",
}


# ------------------------------------------------------------------ store
def load_store(path: Path = STORE_PATH) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("settings", {})
    data.setdefault("prospects", [])
    data.setdefault("runs", [])
    sources = data.setdefault("sources", {})
    for st, seed in STATE_SOURCE_SEED.items():
        sources.setdefault(st, {**seed, "verified": False, "health": "unchecked",
                                "last_import": None})
    return data


def save_store(store: dict, path: Path = STORE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2))
        tmp.replace(path)


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------- prospects
def _norm_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    s = re.sub(r"\b(the|inc|llc|school|academy|center|centre|of|and)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", (url or "").lower())
    return m.group(1) if m else ""


def dedup_key(p: dict) -> str:
    dom = _norm_domain(p.get("website", ""))
    if dom:
        return f"dom:{dom}"
    return f"name:{_norm_name(p.get('school', ''))}|{(p.get('state') or '').upper()}"


def new_prospect(school, org_type, state, source, website="", notes="") -> dict:
    st = (state or "").upper()
    return {
        "id": f"inst-{int(time.time()*1000) % 10**10}-{abs(hash(school)) % 10000:04d}",
        "school": (school or "").strip(),
        "org_type": org_type if org_type in ORG_TYPES else "childcare_center",
        "state": st,
        "southeast": st in SOUTHEAST,
        "source": source,
        "website": (website or "").strip(),
        "contact_name": "", "contact_title": "", "email": "",
        "contact_confidence": "none",
        "solicitation_id": "",
        "evidence": None,          # {"fact": ..., "url": ...} — retrieved, never invented
        "status": "new",
        "draft": None,
        "notes": notes or "",
        "created_at": _now(), "updated_at": _now(),
    }


def add_prospects(store, prospects):
    """Dedup-aware insert. Suppressed/opted-out/customer orgs never resurface.
    Returns (added, skipped_dupes)."""
    existing = {dedup_key(p): p for p in store["prospects"]}
    added, dupes = [], 0
    for p in prospects:
        k = dedup_key(p)
        if not p.get("school"):
            continue
        if k in existing:
            dupes += 1
            continue
        existing[k] = p
        store["prospects"].append(p)
        added.append(p)
    return added, dupes


def rank_key(p: dict):
    """Southeast first, Montessori first, has-website first, newest last-ish."""
    return (0 if p.get("southeast") else 1,
            _SEGMENT_RANK.get(p.get("org_type"), 9),
            0 if p.get("website") else 1,
            p.get("school", "").lower())


# ------------------------------------------------------------ CSV in/out
CSV_COLUMNS = ["school", "org_type", "state", "southeast", "source", "website",
               "contact_name", "contact_title", "email", "contact_confidence",
               "solicitation_id", "notes"]

# Header aliases seen across state licensing exports.
_IMPORT_ALIASES = {
    "school": {"school", "name", "facility name", "facility", "provider name",
               "provider", "program name", "center name", "business name",
               "operation name", "site name", "legal name"},
    "state": {"state", "st"},
    "website": {"website", "url", "web site", "web address", "homepage"},
    "notes": {"city", "county", "address", "phone", "zip", "zip code",
              "capacity", "license type", "license number", "type"},
}


def import_csv(store, csv_text, state, source_label):
    """Normalize a state-licensing CSV into prospects. Unknown columns are
    folded into notes so nothing is silently lost. Returns (added, dupes,
    total_rows, error)."""
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
    except csv.Error as e:
        return 0, 0, 0, f"CSV parse failed: {e}"
    if not rows:
        return 0, 0, 0, "No data rows found (is the first line a header?)"

    def pick(row, field):
        for k, v in row.items():
            if k and k.strip().lower() in _IMPORT_ALIASES[field]:
                if v and str(v).strip():
                    return str(v).strip()
        return ""

    prospects = []
    for row in rows:
        name = pick(row, "school")
        if not name:
            continue
        note_bits = []
        for k, v in row.items():
            if k and v and k.strip().lower() in _IMPORT_ALIASES["notes"]:
                note_bits.append(f"{k.strip()}: {str(v).strip()}")
        p = new_prospect(name, "childcare_center", state,
                         source_label, pick(row, "website"),
                         "; ".join(note_bits[:6]))
        prospects.append(p)
    added, dupes = add_prospects(store, prospects)
    src = store["sources"].setdefault(state.upper(), {})
    src["last_import"] = _now()
    src["verified"] = True  # a successful import proves the source is real
    return len(added), dupes, len(rows), None


def export_csv(store) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CSV_COLUMNS + ["status"])
    w.writeheader()
    for p in sorted(store["prospects"], key=rank_key):
        row = {c: p.get(c, "") for c in CSV_COLUMNS}
        row["southeast"] = "yes" if p.get("southeast") else "no"
        row["status"] = p.get("status", "new")
        w.writerow(row)
    return out.getvalue()


# --------------------------------------------------------------- reports
def run_report(store, run):
    """Append + return the per-run report the spec demands — with zero-yield
    sources called out loudly (silent zero-yield is the main failure mode)."""
    run["at"] = _now()
    store["runs"] = (store.get("runs") or [])[-19:] + [run]
    return run
