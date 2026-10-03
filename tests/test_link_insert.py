"""Tests for the interlink body-insertion logic (append / merge / revert)."""
from src.analysis import link_insert as li


DESC = "<p>Our Montessori toys are natural wood.</p>"
L1 = {"url": "https://x.com/blog/a", "anchor": "Guide A"}
L2 = {"url": "https://x.com/blog/b", "anchor": "Guide B & C"}
L3 = {"url": "https://x.com/blog/c", "anchor": "Guide C"}


def test_append_keeps_original_and_adds_block():
    out = li.merge_description(DESC, "Montessori Toys", [L1, L2])
    assert DESC in out
    assert li.MARK_START in out and li.MARK_END in out
    pairs = li.existing_links(out)
    assert ("https://x.com/blog/a", "Guide A") in pairs
    # anchor text is escaped in the HTML but reads back clean (no double-escape)
    assert ("https://x.com/blog/b", "Guide B & C") in pairs


def test_reapply_merges_and_dedupes_no_double_escape():
    out = li.merge_description(DESC, "Montessori Toys", [L1, L2])
    out2 = li.merge_description(out, "Montessori Toys", [L2, L3])  # L2 overlaps
    pairs = dict(li.existing_links(out2))
    assert set(pairs) == {"https://x.com/blog/a", "https://x.com/blog/b", "https://x.com/blog/c"}
    assert pairs["https://x.com/blog/b"] == "Guide B & C"   # not "B &amp; C"
    # Only ONE block, original content intact.
    assert out2.count(li.MARK_START) == 1
    assert DESC in out2


def test_revert_restores_exactly():
    out = li.merge_description(DESC, "Montessori Toys", [L1, L2, L3])
    assert li.strip_block(out) == DESC


def test_added_pairs_reports_only_new():
    out = li.merge_description(DESC, "Montessori Toys", [L1])
    added = li.added_pairs(out, "Montessori Toys", [L1, L2])  # L1 present, L2 new
    assert added == [("https://x.com/blog/b", "Guide B & C")]


def test_empty_links_is_noop():
    assert li.merge_description(DESC, "Montessori Toys", []) == DESC


def test_block_escapes_html_in_anchor_and_url():
    out = li.build_block("Toys", [("https://x.com/?a=1&b=2", "A <script> & \"x\"")])
    assert "<script>" not in out          # escaped
    assert "&amp;" in out


# --- in-prose (contextual) weaving --------------------------------------------

PROSE = ("<p>Our wooden train track sets are built to last. Pair them with a "
         "personalized name train for extra fun.</p>")


def test_linkify_wraps_existing_phrase_only():
    out, ok = li.linkify_phrase(PROSE, "personalized name train", "/pnt.html")
    assert ok
    assert '<a href="/pnt.html">personalized name train</a>' in out
    # nothing but the one anchor was added — strip it and we're back to the original
    assert out.replace('<a href="/pnt.html">', "").replace("</a>", "") == PROSE


def test_linkify_misses_phrase_not_present():
    out, ok = li.linkify_phrase(PROSE, "classroom rug", "/rug.html")
    assert not ok and out == PROSE          # no spot → no change


def test_linkify_never_touches_existing_anchor_links_free_occurrence():
    d = '<p>See our <a href="/a.html">wooden train</a> and more wooden train fun.</p>'
    out, ok = li.linkify_phrase(d, "wooden train", "/b.html")
    assert ok
    assert out.count("<a ") == 2                      # original + new, original intact
    assert '<a href="/a.html">wooden train</a>' in out
    assert '<a href="/b.html">wooden train</a>' in out


def test_linkify_case_insensitive_keeps_page_casing():
    d = "<p>Wooden Train Track Sets are great.</p>"
    out, ok = li.linkify_phrase(d, "wooden train track sets", "/w.html")
    assert ok
    assert '<a href="/w.html">Wooden Train Track Sets</a>' in out   # page's casing kept


def test_linkify_respects_word_boundary():
    d = "<p>The nametrain is not a name train.</p>"
    out, ok = li.linkify_phrase(d, "name train", "/n.html")
    assert ok
    assert "<a" in out and "nametrain is" in out       # did NOT match inside 'nametrain'


def test_weave_phrases_splits_woven_and_missed_and_no_double_url():
    html, woven, missed = li.weave_phrases(PROSE, [
        {"url": "/pnt.html", "phrase": "personalized name train"},
        {"url": "/none.html", "phrase": "no such phrase"},
        {"url": "/pnt.html", "phrase": "wooden train track sets"},   # url already linked
    ])
    assert [p["url"] for p in woven] == ["/pnt.html"]
    assert {p["url"] for p in missed} == {"/none.html", "/pnt.html"}
    assert html.count('href="/pnt.html"') == 1


def test_weave_snippet_marks_phrase_in_context():
    snip, matched = li.weave_context_snippet(PROSE, "personalized name train")
    assert matched == "personalized name train"
    assert "personalized name train" in snip and "<" not in snip   # plain text window


def test_weave_snippet_empty_when_absent():
    assert li.weave_context_snippet(PROSE, "classroom rug") == ("", "")


STYLED = ('<style>/* ===== Name Trains category brand styles ===== */ '
          '.nt-banner { position: relative; }</style>'
          '<p>Our personalized Name Trains spell any name.</p>')


def test_never_weaves_into_style_block():
    # "Name Trains" appears in a CSS comment AND in real prose. The CSS one must be
    # untouched; only the prose occurrence gets linked.
    out, ok = li.linkify_phrase(STYLED, "Name Trains", "/nt.html")
    assert ok
    assert "<style>" in out and "</style>" in out
    assert "/* ===== Name Trains category" in out          # CSS comment intact
    assert '<a href="/nt.html">Name Trains</a>' in out      # prose occurrence linked
    assert out.count("<a ") == 1                             # exactly one link added


def test_visible_text_drops_css_and_tags():
    vt = li.visible_text(STYLED)
    assert "position: relative" not in vt and "nt-banner" not in vt
    assert "personalized Name Trains spell any name" in vt


def test_weave_phrases_skips_style_only_phrase():
    # A phrase that exists ONLY inside the style block must miss (→ box fallback).
    css_only = '<style>.foo{content:"wooden train track";}</style><p>Hello world.</p>'
    html, woven, missed = li.weave_phrases(css_only, [{"url": "/w.html", "phrase": "wooden train track"}])
    assert woven == [] and [p["url"] for p in missed] == ["/w.html"]
    assert html == css_only                                  # untouched
