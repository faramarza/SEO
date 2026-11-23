import json

import pytest

from src.analysis import reddit_helper as rh

SERPER = {"organic": [
    {"title": "Best rug for a pre-K classroom? : r/Teachers", "link": "https://www.reddit.com/r/Teachers/comments/1abc23x/best_rug_for_a_prek_classroom/",
     "snippet": "Our old rug is falling apart, need something 8x12..."},
    {"title": "Not a thread", "link": "https://www.reddit.com/r/Teachers/wiki/index"}]}


def test_parse_and_dedupe():
    t = rh.parse_results(SERPER, "Teachers", "classroom rugs")
    assert len(t) == 1 and t[0]["id"] == "1abc23x" and t[0]["title"] == "Best rug for a pre-K classroom?"
    assert rh.new_threads(t + t, seen=set()) == t
    assert rh.new_threads(t, seen={"1abc23x"}) == []


def test_warm_up_removes_store_mentions_links_and_fake_claims():
    text = ("A low-pile rug with squares helps kids find a spot. Look for a non-slip back. My kids loved ours. "
            "Full disclosure: I run a small toy shop (Alphabet Trains), so I'm biased, but our seating rugs work. "
            "See https://alphabet-trains.com/classroom-rugs.html for options.")
    warm = rh.clean_reply(text, mention=False)
    assert "Alphabet Trains" not in warm and "http" not in warm and "loved" not in warm
    assert warm.startswith("A low-pile rug with squares")
    after = rh.clean_reply(text, mention=True)
    assert "Full disclosure" in after and "http" not in after


def test_prompt_respects_mode():
    t = {"sub": "Teachers", "title": "rug?", "snippet": "help", "offer": "classroom rugs"}
    assert "Do NOT mention" in rh.user_prompt(t, False)
    assert "Full disclosure" in rh.user_prompt(t, True)


app_mod = pytest.importorskip("web.app")


def test_find_and_mark(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "_RD_FILE", tmp_path / "rd.json")
    monkeypatch.setattr(app_mod, "_serper_search", lambda q: SERPER if "Teachers" in q else {"organic": []})
    monkeypatch.setattr(app_mod, "_llm_json", lambda s, p, c, max_tokens=500: (
        {"reply": "Get a low-pile rug. Full disclosure: I run a small toy shop (Alphabet Trains).", "fits": True}, None))
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "t"
    r = c.post("/api/reddit/find")
    if r.status_code in (302, 401):
        pytest.skip("login wall")
    assert r.get_json()["new"] == 1
    d = json.loads((tmp_path / "rd.json").read_text())
    assert d["threads"][0]["draft"] == "Get a low-pile rug."          # warm-up: no store mention
    assert c.post("/api/reddit/find").get_json()["new"] == 0          # same thread not shown twice
    c.post("/api/reddit/mark", json={"id": "1abc23x", "status": "posted", "draft": "edited"})
    d = json.loads((tmp_path / "rd.json").read_text())
    assert d["threads"][0]["status"] == "posted" and d["threads"][0]["draft"] == "edited"
