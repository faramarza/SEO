from src.analysis.demand_links import insert_at_paragraph_end

PASTE = 'See our <a href="/playroom-carpets.html">playroom carpets</a>.'


def test_inserts_at_end_of_the_paragraph():
    html = "<p>These rugs promote calming sensory input.</p><p>Next.</p>"
    out = insert_at_paragraph_end(html, "calming sensory input.", PASTE)
    assert out == ('<p>These rugs promote calming sensory input. See our <a href="/playroom-carpets.html">'
                   'playroom carpets</a>.</p><p>Next.</p>')


def test_refuses_when_unsafe():
    assert insert_at_paragraph_end("<p>A rug, mid sentence here and more.</p>", "mid sentence", PASTE) is None
    assert insert_at_paragraph_end('<p>Has a <a href="/x">link</a> and ends here.</p>', "ends here.", PASTE) is None
    assert insert_at_paragraph_end("<p>Twice end.</p><p>Twice end.</p>", "Twice end.", PASTE) is None
    assert insert_at_paragraph_end("<p>Text.</p>", "not there", PASTE) is None


def test_blog_post_id_from_post_id_field(monkeypatch):
    from src.data_sources.magento_client import MagentoClient
    mc = MagentoClient.__new__(MagentoClient)
    monkeypatch.setattr(MagentoClient, "_blog_search",
                        lambda self, lp, slug: [{"post_id": 42, "url_key": slug, "name": "P", "content": "<p>x</p>"}])
    p = mc.find_blog_post_by_url("/blog/montessori-activities-by-age")
    assert p["entity_id"] == 42
    monkeypatch.setattr(MagentoClient, "_blog_search", lambda self, lp, slug: [{"url_key": slug, "name": "P"}])
    assert mc.find_blog_post_by_url("/blog/x") is None
