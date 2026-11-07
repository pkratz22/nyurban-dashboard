"""Entry-point orchestration, with the network layer mocked and fixture HTML."""
import json
import sys
from unittest.mock import Mock, patch

import scraper


def test_get_team_links_combines_list_and_season_labels(fixture_html):
    responses = [Mock(text=fixture_html("team_list.html")),
                 Mock(text=fixture_html("team_detail_select.html")),
                 Mock(text=fixture_html("team_detail_select.html"))]
    with patch.object(scraper.login_mod, "get", side_effect=responses):
        teams = scraper.get_team_links(Mock())
    assert [(t["team_id"], t["season_label"]) for t in teams] == [("101", "Fall 2026"), ("87", "Spring 2026")]


def test_get_team_links_empty_when_no_valid_links():
    with patch.object(scraper.login_mod, "get", return_value=Mock(text='<a href="/team-details">x</a>')) as get:
        assert scraper.get_team_links(Mock()) == []
    assert get.call_count == 1


def test_scrape_team_fetches_and_parses(fixture_html):
    team = {"name": "Hardwood Heroes", "team_id": "101", "url": "/t", "season_label": "Fall 2026"}
    with patch.object(scraper.login_mod, "get", return_value=Mock(text=fixture_html("team_page.html"))):
        data = scraper.scrape_team(Mock(), team)
    assert data["record"] == {"wins": 2, "losses": 1}


def _run_main(tmp_path, argv, **patches):
    out = tmp_path / "data.json"
    with patch.object(sys, "argv", ["scraper.py", *argv]), \
         patch.object(scraper.storage, "OUTPUT_FILE", str(out)), \
         patch.object(scraper.login_mod, "create_session", return_value=Mock()), \
         patch.object(scraper.login_mod, "login", return_value=patches.get("login", True)), \
         patch.object(scraper, "get_team_links", return_value=patches.get("teams", [])), \
         patch.object(scraper, "scrape_team", side_effect=patches.get("seasons", [])):
        scraper.main()
    return out


def test_main_filters_groups_and_anonymizes(tmp_path):
    seasons = [
        {"team_name": "Alpha", "season_label": "Fall 2026", "division": "Open A",
         "box_scores": [{"players": [{"name": "Private", "total": 11}]}]},
        {"team_name": "Alpha", "season_label": "Fall 2026", "division": "Open B",
         "season_totals": [{"player_name": "Private", "fg": 4}]},
        {"team_name": "Alpha", "season_label": "Winter 2026", "division": "Open C"},
    ]
    out = _run_main(tmp_path, ["--season", "Fall 2026"],
                    teams=[{"team_id": str(i)} for i in range(3)], seasons=seasons)
    result = json.loads(out.read_text())["teams"][0]["seasons"]
    assert [s["season_label"] for s in result] == ["Fall 2026 · Open A", "Fall 2026 · Open B"]
    assert result[0]["box_scores"][0]["players"] == [{"total": 11}]
    assert result[1]["season_totals"] == [{"fg": 4}]


def test_main_writes_nothing_when_login_fails(tmp_path):
    assert not _run_main(tmp_path, [], login=False).exists()


def test_main_writes_nothing_when_no_season_matches(tmp_path):
    out = _run_main(tmp_path, ["--season", "Fall 2026"], teams=[{"team_id": "1"}],
                    seasons=[{"season_label": "Winter 2026"}])
    assert not out.exists()
