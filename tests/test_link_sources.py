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
