"""Pricesheet ingestion: compute REAL product margins from a vendor price list.

Replaces the coarse per-category placeholder margins in config with numbers
derived from an actual pricesheet (dealer cost vs the price you sell at). Reads
.xlsx (stdlib zip + XML, no third-party dependency) and .csv.

Column detection is fuzzy so it works across vendors: it finds the header row
and maps columns to roles (sku, title, cost, map, msrp, size) by header text. If
it can't find cost or a sell price it raises with the headers it did see, rather
than guessing, because a wrong margin is worse than an honest failure.

Margin convention matches config: GROSS product margin = (sell - cost) / sell,
BEFORE the card fee (break-even math subtracts card_fee separately). Sell price
defaults to MAP (what you actually advertise/sell at); MSRP is available as an
upper bound.
"""
from __future__ import annotations

import csv
import re
import statistics
import zipfile
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Header text -> role. Checked as "normalized header contains phrase".
# Order matters: more specific phrases first so "dealer cost" wins over "cost",
# and we never match "character count" as a cost column.
_ROLE_RULES = [
    ("sku", ["sku", "item number", "item #", "model #", "model number"]),
    ("cost", ["dealer cost", "dealer price", "wholesale", "your cost", "unit cost"]),
    ("map", ["map pricing", "map price", "minimum advertised", "map"]),
    ("msrp", ["msrp", "suggested list", "list price", "retail price", "retail"]),
    ("ship_weight", ["ship weight", "shipping weight", "carton weight"]),
    ("title", ["title", "product name", "description", "name"]),
    ("size", ["size & shape", "size/shape", "size"]),
]
_COST_FALLBACK = re.compile(r"\bcost\b")  # only if no explicit cost header; never 'count'

# "8ft 4in x 13ft 4in", "7ft 6in x 12ft", "8' x 12'", "8'3\" x 11'8\"" -> feet.
_DIM_RE = re.compile(r"(\d+)\s*(?:ft|')\s*(?:(\d+)\s*(?:in|\"))?", re.I)


def _dims_feet(size: str) -> list[float]:
    """Pull dimension values (in feet) out of a size string."""
    out = []
    for ft, inch in _DIM_RE.findall(size or ""):
        out.append(int(ft) + (int(inch) / 12.0 if inch else 0.0))
    return out


def is_xl(size: str) -> bool:
    """True for rugs the vendor's XL freight surcharge targets: roughly
    8' x 12' and larger (the surcharge letter also lists 8'3\"x11'8\", so the
    long side threshold is ~11.5'). Needs two parsed dimensions."""
    d = _dims_feet(size)
    if len(d) < 2:
        return False
    return min(d[0], d[1]) >= 8.0 and max(d[0], d[1]) >= 11.5


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def money(x) -> float | None:
    """Parse a price cell to float, tolerating $ , and stray text. None if blank."""
    if x is None:
        return None
    s = str(x).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ----------------------------------------------------------------- readers
