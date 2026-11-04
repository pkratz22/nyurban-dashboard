import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from bs4 import BeautifulSoup


SCRAPER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRAPER_DIR))

with patch.dict(os.environ, {"NYURBAN_USERNAME": "test-user", "NYURBAN_PASSWORD": "test-password"}):
    import scraper


class RequestTests(unittest.TestCase):
    def test_request_retries_server_errors_then_returns_success(self):
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=503),
            Mock(status_code=200, text="ok"),
        ]

        with patch.object(scraper.time, "sleep") as sleep:
            response = scraper.get(session, "https://example.test", timeout=10)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.get.call_args_list, [
            call("https://example.test", timeout=10),
            call("https://example.test", timeout=10),
        ])
        sleep.assert_called_once_with(5)

    def test_request_returns_final_server_error_after_all_retries(self):
        session = Mock()
        final_response = Mock(status_code=503)
        session.get.side_effect = [Mock(status_code=503) for _ in range(4)] + [final_response]

        with patch.object(scraper.time, "sleep"):
            response = scraper.get(session, "https://example.test")

        self.assertIs(response, final_response)
        self.assertEqual(session.get.call_count, 5)

    def test_request_honors_retry_after_for_rate_limits(self):
        session = Mock()
        session.get.side_effect = [
            Mock(status_code=429, headers={"Retry-After": "7"}),
            Mock(status_code=200),
        ]

        with patch.object(scraper.time, "sleep") as sleep:
            response = scraper.get(session, "https://example.test")

        self.assertEqual(response.status_code, 200)
        sleep.assert_called_once_with(7)
        self.assertEqual(session.get.call_count, 2)

    def test_post_uses_the_shared_request_wrapper(self):
        session = Mock()
        session.post.return_value = Mock(status_code=200)

        response = scraper.post(session, "https://example.test", data={"key": "value"})

        self.assertEqual(response.status_code, 200)
        session.post.assert_called_once_with(
            "https://example.test", data={"key": "value"}
        )

    def test_login_posts_credentials_and_detects_success(self):
        session = Mock()
        with patch.object(scraper, "post", return_value=Mock(text="Welcome back")) as post:
            self.assertTrue(scraper.login(session))

        post.assert_called_once_with(
            session,
            scraper.LOGIN_URL,
            data={
                "ny_username": "test-user",
                "ny_password": "test-password",
                "submit": "login",
            },
            allow_redirects=True,
        )

    def test_login_returns_false_when_response_does_not_indicate_success(self):
        with patch.object(scraper, "post", return_value=Mock(text="Invalid credentials")):
            self.assertFalse(scraper.login(Mock()))


