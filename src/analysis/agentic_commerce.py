"""Agentic Commerce Readiness — evaluate the Magento catalog as machine-
consumable commerce data for AI shopping agents.

PURE and testable: it takes raw Magento product dicts (+ optional storefront /
schema / feed surface values) and produces normalized Agentic Product Records,
completeness/consistency findings, a 0–100 readiness score, agent-query
resolutions, and Governor opportunities.

Product-truth-first: it NEVER invents a fact. A missing field is reported as a
gap ('needs human data'), never filled with a plausible guess. Where sources
conflict it reports the conflict rather than picking the marketing value.
"""

import re
from urllib.parse import urlparse

# ── Agent-critical concepts and how to detect this store's attribute for each.
# `patterns` match against a product attribute's code OR label (case-insensitive
# substring). `critical` marks a fact an agent needs to filter/recommend/buy.
# `never_invent` fields are only ever read, never proposed from inference.
CONCEPTS = {
    "brand":          {"patterns": ["brand", "manufacturer"], "domain": "identity", "critical": False},
    "gtin":           {"patterns": ["gtin", "upc", "ean", "barcode"], "domain": "identifiers", "critical": True, "never_invent": True},
    "mpn":            {"patterns": ["mpn", "model_number", "manufacturer_part"], "domain": "identifiers", "critical": False, "never_invent": True},
    "age":            {"patterns": ["age", "min_age", "age_range", "recommended_age", "suitable_age", "age_group"], "domain": "audience", "critical": True},
    "learning_skill": {"patterns": ["skill", "learning", "developmental", "educational_focus", "develops", "montessori_skill"], "domain": "learning_intent", "critical": True},
    "material":       {"patterns": ["material", "made_of", "composition", "wood_type"], "domain": "physical", "critical": True},
    "battery":        {"patterns": ["battery", "batteries", "requires_batter", "battery_required"], "domain": "physical", "critical": False},
    "dimensions":     {"patterns": ["dimension", "length", "width", "height", "product_size"], "domain": "physical", "critical": False},
    "color":          {"patterns": ["color", "colour"], "domain": "physical", "critical": False},
    "piece_count":    {"patterns": ["piece", "pieces", "quantity_in_set", "number_of_pieces"], "domain": "physical", "critical": False},
    "personalization": {"patterns": ["personaliz", "personalis", "custom_name", "customizable", "monogram", "name_options"], "domain": "personalization", "critical": True},
    "lead_time":      {"patterns": ["lead_time", "production_time", "ships_in", "dispatch", "made_to_order", "processing_time", "handling_time"], "domain": "fulfillment", "critical": True},
}
# Concepts that are agent-critical, for scoring and prioritization.
CRITICAL = [k for k, v in CONCEPTS.items() if v.get("critical")]

# Readiness-score component weights (spec §8).
SCORE_WEIGHTS = {
    "identity_identifiers": 0.15,
    "commerce_accuracy": 0.15,
    "attribute_completeness": 0.20,
    "audience_learning": 0.15,
    "personalization_fulfillment": 0.15,
    "structured_feed_consistency": 0.15,
    "media_url": 0.05,
}


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _ca(product):
    return {a.get("attribute_code"): a.get("value")
            for a in (product.get("custom_attributes") or []) if a.get("attribute_code")}


def concept_code_index(attr_meta):
    """{concept: [attribute_code,…]} — this store's attribute codes that match
    each concept, by code or label. attr_meta = {code: {'label',…}}."""
    idx = {}
    for concept, spec in CONCEPTS.items():
        hits = []
        for code, meta in (attr_meta or {}).items():
            hay = (code + " " + (meta.get("label") or "")).lower()
            if any(p in hay for p in spec["patterns"]):
                hits.append(code)
        idx[concept] = hits
    return idx


def _opt_label(meta_for_code, value):
    """Map a select attribute's option id to its label when available."""
    opts = (meta_for_code or {}).get("options") or {}
    if isinstance(value, (list, tuple)):
        return ", ".join(opts.get(str(v), str(v)) for v in value)
    return opts.get(str(value), value)


def _image_url(product, ca, media_base):
    entries = product.get("media_gallery_entries") or []
    path = ""
    if entries:
        path = entries[0].get("file") or ""
    if not path:
        path = ca.get("image") or ca.get("small_image") or ""
    if path and media_base:
        return media_base.rstrip("/") + path
    return path or ""


