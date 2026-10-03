"""Tests for the Topical Authority Engine — Phase 1 cluster map.

Grounded fixtures modeled on the real store: product/category/blog pages across
a couple of families, with a realistic internal-link mesh, so the hub/spoke
assignment and link-health math are exercised on data shaped like production.
"""
from src.analysis.topical_authority import (
    build_cluster_map, build_interlink_plan, build_topic_plan, _norm_url,
    _slug_phrase, _assign_family, _family_matchers, _build_link_graph,
    _anchor_for, _keyword_family, _build_cadence, TARGET_DEPTH,
)


def _page(url, atype, title="", outlinks=None, impr=0, clicks=0):
    return {
        "url": url,
        "asset_type": atype,
        "gsc_impressions": impr,
        "gsc_clicks": clicks,
        "page_metadata": {
            "title": title or url,
            "h1": title or "",
            "internal_outlinks": [
                {"target_url": t, "anchor_text": a, "location": "body"}
                for (t, a) in (outlinks or [])
            ],
        },
    }


FAMILIES = ["name trains", "step stools", "circle time rugs"]


def _store():
    base = "https://alphabet-trains.com"
    hub_nt = f"{base}/name-trains.html"
    prod_nt = f"{base}/personalized-name-train.html"
    blog_nt = f"{base}/blog/how-name-trains-help-letter-recognition.html"
    hub_ss = f"{base}/personalized-step-stools.html"
    prod_ss = f"{base}/wooden-step-stool.html"
    orphan = f"{base}/circle-time-rugs.html"  # family with a hub but no inlinks
    return {
        "hub_nt": hub_nt, "prod_nt": prod_nt, "blog_nt": blog_nt,
        "hub_ss": hub_ss, "prod_ss": prod_ss, "orphan": orphan,
        "results": [
            # Name trains cluster: hub links down to product + blog; both link up.
            _page(hub_nt, "category", "Personalized Name Trains",
                  outlinks=[(prod_nt, "shop name trains"), (blog_nt, "how name trains help")],
                  impr=5000, clicks=200),
            _page(prod_nt, "product", "Personalized Name Train",
                  outlinks=[(hub_nt, "all name trains")], impr=800, clicks=40),
            _page(blog_nt, "blog", "How Name Trains Help Letter Recognition",
                  outlinks=[(hub_nt, "name trains")], impr=300, clicks=5),
            # Step stools cluster: hub does NOT link to product; product does not
            # link up either (both-missing). No blog → topical-depth flag.
            _page(hub_ss, "category", "Personalized Step Stools",
                  outlinks=[], impr=1200, clicks=60),
            _page(prod_ss, "product", "Wooden Step Stool",
                  outlinks=[], impr=200, clicks=10),
            # Circle time rugs: only a category page, nothing links to it → orphan
            # hub, no spokes.
            _page(orphan, "category", "Circle Time Rugs", outlinks=[], impr=400, clicks=8),
            # An off-topic page that matches no family → unclustered.
            _page(f"{base}/about-us.html", "other", "About Us", impr=50),
        ],
    }


def test_norm_url():
    assert _norm_url("https://www.X.com/A/") == "x.com/a"
    assert _norm_url("http://x.com/a?b=1#c") == "x.com/a"
    assert _norm_url("") == ""


def test_slug_phrase():
    assert _slug_phrase("https://x.com/name-trains.html") == "name trains"
    assert _slug_phrase("https://x.com/blog/step-stool-guide.html") == "blog step stool guide"


def test_family_assignment_grounded_in_slug_and_title():
    matchers = _family_matchers(FAMILIES)
    nt = _page("https://x.com/name-trains.html", "category", "Personalized Name Trains")
    fam, score = _assign_family(nt, matchers)
    assert fam == "name trains"
    assert score >= 1.0  # literal slug match bonus

    # A page that matches no family stays unclustered (honest, not forced).
    about = _page("https://x.com/about-us.html", "other", "About Us")
    fam2, _ = _assign_family(about, matchers)
    assert fam2 is None


def test_link_graph_only_known_nodes_and_dedup():
    s = _store()
    out, inn = _build_link_graph(s["results"])
    hub = _norm_url(s["hub_nt"])
    # Hub links to product + blog (both known) → 2 outlinks.
    assert len(out[hub]) == 2
    # Product + blog each link back to the hub → hub has 2 inlinks.
    assert len(inn[hub]) == 2


