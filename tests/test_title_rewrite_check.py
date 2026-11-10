from scripts.title_rewrite_check import style, verdict, summarize


def test_style():
    assert style("Name Trains | Wooden") == "separator"
    assert style("Wooden Blocks - Sets") == "separator"
    assert style("Personalized Wooden Name Trains Made in USA") == "plain"
    assert style("Hand-made Trains") == "plain"


def test_verdict():
    assert verdict("Name Trains Made in USA", "Name Trains Made in USA") == "kept"
    assert verdict("Alphabet Name Train Toys Personalized Wooden Letter Trains",
                   "Alphabet Name Train Toys Personalized Wooden Letter ...") == "truncated"
    assert verdict("Name Trains | Personalized Wooden", "Name Trains - Alphabet Trains") == "site-name-changed"
    assert verdict("Buy 5 Letter Name Train at Discount", "5 Letter Maple Landmark Name Train") == "rewritten"


def test_summarize():
    s = summarize([{"style": "plain", "verdict": "kept"}, {"style": "plain", "verdict": "rewritten"}])
    assert s["plain"]["kept"] == 1 and s["plain"]["rewritten"] == 1