def build_record(product, attr_meta=None, media_base="", base_url=""):
    """Normalize one Magento product into an Agentic Product Record with per-
    field provenance. Missing agent-critical fields are listed, never filled."""
    attr_meta = attr_meta or {}
    ca = _ca(product)
    idx = concept_code_index(attr_meta)
    stock = (product.get("extension_attributes") or {}).get("stock_item") or {}
    url_key = ca.get("url_key") or ""
    canonical = ""
    if base_url and url_key:
        canonical = base_url.rstrip("/") + "/" + url_key + ".html"

    is_custom = bool(product.get("options")) or _norm(ca.get("has_options")) in ("1", "true", "yes")

    rec = {
        "sku": product.get("sku", ""),
        "name": product.get("name", ""),
        "type_id": product.get("type_id", ""),
        "status": product.get("status"),
        "canonical_url": canonical,
        "url_key": url_key,
        "price": product.get("price"),
        "special_price": ca.get("special_price"),
        "in_stock": stock.get("is_in_stock"),
        "qty": stock.get("qty"),
        "image": _image_url(product, ca, media_base),
        "is_customizable": is_custom,
        "fields": {},       # concept -> value (or "")
        "provenance": {},   # concept -> source string
        "missing": [],      # concept keys with no value
        "missing_critical": [],
    }
    for concept, spec in CONCEPTS.items():
        val, src = "", ""
        for code in idx.get(concept, []):
            v = ca.get(code) or product.get(code)
            if v not in (None, "", []):
                val = _opt_label(attr_meta.get(code), v)
                src = f"magento:{code}"
                break
        # personalization is proven by the product actually having options
        if concept == "personalization" and not val and is_custom:
            val, src = "customizable (has options)", "magento:options"
        rec["fields"][concept] = val
        rec["provenance"][concept] = src
        if not val:
            rec["missing"].append(concept)
            if spec.get("critical"):
                rec["missing_critical"].append(concept)
    return rec


# ── Completeness + score ─────────────────────────────────────────────────
def _hard_fails(rec, consistency=None):
    """Facts an agent must trust — if wrong, they override any score. §8."""
    fails = []
    if rec.get("price") in (None, "", 0):
        fails.append("no price")
    if rec.get("in_stock") is None:
        fails.append("no availability signal")
    if not rec.get("canonical_url"):
        fails.append("no canonical URL")
    for c in (consistency or []):
        if c.get("field") in ("price", "availability") and c.get("severity") == "high":
            fails.append(f"{c['field']} conflicts across surfaces")
    return fails


def readiness_score(rec, consistency=None, schema=None, feed=None):
    """0–100 diagnostic (prioritization only — NOT a claim about ranking in any
    AI platform). Hard-fail flags are returned alongside and override it."""
    have = lambda *cs: sum(1 for c in cs if rec["fields"].get(c)) / max(1, len(cs))  # noqa: E731
    comp = {
        "identity_identifiers": 0.5 * (1 if rec.get("name") else 0) + 0.5 * have("brand", "gtin"),
        "commerce_accuracy": (0.5 if rec.get("price") not in (None, "", 0) else 0)
                             + (0.5 if rec.get("in_stock") is not None else 0),
        "attribute_completeness": have("material", "dimensions", "battery", "piece_count", "color"),
        "audience_learning": have("age", "learning_skill"),
        "personalization_fulfillment": have("personalization", "lead_time"),
        "structured_feed_consistency": _surface_component(consistency, schema, feed),
        "media_url": 0.5 * (1 if rec.get("image") else 0) + 0.5 * (1 if rec.get("canonical_url") else 0),
    }
    score = round(100 * sum(SCORE_WEIGHTS[k] * v for k, v in comp.items()))
    fails = _hard_fails(rec, consistency)
    return {"score": score, "components": {k: round(v, 2) for k, v in comp.items()},
            "hard_fails": fails}


def _surface_component(consistency, schema, feed):
    if schema is None and feed is None and consistency is None:
        return 0.0  # surfaces not audited yet
    conflicts = len([c for c in (consistency or []) if c.get("severity") in ("high", "medium")])
    has_schema = 1.0 if (schema and schema.get("has_product")) else 0.0
    in_feed = 1.0 if (feed and feed.get("present")) else 0.0
    penalty = min(1.0, conflicts * 0.34)
    return max(0.0, (0.5 * has_schema + 0.5 * in_feed) - penalty)


