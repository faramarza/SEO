"""Tests for the Topical Authority Engine — Phase 1 cluster map.

Grounded fixtures modeled on the real store: product/category/blog pages across
a couple of families, with a realistic internal-link mesh, so the hub/spoke
assignment and link-health math are exercised on data shaped like production.
"""
from src.analysis.topical_authority import (
    build_cluster_map, _norm_url, _slug_phrase, _assign_family, _family_matchers,
    _build_link_graph,
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

    # About Us matched no family.
    assert any("about-us" in c["url"] for c in cmap["unclustered"])
    assert cmap["totals"]["families_total"] == 3


def test_empty_family_is_visible_as_gap():
    # A configured family with no pages at all is still surfaced, flagged.
    cmap = build_cluster_map([], ["name trains"])
    fam = cmap["families"][0]
    assert fam["hub"] is None
    assert fam["counts"]["total"] == 0
    assert any("No pages found" in f for f in fam["flags"])


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
