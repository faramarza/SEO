from scripts.winnable_action_sheet import _title_rewrite, _junk_query, recently_changed


def test_synonyms_count_as_present():
    assert _title_rewrite("alphabet carpet", "Alphabet Rugs for Classrooms and Kids That Teach the ABC") is None
    assert _title_rewrite("personalised fairy tale books", "Personalized Fairy Tale Book") is None


def test_no_separator_titles():
    note = _title_rewrite("montessori toddlers", "Toys for Toddlers - Collection")
    assert "montessori" in note and "|" not in note.split("(")[0]


def test_junk():
    assert _junk_query("8.33 x 12")
    assert _junk_query('"dog dna star stool" puzzle')
    assert not _junk_query("noahs ark rug")


def test_recently_changed(tmp_path):
    from datetime import date
    log = tmp_path / "log.md"
    log.write_text(f"| 9 | {date.today().isoformat()} | /alphabet-themed-carpets.html | title |\n"
                   "| 1 | 2020-01-01 | /old-page.html | x |\n")
    r = recently_changed(str(log))
    assert "/alphabet-themed-carpets.html" in r and "/old-page.html" not in r
