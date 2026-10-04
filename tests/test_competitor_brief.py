"""Tests for the Competitor Brief (SERP coverage extraction). Pure — the SERP and
page fetchers are injected, so no network."""
from src.analysis import competitor_brief as cbf


_PAGES = [
    {"url": "https://good-a.com/montessori-1yo",
     "html": ("<h2>Best Montessori Toys for 1 Year Olds</h2>"
              "<p>Object permanence box and stacking rings build fine motor skills. "
              "Shape sorters and wooden puzzles help hand eye coordination.</p>"
              "<h3>Object permanence box</h3><p>Teaches cause and effect.</p>"
              "<h3>Stacking rings</h3><p>Develop problem solving.</p>") * 4},
    {"url": "https://good-b.com/guide",
     "html": ("<h2>Why Montessori Toys Work</h2>"
              "<p>Montessori toys encourage independent play, fine motor skills, "
              "problem solving, object permanence and hand eye coordination. Shape "
              "sorters and stacking rings are classics.</p><h3>Fine motor skills</h3>") * 4},
    {"url": "https://good-c.com/post",
     "html": ("<h2>Developmental Benefits</h2>"
              "<p>Open ended play and sensory exploration. Object permanence, stacking "
              "rings, shape sorter, wooden puzzle. Fine motor development.</p>"
              "<h3>Shape sorter</h3>") * 4},
]


def _fake_serp(_q):
    return {"organic_results": [{"url": p["url"]} for p in _PAGES]
            + [{"url": "https://www.amazon.com/dp/x"}],       # marketplace → skipped
            "people_also_ask": ["What age are Montessori toys for?",
                                "Are Montessori toys worth it?"]}


def _fake_page(url):
    return {p["url"]: p["html"] for p in _PAGES}.get(url)


def test_extracts_df_weighted_terms():
    b = cbf.build_competitor_brief("montessori toys for 1 year old", _fake_serp, _fake_page,
                                   own_domain="alphabet-trains.com")
    terms = [t["term"] for t in b["terms"]]
    assert "object permanence" in terms and "stacking rings" in terms
    # the query's own words alone are not returned as a term
    assert "montessori toys" not in terms
    # phrases carry their document frequency
    assert all("in_pages" in t for t in b["terms"])


def test_skips_marketplaces_and_own_domain():
    b = cbf.build_competitor_brief("montessori toys", _fake_serp, _fake_page,
                                   own_domain="good-a.com")  # pretend we own good-a
    assert all("amazon." not in u for u in b["source_urls"])
    assert all("good-a.com" not in u for u in b["source_urls"])   # own page excluded


def test_questions_merge_paa_and_headings():
    b = cbf.build_competitor_brief("montessori toys 1 year old", _fake_serp, _fake_page)
    assert "What age are Montessori toys for?" in b["questions"]
    # question-style heading surfaced too ("Why Montessori Toys Work")
    assert any(q.lower().startswith("why") for q in b["questions"])


def test_target_words_is_median_and_headings_present():
    b = cbf.build_competitor_brief("montessori toys 1 year old", _fake_serp, _fake_page)
    assert b["target_words"] > 0
    assert any("Object permanence" in h for h in b["headings"])
    assert b["pages_analyzed"] == 3


def test_competitor_brand_and_promo_filtered_out():
    # A competitor's self-promo heading must never surface as a section/question.
    pages = [{"url": "https://rival.com/p",
              "html": ("<h2>What makes Hazel & Fawn a trusted source for Montessori toys?</h2>"
                       "<p>object permanence stacking rings fine motor hand eye coordination "
                       "shape sorter wooden puzzle problem solving sensory play development "
                       "milestones motor skills.</p>"
                       "<h2>About Us</h2><h2>Developmental Benefits</h2>"
                       "<h3>Why choose Lovevery</h3>") * 4}]
    serp = lambda q: {"organic_results": [{"url": pages[0]["url"]}],
                      "people_also_ask": ["What age are Montessori toys for?",
                                          "Why shop at Hazel & Fawn?"]}
    page = lambda u: pages[0]["html"]
    b = cbf.build_competitor_brief("montessori toys", serp, page)
    blob = " || ".join((b.get("headings") or []) + (b.get("questions") or []))
    assert "Hazel" not in blob and "Lovevery" not in blob and "About Us" not in blob
    assert "What age are Montessori toys for?" in b["questions"]      # legit PAA kept
    assert any("Developmental Benefits" in h for h in b["headings"])  # legit heading kept


def test_authority_links_sourced_from_serp():
    # Real authority URLs in the SERP are surfaced to cite; rivals/marketplaces aren't.
    serp = lambda q: {"organic_results": [
        {"url": "https://www.healthychildren.org/English/ages/Play.aspx", "title": "AAP Play"},
        {"url": "https://rival-store.com/montessori", "title": "Rival"},
        {"url": "https://www.cdc.gov/child/toddlers.html", "title": "CDC"},
        {"url": "https://www.amazon.com/x", "title": "Amazon"}],
        "people_also_ask": ["What age are Montessori toys for?"]}
    page = lambda u: ("<h2>Benefits</h2><p>object permanence stacking rings fine motor "
                      "shape sorter hand eye coordination problem solving sensory play.</p>") * 5
    b = cbf.build_competitor_brief("montessori toys 1 year old", serp, page)
    auth = [a["url"] for a in b.get("authority_links", [])]
    assert any("healthychildren.org" in u for u in auth)
    assert any("cdc.gov" in u for u in auth)
    assert not any("rival-store" in u or "amazon" in u for u in auth)


def test_authority_links_filtered_by_age_relevance():
    # On a 1-year-old article, a CDC "5-years" page must be rejected in favour of the
    # site's age-appropriate "12-months" page (same domain).
    def serp(q):
        if q.startswith("site:cdc.gov"):
            return {"organic_results": [
                {"url": "https://www.cdc.gov/act-early/milestones/5-years.html", "title": "Milestones by 5 Years"},
                {"url": "https://www.cdc.gov/act-early/milestones/12-months.html", "title": "Milestones by 1 Year"}]}
        if q.startswith("site:"):
            return {"organic_results": []}
        return {"organic_results": [{"url": "https://rival.com/x"}], "people_also_ask": ["What age?"]}
    page = lambda u: "<p>play, toys and developmental milestones for your child</p>" * 5
    b = cbf.build_competitor_brief("montessori toys for 1 year old", serp, page)
    urls = [a["url"] for a in b["authority_links"]]
    assert any("12-months" in u for u in urls)
    assert not any("5-years" in u for u in urls)


def test_authority_candidate_rejects_offtopic_page():
    # A live authority page that never mentions the topic is dropped (content check).
    assert cbf._authority_candidate(
        "https://www.naeyc.org/x", "Some Page",
        "<p>membership dues and conference registration</p>", {"montessori", "toys"}, 12) is False
    assert cbf._authority_candidate(
        "https://www.naeyc.org/x", "Toys & Play",
        "<p>the right toys support play and learning</p>", {"montessori", "toys"}, 12) is True


def test_none_when_no_serp():
    assert cbf.build_competitor_brief("x", lambda q: None, _fake_page) is None


def test_thin_pages_ignored_but_paa_keeps_brief():
    # All pages too thin to analyze, but PAA exists → still returns a (terms-empty) brief.
    thin = lambda u: "<p>hi</p>"
    b = cbf.build_competitor_brief("montessori toys", _fake_serp, thin)
    assert b is not None and b["questions"] and b["pages_analyzed"] == 0
