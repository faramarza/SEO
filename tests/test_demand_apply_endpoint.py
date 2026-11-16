"""The 'Add these links for me' button on demand tasks, end to end on a fake page."""
import pytest

app_mod = pytest.importorskip("web.app")


def test_apply_adds_link_at_paragraph_end_and_keeps_original(monkeypatch, tmp_path):
    page = {"description": "<p>These rugs promote calming sensory input.</p>"}
    written = []

    def fake_resolve(url, asset_type=""):
        ent = {"category_id": 7, "name": "Sensory Carpets", "description": page["description"]}
        ent["_write_desc"] = lambda new: (written.append(new), page.__setitem__("description", new))
        return None, ent, None

    monkeypatch.setattr(app_mod, "_il_resolve_category", fake_resolve)
    monkeypatch.setattr(app_mod, "_INTERLINK_WRITES_PATH", tmp_path / "w.json")
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "test"
    link = {"url": "/sensory-carpets.html", "name": "Sensory Carpets", "find": "calming sensory input.",
            "paste": 'See our <a href="/playroom-carpets.html">playroom carpets</a>.'}
    r = c.post("/api/demand/apply-links", json={"links": [link]})
    if r.status_code in (302, 401):
        pytest.skip("login wall in this app config")
    res = r.get_json()["results"][0]
    assert res["ok"] and res["confirmed"]
    assert page["description"].endswith('input. See our <a href="/playroom-carpets.html">playroom carpets</a>.</p>')
    assert app_mod._il_writes_load()["cat:7"]["original_description"] == "<p>These rugs promote calming sensory input.</p>"
    # second click: already there, nothing written again
    r2 = c.post("/api/demand/apply-links", json={"links": [link]}).get_json()["results"][0]
    assert r2["why"] == "already links there" and len(written) == 1