def test_cluster_map_hub_spoke_and_health():
    s = _store()
    cmap = build_cluster_map(s["results"], FAMILIES)
    fams = {f["family"]: f for f in cmap["families"]}
    assert set(fams) == set(FAMILIES)

    # Name trains: category page is the hub (not inferred), 2 spokes, both linking
    # up, hub links down to both, no orphans, has a blog.
    nt = fams["name trains"]
    assert _norm_url(nt["hub"]["url"]) == _norm_url(s["hub_nt"])
    assert nt["hub_inferred"] is False
    assert nt["link_health"]["spokes_total"] == 2
    assert nt["link_health"]["spokes_linking_to_hub"] == 2
    assert nt["link_health"]["hub_links_to_spokes"] == 2
    assert nt["link_health"]["orphan_spokes"] == []
    assert nt["counts"]["blog"] == 1

    # Step stools: hub present, 1 spoke, no links either way → flagged both ways
    # and flagged for missing blog depth. Product is an orphan (no inlinks).
    ss = fams["step stools"]
    assert ss["link_health"]["spokes_total"] == 1
    assert ss["link_health"]["spokes_linking_to_hub"] == 0
    assert ss["link_health"]["hub_links_to_spokes"] == 0
    assert _norm_url(s["prod_ss"]) in [_norm_url(u) for u in ss["link_health"]["orphan_spokes"]]
    assert any("blog" in f.lower() for f in ss["flags"])

    # Circle time rugs: a lone category page becomes the hub, zero spokes.
    rugs = fams["circle time rugs"]
    assert rugs["hub"] is not None
    assert rugs["link_health"]["spokes_total"] == 0

    # About Us is a non-topical utility page — EXCLUDED entirely, not clustered
    # and not even in "unclustered" (never offered as a link target).
    all_urls = [c["url"] for c in cmap["unclustered"]]
    for f in cmap["families"]:
        all_urls += [sp["url"] for sp in f["spokes"]] + ([f["hub"]["url"]] if f["hub"] else [])
    assert not any("about-us" in u for u in all_urls)
    assert cmap["totals"]["families_total"] == 3


def test_empty_family_is_visible_as_gap():
    # A configured family with no pages at all is still surfaced, flagged.
    cmap = build_cluster_map([], ["name trains"])
    fam = cmap["families"][0]
    assert fam["hub"] is None
    assert fam["counts"]["total"] == 0
    assert any("No pages found" in f for f in fam["flags"])


def test_anchor_for_trims_boilerplate_and_falls_back():
    assert _anchor_for("Personalized Name Train | Alphabet Trains", "name trains") == "Personalized Name Train"
    # Over-long / empty title → fall back to the family term.
    assert _anchor_for("", "name trains") == "name trains"
    assert _anchor_for("x" * 80, "name trains") == "name trains"


def test_interlink_plan_grouped_by_source_and_prioritized():
    s = _store()
    plan = build_interlink_plan(s["results"], FAMILIES)
    assert plan["available"] is True
    groups = {g["source_url"]: g for g in plan["groups"]}

    # Name-trains cluster is fully wired → it contributes NO interlink jobs.
    assert s["hub_nt"] not in groups
    assert s["prod_nt"] not in groups

    # Step-stools: hub must link down to the product (orphan fix) AND the product
    # must link up to the hub. Both appear as jobs.
    hub_ss = groups.get(s["hub_ss"])
    prod_ss = groups.get(s["prod_ss"])
    assert hub_ss is not None and prod_ss is not None
    # Hub→spoke here fixes an orphan → that job is top-priority (first group).
    assert plan["groups"][0]["source_url"] == s["hub_ss"]
    hub_link = hub_ss["links"][0]
    assert hub_link["target_url"] == s["prod_ss"]
    assert hub_link["direction"] == "hub_to_spoke"
    assert hub_link["fixes_orphan"] is True
    # Spoke→hub job carries an editable anchor grounded in the hub's title.
    up = prod_ss["links"][0]
    assert up["target_url"] == s["hub_ss"]
    assert up["direction"] == "spoke_to_hub"
    assert up["anchor"]

    assert plan["totals"]["orphans_fixed"] >= 1
    assert plan["totals"]["links_total"] == sum(g["link_count"] for g in plan["groups"])


def test_interlink_plan_skips_clusters_without_hub():
    # A family with no pages → no hub → no interlink jobs (it's a Phase-1 flag).
    plan = build_interlink_plan([], ["name trains"])
    assert plan["groups"] == []
    assert plan["totals"]["links_total"] == 0


def test_keyword_family_maps_or_abstains():
    m = _family_matchers(FAMILIES)
    assert _keyword_family("best name train for toddlers", m) == "name trains"
    assert _keyword_family("wooden step stool for kids", m) == "step stools"
    assert _keyword_family("montessori floor bed", m) is None  # no family matches


