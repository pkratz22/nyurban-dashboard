import json

import storage


def test_anonymize_output_strips_player_names_recursively():
    data = {
        "team_name": "Alpha",
        "players": [{"No.": "7", "Name": "Player One", "total": 11}, {"player_name": "Two", "points": 5}],
        "nested": {"player": "Three", "value": 1},
    }
    assert storage.anonymize_output(data) == {
        "team_name": "Alpha",
        "players": [{"No.": "7", "total": 11}, {"points": 5}],
        "nested": {"value": 1},
    }


def test_season_filter_accepts_disambiguated_labels():
    assert storage.season_matches_filter("Fall 2026", "Fall 2026")
    assert storage.season_matches_filter("Fall 2026 · Division A", "Fall 2026")
    assert storage.season_matches_filter("Fall 2026 (2)", "Fall 2026")
    assert storage.season_matches_filter("Fall 2026", "")
    assert not storage.season_matches_filter("Winter 2026", "Fall 2026")


def test_group_seasons_disambiguates_duplicate_labels():
    seasons = [
        {"team_name": "A", "season_label": "Fall 2026", "division": "Open A"},
        {"team_name": "A", "season_label": "Fall 2026", "division": ""},
        {"team_name": "A", "season_label": "Fall 2026", "division": ""},
        {"team_name": "B", "season_label": "Fall 2026", "division": "X"},
    ]
    teams = storage.group_seasons_by_team(seasons)
    assert [t["team_name"] for t in teams] == ["A", "B"]
    assert [s["season_label"] for s in teams[0]["seasons"]] == [
        "Fall 2026 · Open A", "Fall 2026 (2)", "Fall 2026 (3)"]
    assert teams[1]["seasons"][0]["season_label"] == "Fall 2026"


def test_write_output_creates_directories(tmp_path):
    path = storage.write_output({"a": 1}, str(tmp_path / "nested" / "data.json"))
    assert json.loads(open(path).read()) == {"a": 1}