class SeasonTests(unittest.TestCase):
    def test_normalize_site_season_label(self):
        cases = {
            " Fall Basketball 2026 ": "Fall 2026",
            "WINTER basketball 2025": "Winter 2025",
            "Spring 2024": "Spring 2024",
            "Summer Open League": "Summer Open League",
            "": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(scraper._normalize_site_season_label(raw), expected)

    def test_infer_season_uses_fallback_year_and_skips_invalid_month(self):
        schedule = [{"date": "15/01"}, {"date": "08/12"}]
        self.assertEqual(scraper.infer_season_label(schedule, fallback_year=2025), "Summer 2025")
        self.assertEqual(scraper.infer_season_label([], fallback_year=2025), "Unknown Season")

    def test_season_filter_accepts_disambiguated_labels(self):
        self.assertTrue(scraper.season_matches_filter("Fall 2026", "Fall 2026"))
        self.assertTrue(scraper.season_matches_filter("Fall 2026 · Division A", "Fall 2026"))
        self.assertTrue(scraper.season_matches_filter("Fall 2026 (2)", "Fall 2026"))
        self.assertTrue(scraper.season_matches_filter("Fall 2026", ""))
        self.assertFalse(scraper.season_matches_filter("Winter 2026", "Fall 2026"))


class TeamDiscoveryTests(unittest.TestCase):
    def test_get_team_links_deduplicates_ids_and_reads_season_metadata(self):
        responses = [
            Mock(text="""
                <a href="/team-details?team_id=10">Alpha</a>
                <a href="/team-details?team_id=10">Alpha duplicate</a>
                <a href="/other?team_id=20">Not a team</a>
                <a href="/team-details?team_id=invalid">Invalid id</a>
            """),
            Mock(text="""
                <select id="my_team_for_image">
                  <option value="">Choose team</option>
                  <option value="10">Alpha (Fall Basketball 2026)</option>
                  <option value="20">Other (Winter 2025)</option>
                  <option value="30">Missing season</option>
                </select>
            """),
        ]
        session = Mock()
        with patch.object(scraper, "get", side_effect=responses) as get:
            result = scraper.get_team_links(session)

        self.assertEqual(result, [{
            "name": "Alpha",
            "url": "/team-details?team_id=10",
            "team_id": "10",
            "season_label": "Fall 2026",
        }])
        self.assertEqual(get.call_count, 2)

    def test_get_team_links_returns_empty_when_team_list_has_no_valid_links(self):
        session = Mock()
        with patch.object(
            scraper, "get", return_value=Mock(text='<a href="/team-details">Missing ID</a>')
        ) as get:
            result = scraper.get_team_links(session)

        self.assertEqual(result, [])
        get.assert_called_once_with(session, scraper.TEAM_LIST_URL)


class ParsingTests(unittest.TestCase):
    def test_parse_result_handles_win_loss_forfeit_and_unplayed_game(self):
        self.assertEqual(
            scraper.parse_result("W 64-51"),
            {"outcome": "W", "team_score": 64, "opp_score": 51, "forfeit": False},
        )
        self.assertEqual(
            scraper.parse_result("L 51-64"),
            {"outcome": "L", "team_score": 64, "opp_score": 51, "forfeit": False},
        )
        self.assertEqual(
            scraper.parse_result("w F-2"),
            {"outcome": "W", "team_score": None, "opp_score": None, "forfeit": True},
        )
        self.assertEqual(
            scraper.parse_result(""),
            {"outcome": "", "team_score": None, "opp_score": None, "forfeit": False},
        )

    def test_parse_standings_extracts_results_and_trims_playoffs(self):
        table = BeautifulSoup("""
            <table>
              <tr><th>Rank</th><th>Team</th><th>W</th><th>L</th><th>Pct</th></tr>
              <tr><td>1</td><td><a>Alpha</a><div>Team details</div><table>
                <tr><th>Opponent</th><th>Result</th></tr>
                <tr><td>Beta</td><td>W 60-50</td></tr>
                <tr><td>Gamma</td><td>L 40-45</td></tr>
                <tr><td>Delta</td><td>W 70-60</td></tr>
              </table></td><td>1</td><td>1</td><td>.500</td></tr>
              <tr><td>2</td><td>Incomplete</td><td>n/a</td><td>0</td><td>—</td></tr>
            </table>
        """, "html.parser").table

        self.assertEqual(scraper.parse_standings(table), [{
            "team": "Alpha",
            "wins": 1,
            "losses": 1,
            "pct": ".500",
            "games": [
                {"opponent": "Beta", "outcome": "W", "pts_for": 60, "pts_against": 50},
                {"opponent": "Gamma", "outcome": "L", "pts_for": 45, "pts_against": 40},
            ],
        }])

    def test_enrich_standings_calculates_averages_and_empty_values(self):
        standings = [
            {
                "team": "Alpha",
                "games": [{"opponent": "Unknown", "pts_for": 60, "pts_against": 50}],
            },
            {"team": "Idle", "games": []},
        ]

        result = scraper.enrich_standings(standings)

        self.assertEqual(
            {key: result[0][key] for key in ("ppg", "opp_ppg", "pt_diff", "sos", "srs")},
            {"ppg": 60.0, "opp_ppg": 50.0, "pt_diff": 10.0, "sos": 0.0, "srs": 10.0},
        )
        self.assertEqual(
            {key: result[1][key] for key in ("ppg", "opp_ppg", "pt_diff", "sos", "srs")},
            {"ppg": None, "opp_ppg": None, "pt_diff": None, "sos": None, "srs": None},
        )

    def test_parse_schedule_extracts_location_and_skips_no_game(self):
        table = BeautifulSoup("""
            <table>
              <tr><td>Date</td><td>Location</td><td>Time</td><td>Opponent</td><td>Results</td></tr>
              <tr>
                <td>01/10</td>
                <td><a href="?address=10%20Main+St">GYM1</a><div id="popup">Main Gym</div></td>
                <td>7:00 PM</td><td>Beta arrow</td><td>L 51-64</td>
              </tr>
              <tr><td>01/17</td><td></td><td></td><td>*** No Game This Week</td><td></td></tr>
              <tr><td></td><td></td><td></td><td>Gamma</td><td>W 1-0</td></tr>
            </table>
        """, "html.parser").table

        self.assertEqual(scraper.parse_schedule_from_results_table(table), [{
            "date": "01/10",
            "time": "7:00 PM",
            "location": "GYM1",
            "gym_name": "Main Gym",
            "gym_address": "10 Main St",
            "opponent": "Beta",
            "result": "L 51-64",
            "outcome": "L",
            "team_score": 64,
            "opp_score": 51,
            "forfeit": False,
        }])

    def test_parse_boxscores_converts_numeric_stats_and_joins_result(self):
        table = BeautifulSoup("""
            <table>
              <tr><td>+-</td><td>01/10</td><td>GYM1</td><td>Beta</td></tr>
              <tr><td><table>
                <tr><th>No.</th><th>Name</th><th>FG</th><th>3Pts</th><th>FT</th><th>Total</th></tr>
                <tr><td>7</td><td>Player One</td><td>4</td><td>1</td><td>2</td><td>11</td></tr>
                <tr><td>8</td><td>Player Two</td><td>-</td><td>0</td><td>0</td><td>0</td></tr>
              </table></td></tr>
            </table>
        """, "html.parser").table
        schedule = [{"opponent": "Beta", "result": "W 60-50"}]

        result = scraper.parse_boxscores_from_combo_table(table, schedule)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["team_score"], 60)
        self.assertEqual(result[0]["opp_score"], 50)
        self.assertEqual(result[0]["players"][0]["total"], 11)
        self.assertEqual(result[0]["players"][1]["fg"], "-")

    def test_parse_season_totals_converts_stats_and_preserves_invalid_values(self):
        table = BeautifulSoup("""
            <table>
              <tr><td>Season Totals</td></tr>
              <tr><th>No.</th><th>Name</th><th>FG</th><th>3Pts</th><th>FT</th>
                  <th>Tot.</th><th>D.Rank</th><th>GP</th><th>Avg.</th><th>Rank</th></tr>
              <tr><td>7</td><td>Player One</td><td>12</td><td>3</td><td>4</td>
                  <td>31</td><td>2</td><td>5</td><td>6.2</td><td>1</td></tr>
              <tr><td>8</td><td>Player Two</td><td>bad</td><td>0</td><td>0</td>
                  <td>0</td><td>—</td><td>bad</td><td>—</td><td>—</td></tr>
              <tr><td>Team</td><td>Totals</td></tr>
            </table>
        """, "html.parser").table

        result = scraper.parse_season_totals(table)

        self.assertEqual(result[0]["fg"], 12)
        self.assertEqual(result[0]["avg."], 6.2)
        self.assertEqual(result[1]["fg"], "bad")
        self.assertEqual(result[1]["gp"], "bad")
        self.assertEqual(len(result), 2)

    def test_parse_season_totals_supports_tables_without_section_title(self):
        table = BeautifulSoup("""
            <table>
              <tr><th>Name</th><th>FG</th><th>GP</th><th>Avg.</th></tr>
              <tr><td>Player</td><td>10</td><td>2</td><td>5.0</td></tr>
            </table>
        """, "html.parser").table

        self.assertEqual(
            scraper.parse_season_totals(table),
            [{"name": "Player", "fg": 10, "gp": 2, "avg.": 5.0}],
        )

    def test_parse_division_leaders_assigns_categories_in_table_order(self):
        tables = BeautifulSoup("""
            <table>
              <tr><th>Rank</th><th>Team</th><th>No.</th><th>Name</th></tr>
              <tr><td>1</td><td>Alpha</td><td>7</td><td>Player One</td></tr>
            </table>
            <table>
              <tr><th>Rank</th><th>Team</th><th>No.</th><th>Name</th></tr>
              <tr><td>1</td><td>Beta</td><td>8</td><td>Player Two</td></tr>
            </table>
            <table><tr><th>Team</th><th>Name</th></tr><tr><td>Other</td><td>Row</td></tr></table>
        """, "html.parser").find_all("table")

        result = scraper.parse_division_leaders(tables)

        self.assertEqual(list(result), ["total_points", "scoring_avg"])
        self.assertEqual(result["total_points"][0]["team"], "Alpha")
        self.assertEqual(result["scoring_avg"][0]["name"], "Player Two")

    def test_anonymize_output_strips_player_names_recursively(self):
        data = {
            "team_name": "Alpha",
            "players": [
                {"No.": "7", "Name": "Player One", "total": 11},
                {"player_name": "Player Two", "points": 5},
            ],
            "nested": {"player": "Player Three", "value": 1},
        }

        self.assertEqual(scraper.anonymize_output(data), {
            "team_name": "Alpha",
            "players": [{"No.": "7", "total": 11}, {"points": 5}],
            "nested": {"value": 1},
        })