def test_coverage_scorecard_bands_and_subscores():
    s = _store()
    plan = build_topic_plan(s["results"], FAMILIES)
    fams = {f["family"]: f for f in plan["families"]}

    # Name trains: pillar + a blog + both-way links, no orphans → strong score.
    nt = fams["name trains"]["coverage"]
    assert nt["subscores"]["pillar"] == 25
    assert nt["subscores"]["product"] == 10
    assert nt["status"] in ("developing", "solid")

    # Circle time rugs: lone category, no products/articles/spokes → thin.
    rugs = fams["circle time rugs"]["coverage"]
    assert rugs["status"] in ("thin", "developing")
    assert rugs["n_products"] == 0

    assert 0 <= plan["totals"]["avg_coverage"] <= 100


def test_topic_gaps_use_real_articles_and_flag_thin():
    s = _store()
    # One real content-gap article for name trains, one unmappable.
    gap_articles = [
        {"primary_keyword": "name train letter recognition", "title": "Do Name Trains Teach Letters?",
         "total_volume": 400, "word_count_target": 1200, "outline": ["Intro", "How"],
         "supporting_keywords": ["name train benefits"]},
        {"primary_keyword": "montessori floor bed", "title": "Floor Bed Guide",
         "total_volume": 900, "word_count_target": 1500, "outline": [], "supporting_keywords": []},
    ]
    plan = build_topic_plan(s["results"], FAMILIES, gap_articles=gap_articles)
    fams = {f["family"]: f for f in plan["families"]}

    # The mappable article attaches to name trains as a concrete article gap.
    nt_articles = [g for g in fams["name trains"]["gaps"] if g["type"] == "article"]
    assert any("Name Trains" in (g.get("title") or "") for g in nt_articles)

    # Step stools (no pillar? it has a category hub) is thin on depth → a research
    # pointer appears (no fabricated article title).
    ss_gaps = fams["step stools"]["gaps"]
    assert any(g["type"] == "research" for g in ss_gaps)
    # The unmappable "floor bed" article never invents a family.
    for f in plan["families"]:
        for g in f["gaps"]:
            assert "floor bed" not in (g.get("title") or "").lower()


def test_cadence_ramps_and_never_pads():
    # 30 writable items, ramp 4,5,6,7,8 → months fill exactly 4,5,6,7,8.
    items = [{"family": "f", "coverage_score": 10, "type": "article",
              "priority": "normal", "what": "x", "title": f"A{i}",
              "primary_keyword": f"k{i}", "volume": 100 - i} for i in range(30)]
    months = _build_cadence(items, [4, 5, 6, 7, 8])
    caps = [len(mo["items"]) for mo in months]
    assert caps == [4, 5, 6, 7, 8]
    assert sum(caps) == 30  # every item scheduled, none invented
    # A short tail month is allowed (not padded): 32 items → last month holds 2.
    months2 = _build_cadence(items + items[:2], [4, 5, 6, 7, 8])
    assert len(months2[-1]["items"]) == 2 and months2[-1]["capacity"] == 8
    # Pillars are scheduled before articles.
    mixed = [{"family": "f", "coverage_score": 50, "type": "article", "priority": "normal",
              "what": "a", "title": "T", "primary_keyword": "k", "volume": 10},
             {"family": "g", "coverage_score": 90, "type": "pillar", "priority": "high",
              "what": "p", "title": None, "primary_keyword": None, "volume": 0}]
    mo = _build_cadence(mixed, [4])
    assert mo[0]["items"][0]["type"] == "pillar"


def test_inferred_hub_when_no_category():
    # Family present only as a product + blog (no category) → hub inferred, flagged.
    base = "https://alphabet-trains.com"
    results = [
        _page(f"{base}/wooden-name-puzzle.html", "product", "Wooden Name Puzzle",
              outlinks=[], impr=900, clicks=30),
        _page(f"{base}/blog/name-puzzle-benefits.html", "blog", "Name Puzzle Benefits",
              outlinks=[(f"{base}/wooden-name-puzzle.html", "name puzzle")], impr=100),
    ]
    cmap = build_cluster_map(results, ["name puzzles"])
    fam = cmap["families"][0]
    assert fam["hub"] is not None
    assert fam["hub_inferred"] is True
    # The product has an inlink (from the blog) so it wins the inferred hub.
    assert "wooden-name-puzzle" in fam["hub"]["url"]
    assert any("de-facto hub" in f for f in fam["flags"])


