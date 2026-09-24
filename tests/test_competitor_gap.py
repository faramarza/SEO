import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.analysis import competitor_gap as cg


def _mine_thin(q):
    return cg.profile({"url": "https://x.com/cat.html", "title": "Toys | X", "h1": "Toys",
                       "word_count": 110, "headings": [], "schema_types": ["Product", "ItemList"],
                       "content_preview": "shop toys", "internal_links": 40}, q)


def _guide(url, q):
    return cg.profile({"url": url, "title": f"Best {q} (2026 Guide)", "h1": f"Best {q}",
                       "word_count": 2000, "headings": ["a", "b", "c", "d"],
                       "schema_types": ["Article", "FAQPage"],
                       "content_preview": f"the best {q} are", "internal_links": 15}, q)


def test_thin_category_vs_guides_flags_page_type_and_depth():
    q = "montessori toys for 2 year olds"
    r = cg.analyze(_mine_thin(q), [_guide("https://a.com/g", q), _guide("https://b.com/g", q)], q)
    assert r["available"]
    titles = [g["title"] for g in r["gaps"]]
    assert any("product grid" in t for t in titles)          # page-type/intent gap
    assert any("Far less content" in t for t in titles)      # depth gap
    assert r["gaps"][0]["severity"] == "high"                # a high-sev gap leads


def test_no_competitors_is_not_available():
    r = cg.analyze(_mine_thin("x"), [], "x")
    assert r["available"] is False


def test_parity_page_yields_offpage_verdict():
    q = "wooden name train"
    strong = cg.profile({"url": "https://x.com/p", "title": f"Wooden Name Train", "h1": f"Wooden Name Train",
                         "word_count": 1900, "headings": ["a", "b", "c", "d"], "schema_types": ["Product"],
                         "content_preview": f"wooden name train ...", "internal_links": 20}, q)
    comp = cg.profile({"url": "https://c.com/p", "title": "Wooden Name Train", "h1": "Wooden Name Train",
                       "word_count": 1850, "headings": ["a", "b", "c", "d"], "schema_types": ["Product"],
                       "content_preview": "wooden name train ...", "internal_links": 18}, q)
    r = cg.analyze(strong, [comp], q)
    assert not r["gaps"]
    assert "off-page" in r["verdict"].lower()
