"""Carpets for Kids freight rate card (2026 Seasonal Guide, valid Sept 28 2026 -
Jan 17 2027). Turns a rug's size into a shipping COGS so margins reflect freight,
which for rugs is a large share of cost.

Shipping is a SIZE-CLASS x DESTINATION-ZONE matrix. Size class comes from the
vendor's sizing chart; zone is the customer's region (1 nearest GA .. 7 West
Coast). X-Large adds a temporary +$60 parcel surcharge during the window.

Source: Shipping_Guide_2026_Seasonal_Surcharge (FOB West Point, GA).
"""
from __future__ import annotations

import re

# 2026 pricing by size, Zones 1..7 (index 0..6).
RATES = {
    "kit":    [18.95, 20.95, 21.95, 23.95, 27.95, 29.95, 33.95],
    "small":  [25.95, 31.95, 34.95, 39.95, 43.95, 49.95, 55.95],
    "medium": [32.95, 36.95, 39.95, 43.95, 49.95, 52.95, 59.95],
    "large":  [39.95, 47.95, 49.95, 56.95, 63.95, 69.95, 75.95],
    "xlarge": [106.95, 116.95, 121.95, 135.95, 145.95, 155.95, 162.95],
}
XL_SEASONAL_SURCHARGE = 60.0   # per XL piece, Sept 28 2026 - Jan 17 2027

# Sizing chart -> class, as (min_ft, max_ft) canonical pairs.
_CHART = {
    "kit":    [(2.667, 4), (3, 4), (3, 5), (3, 4.5), (3, 12), (3.833, 5.417)],
    "small":  [(4.083, 5.833), (4.417, 5.833), (4, 6), (4, 8), (6, 6)],
    "medium": [(5.417, 7.667), (5.833, 8.333), (5.833, 11.667), (6, 9),
               (6.75, 9.417), (6, 10), (6, 12)],
    "large":  [(6.667, 13.333), (7, 12), (7.5, 12), (7.667, 10.833)],
    "xlarge": [(8, 12), (8.25, 11.667), (8.333, 11.667), (8.333, 12), (8.333, 13.333)],
}
# "10' x 13'6"" and bigger are LTL-only (freight quote), not in the parcel card.
_LTL = [(10, 13.5)]

_DIM_RE = re.compile(r"(\d+)\s*(?:ft|')\s*(?:(\d+)\s*(?:in|\"))?", re.I)


def _dims_feet(size: str) -> list[float]:
    out = []
    for ft, inch in _DIM_RE.findall(size or ""):
        out.append(int(ft) + (int(inch) / 12.0 if inch else 0.0))
    return out


def classify_size(size: str) -> tuple[str, bool]:
    """Return (size_class, exact_match). Squares/kits and unparseable sizes fall
    back to 'kit'. Non-exact matches pick the nearest chart size and flag it so
    uncertainty is visible, never hidden."""
    s = (size or "").lower()
    d = _dims_feet(s)
    if len(d) < 2:
        # squares, kits, runners written oddly -> smallest/cheapest class
        return "kit", ("square" in s or "kit" in s)
    lo, hi = min(d[0], d[1]), max(d[0], d[1])
    # LTL-only sizes: treat as xlarge for a parcel estimate but they really need a quote
    for (a, b) in _LTL:
        if abs(lo - a) <= 0.3 and abs(hi - b) <= 0.3:
            return "xlarge", False
    best_cls, best_dist = None, 1e9
    for cls, pairs in _CHART.items():
        for (a, b) in pairs:
            dist = abs(lo - a) + abs(hi - b)
            if dist < best_dist:
                best_cls, best_dist = cls, dist
    return best_cls or "medium", best_dist <= 0.2


def ship_cost(size: str, zone: str | int = "avg", season_surcharge: bool = True) -> float:
    """Freight COGS for a rug of this size. zone is 1..7 or 'avg' (mean across
    zones, a reasonable default when the destination mix is unknown)."""
    cls, _exact = classify_size(size)
    rates = RATES[cls]
    if zone == "avg" or zone is None:
        base = sum(rates) / len(rates)
    else:
        z = max(1, min(7, int(zone)))
        base = rates[z - 1]
    if cls == "xlarge" and season_surcharge:
        base += XL_SEASONAL_SURCHARGE
    return round(base, 2)


def make_ship_fn(zone: str | int = "avg", season_surcharge: bool = True):
    """Build a ship_fn(record)->float for compute_margins, using the record's
    'size' string."""
    def _fn(rec: dict) -> float:
        return ship_cost(rec.get("size", ""), zone=zone, season_surcharge=season_surcharge)
    return _fn