# ── Cross-surface consistency ────────────────────────────────────────────
def consistency_audit(rec, storefront=None, schema=None, feed=None):
    """Compare Magento's value against the other surfaces for the fields that
    must agree. Each surface arg is a dict of {field: value} or None."""
    out = []
    surfaces = {"storefront": storefront or {}, "schema": schema or {}, "feed": feed or {}}
    checks = {
        "name": rec.get("name"),
        "price": rec.get("price"),
        "availability": rec.get("in_stock"),
        "canonical_url": rec.get("canonical_url"),
        "image": rec.get("image"),
        "brand": rec["fields"].get("brand"),
        "gtin": rec["fields"].get("gtin"),
    }
    for field, mag in checks.items():
        if mag in (None, "", []):
            continue
        for sname, svals in surfaces.items():
            if field not in svals or svals[field] in (None, "", []):
                continue
            if not _values_match(field, mag, svals[field]):
                out.append({"field": field, "surface": sname,
                            "magento": mag, "surface_value": svals[field],
                            "severity": "high" if field in ("price", "availability", "canonical_url") else "medium"})
    return out


def _values_match(field, a, b):
    if field == "price":
        try:
            return abs(float(a) - float(b)) < 0.01
        except (TypeError, ValueError):
            return _norm(a) == _norm(b)
    if field in ("canonical_url", "image"):
        return _norm(a).rstrip("/") == _norm(b).rstrip("/")
    if field == "availability":
        return _avail(a) == _avail(b)
    return _norm(a) == _norm(b)


def _avail(v):
    s = _norm(v)
    if s in ("1", "true", "in stock", "instock", "yes", "http://schema.org/instock", "https://schema.org/instock"):
        return "in"
    if s in ("0", "false", "out of stock", "outofstock", "no"):
        return "out"
    return s


# ── Agent query resolvability ────────────────────────────────────────────
_AGE_RE = re.compile(r"(\d+)\s*[- ]?\s*year", re.I)
_PRICE_RE = re.compile(r"(?:under|below|less than|<)\s*\$?\s*(\d+)", re.I)
_SKILL_WORDS = {"fine motor": "fine motor", "letter recognition": "letter recognition",
                "letter": "letter recognition", "sorting": "sorting", "spatial": "spatial reasoning",
                "concentration": "concentration", "problem solving": "problem solving",
                "sensory": "sensory exploration", "counting": "counting", "reading": "reading"}


def parse_agent_query(text):
    """Natural-language shopping request → explicit constraints. Only what's
    stated is extracted (no inference)."""
    t = _norm(text)
    c = {"raw": text}
    m = _AGE_RE.search(t)
    if m:
        c["age_years"] = int(m.group(1))
    m = _PRICE_RE.search(t)
    if m:
        c["max_price"] = float(m.group(1))
    if any(w in t for w in ("personalized", "personalised", "custom", "name", "monogram")):
        c["personalized"] = True
    if "no batter" in t or "without batter" in t or "does not require batter" in t or "battery-free" in t:
        c["no_battery"] = True
    if any(w in t for w in ("wooden", "wood")):
        c["material"] = "wood"
    if "montessori" in t:
        c["montessori"] = True
    for k, v in _SKILL_WORDS.items():
        if k in t:
            c.setdefault("skills", []).append(v)
    m = re.search(r"before\s+([a-z]+\s+\d{1,2})", t)
    if m:
        c["deliver_by"] = m.group(1)
    return c


def resolve_query(constraints, records):
    """Return which constraints the catalog can resolve and candidate products.
    A constraint is 'unknown' when the products lack the structured field to
    decide it — the honest answer, not a false match."""
    resolvable, unknown = {}, {}
    cand = []
    for rec in records:
        matched, blocked = [], []
        for key, want in constraints.items():
            if key == "raw":
                continue
            ok, known = _constraint_ok(key, want, rec)
            if not known:
                blocked.append(key)
            elif ok:
                matched.append(key)
            else:
                blocked.append(key)
        # a candidate must not violate any KNOWN constraint
        if not any(k for k in blocked if _constraint_known(k, rec)):
            cand.append({"sku": rec["sku"], "name": rec["name"],
                         "matched": matched, "unknown": [b for b in blocked if not _constraint_known(b, rec)]})
    for key in constraints:
        if key == "raw":
            continue
        known_any = any(_constraint_known(key, r) for r in records)
        (resolvable if known_any else unknown)[key] = known_any
    cand.sort(key=lambda x: (-len(x["matched"]), len(x["unknown"])))
    return {"constraints": {k: v for k, v in constraints.items() if k != "raw"},
            "resolvable": list(resolvable), "unknown": list(unknown),
            "candidates": cand[:10]}


