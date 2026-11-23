import json

import pytest

app_mod = pytest.importorskip("web.app")


class FakeMC:
    configured = True
    created, deleted = {}, []

    def store_scope(self):
        return "alphabettrains"

    def category_raw(self, cid):
        return {"custom_attributes": [{"attribute_code": "display_mode", "value": "PRODUCTS"}]}

    def category_product_skus(self, cid):
        return ["NT5", "PZ1"]

    def products_info(self, skus):
        cat = {"NT5": {"sku": "NT5", "name": "5 Letter Name Train", "price": 39.99, "url": "/nt5.html"},
               "PZ1": {"sku": "PZ1", "name": "Wooden Name Puzzle", "price": 34.95, "url": "/pz1.html"},
               "BK1": {"sku": "BK1", "name": "Personalized Baby Book", "price": 45.98, "url": "/bk1.html"}}
        return [dict(cat[s]) for s in skus if s in cat]

    def root_category_id(self):
        return 2

    def create_category(self, name, url_key, description, mt, md, parent):
        FakeMC.created[99] = {"name": name, "url_key": url_key, "description": description, "skus": []}
        return 99

    def assign_products(self, cid, skus):
        FakeMC.created[cid]["skus"] = list(skus)

    def delete_category(self, cid):
        FakeMC.deleted.append(cid)


@pytest.fixture
def client(monkeypatch, tmp_path):
    import src.data_sources.magento_client as m
    FakeMC.created, FakeMC.deleted = {}, []
    monkeypatch.setattr(m, "MagentoClient", FakeMC)
    page = {"description": "<p>Owner text about chairs.</p>"}

    def resolve(url, asset_type=""):
        ent = {"category_id": 7, "name": "Kids Chairs", "description": page["description"]}
        ent["_write_desc"] = lambda new: page.__setitem__("description", new)
        return FakeMC(), ent, None

    monkeypatch.setattr(app_mod, "_il_resolve_category", resolve)
    monkeypatch.setattr(app_mod, "_PC_DIR", tmp_path / "drafts")
    monkeypatch.setattr(app_mod, "_GOV_CHANGES", tmp_path / "gov.json")
    monkeypatch.setattr(app_mod, "DATA_PATH", tmp_path)
    (tmp_path / "product_demand_plan.json").write_text(json.dumps({"pages": [
        {"page": "/kids-chairs.html", "kind": "category", "action": "content", "value": 600, "page_name": "Kids Chairs",
         "searches": [{"search": "kids chair", "volume": 27100}]},
        {"page": None, "kind": "new", "action": "new_page", "value": 85, "line": "Gifts", "page_name": "Baby Gifts",
         "import_file": "nonexistent.csv", "searches": [{"search": "baby gifts", "volume": 22200}]}]}))

    def fake_llm(system, prompt, cfg, max_tokens=700):
        if "sections" in prompt:
            return {"intro": "Personalized gifts with their name.",
                    "sections": [{"heading": "For 1 year olds", "items": [{"sku": "NT5", "why": "x"}, {"sku": "PZ1", "why": "Spells it."}]}],
                    "faqs": [{"q": "Can I add a name?", "a": "Yes, every one is personalized."}],
                    "meta_title": "Baby Gifts | Personalized", "meta_description": "Gifts."}, None
        return {"intro": "Kids chairs for classrooms and playrooms. The #1 choice of teachers.",
                "faqs": [{"q": "What heights?", "a": "Several seat heights for different ages."}]}, None

    monkeypatch.setattr(app_mod, "_llm_json", fake_llm)
    monkeypatch.setattr(app_mod, "_pc_products_for",
                        lambda mc, t, cid=None: [dict(p, min_age=3 if p["sku"] == "NT5" else 0)
                                                 for p in FakeMC().products_info(["NT5", "PZ1", "BK1"])])
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "t"
    return c, page


def test_existing_page_intro_faq_approve_undo(client):
    c, page = client
    r = c.post("/api/page-content/generate", json={"limit": 2})
    if r.status_code in (302, 401):
        pytest.skip("login wall")
    drafts = {d["key"]: d for d in r.get_json()}
    d = drafts["/kids-chairs.html"]
    assert "#1" not in d["intro_html"] and d["template"] == "category"
    a = c.post("/api/page-content/approve", json={"id": d["id"]}).get_json()
    assert a["success"] and a["confirmed"]
    assert page["description"].startswith("<!-- governor-intro:start --><p>Kids chairs for classrooms")
    assert "<p>Owner text about chairs.</p>" in page["description"]
    assert "Frequently asked questions" in page["description"]
    u = c.post("/api/page-content/undo", json={"id": d["id"]}).get_json()
    assert u["success"] and page["description"] == "<p>Owner text about chairs.</p>"


def test_new_gift_page_created_with_products_and_undo_deletes(client, tmp_path):
    c, _ = client
    r = c.post("/api/page-content/generate", json={"limit": 2})
    if r.status_code in (302, 401):
        pytest.skip("login wall")
    d = {x["key"]: x for x in r.get_json()}["NEW:baby gifts"]
    assert "nt5.html" not in d["body_html"]          # 3+ name train kept out of the 1-year-olds section
    assert "|" not in d["draft"]["meta_title"]
    a = c.post("/api/page-content/approve", json={"id": d["id"]}).get_json()
    assert a["page"] == "/baby-gifts.html"
    assert FakeMC.created[99]["url_key"] == "baby-gifts" and FakeMC.created[99]["skus"]
    log = json.loads((tmp_path / "gov.json").read_text())
    assert log[0]["pages"] == ["/baby-gifts.html"] and log[0]["queries"][0] == "baby gifts"
    assert c.post("/api/page-content/undo", json={"id": d["id"]}).get_json()["success"]
    assert FakeMC.deleted == [99]
