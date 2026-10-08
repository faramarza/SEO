"""External-citation hardening for content drafts: the model may only cite REAL,
sourced URLs. A guessed URL (even one that merely 'blocks' rather than 404s) must be
replaced with a real page on the same authority, or dropped to a [VERIFY] marker — it
must never reach the published body. Imports web.app, so it needs flask (CI); it is
skipped where flask isn't installed."""
import pytest

pytest.importorskip("flask")
from web import app as A


def test_reg_domain_collapses_subdomains():
    assert A._reg_domain("https://www.aap.org/x/y.aspx") == "aap.org"
    assert A._reg_domain("https://publications.aap.org/z") == "aap.org"
    assert A._reg_domain("not a url") == ""


def test_guessed_external_url_is_replaced_with_sourced_page(monkeypatch):
    sourced_naeyc = "https://www.naeyc.org/resources/topics/play/specific-toys-play"
    guessed_aap = ("https://www.aap.org/en-us/about-the-aap/aap-press-room/pages/"
                   "the-power-of-play.aspx")
    real_aap = ("https://publications.aap.org/pediatrics/article/142/3/e20182058/"
                "38649/The-Power-of-Play")

    # guessed AAP page 401s (blocked); everything else is live
    monkeypatch.setattr(A, "_url_status",
                        lambda u: "blocked" if "the-power-of-play.aspx" in u else "live")
    # a site:aap.org lookup finds the real publications page
    monkeypatch.setattr(A, "_find_live_authority",
                        lambda reg, kw: real_aap if reg == "aap.org" else None)

    body = (f'<p>Research <a href="{guessed_aap}">AAP</a> says play matters.</p>'
            f'<p>Per <a href="{sourced_naeyc}">NAEYC</a> safe play helps.</p>')
    parsed = {"body_html": body,
              "external_links": [{"url": guessed_aap, "name": "AAP"},
                                 {"url": sourced_naeyc, "name": "NAEYC"}]}
    allow = {sourced_naeyc, "https://naeyc.org/", "https://aap.org/"}

    A._validate_and_repair_links(parsed, "montessori toys for 1 year old",
                                 {"grounding_products": []}, allow_external=allow)
    finals = [e["url"] for e in parsed["external_links"]]
    assert guessed_aap not in parsed["body_html"]      # purged from the body
    assert real_aap in finals                          # repaired to the real page
    assert sourced_naeyc in finals                     # a genuinely sourced link is kept


def test_emoji_converted_to_entities_but_plain_text_kept():
    # Raw emoji break Magento content columns; they must become numeric HTML entities.
    s = '<div class="benefit-icon">🧠</div><p>🤲 🌱</p>'
    out = A._emoji_to_entities(s)
    assert "🧠" not in out and "&#129504;" in out      # brain  U+1F9E0
    assert "&#129330;" in out and "&#127793;" in out    # palms, seedling
    assert A._emoji_to_entities("❤") == "&#10084;"  # ❤ (BMP emoji) too
    # readable 3-byte text (curly quote, em dash, accent) stays as-is
    plain = "It’s a well—made café"
    assert A._emoji_to_entities(plain) == plain


def test_insert_before_faq_keeps_faq_last():
    body = '<h2 id="intro">Intro</h2><p>a</p><h2 id="faq">FAQs</h2><h3>Q</h3><p>ans</p>'
    out = A._insert_before_faq(body, "<h2>New Section</h2><p>more depth</p>")
    assert out.index("New Section") < out.index('id="faq"')      # added before FAQ
    assert out.index('id="faq"') < out.index("<h3>Q")            # FAQ block intact
    assert "Intro" in out and "ans" in out                       # nothing dropped
    # no FAQ present -> appended at the end
    assert A._insert_before_faq("<h2>Only</h2>", "<h2>End</h2>").rstrip().endswith("<h2>End</h2>")


def test_pasted_review_verbatim_is_stripped_but_paraphrase_kept():
    notes = ("The latches are quite stiff and my thirteen month old needed help opening "
             "them at first.")
    # a verbatim copy of >=10 words must be removed and flagged
    body = ("<p>Parents note that the latches are quite stiff and my thirteen month old "
            "needed help opening them at first.</p>")
    nb, copied = A._strip_pasted_verbatim(body, notes)
    assert "[VERIFY: paraphrase" in nb and copied
    assert "needed help opening them at first" not in nb
    # a paraphrase sharing only short fragments is left untouched
    para = "<p>The latches can be stiff, so younger babies may need a hand at first.</p>"
    nb2, copied2 = A._strip_pasted_verbatim(para, notes)
    assert nb2 == para and copied2 == []
    # replacement never eats an HTML tag boundary
    split = ("<p>the latches are quite stiff</p><p>and my thirteen month old needed "
             "help opening</p>")
    nb3, _ = A._strip_pasted_verbatim(split, notes)
    assert "</p><p>" in nb3


def test_unsourced_url_with_no_replacement_becomes_verify(monkeypatch):
    guess = "https://www.example-authority.org/made-up.aspx"
    monkeypatch.setattr(A, "_url_status", lambda u: "live")       # even if it "works"
    monkeypatch.setattr(A, "_find_live_authority", lambda reg, kw: None)  # nothing real found
    body = f'<p>Studies <a href="{guess}">show</a> benefits.</p>'
    parsed = {"body_html": body, "external_links": [{"url": guess, "name": "X"}]}
    A._validate_and_repair_links(parsed, "montessori toys", {"grounding_products": []},
                                 allow_external={"https://naeyc.org/"})
    assert guess not in parsed["body_html"]
    assert "[VERIFY" in parsed["body_html"]             # visible marker, not a fake cite
    assert parsed["external_links"] == []               # dropped from the list
