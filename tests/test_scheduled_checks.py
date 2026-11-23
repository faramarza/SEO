import json
from datetime import date
from pathlib import Path

from src.analysis import scheduled_checks as sc

LOG = (Path(__file__).resolve().parent.parent / "docs" / "seo_change_log.md").read_text()


def test_real_change_log_parses():
    e = {x["id"]: x for x in sc.parse_change_log(LOG)}
    assert {"0", "1", "2", "8", "9", "11", "12"} <= set(e)
    assert e["0"]["pages"] == ["/montessori-toys.html"] and e["0"]["check"] == "2026-12-21"
    assert "montessori toys" in e["0"]["queries"]
    assert "name train" in e["8"]["queries"] and "/name-trains.html" in e["8"]["pages"]
    assert "alphabet rug" in e["9"]["queries"]


def row(page, query, pos, imp, clicks):
    return {"page": "https://alphabet-trains.com" + page, "query": query, "position": pos, "impressions": imp, "clicks": clicks}


def test_grade_verdicts():
    e = {"pages": ["/name-trains.html"], "queries": ["name train"]}
    assert sc.grade(e, [row("/name-trains.html", "name train", 7, 900, 20)],
                    [row("/name-trains.html", "name train", 4, 1000, 40)])["verdict"] == "WORKED"
    assert sc.grade(e, [row("/name-trains.html", "name train", 7, 900, 20)],
                    [row("/name-trains.html", "name train", 10, 900, 10)])["verdict"] == "DID NOT WORK"
    assert sc.grade(e, [row("/name-trains.html", "name train", 7, 900, 20)],
                    [row("/name-trains.html", "name train", 7.5, 900, 21)])["verdict"] == "UNCLEAR"
    assert sc.grade(e, [], [])["verdict"] == "NO DATA"


def test_due_and_alerts():
    entries = [{"id": "1", "check": "2026-12-22"}, {"id": "2", "check": "2026-12-01"}, {"id": "3", "check": "2026-11-01"}]
    assert [x["id"] for x in sc.due_entries(entries, {"3": {}}, date(2026, 12, 2))] == ["2"]
    rep = {"ads": {"spend": 100.0, "profit_before_ads": 60.0, "net": -40.0, "roas": 2.4}}
    assert "pause" in sc.ads_alert(rep)
    assert sc.ads_alert({"ads": {"spend": 100.0, "profit_before_ads": 200, "net": 100, "roas": 5}}) is None
    txt, key = sc.awin_alert({"awin": [{"action": "WAIT", "order": "1"}, {"action": "APPROVE", "order": "2",
                                                                           "publisher": "BlueAFF", "commission": "6.40"}]})
    assert "approve order 2" in txt and key == "APPROVE:2"


def test_runner_alerts_once_and_grades(monkeypatch, tmp_path):
    import scripts.scheduled_checks as run
    monkeypatch.setattr(run, "DATA", tmp_path)
    monkeypatch.setattr(run, "NOTES", tmp_path / "n.json")
    monkeypatch.setattr(run, "SENT", tmp_path / "s.json")
    monkeypatch.setattr(run, "GRADES", tmp_path / "g.json")
    (tmp_path / "money_report.json").write_text(json.dumps(
        {"week": ["2026-11-23", "2026-11-30"], "ads": {"spend": 90.0, "profit_before_ads": 40.0, "net": -50.0, "roas": 2.0},
         "awin": [], "stale": []}))
    rows = lambda a, b: [row("/name-trains.html", "name train", 5 if b.month == 12 else 7, 900, 30)]
    run.main(today=date(2026, 12, 23), gsc_rows=rows)
    run.main(today=date(2026, 12, 23), gsc_rows=rows)   # second run: no duplicate alerts
    notes = json.loads((tmp_path / "n.json").read_text())
    assert [n["type"] for n in notes].count("ads_check") == 1
    assert [n["type"] for n in notes].count("seo_grades") == 1
    grades = json.loads((tmp_path / "g.json").read_text())
    assert grades["8"]["verdict"] == "WORKED"
