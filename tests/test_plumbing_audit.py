"""Plumbing Audit checks: parameter/system junk detection, canonicals,
internal-link findings, and the measured junk ratio."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis import plumbing_audit as pa   # noqa: E402


def test_parameter_and_system_url_detection():
    assert pa.is_parameter_url("https://x.com/toys.html?product_list_order=price")
    assert pa.is_parameter_url("https://x.com/toys.html?p=3")
    assert not pa.is_parameter_url("https://x.com/toys.html")
    assert pa.is_system_url("https://x.com/catalogsearch/result/?q=a")
    assert pa.is_system_url("https://x.com/checkout/cart/")
    assert not pa.is_system_url("https://x.com/montessori-toys.html")


def test_check_A_flags_param_and_system_and_sitemap_gaps():
    crawl = [
        {"url": "https://x.com/toys.html?p=2", "status": 200},
        {"url": "https://x.com/catalogsearch/result/?q=z", "status": 200},
        {"url": "https://x.com/toys.html", "status": 200, "canonical": "https://x.com/toys.html"},
    ]
    gsc = [{"url": "https://x.com/secret.html", "impressions": 300},
           {"url": "https://x.com/toys.html", "impressions": 900}]
    sitemap = {"https://x.com/toys.html"}
    out = {f["id"]: f for f in pa.check_A_indexation(crawl, gsc, sitemap)}
    assert "param_urls" in out and out["param_urls"]["url_count"] == 1
    assert "system_urls" in out
    # secret.html has impressions, isn't junk, isn't in sitemap → flagged
    assert "gsc_not_in_sitemap" in out
    assert "https://x.com/secret.html" in out["gsc_not_in_sitemap"]["examples"]
    # toys.html IS in the sitemap → not flagged
    assert "https://x.com/toys.html" not in out["gsc_not_in_sitemap"]["examples"]


def test_check_A_sitemap_bad_when_noindex_or_redirect():
    crawl = [
        {"url": "https://x.com/gone.html", "status": 301, "canonical": ""},
        {"url": "https://x.com/hidden.html", "status": 200, "robots": "noindex,follow"},
        {"url": "https://x.com/ok.html", "status": 200, "canonical": "https://x.com/ok.html"},
    ]
    out = {f["id"]: f for f in pa.check_A_indexation(
        crawl, [], {"https://x.com/gone.html", "https://x.com/hidden.html",
                    "https://x.com/ok.html"})}
    bad = out["sitemap_bad"]["examples"]
    assert "https://x.com/gone.html" in bad and "https://x.com/hidden.html" in bad
    assert "https://x.com/ok.html" not in bad


def test_check_B_missing_and_non200_canonical_and_google_mismatch():
    crawl = [
        {"url": "https://x.com/a.html", "status": 200, "canonical": ""},
        {"url": "https://x.com/b.html", "status": 200, "canonical": "https://x.com/dead.html"},
        {"url": "https://x.com/dead.html", "status": 404},
        {"url": "https://x.com/c.html", "status": 200, "canonical": "https://x.com/c.html"},
    ]
    ins = {"https://x.com/c.html": {"user_canonical": "https://x.com/c.html",
                                    "google_canonical": "https://x.com/other.html"}}
    out = {f["id"]: f for f in pa.check_B_canonicals(crawl, ins)}
    assert "https://x.com/a.html" in out["missing_canonical"]["examples"]
    assert "https://x.com/b.html" in out["canonical_non200"]["examples"]
    assert "https://x.com/c.html" in out["google_canonical_mismatch"]["examples"]


def test_check_C_orphans_deep_and_underlinked():
    crawl = [
        {"url": "https://x.com/deep.html", "status": 200, "asset_type": "product",
         "depth": 5, "inlinks": 4},
        {"url": "https://x.com/orphan.html", "status": 200, "asset_type": "category",
         "depth": 2, "inlinks": 0},
        {"url": "https://x.com/top.html", "status": 200, "asset_type": "product",
         "depth": 2, "inlinks": 1},
    ]
    gsc = [{"url": "https://x.com/top.html", "impressions": 5000}]
    sitemap = {"https://x.com/orphan.html"}
    out = {f["id"]: f for f in pa.check_C_internal_links(crawl, gsc, sitemap)}
    assert "https://x.com/deep.html" in out["deep_pages"]["examples"]
    assert "https://x.com/orphan.html" in out["orphans"]["examples"]
    assert "https://x.com/top.html" in out["underlinked_top"]["examples"]


def test_junk_ratio_measured():
    crawl = [{"url": "https://x.com/toys.html?p=2"},
             {"url": "https://x.com/checkout/cart/"},
             {"url": "https://x.com/toys.html"},
             {"url": "https://x.com/rugs.html"}]
    jr = pa.junk_ratio(crawl, [])
    assert jr["total"] == 4 and jr["junk"] == 2 and jr["ratio"] == 0.5


def test_run_audit_ranks_high_first_and_every_finding_has_a_fix():
    crawl = [{"url": "https://x.com/a.html?p=2", "status": 200},
             {"url": "https://x.com/a.html", "status": 200, "canonical": ""}]
    res = pa.run_audit(crawl_rows=crawl, gsc_pages=[], sitemap_urls=set())
    assert res["findings"]
    sev = [pa._SEV_RANK[f["severity"]] for f in res["findings"]]
    assert sev == sorted(sev)                       # high severity first
    for f in res["findings"]:
        assert f["proposed_fix"] and f["magento_location"] and f["risk"]
        assert f["examples"] and f["url_count"] >= 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
