"""Parser tests driven by saved HTML pages in tests/fixtures/."""
import pytest
from bs4 import BeautifulSoup

import parsing


def soup_table(html):
    return BeautifulSoup(html, "html.parser").table


# ── team list / season labels ────────────────────────────────────────────────

def test_parse_team_list_dedupes_and_ignores_non_team_links(fixture_html):
    teams = parsing.parse_team_list(fixture_html("team_list.html"))
    assert [t["team_id"] for t in teams] == ["101", "87"]
    assert teams[0]["name"] == "Hardwood Heroes"


def test_parse_season_labels_reads_select_options(fixture_html):
    assert parsing.parse_season_labels(fixture_html("team_detail_select.html")) == {
        "101": "Fall 2026",
        "87": "Spring 2026",
    }


@pytest.mark.parametrize("raw,expected", [
    (" Fall Basketball 2026 ", "Fall 2026"),
    ("WINTER basketball 2025", "Winter 2025"),
    ("Spring 2024", "Spring 2024"),
    ("Summer Open League", "Summer Open League"),
    ("", ""),
])
def test_normalize_site_season_label(raw, expected):
    assert parsing._normalize_site_season_label(raw) == expected


def test_infer_season_uses_fallback_year_and_skips_invalid_month():
    assert parsing.infer_season_label([{"date": "15/01"}, {"date": "08/12"}], fallback_year=2025) == "Summer 2025"
    assert parsing.infer_season_label([], fallback_year=2025) == "Unknown Season"


# ── standings ────────────────────────────────────────────────────────────────

def test_parse_standings(team_tables):
    standings = parsing.parse_standings(team_tables[0])
    assert [s["team"] for s in standings] == ["Hardwood Heroes", "Court Jesters", "Net Ninjas"]
    heroes = standings[0]
    assert (heroes["wins"], heroes["losses"], heroes["pct"]) == (2, 1, ".667")
    assert heroes["games"][1] == {"opponent": "Net Ninjas", "outcome": "L", "pts_for": 55, "pts_against": 48}
    assert standings[2]["games"] == []  # no nested table


def test_enrich_standings(team_tables):
    standings = parsing.enrich_standings(parsing.parse_standings(team_tables[0]))
    heroes, jesters, ninjas = standings
    assert heroes["ppg"] == 63.0
    assert heroes["opp_ppg"] == 53.0
    assert heroes["pt_diff"] == 10.0
    assert ninjas["ppg"] is None and ninjas["srs"] is None
    assert jesters["srs"] is not None


# ── schedule ─────────────────────────────────────────────────────────────────

def test_parse_result_cases():
    assert parsing.parse_result("W 64-51") == {"outcome": "W", "team_score": 64, "opp_score": 51, "forfeit": False}
    assert parsing.parse_result("L 51-64") == {"outcome": "L", "team_score": 64, "opp_score": 51, "forfeit": False}
    assert parsing.parse_result("w F-2") == {"outcome": "W", "team_score": None, "opp_score": None, "forfeit": True}
    assert parsing.parse_result("") == {"outcome": "", "team_score": None, "opp_score": None, "forfeit": False}


def test_parse_schedule(team_tables):
    schedule = parsing.parse_schedule_from_results_table(team_tables[1])
    assert [g["date"] for g in schedule] == ["10/05", "10/12", "10/26", "11/02"]  # bye week skipped
    first = schedule[0]
    assert first["location"] == "PS12"
    assert first["gym_address"] == "123 Main St, New York"
    assert first["gym_name"] == "Public School 12 Gym"
    assert first["opponent"] == "Court Jesters"
    assert (first["outcome"], first["team_score"], first["opp_score"]) == ("W", 64, 51)
    assert schedule[2]["forfeit"] is True
    assert schedule[3]["outcome"] == ""  # unplayed


# ── box scores ───────────────────────────────────────────────────────────────

def test_parse_boxscores(team_tables):
    schedule = parsing.parse_schedule_from_results_table(team_tables[1])
    games = parsing.parse_boxscores_from_combo_table(team_tables[2], schedule)
    assert len(games) == 2
    assert games[0]["opponent"] == "Court Jesters"
    assert games[0]["team_score"] == 64
    assert games[0]["players"][0]["total"] == 21
    assert games[0]["players"][1]["ft"] == "-"  # non-numeric preserved


# ── season totals ────────────────────────────────────────────────────────────

def test_parse_season_totals(team_tables):
    totals = parsing.parse_season_totals(team_tables[3])
    assert len(totals) == 2  # "Team Totals" row excluded
    assert totals[0]["fg"] == 14 and totals[0]["gp"] == 2 and totals[0]["avg."] == 18.0


def test_parse_season_totals_returns_empty_for_unrelated_table(team_tables):
    assert parsing.parse_season_totals(team_tables[0]) == []


# ── division leaders ─────────────────────────────────────────────────────────

def test_parse_division_leaders(team_tables):
    leaders = parsing.parse_division_leaders(team_tables)
    assert list(leaders) == ["total_points", "scoring_avg", "three_pointers", "free_throws"]
    assert leaders["three_pointers"][0]["team"] == "Net Ninjas"


# ── whole page ───────────────────────────────────────────────────────────────

TEAM = {"name": "Hardwood Heroes", "team_id": "101", "url": "/team-details/?team_id=101", "season_label": "Fall 2026"}


def test_parse_team_page_full(fixture_html):
    data = parsing.parse_team_page(fixture_html("team_page.html"), TEAM)
    assert data["division"] == "Open A"
    assert data["season_label"] == "Fall 2026"
    assert data["record"] == {"wins": 2, "losses": 1}
    assert len(data["standings"]) == 3
    assert len(data["schedule"]) == 4
    assert len(data["box_scores"]) == 2
    assert len(data["season_totals"]) == 2
    assert len(data["division_leaders"]) == 4


def test_parse_team_page_preseason_has_no_stats(fixture_html):
    team = {**TEAM, "season_label": ""}
    data = parsing.parse_team_page(fixture_html("team_page_preseason.html"), team)
    assert data["division"] == "Open B"
    assert data["record"] == {"wins": 0, "losses": 0}
    assert data["box_scores"] == [] and data["season_totals"] == []
    assert data["division_leaders"] == {}
    assert data["season_label"].startswith("Spring ")  # inferred from 03/07