class ScrapeTeamTests(unittest.TestCase):
    def test_scrape_team_combines_tables_and_infers_season(self):
        html = """
            <h2>Division: Open A</h2>
            <table>
              <tr><th>Rank</th><th>Team</th><th>W</th><th>L</th><th>Pct</th></tr>
              <tr><td>1</td><td><a>Alpha</a><div>Team details</div><table>
                <tr><th>Opponent</th><th>Result</th></tr>
                <tr><td>Beta</td><td>W 60-50</td></tr>
              </table></td><td>1</td><td>0</td><td>1.000</td></tr>
            </table>
            <table>
              <tr><td>Date</td><td>Location</td><td>Time</td><td>Opponent</td><td>Results</td></tr>
              <tr><td>01/10</td><td>GYM1</td><td>7 PM</td><td>Beta</td><td>W 60-50</td></tr>
            </table>
            <table>
              <tr><th>Date</th><th>Gym Location</th><th>Opponent</th></tr>
              <tr><td>+-</td><td>01/10</td><td>GYM1</td><td>Beta</td></tr>
              <tr><td><table>
                <tr><th>No.</th><th>Name</th><th>FG</th><th>3Pts</th><th>FT</th><th>Total</th></tr>
                <tr><td>7</td><td>Player One</td><td>4</td><td>1</td><td>2</td><td>11</td></tr>
              </table></td></tr>
            </table>
            <table>
              <tr><td>Season Totals</td></tr>
              <tr><th>No.</th><th>Name</th><th>FG</th><th>3Pts</th><th>FT</th>
                  <th>Tot.</th><th>D.Rank</th><th>GP</th><th>Avg.</th><th>Rank</th></tr>
              <tr><td>7</td><td>Player One</td><td>4</td><td>1</td><td>2</td>
                  <td>11</td><td>1</td><td>1</td><td>11.0</td><td>1</td></tr>
            </table>
            <table>
              <tr><th>Rank</th><th>Team</th><th>No.</th><th>Name</th></tr>
              <tr><td>1</td><td>Alpha</td><td>7</td><td>Player One</td></tr>
            </table>
        """
        session = Mock()
        with patch.object(scraper, "get", return_value=Mock(text=html)):
            result = scraper.scrape_team(session, {
                "name": "Alpha",
                "team_id": "10",
                "url": "/team-details?team_id=10",
            })

        self.assertEqual(result["division"], "Open A")
        self.assertEqual(result["record"], {"wins": 1, "losses": 0})
        self.assertEqual(result["season_label"], "Winter 2026")
        self.assertEqual(result["schedule"][0]["team_score"], 60)
        self.assertEqual(result["box_scores"][0]["players"][0]["total"], 11)
        self.assertEqual(result["season_totals"][0]["fg"], 4)
        self.assertIn("total_points", result["division_leaders"])


