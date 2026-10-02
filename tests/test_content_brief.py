"""Tests for the content-creator brief + output contract (Phase 4 backbone).

Verifies the brief is GROUNDED — it only references real pages/links from the
cluster, requires the cluster interlinks, holds the Magento block contract, and
that verification catches a missing required link. No network, no LLM.
"""
from src.analysis.content_brief import (
    build_brief, build_generation_messages, verify_interlinks,
    OUTPUT_BLOCKS, EXTERNAL_AUTHORITY_CANDIDATES,
)

BASE = "https://alphabet-trains.com"
FAMILIES = ["name trains", "step stools"]


def _page(url, atype, title, outlinks=None, impr=0):
    return {
        "url": url, "asset_type": atype, "gsc_impressions": impr, "gsc_clicks": 0,
        "page_metadata": {"title": title, "h1": title,
                          "internal_outlinks": [{"target_url": t, "anchor_text": a, "location": "body"}
                                                for (t, a) in (outlinks or [])]},
    }


def _results():
    return [
        _page(f"{BASE}/name-trains.html", "category", "Personalized Name Trains",
              outlinks=[(f"{BASE}/personalized-name-train.html", "shop")], impr=5000),
        _page(f"{BASE}/personalized-name-train.html", "product", "Personalized Name Train",
              outlinks=[(f"{BASE}/name-trains.html", "name trains")], impr=800),
        _page(f"{BASE}/wooden-name-train.html", "product", "Wooden Name Train", impr=400),
        _page(f"{BASE}/blog/name-train-history.html", "blog", "The History of Name Trains", impr=120),
    ]


def test_brief_is_grounded_in_real_pages_only():
    b = build_brief(_results(), FAMILIES, "name trains",
                    target={"title": "How Name Trains Teach Letters",
                            "primary_keyword": "name train letters", "intent": "informational",
                            "word_count_target": 1200, "outline": ["Intro", "How"],
                            "supporting_keywords": ["letter recognition"]})
    # Hub link-up is the real category page.
    assert b["link_up"]["url"] == f"{BASE}/name-trains.html"
    # Only real product URLs are offered for link/CTA.
    prod_urls = {p["url"] for p in b["link_products"]}
    assert prod_urls <= {f"{BASE}/personalized-name-train.html", f"{BASE}/wooden-name-train.html"}
    # Required internal links include the hub (up) and the products.
    dirs = {r["direction"] for r in b["required_internal_links"]}
    assert "up_to_hub" in dirs and "to_product" in dirs
    # External authority candidates are real, named orgs.
    assert b["external_authority_candidates"] is EXTERNAL_AUTHORITY_CANDIDATES
    assert b["output_blocks"] is OUTPUT_BLOCKS


def test_pillar_uses_structural_scaffold_not_facts():
    b = build_brief(_results(), FAMILIES, "name trains", target=None, item_type="pillar")
    assert b["item_type"] == "pillar"
    assert b["topic"]["primary_keyword"] == "name trains"
    assert len(b["topic"]["outline"]) >= 3  # a structural outline, format only


def test_generation_messages_encode_contract_and_grounding():
    b = build_brief(_results(), FAMILIES, "name trains",
                    target={"title": "Name Train Letter Guide", "primary_keyword": "name train letters",
                            "intent": "informational", "word_count_target": 1200,
                            "outline": ["A", "B"], "supporting_keywords": []})
    system, user = build_generation_messages(b)
    # Contract: JSON keys + anti-fabrication rules present in the system message.
    for k in ("meta_title", "meta_description", "body_html", "faq_jsonld", "verify_flags"):
        assert k in system
    assert "NEVER invent" in system
    assert "≤60" in system and "≤155" in system
    # Grounding: the real hub + product URLs appear in the user prompt, and the
    # user prompt never contains a made-up URL.
    assert f"{BASE}/name-trains.html" in user
    assert f"{BASE}/personalized-name-train.html" in user
    assert "healthychildren.org" in user  # real authority candidate surfaced


def test_no_catalog_match_flag_when_family_absent():
    b = build_brief(_results(), FAMILIES, "step stools", target=None, item_type="pillar")
    # No step-stool pages in the fixture → flagged so the LLM won't invent products.
    assert b["no_catalog_match"] is True
    _, user = build_generation_messages(b)
    assert "do NOT" in user and "invent" in user


def test_verify_interlinks_detects_missing():
    b = build_brief(_results(), FAMILIES, "name trains",
                    target={"title": "T", "primary_keyword": "name train letters",
                            "intent": "informational", "word_count_target": 1000,
                            "outline": [], "supporting_keywords": []})
    required = b["required_internal_links"]
    assert required, "fixture should require at least the hub link"
    # Published page links to the hub but not the product → one present, rest missing.
    published = [{"target_url": f"{BASE}/name-trains.html"}]
    v = verify_interlinks(published, b)
    assert any(r["url"] == f"{BASE}/name-trains.html" for r in v["present"])
    assert v["missing"]  # product link(s) not yet placed
    assert v["all_present"] is False
    # All present → all_present True.
    published_all = [{"target_url": r["url"]} for r in required]
    v2 = verify_interlinks(published_all, b)
    assert v2["all_present"] is True and v2["missing"] == []


def test_article_without_gap_article_writes_the_requested_topic():
    # The bug: an article request with no matched gap-article silently became a
    # generic family pillar ("Montessori Toys: The Complete Guide") instead of the
    # topic the task asked for. It must now write about the REQUESTED topic.
    b = build_brief(_results(), FAMILIES, "name trains", target=None, item_type="article",
                    title="15 Best Kids Furniture for Montessori Classrooms (2026)",
                    primary_keyword="kids furniture for montessori classrooms")
    assert b["item_type"] == "article"
    assert b["topic"]["title"] == "15 Best Kids Furniture for Montessori Classrooms (2026)"
    assert b["topic"]["primary_keyword"] == "kids furniture for montessori classrooms"
    # Pillar scaffold must NOT have hijacked it.
    assert "Complete Guide" not in b["topic"]["title"]


def test_inline_gap_article_target_is_used_verbatim():
    art = {"title": "7 Montessori Shelf Setups", "primary_keyword": "montessori shelf ideas",
           "intent": "informational", "word_count_target": 1500,
           "outline": ["Why shelves", "Setups"], "supporting_keywords": ["low shelf"]}
    b = build_brief(_results(), FAMILIES, "name trains", target=art, item_type="article")
    assert b["topic"]["title"] == "7 Montessori Shelf Setups"
    assert b["topic"]["outline"] == ["Why shelves", "Setups"]
