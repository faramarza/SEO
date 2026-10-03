"""Tests for the Mirasvit Blog MX write path — resolution, strict content-only
guard, and read-modify-write payload shaping. Loaded by file path so it doesn't
drag in the pydantic-dependent package __init__."""
import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "data_sources",
                     "magento_client.py")
_spec = importlib.util.spec_from_file_location("magento_client_under_test", _PATH)
mc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc_mod)

MagentoClient = mc_mod.MagentoClient
MagentoError = mc_mod.MagentoError
_assert_blog_content_only = mc_mod._assert_blog_content_only


# --- the content-only guard ---------------------------------------------------

ORIG = {"entity_id": 7, "url_key": "my-post", "name": "My Post",
        "status": 2, "content": "<p>Old body.</p>", "meta_title": "T"}


def test_blog_guard_allows_content_only_change():
    post = dict(ORIG); post["content"] = "<p>New body.</p>"
    _assert_blog_content_only({"post": post}, ORIG)   # no raise


def test_blog_guard_blocks_other_field_change():
    post = dict(ORIG); post["content"] = "x"; post["url_key"] = "hacked"
    try:
        _assert_blog_content_only({"post": post}, ORIG)
        assert False, "should have refused"
    except MagentoError as e:
        assert "non-content" in str(e)


def test_blog_guard_blocks_added_field():
    post = dict(ORIG); post["content"] = "x"; post["price"] = 9
    try:
        _assert_blog_content_only({"post": post}, ORIG)
        assert False, "should have refused"
    except MagentoError as e:
        assert "field set changed" in str(e)


def test_blog_guard_blocks_removed_field():
    post = {"entity_id": 7, "content": "x"}       # dropped url_key/name/status/meta
    try:
        _assert_blog_content_only({"post": post}, ORIG)
        assert False, "should have refused"
    except MagentoError as e:
        assert "field set changed" in str(e)


# --- resolution + write shaping (with a fake transport) -----------------------

def _client_with_req(req_fn, write_fn=None):
    c = MagentoClient(base_url="https://x", token="t")
    c._req = req_fn
    if write_fn:
        c._write = write_fn
    return c


def test_find_blog_post_resolves_single_match():
    raw = {"entity_id": 12, "url_key": "personalized-baby-gift-ideas",
           "name": "Gift Ideas", "content": "<p>Hi.</p>", "status": 2}
    c = _client_with_req(lambda m, p: {"items": [raw]})
    got = c.find_blog_post_by_url("https://s/blog/personalized-baby-gift-ideas")
    assert got["entity_type"] == "blog_post" and got["entity_id"] == 12
    assert got["content"] == "<p>Hi.</p>" and got["_raw"] == raw


def test_find_blog_post_refuses_ambiguous_and_missing():
    assert _client_with_req(lambda m, p: {"items": []}).find_blog_post_by_url("https://s/blog/x") is None
    two = {"items": [{"entity_id": 1}, {"entity_id": 2}]}
    assert _client_with_req(lambda m, p: two).find_blog_post_by_url("https://s/blog/x") is None


def test_update_blog_sends_post_wrapper_with_only_content_changed():
    raw = {"entity_id": 12, "url_key": "p", "name": "N", "status": 2,
           "content": "<p>Old.</p>", "meta_title": "T"}
    captured = {}
    def fake_write(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"entity_id": 12}
    c = _client_with_req(lambda m, p: {"items": [raw]}, write_fn=fake_write)
    c.update_blog_post_content(12, "<p>New with link.</p>", raw)
    assert captured["path"] == "/blog/12"
    assert set(captured["payload"].keys()) == {"post"}
    sent = captured["payload"]["post"]
    assert sent["content"] == "<p>New with link.</p>"
    # every other field identical to what we read
    for k, v in raw.items():
        if k != "content":
            assert sent[k] == v
    # and the original dict was NOT mutated
    assert raw["content"] == "<p>Old.</p>"


def test_update_blog_refuses_without_original():
    c = _client_with_req(lambda m, p: {"items": []})
    try:
        c.update_blog_post_content(1, "x", None)
        assert False
    except MagentoError as e:
        assert "original" in str(e)