def _constraint_known(key, rec):
    m = {"age_years": "age", "material": "material", "no_battery": "battery",
         "personalized": "personalization", "skills": "learning_skill",
         "montessori": "learning_skill", "deliver_by": "lead_time"}
    if key == "max_price":
        return rec.get("price") not in (None, "", 0)
    concept = m.get(key)
    return bool(rec["fields"].get(concept)) if concept else False


def _constraint_ok(key, want, rec):
    """(ok, known). known=False when the product lacks the field to judge."""
    if key == "max_price":
        p = rec.get("price")
        if p in (None, "", 0):
            return False, False
        try:
            return float(p) <= float(want), True
        except (TypeError, ValueError):
            return False, False
    if not _constraint_known(key, rec):
        return False, False
    f = rec["fields"]
    if key == "age_years":
        nums = [int(n) for n in re.findall(r"\d+", str(f.get("age")))]
        if not nums:
            return False, False
        return (min(nums) <= want <= (max(nums) if len(nums) > 1 else 99)), True
    if key == "material":
        got = _norm(f.get("material"))
        want_n = _norm(want)
        if want_n == "wood":
            woods = ("wood", "maple", "oak", "birch", "pine", "beech",
                     "walnut", "bamboo", "plywood", "cherry", "poplar")
            return (any(sp in got for sp in woods)), True
        return want_n in got, True
    if key == "no_battery":
        return "no" in _norm(f.get("battery")) or "false" in _norm(f.get("battery")) or "0" == _norm(f.get("battery")), True
    if key == "personalized":
        return bool(f.get("personalization")), True
    if key in ("skills",):
        got = _norm(f.get("learning_skill"))
        return any(_norm(s) in got for s in want), True
    if key == "montessori":
        return "montessori" in _norm(f.get("learning_skill")) or "montessori" in _norm(rec.get("name")), True
    if key == "deliver_by":
        return True, True  # lead_time exists; agent can compute — mark resolvable
    return False, True


# ── Opportunities ────────────────────────────────────────────────────────
def opportunities(rec, consistency=None, demand=None):
    """One Governor opportunity per gap/conflict. Missing critical fields →
    'needs human data' (never a fabricated value). Conflicts → report both
    surface values. `demand` = {impressions, revenue} for prioritization."""
    demand = demand or {}
    out = []
    for c in rec.get("missing_critical", []):
        never = CONCEPTS[c].get("never_invent")
        out.append({
            "sku": rec["sku"], "url": rec.get("canonical_url"), "field": c,
            "type": "missing_critical", "severity": "high",
            "observed": "(absent)",
            "why": f"An AI agent can't filter/recommend on '{c}' without it.",
            "proposal": "needs human data" if never else
                        f"Add a structured '{c}' attribute in Magento (from authoritative product data).",
            "auto_eligible": False,
            "evidence": rec["provenance"].get(c, "magento (attribute absent)"),
            "demand": demand,
        })
    for conf in (consistency or []):
        out.append({
            "sku": rec["sku"], "url": rec.get("canonical_url"),
            "field": conf["field"], "type": "surface_conflict",
            "severity": conf.get("severity", "medium"),
            "observed": f"magento={conf['magento']} vs {conf['surface']}={conf['surface_value']}",
            "why": f"'{conf['field']}' disagrees across surfaces — agents/Google may distrust or mis-state it.",
            "proposal": f"Reconcile {conf['surface']} to the authoritative Magento value.",
            "auto_eligible": conf["field"] not in ("price", "availability"),  # never auto price/stock
            "evidence": f"{conf['surface']} surface snapshot vs Magento",
            "demand": demand,
        })
    return out


def priority(opp):
    """Priority = Commercial × Demand × AgentImpact × Confidence ÷ Risk, using
    only the signals actually present (never a fabricated margin)."""
    d = opp.get("demand") or {}
    commercial = 1 + (d.get("revenue", 0) or 0) / 100.0
    demand = 1 + (d.get("impressions", 0) or 0) / 500.0
    agent_impact = {"high": 3, "medium": 2, "low": 1}.get(opp.get("severity"), 1)
    confidence = 1.0 if opp.get("type") == "surface_conflict" else 0.7
    risk = 2.0 if not opp.get("auto_eligible") else 1.0
    return round(commercial * demand * agent_impact * confidence / risk, 2)
