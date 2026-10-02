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
