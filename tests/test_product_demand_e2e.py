"""Whole-run test of scripts/product_demand.py on a small fake store, with the kinds
of junk real data produces. Unit tests only checked one function at a time, so
problems that appear when the pieces run together reached Frank's server first."""
import json
import shutil
import sys

import scripts.line_demand as ld
import scripts.product_demand as pd
from src.analysis import task_rules

CATS = {1: {"name": "Name Trains", "active": True, "url_key": "name-trains", "url_path": "name-trains"},
        2: {"name": "Classroom Rugs", "active": True, "url_key": "classroom-rugs", "url_path": "classroom-rugs"},
        3: {"name": "Personalized Books for Kids", "active": True, "url_key": "personalized-books-for-kids",
            "url_path": "personalized-books-for-kids"},
        4: {"name": "Personalized Step Stools", "active": True, "url_key": "personalized-step-stools",
            "url_path": "personalized-step-stools"},
        5: {"name": "Wooden Name Puzzles", "active": True, "url_key": "wooden-name-puzzles", "url_path": "wooden-name-puzzles"},
        6: {"name": "Montessori Language Development Toys", "active": True, "url_key": "montessori-language",
            "url_path": "montessori-language"},
        7: {"name": "Kids Furniture", "active": True, "url_key": "kids-furniture", "url_path": "kids-furniture"}}
PRODUCTS = [
    {"sku": "NT5", "name": "5 Letter Name Train", "price": 49.99, "url": "/5-letter-name-train.html", "cats": [1]},
    {"sku": "NT7", "name": "7 Letter Name Train", "price": 59.99, "url": "/7-letter-name-train.html", "cats": [1]},
    {"sku": "R1", "name": "Carpets for Kids Reading Rug 6x9", "price": 361.95, "url": "/reading-rug.html", "cats": [2]},
    {"sku": "B1", "name": "Pirate Tale Personalized Storybook", "price": 37.99, "url": "/pirate.html", "cats": [3]},
    {"sku": "S1", "name": "Personalized Name Puzzle Stool", "price": 150.99, "url": "/stool.html", "cats": [4]},
    {"sku": "P1", "name": "Personalized Wooden Name Puzzle", "price": 34.95, "url": "/name-puzzle.html", "cats": [5]},
    {"sku": "S2", "name": "Name Puzzle Stool with Two Names", "price": 91.99, "url": "/stool-two.html", "cats": [4, 5]},
    {"sku": "AT", "name": "Alphabet Train Set", "price": 89.99, "url": "/alphabet-train-set.html", "cats": [1, 6]},
    {"sku": "TC", "name": "Wooden Personalized Toy Chest", "price": 395.99, "url": "/toy-chest.html", "cats": [7]},
    {"sku": "BC", "name": "Block Cart And Building Block Set", "price": 470.81, "url": "/block-cart.html", "cats": [7]},
]
VOLUMES = {"name train": 720, "classroom rug": 14800, "personalized toddler gifts": 2400,
           "personalized books for kids": 8100, "wooden train table": 9900,
           "alphabet trains": 500, "gifts for 40 year old": 5000, "name train necklace": 3000,
           "i see me personalized book": 4400, "name puzzle stool": 590, "name puzzle": 2400, "alphabet train": 2400,
           "toy boxes little tikes": 6600, "building block daycare": 2400,
           "building block": 33100}
SHOP = {  # what Google Shopping shows
    "name train": [("Name Trains in Bright Colors", 44.95), ("Letter Name Train", 6.99),
                   ("Personalized Wooden Name Train", 38.99), ("Nametrain Clear Finish", 7.0)],
    "name train necklace": [("Gold Nameplate Necklace", 25.0), ("Sterling Name Necklace", 40.0),
                            ("Custom Script Necklace", 30.0)],
    "name puzzle stool": [("Personalized Name Puzzle Step Stool for Kids", 80.0), ("Kids Name Stool", 90.0)],
    "name puzzle": [("Personalized Name Puzzle for Toddlers", 35.0), ("Wooden Name Puzzle", 30.0)],
    "alphabet train": [("Wooden Alphabet Train for Kids", 40.0), ("ABC Train Toy", 30.0)],
    "toy boxes little tikes": [("Little Tikes Toy Chest for Kids", 60.0)],
    "building block daycare": [("Kids Building Blocks Set", 40.0), ("Toddler Blocks", 30.0)],
    "classroom rug": [("Classroom Rug for Kids 6x9", 280.0), ("Preschool Circle Time Carpet", 320.0)],
    "personalized toddler gifts": [("Personalized Toddler Name Puzzle", 39.0), ("Personalized Kids Book", 30.0)],
    "personalized books for kids": [("Personalized Kids Book", 30.0), ("Custom Baby Book", 35.0)],
    "wooden train table": [("Rustic Farmhouse Dining Table", 900.0), ("Solid Oak Coffee Table", 400.0),
                           ("Train Table for Kids", 150.0), ("Industrial Bar Table", 300.0)],
}


