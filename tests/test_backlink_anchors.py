import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backlink_anchors import anchor_kind, page_words  # noqa: E402


def test_anchor_kinds():
    pw = page_words("/classroom-rugs.html")
    assert anchor_kind("Alphabet Trains", pw) == "brand"
    assert anchor_kind("https://alphabet-trains.com", pw) == "brand"
    assert anchor_kind("www.example.com/x", pw) == "url"
    assert anchor_kind("click here", pw) == "generic"
    assert anchor_kind("", pw) == "empty/image"
    assert anchor_kind("colorful classroom carpets", pw) == "keyword"
    assert anchor_kind("great toys", pw) == "other"


def test_spam_pattern():
    from scripts.backlink_anchors import SPAM
    assert SPAM.search("high quality dofollow backlinks da 50 pa 40 premium")
    assert not SPAM.search("classroom rugs") and not SPAM.search("alphabet trains")