class MainTests(unittest.TestCase):
    def test_main_filters_seasons_groups_duplicates_and_anonymizes_output(self):
        discovered_teams = [
            {"name": "Alpha", "team_id": "10", "url": "/team/10"},
            {"name": "Alpha", "team_id": "11", "url": "/team/11"},
            {"name": "Alpha", "team_id": "12", "url": "/team/12"},
        ]
        scraped_seasons = [
            {
                "team_name": "Alpha",
                "season_label": "Fall 2026",
                "division": "Open A",
                "box_scores": [{"players": [{"name": "Private Name", "total": 11}]}],
            },
            {
                "team_name": "Alpha",
                "season_label": "Fall 2026",
                "division": "Open B",
                "season_totals": [{"player_name": "Private Name", "fg": 4}],
            },
            {
                "team_name": "Alpha",
                "season_label": "Winter 2026",
                "division": "Open C",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "data.json"
            with (
                patch.object(sys, "argv", ["scraper.py", "--season", "Fall 2026"]),
                patch.object(scraper, "OUTPUT_FILE", str(output_path)),
                patch.object(scraper, "login", return_value=True),
                patch.object(scraper, "get_team_links", return_value=discovered_teams),
                patch.object(scraper, "scrape_team", side_effect=scraped_seasons),
            ):
                scraper.main()

            output = json.loads(output_path.read_text())

        seasons = output["teams"][0]["seasons"]
        self.assertEqual([season["season_label"] for season in seasons], [
            "Fall 2026 · Open A",
            "Fall 2026 · Open B",
        ])
        self.assertEqual(seasons[0]["box_scores"][0]["players"], [{"total": 11}])
        self.assertEqual(seasons[1]["season_totals"], [{"fg": 4}])

    def test_main_does_not_write_output_when_login_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "data.json"
            with (
                patch.object(sys, "argv", ["scraper.py"]),
                patch.object(scraper, "OUTPUT_FILE", str(output_path)),
                patch.object(scraper, "login", return_value=False),
                patch.object(scraper, "get_team_links") as get_team_links,
            ):
                scraper.main()

            self.assertFalse(output_path.exists())
            get_team_links.assert_not_called()

    def test_main_does_not_write_output_when_no_season_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "data.json"
            with (
                patch.object(sys, "argv", ["scraper.py", "--season", "Fall 2026"]),
                patch.object(scraper, "OUTPUT_FILE", str(output_path)),
                patch.object(scraper, "login", return_value=True),
                patch.object(scraper, "get_team_links", return_value=[{"team_id": "10"}]),
                patch.object(scraper, "scrape_team", return_value={
                    "season_label": "Winter 2026",
                }),
            ):
                scraper.main()

            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
