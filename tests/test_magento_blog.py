"""Tests for the Mirasvit Blog MX write path — route probing, strict
content-only partial update, and url_key resolution. Loaded by file path so it
doesn't drag in the pydantic-dependent package __init__."""
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


# --- the content-only partial guard -------------------------------------------

def test_blog_guard_allows_content_only():
    _assert_blog_content_only({"post": {"content": "<p>x</p>"}})   # no raise


def test_blog_guard_blocks_extra_field():
    for bad in ({"content": "x", "url_key": "y"}, {"url_key": "y"}, {}):
        try:
            _assert_blog_content_only({"post": bad})
            assert False, f"should have refused {bad}"
        except MagentoError:
            pass


# --- helpers ------------------------------------------------------------------

def _client(req_fn, write_fn=None):
    c = MagentoClient(base_url="https://x", token="t")
    c._req = req_fn
    if write_fn:
        c._write = write_fn
    return c


def _no_route(path):
    return MagentoError(f"Magento GET {path} → 404: "
                        '{"message":"Request does not match any route."}')


# --- route probing + resolution -----------------------------------------------

def test_find_blog_uses_mx_route_when_present():
    raw = {"entity_id": 9, "url_key": "p", "name": "N", "content": "<p>b</p>"}
    seen = {}
    def req(method, path):
        seen["path"] = path
        assert path.startswith("/blog/post")          # MX shape tried first
        return {"items": [raw]}
    got = _client(req).find_blog_post_by_url("https://s/blog/p")
    assert got["entity_id"] == 9 and got["_base"] == "/blog/post"


def test_find_blog_falls_back_to_legacy_route():
    raw = {"entity_id": 3, "url_key": "p", "name": "N", "content": "c"}
    def req(method, path):
        if path.startswith("/blog/post"):
            raise _no_route(path)                      # MX not registered
        return {"items": [raw]}                        # legacy /blog answers
    got = _client(req).find_blog_post_by_url("https://s/blog/p")
    assert got["entity_id"] == 3 and got["_base"] == "/blog"


def test_find_blog_raises_when_no_route_at_all():
    def req(method, path):
        raise _no_route(path)
    try:
        _client(req).find_blog_post_by_url("https://s/blog/p")
        assert False
    except MagentoError as e:
        assert "not available" in str(e)


def test_find_blog_refuses_ambiguous_or_missing_when_route_exists():
    # route exists but 0 or 2+ matches → None (never guess)
    assert _client(lambda m, p: {"items": []}).find_blog_post_by_url("https://s/blog/p") is None
    two = {"items": [{"entity_id": 1}, {"entity_id": 2}]}
    assert _client(lambda m, p: two).find_blog_post_by_url("https://s/blog/p") is None


# --- write shaping ------------------------------------------------------------

def test_update_blog_sends_partial_content_only():
    cap = {}
    def fake_write(path, payload):
        cap["path"] = path; cap["payload"] = payload; return {"ok": 1}
    c = _client(lambda m, p: {"items": []}, write_fn=fake_write)
    c.update_blog_post_content(9, "<p>new</p>", base="/blog/post")
    assert cap["path"] == "/blog/post/9"
    assert cap["payload"] == {"post": {"content": "<p>new</p>"}}


def test_update_blog_honours_probed_base():
    cap = {}
    c = _client(lambda m, p: {"items": []},
                write_fn=lambda path, payload: cap.setdefault("path", path))
    c.update_blog_post_content(3, "x", base="/blog")
    assert cap["path"] == "/blog/3"
