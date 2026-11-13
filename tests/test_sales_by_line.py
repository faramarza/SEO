import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sales_by_line import by_month, line_of, summarize  # noqa: E402


def test_lines_and_summary():
    assert line_of("5 Letter Maple Landmark Alphabet Name Train") == "Name trains"
    assert line_of("Heirloom Name Puzzle Stool") == "Name puzzle stools"
    assert line_of("Carpets For Kids Microdots PLUS Rug Brown") == "Rugs"
    assert line_of("A Christmas Dream for Me Personalized Book") == "Personalized books"
    items = [{"sku": "A", "name": "Classroom Rug 6x9", "qty": 1, "revenue": 300, "order_id": 1},
             {"sku": "A", "name": "Classroom Rug 6x9", "qty": 1, "revenue": 300, "order_id": 2},
             {"sku": "B", "name": "Name Puzzle Stool", "qty": 2, "revenue": 150, "order_id": 2}]
    prod, lines = summarize(items)
    assert prod["A"]["qty"] == 2 and len(prod["A"]["orders"]) == 2
    assert lines["Rugs"]["revenue"] == 600 and len(lines["Name puzzle stools"]["orders"]) == 1


def test_by_month():
    items = [{"order_id": 1, "qty": 1, "revenue": 50, "month": "2026-05"},
             {"order_id": 1, "qty": 1, "revenue": 30, "month": "2026-05"},
             {"order_id": 2, "qty": 2, "revenue": 90, "month": "2026-07"}]
    assert by_month(items) == {"2026-05": (1, 2.0, 80.0), "2026-07": (1, 2.0, 90.0)}


def test_filter_skus():
    from scripts.sales_by_line import filter_skus
    items = [{"sku": "SM11"}, {"sku": "G526"}, {"sku": "sc2 "}]
    assert [i["sku"] for i in filter_skus(items, {"sm11", "sc2"})] == ["SM11", "sc2 "]