def test_anchor_for_long_titles_are_descriptive_not_generic():
    # Long titles must yield a distinct, descriptive anchor — NOT the bare family
    # term repeated (which reads as spammy duplicate exact-match anchors).
    a1 = _anchor_for("The Ultimate Guide to Montessori Toys: Types, Benefits, and Choices", "montessori toys")
    a2 = _anchor_for("Montessori Sensory Toys That Spark Creativity: Spotlight on the Drum", "montessori toys")
    assert a1 == "The Ultimate Guide to Montessori Toys"
    assert a2 == "Montessori Sensory Toys That Spark Creativity"
    assert a1 != a2 and a1.lower() != "montessori toys" and a2.lower() != "montessori toys"


def test_nontopical_and_brand_pollution_excluded():
    from src.analysis.topical_authority import _nontopical, _page_tokens
    base = "https://alphabet-trains.com"
    # utility/legal/homepage pages excluded
    for u in ("/contact", "/shipping-policy", "/terms-conditions", "/faqs",
              "/about-us", "/privacy-policy", "/", "/wishlist"):
        assert _nontopical(base + u), u
    # real product/category pages kept
    for u in ("/name-trains.html", "/5-lbs-white-play-sand.html", "/montessori-toys.html"):
        assert not _nontopical(base + u), u
    # brand suffix in the title doesn't inject "train" into an unrelated product
    toks = _page_tokens({"url": base + "/5-lbs-white-play-sand.html",
                         "page_metadata": {"title": "5 lbs White Play Sand | Alphabet Trains & Toys",
                                           "h1": "5 lbs White Play Sand"}})
    assert "train" not in toks and "trains" not in toks


def test_cluster_map_drops_nontopical_pages_from_name_trains():
    base = "https://alphabet-trains.com"
    def pg(url, atype, title, h1=""):
        return {"url": url, "asset_type": atype, "gsc_impressions": 10,
                "page_metadata": {"title": title, "h1": h1 or title, "internal_outlinks": []}}
    results = [
        pg(f"{base}/name-trains.html", "category", "Personalized Name Trains"),
        pg(f"{base}/5-letter-name-train.html", "product", "5 Letter Name Train"),
        pg(f"{base}/contact", "other", "Contact US | Alphabet Trains & Toys"),
        pg(f"{base}/shipping-policy", "other", "Shipping Policy | Alphabet Trains"),
        pg(f"{base}/5-lbs-white-play-sand.html", "product", "5 lbs White Play Sand | Alphabet Trains & Toys"),
    ]
    cmap = build_cluster_map(results, ["name trains"])
    nt = cmap["families"][0]
    urls = ([nt["hub"]["url"]] if nt["hub"] else []) + [s["url"] for s in nt["spokes"]]
    joined = " ".join(urls).lower()
    assert "contact" not in joined and "shipping" not in joined   # utility pages gone
    assert "play-sand" not in joined                              # brand-pollution gone
    assert "name-train" in joined                                 # real page kept


def test_malformed_urls_excluded():
    from src.analysis.topical_authority import _nontopical
    assert _nontopical("https://x.com/blog/name-recognition-early-literacy-developm=")
    assert _nontopical("https://x.com/page%20broken")
    assert not _nontopical("https://x.com/5-letter-name-train.html")


def test_interlink_excludes_near_duplicate_of_hub():
    # The hub /name-trains must NOT get a link to its near-duplicate /name-train
    # (same thing, singular) — that's a self-ish link, not a real interlink.
    base = "https://alphabet-trains.com"
    def pg(url, atype, title, outlinks=None, impr=0):
        return {"url": url, "asset_type": atype, "gsc_impressions": impr, "gsc_clicks": 0,
                "page_metadata": {"title": title, "h1": title,
                    "internal_outlinks": [{"target_url": t, "anchor_text": "", "location": "body"}
                                          for t in (outlinks or [])]}}
    results = [
        pg(f"{base}/name-trains.html", "category", "Personalized Name Trains", impr=5000),
        pg(f"{base}/name-train.html", "category", "Name Train", impr=10),   # near-dup
        pg(f"{base}/5-letter-name-train-set.html", "product", "5 Letter Name Train Set", impr=600),
    ]
    plan = build_interlink_plan(results, ["name trains"])
    targets = []
    for g in plan["groups"]:
        for l in g["links"]:
            targets.append(_norm_url(l["target_url"]))
        targets.append(_norm_url(g["source_url"]))
    assert _norm_url(f"{base}/name-train.html") not in targets   # near-dup never linked
