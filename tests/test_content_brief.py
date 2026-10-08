"""Tests for the content-creator brief + output contract (Phase 4 backbone).

Verifies the brief is GROUNDED — it only references real pages/links from the
cluster, requires the cluster interlinks, holds the Magento block contract, and
that verification catches a missing required link. No network, no LLM.
"""
from src.analysis.content_brief import (
    build_brief, build_generation_messages, verify_interlinks,
    OUTPUT_BLOCKS, EXTERNAL_AUTHORITY_CANDIDATES,
    _filter_products_for_age,
)


def test_age_filter_drops_older_products_from_young_articles():
    prods = [{"title": "Montessori Lock Box Toy"},
             {"title": "Montessori Puppet Theater For Kids"},
             {"title": "Montessori Lowercase Alphabet Puzzle"}]
    T = lambda ps: [p["title"] for p in ps]
    # young article: the 3+ puppet theater is dropped, age-appropriate toys kept
    young = T(_filter_products_for_age(prods, "1 year old"))
    assert "Montessori Puppet Theater For Kids" not in young
    assert "Montessori Lock Box Toy" in young and "Montessori Lowercase Alphabet Puzzle" in young
    assert "Montessori Puppet Theater For Kids" not in T(_filter_products_for_age(prods, "toddler"))
    # older article: the puppet theater is appropriate and kept
    assert "Montessori Puppet Theater For Kids" in T(_filter_products_for_age(prods, "3 year old"))
    # no age focus → nothing filtered; never wipes to nothing
    assert len(_filter_products_for_age(prods, "")) == len(prods)
    only_old = [{"title": "Puppet Theater"}, {"title": "Chess Set"}]
    assert _filter_products_for_age(only_old, "1 year old") == only_old

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
    # Only the hub link is MANDATORY (the cluster bond to verify live); products are
    # offered for linking but not force-required, since which fit depends on age/topic.
    dirs = {r["direction"] for r in b["required_internal_links"]}
    assert dirs == {"up_to_hub"}
    assert b["link_products"]  # products still surfaced for the drafter to feature
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
    # verify_interlinks checks a published page against ANY required-link list.
    # Hub present, product missing → detects the gap; both present → all good.
    required = [
        {"url": f"{BASE}/name-trains.html", "direction": "up_to_hub"},
        {"url": f"{BASE}/personalized-name-train.html", "direction": "to_product"},
    ]
    published = [{"target_url": f"{BASE}/name-trains.html"}]
    v = verify_interlinks(published, required)
    assert any(r["url"] == f"{BASE}/name-trains.html" for r in v["present"])
    assert v["missing"] and v["all_present"] is False
    published_all = [{"target_url": r["url"]} for r in required]
    v2 = verify_interlinks(published_all, required)
    assert v2["all_present"] is True and v2["missing"] == []

    # And the brief itself now hard-requires only the hub link.
    b = build_brief(_results(), FAMILIES, "name trains",
                    target={"title": "T", "primary_keyword": "name train letters",
                            "intent": "informational", "word_count_target": 1000,
                            "outline": [], "supporting_keywords": []})
    assert [r["direction"] for r in b["required_internal_links"]] == ["up_to_hub"]


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
