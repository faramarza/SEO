"""Duplication detector tests — the shingling method must catch templated
passages while NOT flagging pages that merely share topical vocabulary."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analysis import duplication as dup   # noqa: E402


def test_shingles_and_overlap():
    a = dup.shingles("the quick brown fox jumps over the lazy dog again now".split(), n=4)
    b = dup.shingles("the quick brown fox sleeps under the lazy dog again now".split(), n=4)
    assert a and b
    o = dup.overlap_pct(a, b)
    assert 0 < o < 100


def test_page_words_strips_chrome():
    html = ("<nav>home shop about contact menu</nav><header>logo</header>"
            "<p>Montessori wooden toys support fine motor development in toddlers.</p>"
            "<footer>copyright privacy terms</footer>")
    w = dup.page_words(html)
    assert "montessori" in w and "toddlers" in w
    assert "home" not in w and "copyright" not in w  # chrome removed


def test_templated_pages_flagged_distinct_pages_not():
    boiler = ("our montessori wooden toys are crafted from natural sustainable wood "
              "with non toxic finishes built to last from toddler years through childhood "
              "designed to support hands on learning and independent play every single day ")
    # Two age pages: identical boilerplate + a swapped age line.
    p_3yo = boiler + "these toys are perfect for three year old children learning fast"
    p_4yo = boiler + "these toys are perfect for four year old children learning fast"
    # A genuinely distinct page: different topic, no shared passages.
    p_rug = ("classroom alphabet carpets sized for schools daycares and playrooms in "
             "durable colorful designs that make letter learning hands on find your size today "
             "with rubber backing and stain resistant fibers for busy early learning spaces")
    pages = [
        {"url": "http://x/montessori-toys-3-year-olds.html", "shingles": dup.shingles(p_3yo.split()), "impressions": 500, "title": "3yo"},
        {"url": "http://x/montessori-toys-4-year-olds.html", "shingles": dup.shingles(p_4yo.split()), "impressions": 300, "title": "4yo"},
        {"url": "http://x/alphabet-carpets.html", "shingles": dup.shingles(p_rug.split()), "impressions": 900, "title": "rug"},
    ]
    clusters = dup.find_clusters(pages, threshold=dup.DUP_THRESHOLD)
    # The two age pages cluster; the rug page does NOT join.
    assert len(clusters) == 1
    c = clusters[0]
    urls = {c["pillar"]["url"]} | {o["url"] for o in c["consolidate"]}
    assert urls == {"http://x/montessori-toys-3-year-olds.html",
                    "http://x/montessori-toys-4-year-olds.html"}
    assert "carpets" not in " ".join(urls)
    # Pillar = highest impressions of the cluster (3yo=500 > 4yo=300).
    assert c["pillar"]["url"].endswith("3-year-olds.html")
    assert c["max_overlap"] >= dup.DUP_THRESHOLD


def test_no_false_cluster_on_topical_vocab_only():
    # Same words, totally different sentence order/structure → few shared
    # 8-grams → must NOT cluster.
    import random
    vocab = "montessori wooden toys learning children play natural safe skills development".split()
    def scramble(seed):
        r = random.Random(seed)
        return " ".join(r.choice(vocab) for _ in range(200))
    pages = [{"url": f"http://x/p{i}.html", "shingles": dup.shingles(scramble(i).split()),
              "impressions": 100, "title": f"p{i}"} for i in range(4)]
    clusters = dup.find_clusters(pages, threshold=dup.DUP_THRESHOLD)
    assert clusters == []   # shared vocabulary, no shared passages → no cluster


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} tests passed")
