"""Institutional agent tests: never-invent-a-contact discipline, dedup,
CSV import aliasing, AMI parsing, ranking, and draft gating."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json as _json                                     # noqa: E402
from src.analysis import institutional as inst           # noqa: E402
from src.analysis import institutional_crawl as ic       # noqa: E402
from src.analysis.institutional_crawl import (           # noqa: E402
    parse_ami_state_page, _find_contact, _find_evidence)


def test_ami_json_extract_and_paginate(monkeypatch=None):
    # Two pages of the Squarespace collection; state filter keeps only GA/DC.
    def _item(title, addr, cats, site):
        return {"title": title, "fullUrl": "/school-locator1/x",
                "categories": cats, "location": {"addressLine2": addr},
                "body": f'School Administrator: Pat Lee 100 Main Street Email: '
                        f'p@{site.split("//")[1].rstrip("/")} '
                        f'<a href="{site}">site</a>'}
    page1 = {"items": [_item("Aidan Montessori", "Washington, DC, 20008",
                             ["Washington DC"], "http://aidanschool.org/"),
                       _item("Arbor Montessori", "Decatur, GA, 30030",
                             ["Georgia"], "https://arbormontessori.org")],
             "pagination": {"nextPage": True, "nextPageOffset": 999}}
    page2 = {"items": [_item("Bay Montessori", "Oakland, CA, 94601",
                             ["California"], "https://baymontessori.org")],
             "pagination": {"nextPage": False}}
    calls = []

    def fake_fetch(url, timeout=20):
        calls.append(url)
        return _json.dumps(page2 if "offset=999" in url else page1)

    orig = ic.fetch
    ic.fetch = fake_fetch
    try:
        prospects, note = ic.crawl_ami_schools({"GA", "DC"})
    finally:
        ic.fetch = orig
    names = sorted(p["school"] for p in prospects)
    assert names == ["Aidan Montessori", "Arbor Montessori"]      # CA filtered out
    assert len(calls) == 2                                         # both pages fetched
    ga = next(p for p in prospects if p["school"] == "Arbor Montessori")
    assert ga["state"] == "GA" and ga["website"] == "https://arbormontessori.org"
    assert ga["email"] == "p@arbormontessori.org" and ga["contact_confidence"] == "directory"
    assert ga["contact_name"] == "Pat Lee"


def test_ams_algolia_extract_and_filter():
    # Stub the Algolia call; verify state filtering, website from content,
    # and affiliation tier in the source.
    def _hit(title, state, site, tier, content_site=None):
        c = f'<a href="{content_site}">web</a>' if content_site else ""
        return {"post_title": title, "address_state_code": state, "pathway": tier,
                "permalink": f"https://amshq.org/schools/{title.lower().replace(' ','-')}/",
                "address": f"1 Main St, {state}", "content": c}
    page0 = {"hits": [
        _hit("Arbor Montessori", "GA", None, "ACCREDITATED",
             content_site="https://arbormontessori.org"),
        _hit("Yakima Montessori", "WA", None, "MEMBERSHIP",
             content_site="https://yakima-m.org"),
    ], "nbHits": 2, "nbPages": 1}

    orig = ic._algolia_query
    ic._algolia_query = lambda params, timeout=25: page0
    try:
        prospects, note = ic.crawl_ams_schools({"GA"})
    finally:
        ic._algolia_query = orig
    assert [p["school"] for p in prospects] == ["Arbor Montessori"]   # WA filtered out
    p = prospects[0]
    assert p["state"] == "GA" and p["website"] == "https://arbormontessori.org"
    assert "accreditated" in p["source"].lower() and "AMS" in p["source"]


def test_dedup_by_domain_and_name():
    store = {"prospects": [], "sources": {}, "settings": {}, "runs": []}
    a = inst.new_prospect("Casa Montessori", "montessori_school", "GA",
                          "test", "https://www.casamontessori.org/")
    b = inst.new_prospect("The Casa Montessori School", "montessori_school",
                          "GA", "test", "https://casamontessori.org/about")
    c = inst.new_prospect("Casa Montessori", "montessori_school", "GA", "test")
    added, dupes = inst.add_prospects(store, [a, b, c])
    # a and b share a domain; c matches a's normalized name+state? c has no
    # website so it keys by name — 'Casa Montessori'|GA vs a's dom key: c adds.
    assert len(added) == 2 and dupes == 1


def test_suppressed_never_resurfaces():
    store = {"prospects": [], "sources": {}, "settings": {}, "runs": []}
    p = inst.new_prospect("Sunny Kids", "childcare_center", "FL", "test",
                          "https://sunnykids.com")
    p["status"] = "opted_out"
    store["prospects"].append(p)
    again = inst.new_prospect("Sunny Kids", "childcare_center", "FL", "test2",
                              "https://www.sunnykids.com/")
    added, dupes = inst.add_prospects(store, [again])
    assert added == [] and dupes == 1


def test_import_csv_aliases_and_notes():
    store = {"prospects": [], "sources": {"GA": {}}, "settings": {}, "runs": []}
    csv_text = ("Facility Name,City,County,Phone,Capacity\n"
                "Little Sprouts Learning Center,Macon,Bibb,478-555-1000,120\n"
                "Bright Beginnings,Atlanta,Fulton,404-555-2000,80\n"
                ",Atlanta,Fulton,,\n")
    added, dupes, total, err = inst.import_csv(store, csv_text, "GA", "test:GA")
    assert err is None and added == 2 and total == 3
    p = store["prospects"][0]
    assert p["org_type"] == "childcare_center" and p["southeast"] is True
    assert "City: Macon" in p["notes"] and "Capacity: 120" in p["notes"]
    assert store["sources"]["GA"]["verified"] is True


def test_export_roundtrip_columns():
    store = {"prospects": [inst.new_prospect("A School", "montessori_school",
                                             "GA", "t", "https://a.org")],
             "sources": {}, "settings": {}, "runs": []}
    out = inst.export_csv(store)
    head = out.splitlines()[0]
    for col in inst.CSV_COLUMNS:
        assert col in head, col


def test_rank_southeast_montessori_first():
    rows = [inst.new_prospect("Z Daycare", "childcare_center", "GA", "t", "https://z.com"),
            inst.new_prospect("A Museum", "museum_retail", "NY", "t", "https://a.com"),
            inst.new_prospect("M School", "montessori_school", "FL", "t", "https://m.com"),
            inst.new_prospect("N School", "montessori_school", "OH", "t", "https://n.com")]
    ranked = sorted(rows, key=inst.rank_key)
    assert [r["school"] for r in ranked] == \
        ["M School", "Z Daycare", "N School", "A Museum"]


def test_parse_ami_headings():
    html = """
    <h3>Casa dei Bambini Atlanta</h3><p>AMI recognized.</p>
    <a href="https://casadeibambini-atl.org">Visit website</a>
    <h3>Follow us</h3><a href="https://facebook.com/x">Facebook</a>
    <h3>Peachtree Montessori School</h3>
    <a href="https://peachtreemontessori.com">site</a>
    """
    pairs = parse_ami_state_page(html)
    names = [n for n, _ in pairs]
    assert "Casa dei Bambini Atlanta" in names
    assert "Peachtree Montessori School" in names
    assert all("facebook" not in u for _, u in pairs)


def test_parse_ami_anchor_fallback():
    html = ('<li><a href="https://oakmontessori.org">Oak Montessori School'
            '</a></li><li><a href="https://facebook.com/oak">Oak on FB</a></li>')
    pairs = parse_ami_state_page(html)
    assert pairs == [("Oak Montessori School", "https://oakmontessori.org")]


def test_contact_extraction():
    text = ("Welcome to Oak Montessori. Jane Q. Smith, Head of School, "
            "welcomes you. Reach us at office@oakmontessori.org or "
            "img@2x.png nonsense, also spam@sentry.wixpress.com.")
    name, title, email = _find_contact(text, "oakmontessori.org")
    assert email == "office@oakmontessori.org"
    assert name == "Jane Q. Smith" and title == "Head of School"


def test_contact_title_first_order():
    text = "Our Director: Maria Lopez leads the toddler program."
    name, title, _ = _find_contact(text, "")
    assert name == "Maria Lopez" and title == "Director"


def test_evidence_is_specific_or_absent():
    good = ("We opened our doors in autumn. Our toddler and primary programs "
            "have been AMI accredited since 1998 in downtown Decatur. "
            "This site uses cookies to improve montessori experience.")
    ev = _find_evidence(good, "https://x.org/about")
    assert ev and "AMI accredited since 1998" in ev["fact"]
    assert _find_evidence("Buy now! Great deals await you here today.", "u") is None


def test_draft_gating_refusals():
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from web.app import _inst_generate_draft
    p = inst.new_prospect("A School", "montessori_school", "GA", "t", "https://a.org")
    # 1) no mailing address
    d, err = _inst_generate_draft(p, {}, {})
    assert d is None and "mailing address" in err
    # 2) address set but no verified contact
    d, err = _inst_generate_draft(p, {"mailing_address": "1 Main St"}, {})
    assert d is None and "verified" in err.lower()
    # 3) verified contact but no evidence
    p["email"], p["contact_confidence"] = "x@a.org", "verified"
    d, err = _inst_generate_draft(p, {"mailing_address": "1 Main St"}, {})
    assert d is None and "evidence" in err.lower()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
