"""iter_all_products must request a stable sort order and never yield a SKU twice."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.data_sources.magento_client import MagentoClient  # noqa: E402


class FakeClient(MagentoClient):
    def __init__(self, pages):
        super().__init__(base_url="https://x", token="t")
        self.pages, self.queries = pages, []

    def _req(self, method, path, payload=None, scope="all"):
        self.queries.append(path)
        page = int(path.split("currentPage]=")[1].split("&")[0])
        items = self.pages[page - 1] if page <= len(self.pages) else []
        return {"items": items, "total_count": sum(len(p) for p in self.pages)}


def _p(i):
    return {"id": i, "sku": f"S{i}", "custom_attributes": [{"attribute_code": "visibility", "value": "4"}]}


def test_sorted_and_deduped():
    fc = FakeClient([[_p(1), _p(2)], [_p(2), _p(3)]])   # SKU S2 on two pages
    skus = [p["sku"] for p in fc.iter_all_products(page_size=2)]
    assert skus == ["S1", "S2", "S3"]
    assert all("sortOrders][0][field]=entity_id" in q for q in fc.queries)


def _run():
    test_sorted_and_deduped()
    print("  PASS test_sorted_and_deduped\n1 tests passed")


if __name__ == "__main__":
    _run()
