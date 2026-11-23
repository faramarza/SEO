from datetime import date

from src.analysis import money_report as mr

TODAY = date(2026, 11, 23)


def test_week_split_and_totals():
    res = [{"date": "2026-11-20", "revenue": 50, "contribution": 12, "basis": "exact"},
           {"date": "2026-11-12", "revenue": 40, "contribution": 8, "basis": "exact"},
           {"date": "2026-11-01", "revenue": 99, "contribution": 9, "basis": "exact"}]
    this, prev = mr.week_split(res, TODAY)
    assert len(this) == 1 and len(prev) == 1
    assert mr.totals(this) == {"orders": 1, "revenue": 50, "profit": 12, "margin_pct": 24.0}


def test_ads_line_nets_spend():
    res = [{"number": "1", "revenue": 100, "contribution": 25}, {"number": "2", "revenue": 60, "contribution": 15}]
    a = mr.ads_line(res, {"1": "Google Ads (google / cpc)"}, 60.0)
    assert a["profit_before_ads"] == 25 and a["net"] == -35 and a["roas"] == 1.67
    assert mr.ads_line(res, {}, None)["spend"] is None


def test_awin_actions_follow_the_return_window():
    rows = [{"commission_status": "0", "extra": "2000010100", "date": "2026-10-01 11:33:00", "id": "0"},
            {"commission_status": "0", "extra": "2000010159", "date": "2026-10-24 11:33:00", "id": "1"},
            {"commission_status": "0", "extra": "2000010217", "date": "2026-11-16 10:06:00", "id": "2"},
            {"commission_status": "0", "extra": "2000010184", "date": "2026-11-08 10:00:00", "id": "3"},
            {"commission_status": "1", "extra": "x", "date": "2026-09-01 10:00:00", "id": "4"}]
    st = {"2000010100": "complete", "2000010159": "complete", "2000010217": "complete", "2000010184": "closed"}
    out = {x["order"]: x for x in mr.awin_due(rows, st, TODAY)}
    assert out["2000010100"]["action"] == "APPROVE"
    assert out["2000010159"]["action"] == "WAIT" and "2026-11-28" in out["2000010159"]["why"]
    assert out["2000010217"]["action"] == "WAIT" and "2026-12-21" in out["2000010217"]["why"]
    assert out["2000010184"]["action"] == "DECLINE"
    assert "x" not in out


def test_stale():
    s = mr.stale({"Guidecraft stock": "2026-11-20", "Carpets for Kids stock": "2026-11-10", "Awin export": None}, TODAY)
    assert s == ["Carpets for Kids stock: last updated 13 days ago", "Awin export: never updated through Governor"]


def test_whole_build(monkeypatch, tmp_path):
    import scripts.money_report as smr
    import src.data_sources.magento_client as m

    class FakeMC:
        configured = True

        def _req(self, method, path):
            return {"items": [{"increment_id": "2000010217", "status": "complete"}]}

    monkeypatch.setattr(m, "MagentoClient", FakeMC)
    monkeypatch.setattr(smr, "fetch_orders", lambda mc, a, b: [
        {"number": "2000010217", "date": "2026-11-20", "status": "complete", "charged": 49.99, "refunded": 0,
         "shipping_charged": 0, "items": [{"sku": "5", "name": "5 Letter Maple Landmark Name Train", "qty": 1,
                                            "revenue": 49.99, "options": []}]}])
    monkeypatch.setattr(smr, "ad_spend", lambda cfg, days=7: (30.0, None))
    import scripts.sales_source as ss
    monkeypatch.setattr(ss, "ga4_sources", lambda a, b: {"2000010217": "google / cpc"})
    monkeypatch.setattr(smr, "REPORT", tmp_path / "r.json")
    monkeypatch.setattr(smr, "AWIN_FILE", tmp_path / "awin.csv")
    (tmp_path / "awin.csv").write_text('"id","extra","date","commission","commission_status","site_name","sale_amount"\n'
                                       '"2","2000010217","2026-11-16 10:06:00","4.99","0","digidip","49.99"\n')
    r = smr.build(today=TODAY)
    assert r["this_week"]["orders"] == 1
    assert r["ads"]["spend"] == 30.0 and r["ads"]["orders"] == 1
    assert r["awin"][0]["action"] == "WAIT"
    assert (tmp_path / "r.json").exists()
