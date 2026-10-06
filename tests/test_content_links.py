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
