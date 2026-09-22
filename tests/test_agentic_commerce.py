"""Agentic Commerce Readiness: record normalization (never invents), concept
detection, readiness score + hard-fails, cross-surface consistency, agent-query
resolvability, and opportunity generation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis import agentic_commerce as ac  # noqa: E402

ATTR_META = {
    "material": {"label": "Material", "input": "text", "options": {}},
    "recommended_age": {"label": "Recommended Age", "input": "select",
                        "options": {"12": "3-5 years", "13": "5-8 years"}},
    "learning_focus": {"label": "Learning Focus", "input": "multiselect",
                       "options": {"20": "fine motor", "21": "letter recognition"}},
    "brand": {"label": "Brand", "input": "select", "options": {"5": "Alphabet Trains"}},
    "meta_title": {"label": "Meta Title", "input": "text", "options": {}},
}


def _product(**over):
    p = {"sku": "WNT-01", "name": "Personalized Wooden Name Train",
         "type_id": "simple", "status": 1, "price": 59.0,
         "extension_attributes": {"stock_item": {"is_in_stock": True, "qty": 20}},
         "media_gallery_entries": [{"file": "/w/n/wnt01.jpg"}],
         "options": [{"title": "Name"}],  # customizable
         "custom_attributes": [
             {"attribute_code": "url_key", "value": "wooden-name-train"},
             {"attribute_code": "material", "value": "Solid maple"},
             {"attribute_code": "recommended_age", "value": "12"},
             {"attribute_code": "learning_focus", "value": ["20", "21"]},
             {"attribute_code": "brand", "value": "5"},
         ]}
    p.update(over)
    return p


def test_build_record_maps_concepts_and_never_invents():
    rec = ac.build_record(_product(), ATTR_META, media_base="https://x.com/media/catalog/product",
                          base_url="https://x.com")
    assert rec["sku"] == "WNT-01" and rec["price"] == 59.0 and rec["in_stock"] is True
    assert rec["canonical_url"] == "https://x.com/wooden-name-train.html"
    assert rec["image"] == "https://x.com/media/catalog/product/w/n/wnt01.jpg"
    # select/multiselect option ids resolved to labels
    assert rec["fields"]["age"] == "3-5 years"
    assert rec["fields"]["learning_skill"] == "fine motor, letter recognition"
    assert rec["fields"]["material"] == "Solid maple"
    assert rec["fields"]["personalization"].startswith("customizable")  # from options
    # GTIN not present → listed as a missing CRITICAL gap, never filled
    assert rec["fields"]["gtin"] == "" and "gtin" in rec["missing_critical"]


def test_score_and_hard_fails():
    rec = ac.build_record(_product(), ATTR_META, base_url="https://x.com")
    s = ac.readiness_score(rec)
    assert 0 <= s["score"] <= 100
    assert s["hard_fails"] == []            # has price, stock, canonical
    # break commerce truth → hard fail regardless of other completeness
    rec2 = ac.build_record(_product(price=None,
                           extension_attributes={"stock_item": {}}), ATTR_META, base_url="https://x.com")
    s2 = ac.readiness_score(rec2)
    assert "no price" in s2["hard_fails"] and "no availability signal" in s2["hard_fails"]


def test_consistency_reports_conflict_not_a_pick():
    rec = ac.build_record(_product(), ATTR_META, base_url="https://x.com")
    conf = ac.consistency_audit(rec, feed={"price": 75.0, "availability": "out of stock"})
    fields = {c["field"]: c for c in conf}
    assert fields["price"]["severity"] == "high"
    assert fields["price"]["magento"] == 59.0 and fields["price"]["surface_value"] == 75.0
    assert fields["availability"]["severity"] == "high"


def test_parse_and_resolve_agent_query():
    q = ac.parse_agent_query("Find a personalized wooden train for a 4-year-old named Oliver under $70")
    assert q["age_years"] == 4 and q["max_price"] == 70.0 and q["personalized"] is True
    assert q["material"] == "wood"
    rec = ac.build_record(_product(), ATTR_META, base_url="https://x.com")
    res = ac.resolve_query(q, [rec])
    # price + personalization + material + age all resolvable from structured data
    assert "max_price" in res["resolvable"] and "personalized" in res["resolvable"]
    assert res["candidates"] and res["candidates"][0]["sku"] == "WNT-01"


def test_resolve_marks_unknown_when_field_absent():
    # a product with no age attribute → the age constraint is UNKNOWN, not a match
    bare = _product(custom_attributes=[{"attribute_code": "url_key", "value": "x"}])
    rec = ac.build_record(bare, ATTR_META, base_url="https://x.com")
    q = {"age_years": 4, "no_battery": True}
    res = ac.resolve_query(q, [rec])
    assert "age_years" in res["unknown"] and "no_battery" in res["unknown"]


def test_opportunities_needs_human_data_for_identifiers():
    rec = ac.build_record(_product(), ATTR_META, base_url="https://x.com")
    opps = ac.opportunities(rec, demand={"impressions": 500, "revenue": 200})
    gtin = next(o for o in opps if o["field"] == "gtin")
    assert gtin["proposal"] == "needs human data" and gtin["auto_eligible"] is False
    # a surface conflict opportunity is proposable but never auto for price
    conf = ac.consistency_audit(rec, feed={"price": 80.0})
    copps = ac.opportunities(rec, consistency=conf)
    price_opp = next(o for o in copps if o["field"] == "price")
    assert price_opp["type"] == "surface_conflict" and price_opp["auto_eligible"] is False
    assert ac.priority(price_opp) > 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
