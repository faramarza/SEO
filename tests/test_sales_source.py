from scripts.sales_source import by_source, label, NOT_SEEN


def test_orders_grouped_by_where_the_visit_came_from():
    orders = [{"number": "100", "revenue": 50.0}, {"number": "101", "revenue": 60.0},
              {"number": "102", "revenue": 70.0}, {"number": "103", "revenue": 40.0}]
    ga4 = {"100": "google / cpc", "101": "google / organic", "102": "google / organic"}
    out = dict((s, (n, r)) for s, n, r in by_source(orders, ga4))
    assert out[label("google / organic")] == (2, 130.0)
    assert out[label("google / cpc")] == (1, 50.0)
    assert out[NOT_SEEN] == (1, 40.0)
    assert label("(direct) / (none)").startswith("Direct")