def run(monkeypatch, tmp_path, capsys, recent=None):
    (tmp_path / "config").mkdir()
    shutil.copy(pd.ROOT / "config" / "defaults.json", tmp_path / "config" / "defaults.json")
    (tmp_path / "data").mkdir()
    (tmp_path / "docs" / "imports").mkdir(parents=True)
    monkeypatch.setattr(pd, "ROOT", tmp_path)
    monkeypatch.setattr(pd, "CACHE", tmp_path / "data" / "cache.json")
    monkeypatch.setattr(ld, "_magento", lambda: object())
    monkeypatch.setattr(pd, "load_catalog", lambda mc: (CATS, [dict(p) for p in PRODUCTS]))
    monkeypatch.setattr(pd, "search_volumes",
                        lambda kws: {k: {"volume": VOLUMES[k], "cpc": 1.0} for k in kws if k in VOLUMES})
    monkeypatch.setattr(pd, "expand", lambda seed, cache: [{"keyword": k, "volume": v, "cpc": 1.0}
                                                         for k, v in VOLUMES.items() if seed in k])
    monkeypatch.setattr(pd, "load_gsc_queries", lambda cfg: {
        "alphabet train": {"position": 7.0, "clicks": 18, "impressions": 2000},
        "name train": {"position": 10.0, "clicks": 16, "impressions": 931},
        "alphabet trains": {"position": 1.0, "clicks": 300, "impressions": 900},
        "classroom rug": {"position": 14.0, "clicks": 1, "impressions": 800}})
    monkeypatch.setattr(pd, "organic_conversion", lambda cfg: (0.0115, 0, 0))
    monkeypatch.setattr(pd, "product_margin", lambda p, cfg: 0.3)
    monkeypatch.setattr(pd, "_shopping_items", lambda q: SHOP.get(q, [("Adult thing", 10.0)]))
    monkeypatch.setattr(pd, "load_rank_pages", lambda cfg: {"alphabet train": "/alphabet-train-set.html",
                                                            "name train": "/blog/name-trains-history.html"})
    monkeypatch.setattr(pd, "search_intents", lambda kws, cache: {
        k: ("navigational", 0.9) if "daycare" in k else ("commercial", 0.8) for k in kws})
    monkeypatch.setattr(pd, "live_title_h1", lambda url: ("Some Title", "Some H1"))
    monkeypatch.setattr(pd, "aio_check", lambda searches, n=2: [])
    monkeypatch.setattr(task_rules, "recently_changed", lambda *a, **k: dict(recent or {}))
    import scripts.link_sources as ls
    monkeypatch.setattr(ls, "list_posts", lambda mc: [])
    monkeypatch.setattr(sys, "argv", ["product_demand.py"])
    pd.main()
    out = capsys.readouterr().out
    plan = json.loads((tmp_path / "data" / "product_demand_plan.json").read_text())
    return out, plan


def test_every_line_shows_up_and_junk_never_does(monkeypatch, tmp_path, capsys):
    out, plan = run(monkeypatch, tmp_path, capsys)
    line_check = out.split("LINE CHECK")[1].split("PAGE PLAN")[0]
    assert "name train" in line_check
    assert "classroom rug" in line_check
    # junk and adults are never listed anywhere
    import re
    assert int(re.search(r"dropped (\d+) that Google shows as not for kids", out).group(1)) >= 1
    for bad in ("wooden train table", "gifts for 40 year old", "alphabet trains", "name train necklace",
                "i see me personalized book"):
        assert bad not in out.split("All ")[0].split("example product")[1], bad
        assert bad not in line_check, bad
    for g in plan["pages"]:   # every task is complete on its own
        assert g["how"] and "Claude" not in " ".join(g["how"]), g["how"]
    pages = {g["page"] or "NEW" for g in plan["pages"]}
    assert "/classroom-rugs.html" in pages


def test_gift_search_makes_one_gift_page_without_stools(monkeypatch, tmp_path, capsys):
    out, plan = run(monkeypatch, tmp_path, capsys)
    gift = [g for g in plan["pages"] if g["action"] == "new_page"]
    assert len(gift) == 1 and gift[0]["searches"][0]["search"] == "personalized toddler gifts"
    rows = (tmp_path / gift[0]["import_file"]).read_text()
    assert "NT5" in rows and "B1" in rows and "S1" not in rows


def test_recently_changed_pages_wait(monkeypatch, tmp_path, capsys):
    out, plan = run(monkeypatch, tmp_path, capsys, recent={"/classroom-rugs.html": "2026-11-10"})
    assert "/classroom-rugs.html" not in {g["page"] for g in plan["pages"]}
    assert "/classroom-rugs.html" in {g["page"] for g in plan["waiting"]}
    assert "WAIT: page changed 2026-11-10" in out


def test_this_runs_owners_and_matches(monkeypatch, tmp_path, capsys):
    """Nov 15 run: alphabet train went to a Montessori category, name puzzle stool was
    priced as a puzzle, a Little Tikes search and a daycare's name were planned."""
    out, plan = run(monkeypatch, tmp_path, capsys)
    owner = {x["search"]: g["page"] for g in plan["pages"] for x in g["searches"]}
    assert owner.get("alphabet train") == "/alphabet-train-set.html"
    import csv
    rows = {r["search"]: r for r in csv.DictReader(open(tmp_path / "data" / "product_demand.csv"))}
    assert "Stool" in rows["name puzzle stool"]["example"]
    assert "toy boxes little tikes" not in rows
    assert rows["building block daycare"]["flag"] == "NAV"
    assert "building block daycare" not in owner
