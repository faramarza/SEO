import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.analysis import content_gap as cg


def _kw(k, v, p=5):
    return {"keyword": k, "volume": v, "position": p}


def test_gap_excludes_what_you_rank_for_and_low_volume():
    comp = [_kw("montessori toys for 2 year olds", 1900), _kw("montessori toys", 2600),
            _kw("montessori nap mat", 10)]
    yours = [_kw("montessori toys", 2600)]
    gap = [g["keyword"] for g in cg.compute_gap(comp, yours, min_volume=30)]
    assert "montessori toys" not in gap          # you already rank
    assert "montessori nap mat" not in gap       # below min volume
    assert "montessori toys for 2 year olds" in gap


def test_clustering_separates_topics_not_the_niche_name():
    comp = [_kw(k, v) for k, v in [
        ("montessori toys for 1 year old", 2400), ("montessori toys for 2 year olds", 1900),
        ("best montessori toys for toddlers", 1600), ("montessori toys for 3 year olds", 1100),
        ("montessori bookshelf", 880), ("montessori wooden blocks", 600),
        ("personalized montessori name puzzle", 390), ("what is the montessori method", 140),
        ("montessori vs waldorf", 480), ("montessori shelf ideas", 720)]]
    clusters = cg.cluster_topics(cg.compute_gap(comp, []))
    # The age-variant "toys for X" keywords should collapse into ONE cluster,
    # not fragment, and not swallow bookshelf/puzzle/blocks.
    assert len(clusters) >= 4
    toys_cluster = max(clusters, key=lambda c: len(c["keywords"]))
    kws = [k["keyword"] for k in toys_cluster["keywords"]]
    assert sum("toys for" in k for k in kws) >= 3


def test_intent_and_plan_shape():
    assert cg._intent("montessori vs waldorf") == "informational"
    assert cg._intent("what is the montessori method") == "informational"
    assert cg._intent("personalized montessori name puzzle") == "commercial"
    assert cg._intent("montessori toys for 1 year old") == "commercial"  # shop turf
    comp = [_kw(k, v) for k, v in [
        ("montessori toys for 1 year old", 2400), ("montessori toys for 2 year olds", 1900),
        ("best montessori toys for toddlers", 1600), ("montessori bookshelf", 880),
        ("montessori wooden blocks", 600), ("personalized montessori name puzzle", 390),
        ("montessori shelf ideas", 720), ("what is the montessori method", 140)]]
    plan = cg.build_plan(cg.cluster_topics(cg.compute_gap(comp, [])))
    a = plan["articles"][0]
    assert a["role"].startswith("pillar")
    assert a["primary_keyword"]
    assert a["word_count_target"] > 0
    assert any("H2" in s or "Intro" in s for s in a["outline"])
    assert plan["article_count"] >= 4
    assert all("links_to_pillar" in x for x in plan["articles"][1:])  # spokes link up