def _col_to_idx(ref: str) -> int:
    letters = re.match(r"([A-Za-z]+)", ref).group(1).upper()
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_rows(path: str, sheet_index: int = 0) -> list[list]:
    z = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    sheets = sorted(n for n in z.namelist()
                    if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        return []
    root = ET.fromstring(z.read(sheets[min(sheet_index, len(sheets) - 1)]))
    out = []
    for row in root.iter(f"{_NS}row"):
        cells, maxc = {}, -1
        for c in row.findall(f"{_NS}c"):
            ref = c.get("r", "")
            idx = _col_to_idx(ref) if ref else 0
            t = c.get("t")
            v = c.find(f"{_NS}v")
            isn = c.find(f"{_NS}is")
            if t == "s" and v is not None and v.text is not None:
                i = int(v.text)
                val = shared[i] if 0 <= i < len(shared) else ""
            elif t == "inlineStr" and isn is not None:
                val = "".join(x.text or "" for x in isn.iter(f"{_NS}t"))
            elif v is not None:
                val = v.text
            else:
                val = ""
            cells[idx] = val.strip() if isinstance(val, str) else val
            maxc = max(maxc, idx)
        out.append([cells.get(i, "") for i in range(maxc + 1)])
    return out


def _csv_rows(path: str) -> list[list]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return [row for row in csv.reader(f)]


def _read_rows(path: str, sheet_index: int = 0) -> list[list]:
    p = path.lower()
    if p.endswith((".xlsx", ".xlsm")):
        return _xlsx_rows(path, sheet_index)
    if p.endswith((".csv", ".tsv", ".txt")):
        return _csv_rows(path)
    raise ValueError(f"Unsupported pricesheet type: {path} (use .xlsx or .csv)")


# ----------------------------------------------------------------- detection
def _detect_columns(rows: list[list]) -> tuple[int, dict]:
    """Return (header_row_index, {role: col_index}). Header row = the first row
    that yields both a 'cost' column and a sell price (map or msrp)."""
    best = None
    for ri, row in enumerate(rows[:25]):
        roles: dict[str, int] = {}
        for ci, cell in enumerate(row):
            h = _norm(cell)
            if not h:
                continue
            for role, phrases in _ROLE_RULES:
                if role in roles:
                    continue
                if any(ph in h for ph in phrases):
                    roles[role] = ci
                    break
        # cost fallback: a header containing 'cost' but not 'count'
        if "cost" not in roles:
            for ci, cell in enumerate(row):
                h = _norm(cell)
                if _COST_FALLBACK.search(h) and "count" not in h:
                    roles["cost"] = ci
                    break
        if "cost" in roles and ("map" in roles or "msrp" in roles) and "sku" in roles:
            return ri, roles
        if best is None and "cost" in roles:
            best = (ri, roles)
    if best is None:
        headers = rows[0] if rows else []
        raise ValueError(
            "Could not find a cost column in the pricesheet. Headers seen in row 1: "
            + ", ".join(str(h) for h in headers[:30]))
    ri, roles = best
    if "map" not in roles and "msrp" not in roles:
        raise ValueError(
            "Found a cost column but no sell price (MAP/MSRP). Columns detected: "
            + ", ".join(f"{k}=col{v}" for k, v in roles.items()))
    return ri, roles


# ----------------------------------------------------------------- public API
def parse_pricesheet(path: str, sheet_index: int = 0) -> tuple[list[dict], dict]:
    """Parse a pricesheet into per-SKU records. Returns (records, colmap).
    Each record: {sku, title, cost, map, msrp, size}."""
    rows = _read_rows(path, sheet_index)
    if not rows:
        raise ValueError(f"Pricesheet is empty: {path}")
    header_i, roles = _detect_columns(rows)

    def g(row, role):
        i = roles.get(role)
        return row[i] if i is not None and i < len(row) else ""

    records = []
    for row in rows[header_i + 1:]:
        sku = str(g(row, "sku")).strip()
        cost = money(g(row, "cost"))
        mapp = money(g(row, "map"))
        msrp = money(g(row, "msrp"))
        if not sku or cost is None or (mapp is None and msrp is None):
            continue
        size = str(g(row, "size")).strip()
        records.append({
            "sku": sku,
            "title": str(g(row, "title")).strip()[:80],
            "cost": cost,
            "map": mapp,
            "msrp": msrp,
            "size": size,
            "ship_weight": money(g(row, "ship_weight")),
            "xl": is_xl(size),
        })
    return records, roles


def shipping_cost_for(rec: dict, shipping_flat: float = 0.0,
                      shipping_per_lb: float = 0.0, xl_surcharge: float = 0.0) -> float:
    """Per-unit shipping COGS for one SKU, from the chosen model:
      flat per unit + per-lb * ship_weight + xl_surcharge (if the size is XL).
    All default 0.0, so with no shipping inputs this returns 0 (gross margin)."""
    cost = float(shipping_flat)
    if shipping_per_lb and rec.get("ship_weight"):
        cost += float(shipping_per_lb) * float(rec["ship_weight"])
    if xl_surcharge and rec.get("xl"):
        cost += float(xl_surcharge)
    return round(cost, 2)


def compute_margins(records: list[dict], sell_basis: str = "map",
                    shipping_flat: float = 0.0, shipping_per_lb: float = 0.0,
                    xl_surcharge: float = 0.0, ship_fn=None) -> dict:
    """Per-SKU NET margin (sell - cost - shipping)/sell on the chosen basis, with
    a summary. sell_basis 'map' falls back to msrp per-row when MAP is missing,
    and vice versa, so no priced row is dropped. Shipping defaults to 0 (then
    this is gross margin). Pass ship_fn(record)->float for a per-SKU freight model
    (e.g. a vendor rate card), or the flat/per_lb/xl params for a simple one."""
    alt = "msrp" if sell_basis == "map" else "map"
    per_sku = {}
    margins = []
    ships = []
    xl_count = 0
    for r in records:
        sell = r.get(sell_basis) or r.get(alt)
        if not sell or sell <= 0 or r["cost"] is None:
            continue
        ship = (round(float(ship_fn(r)), 2) if ship_fn is not None
                else shipping_cost_for(r, shipping_flat, shipping_per_lb, xl_surcharge))
        m = (sell - r["cost"] - ship) / sell
        if r.get("xl"):
            xl_count += 1
        per_sku[r["sku"]] = {"cost": r["cost"], "sell": round(sell, 2),
                             "shipping": ship, "margin": round(m, 4),
                             "size": r["size"], "xl": bool(r.get("xl"))}
        margins.append(m)
        ships.append(ship)
    summary = {"n": len(margins), "xl_skus": xl_count}
    if margins:
        margins_sorted = sorted(margins)
        summary.update({
            "mean": round(statistics.mean(margins), 4),
            "median": round(statistics.median(margins), 4),
            "min": round(margins_sorted[0], 4),
            "max": round(margins_sorted[-1], 4),
            "sell_basis": sell_basis,
            "shipping_model": {"flat": shipping_flat, "per_lb": shipping_per_lb,
                               "xl_surcharge": xl_surcharge},
            "avg_shipping": round(statistics.mean(ships), 2) if ships else 0.0,
        })
    return {"summary": summary, "skus": per_sku}


def recommended_line_margin(summary: dict) -> float | None:
    """The single number to feed a config product_line: the MEDIAN per-SKU gross
    margin (robust to outliers). None if nothing priced."""
    return summary.get("median")
