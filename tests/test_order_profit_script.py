import sys

import scripts.order_profit as op


class FakeMC:
    configured = True

    def _orders_date_query(self, *a, **k):
        return "/orders"

    def _req(self, method, path):
        return {"total_count": 2, "items": [
            {"increment_id": "2000010217", "created_at": "2026-11-16 10:06:00", "status": "processing",
             "base_grand_total": 481.95, "base_shipping_amount": 0, "base_tax_amount": 0, "base_total_refunded": 0,
             "items": [{"sku": "13728", "name": "Calming Circles Rug", "qty_ordered": 1,
                        "base_row_total": 481.95, "base_discount_amount": 0}]},
            {"increment_id": "2000010184", "created_at": "2026-11-08 10:00:00", "status": "closed",
             "base_grand_total": 119.99, "base_shipping_amount": 0, "base_tax_amount": 0, "base_total_refunded": 119.99,
             "items": [{"sku": "T1", "name": "Montessori Wooden Ball Tracker", "qty_ordered": 1,
                        "base_row_total": 119.99, "base_discount_amount": 0}]}]}


def test_whole_run(monkeypatch, capsys, tmp_path):
    import src.data_sources.magento_client as m
    monkeypatch.setattr(m, "MagentoClient", FakeMC)
    awin = tmp_path / "awin.csv"
    awin.write_text('"extra","commission","commission_status"\n"2000010217","48.20","0"\n')
    monkeypatch.setattr(sys, "argv", ["order_profit.py", "--no-ga4", "--list", "--awin", str(awin)])
    op.main()
    out = capsys.readouterr().out
    assert "ORDERS" in out and "PROFIT before ads" in out
    assert "#2000010217" in out and "exact" in out and "refunded" in out
