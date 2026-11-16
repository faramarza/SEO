from scripts.link_sources import score_post, sentences_with


def test_score_post_counts_words_and_existing_link():
    html = '<p>An alphabet rug helps. Letters matter.</p><a href="/alphabet-themed-carpets.html">x</a>'
    hits, links = score_post(html, ["alphabet", "letter"], "alphabet-themed-carpets")
    assert hits == {"alphabet": 1, "letter": 1} and links is True
    hits, links = score_post("<p>Blocks only.</p>", ["alphabet"], "alphabet-themed-carpets")
    assert hits == {"alphabet": 0} and links is False


def test_sentences_with():
    s = sentences_with("<p>Blocks are fun. Circle time with an ABC rug works. Done.</p>", ["abc"])
    assert s == ["Circle time with an ABC rug works."]


def test_ignores_brand_and_schema():
    html = '<script>{"name": "Alphabet Trains"}</script><p>At Alphabet Trains we love rugs.</p>'
    hits, _ = score_post(html, ["alphabet", "rug"], "x")
    assert hits == {"alphabet": 0, "rug": 1}


def test_demand_links_only_where_the_text_talks_about_the_product():
    from scripts.product_demand import find_link_sources
    posts = [{"name": "Setup", "url_key": "setup", "content":
              "<p>Our carpets and rugs are designed for comfort, durability, and style, perfect for any playroom or classroom.</p>"},
             {"name": "Guide", "url_key": "guide", "content":
              "<p>Puzzles are versatile and can be used in classrooms, playrooms, or homeschooling setups.</p>"},
             {"name": "Rugs", "url_key": "rugs", "content":
              "<p>A soft playroom rug gives toddlers a warm and safe place to sit and play every day.</p>"}]
    out = find_link_sources(posts, "/playroom-carpets.html", "playroom carpets",
                            anchors=["playroom carpets", "playroom rug"])
    assert [x["post"] for x in out] == ["Setup", "Rugs"] or [x["post"] for x in out] == ["Rugs", "Setup"]
    assert {x["paste"] for x in out} == {' See our <a href="/playroom-carpets.html">playroom carpets</a>.',
                                         ' See our <a href="/playroom-carpets.html">playroom rugs</a>.'}


def test_plural_phrase():
    from scripts.product_demand import plural_phrase
    assert plural_phrase("playroom rug") == "playroom rugs"
    assert plural_phrase("wooden toy box") == "wooden toy boxes"
    assert plural_phrase("playroom carpets") == "playroom carpets"
    assert plural_phrase("montessori educational toy") == "montessori educational toys"


def test_never_links_from_a_promo_box():
    from scripts.product_demand import find_link_sources
    posts = [{"name": "Dance Games", "url_key": "dance", "content":
              '<p style="color:#666">Our <strong>Circle Time Rugs</strong> are built for group activities&mdash;soft, '
              'durable, and sized for classrooms and playrooms alike.</p>\n  <a class="cta-button" '
              'href="https://alphabet-trains.com/circle-time-rugs.html">Shop Circle Time Rugs</a>'}]
    assert find_link_sources(posts, "/playroom-carpets.html", "playroom carpets") == []
