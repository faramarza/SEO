from scripts.page_queries import group


def test_url_variants_merge_into_one_page():
    rows = [{"query": "q", "page": "https://x.com/a.html", "impressions": 30, "clicks": 1, "position": 10},
            {"query": "q", "page": "https://x.com/a.html?p=2", "impressions": 10, "clicks": 0, "position": 20},
            {"query": "q", "page": "https://x.com/b.html", "impressions": 5, "clicks": 0, "position": 30}]
    g = group(rows, ["a.html", "b.html"])
    a = [r for r in g["q"] if r["path"] == "/a.html"][0]
    assert len(g["q"]) == 2 and a["impressions"] == 40 and a["position"] == 12.5
