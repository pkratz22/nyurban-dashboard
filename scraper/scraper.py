"""
NYUrban Basketball Scraper — entry point.

Logs in, finds every team season, fetches each team page, and writes an
anonymized web/data.json. See login.py (HTTP + auth), parsing.py (HTML ->
data), and storage.py (grouping + output).
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime

from dotenv import load_dotenv

import login as login_mod
import parsing
import storage

load_dotenv()


def get_team_links(session) -> list[dict]:
    print("Step 2: Fetching team list...")
    resp = login_mod.get(session, login_mod.TEAM_LIST_URL)
    team_urls = parsing.parse_team_list(resp.text)

    season_by_team_id = {}
    for team in team_urls:
        detail = login_mod.get(session, team["url"])
        season_by_team_id.update(parsing.parse_season_labels(detail.text))

    teams = [{**team, "season_label": season_by_team_id.get(team["team_id"], "")}
             for team in team_urls]
    print(f"  Found {len(teams)} team season(s)")
    return teams


def scrape_team(session, team: dict) -> dict:
    print(f"Step 3: Scraping '{team['name']}'...")
    resp = login_mod.get(session, team["url"])
    data = parsing.parse_team_page(resp.text, team)

    print(f"  ✓ Season: {data['season_label']}  Division: {data['division']}")
    print(f"  ✓ Record: {data['record']['wins']}-{data['record']['losses']}")
    print(f"  ✓ Standings: {len(data['standings'])} teams")
    print(f"  ✓ Schedule: {len(data['schedule'])} games")
    print(f"  ✓ Box scores: {len(data['box_scores'])} games")
    print(f"  ✓ Season totals: {len(data['season_totals'])} players")
    print(f"  ✓ Leader categories: {list(data['division_leaders'].keys())}")
    return data


def _parse_args():
    parser = argparse.ArgumentParser(description="Scrape NYUrban stats and write JSON for a season or all seasons.")
    parser.add_argument("--season", default=os.environ.get("NYURBAN_SEASON"), help="Only include seasons matching this label, e.g. 'Fall 2026'.")
    return parser.parse_args()


def _scrape_seasons(session, teams: list[dict], season_filter: str | None) -> list[dict]:
    seasons = []
    for team in teams:
        team_data = scrape_team(session, team)
        if season_filter and not storage.season_matches_filter(
            team_data.get("season_label", ""), season_filter
        ):
            continue
        seasons.append(team_data)
    return seasons


def main():
    args = _parse_args()
    session = login_mod.create_session()
    if not login_mod.login(session):
        print("Aborting — login failed.")
        return

    teams = get_team_links(session)
    if not teams:
        print("No teams found.")
        return

    seasons = _scrape_seasons(session, teams, args.season)
    if args.season and not seasons:
        print(f"No seasons matched '{args.season}'.")
        return

    output = storage.anonymize_output({
        "teams": storage.group_seasons_by_team(seasons),
        "scraped_at": datetime.now().isoformat(),
    })
    path = storage.write_output(output)
    print(f"\n✓ Done! Data saved to {path}")


if __name__ == "__main__":
    main()
