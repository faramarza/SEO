import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sales_by_line import line_of, summarize  # noqa: E402


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
