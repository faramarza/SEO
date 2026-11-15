from src.analysis.task_rules import task_verdict, apply


def test_rules():
    recent = {"/alphabet-themed-carpets.html": "2026-11-10"}
    assert task_verdict("https://x/shop-by-brands/iseeme-books.html", "iseeme", recent)[0] == 0
    assert task_verdict("https://x/alphabet-themed-carpets.html", "alphabet carpet", recent)[0] == 0
    assert task_verdict("https://x/ch-personalized-step-stool.html", "name stool", recent)[0] == 0
    assert task_verdict("https://x/speedster-book.html", "personalized race car book", recent)[0] == 0.25
    assert task_verdict("https://x/name-trains.html", "name train", recent)[0] == 1.0


def test_apply_demotes_and_drops():
    tasks = [{"url": "https://x/a-book.html", "metric": {"query": "storybook"}, "lever_score": 100},
             {"url": "https://x/shop-by-brands/iseeme.html", "metric": {"query": "iseeme"}, "lever_score": 500},
             {"url": "https://x/sensory-toys.html", "metric": {"query": "sensory toys"}, "lever_score": 50}]
    kept, dropped = apply(tasks, recent={})
    assert [t["url"] for t in kept] == ["https://x/a-book.html", "https://x/sensory-toys.html"]
    assert kept[0]["lever_score"] == 25 and len(dropped) == 1


def test_small_decay_is_noise():
    from src.analysis.action_plan import _from_decay
    out = []
    _from_decay({"decaying": [{"url": "https://x/toy-chest.html", "peak_clicks": 10, "current_clicks": 5},
                              {"url": "https://x/name-trains.html", "peak_clicks": 80, "current_clicks": 40}]}, out)
    assert [t["url"] for t in out] == ["https://x/name-trains.html"]
