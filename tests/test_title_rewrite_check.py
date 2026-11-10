from scripts.title_rewrite_check import style, verdict, summarize


def test_style():
    assert style("Name Trains | Wooden") == "pipe"
    assert style("Wooden Blocks - Sets") == "dash"
    assert style("Fun Dance Games: 51 Ideas") == "colon"
    assert style("Personalized Wooden Name Trains Made in USA") == "none"
    assert style("Hand-made Trains") == "none"


def test_verdict():
    assert verdict("Name Trains Made in USA", "Name Trains Made in USA") == "kept"
    assert verdict("Alphabet Name Train Toys Personalized Wooden Letter Trains",
                   "Alphabet Name Train Toys Personalized Wooden Letter ...") == "truncated"
    assert verdict("Name Trains | Personalized Wooden", "Name Trains - Alphabet Trains") == "site-name-changed"
    assert verdict("Buy 5 Letter Name Train at Discount", "5 Letter Maple Landmark Name Train") == "rewritten"


def test_summarize():
    s = summarize([{"style": "none", "verdict": "kept"}, {"style": "none", "verdict": "rewritten"}])
    assert s["none"]["kept"] == 1 and s["none"]["rewritten"] == 1
