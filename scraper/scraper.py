"""
NYUrban Basketball Scraper
Logs in via the site's custom form, finds all team pages, and scrapes:
  - Division standings
  - Team schedule & results
  - Per-game player box scores
  - Season totals per player
  - Division scoring leaders
"""

from __future__ import annotations

import argparse
import requests
from bs4 import BeautifulSoup
import json
import re
import os
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.nyurban.com"
LOGIN_URL = f"{BASE_URL}/basketball/new-scoring-stats/"
TEAM_LIST_URL = f"{BASE_URL}/waiver-team-listing/"

USERNAME = os.environ["NYURBAN_USERNAME"]
PASSWORD = os.environ["NYURBAN_PASSWORD"]

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "../web/data.json")
HTML_PARSER = "html.parser"


# ── HTTP helpers with retry + backoff ─────────────────────────────────────────

def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    """
    Wrapper around session.get/post that retries on 429 (Too Many Requests)
    or transient 5xx errors using exponential backoff.
    """
    delays = [5, 15, 30, 60]  # seconds between attempts
    for attempt, delay in enumerate(delays + [None], start=1):
        resp = getattr(session, method)(url, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", delay or 60))
            print(f"  ⚠ 429 Too Many Requests — waiting {retry_after}s (attempt {attempt})")
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500 and delay is not None:
            print(f"  ⚠ {resp.status_code} server error — waiting {delay}s (attempt {attempt})")
            time.sleep(delay)
            continue
        return resp
    # Final attempt after all delays exhausted
    return getattr(session, method)(url, **kwargs)


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return _request(session, "get", url, **kwargs)


def post(session: requests.Session, url: str, **kwargs) -> requests.Response:
    return _request(session, "post", url, **kwargs)


# ── Step 1: Login ─────────────────────────────────────────────────────────────

def login(session: requests.Session) -> bool:
    print("Step 1: Logging in...")
    resp = post(
        session, LOGIN_URL,
        data={"ny_username": USERNAME, "ny_password": PASSWORD, "submit": "login"},
        allow_redirects=True,
    )
    if "logout" in resp.text.lower() or "welcome" in resp.text.lower():
        print("  ✓ Login successful")
        return True
    print("  ✗ Login failed")
    return False


# ── Step 2: Get team list (all seasons) ───────────────────────────────────────

def _normalize_site_season_label(raw: str) -> str:
    """Convert a site label like 'Fall Basketball 2026' to 'Fall 2026'."""
    if not raw:
        return ""
    crop = raw.strip()
    match = re.search(r"(?i)\b(fall|winter|spring|summer)\s+basketball\s+(\d{4})\b", crop)
    if match:
        return f"{match.group(1).title()} {match.group(2)}"
    match = re.search(r"(?i)\b(fall|winter|spring|summer)\s+(\d{4})\b", crop)
    if match:
        return f"{match.group(1).title()} {match.group(2)}"
    return crop


def _extract_team_urls(soup) -> list[dict]:
    team_urls = []
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "team-details" not in href or "team_id=" not in href:
            continue
        match = re.search(r"team_id=(\d+)", href)
        if not match:
            continue
        team_id = match.group(1)
        if team_id in seen_ids:
            continue
        seen_ids.add(team_id)
        team_urls.append({
            "name": a.get_text(strip=True),
            "url": href,
            "team_id": team_id,
        })
    return team_urls


def _season_label_from_option(option) -> tuple[str, str]:
    team_id = str(option.get("value", "")).strip()
    label = option.get_text(" ", strip=True)
    if not team_id or not label:
        return "", ""
    match = re.search(r"\(([^()]+)\)$", label)
    if not match:
        return "", ""
    return team_id, _normalize_site_season_label(match.group(1))


def _get_season_labels(session: requests.Session, team_urls: list[dict]) -> dict[str, str]:
    season_by_team_id = {}
    for team in team_urls:
        detail_resp = get(session, team["url"])
        detail_soup = BeautifulSoup(detail_resp.text, HTML_PARSER)
        options = detail_soup.select(
            "select#my_team_for_image option, select[name='my_team_for_image'] option"
        )
        for option in options:
            team_id, season_label = _season_label_from_option(option)
            if team_id and season_label:
                season_by_team_id[team_id] = season_label
    return season_by_team_id


def get_team_links(session: requests.Session) -> list[dict]:
    print("Step 2: Fetching team list...")
    resp = get(session, TEAM_LIST_URL)
    soup = BeautifulSoup(resp.text, HTML_PARSER)
    team_urls = _extract_team_urls(soup)
    season_by_team_id = _get_season_labels(session, team_urls)

    teams = []
    for team in team_urls:
        teams.append({
            "name": team["name"],
            "url": team["url"],
            "team_id": team["team_id"],
            "season_label": season_by_team_id.get(team["team_id"], ""),
        })

    print(f"  Found {len(teams)} team season(s)")
    return teams


# ── Step 3: Parse standings from table 0 ──────────────────────────────────────

def _standings_child_name(content) -> tuple[str, bool]:
    if hasattr(content, "get_text"):
        text = content.get_text(strip=True)
        if not text or text == "arrow":
            return "", False
        if content.name == "div":
            return "", True
        return text, False
    text = str(content).strip()
    return text, bool(text)


def _standings_team_name(name_cell) -> str:
    name = ""
    for content in name_cell.children:
        candidate, should_stop = _standings_child_name(content)
        if candidate:
            name = candidate
        if should_stop:
            break
    if not name:
        name = name_cell.get_text(" ", strip=True).split("arrow")[0].strip()
    return name


def _parse_standings_game(row) -> dict | None:
    cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
    if len(cells) < 2:
        return None
    match = re.match(r"([WL])\s*(\d+)-(\d+)", cells[1])
    if not match:
        return None
    outcome, score_a, score_b = match.group(1), int(match.group(2)), int(match.group(3))
    pts_for, pts_against = (score_a, score_b) if outcome == "W" else (score_b, score_a)
    return {
        "opponent": cells[0],
        "outcome": outcome,
        "pts_for": pts_for,
        "pts_against": pts_against,
    }


def _standings_games(name_cell, regular_season_gp: int) -> list[dict]:
    nested = name_cell.find("table")
    if not nested:
        return []
    games = []
    for row in nested.find_all("tr")[1:]:
        game = _parse_standings_game(row)
        if game:
            games.append(game)
    return games[:regular_season_gp]


def _parse_standings_row(row) -> dict | None:
    cells = row.find_all("td", recursive=False)
    if len(cells) < 4:
        return None
    try:
        wins = int(cells[2].get_text(strip=True))
        losses = int(cells[3].get_text(strip=True))
    except ValueError:
        return None
    return {
        "team": _standings_team_name(cells[1]),
        "wins": wins,
        "losses": losses,
        "pct": cells[4].get_text(strip=True) if len(cells) > 4 else "",
        "games": _standings_games(cells[1], wins + losses),
    }


def parse_standings(table) -> list[dict]:
    standings = []
    for row in table.find_all("tr", recursive=False)[1:]:
        entry = _parse_standings_row(row)
        if entry:
            standings.append(entry)
    return standings


def _set_scoring_averages(standing: dict) -> None:
    games = standing.get("games", [])
    if not games:
        standing.update({
            "ppg": None, "opp_ppg": None, "pt_diff": None, "sos": None, "srs": None,
        })
        return
    points_for = sum(game["pts_for"] for game in games)
    points_against = sum(game["pts_against"] for game in games)
    standing["ppg"] = round(points_for / len(games), 1)
    standing["opp_ppg"] = round(points_against / len(games), 1)
    standing["pt_diff"] = round((points_for - points_against) / len(games), 1)


def _srs_iteration(standings: list[dict], current_srs: dict[str, float]) -> dict[str, float]:
    updated_srs = {}
    for standing in standings:
        games = standing.get("games", [])
        if not games:
            updated_srs[standing["team"]] = 0.0
            continue
        opponents_srs = sum(
            current_srs.get(game["opponent"], 0.0) for game in games
        ) / len(games)
        updated_srs[standing["team"]] = (standing["pt_diff"] or 0.0) + opponents_srs
    return updated_srs


def _solve_srs(standings: list[dict]) -> dict[str, float]:
    srs = {standing["team"]: (standing["pt_diff"] or 0.0) for standing in standings}
    for _ in range(50):
        updated_srs = _srs_iteration(standings, srs)
        converged = all(abs(updated_srs[team] - srs[team]) < 0.001 for team in srs)
        srs = updated_srs
        if converged:
            break
    return srs


def _set_strength_ratings(standings: list[dict], srs: dict[str, float]) -> None:
    for standing in standings:
        if standing["pt_diff"] is None:
            standing["sos"] = None
            standing["srs"] = None
            continue
        standing["srs"] = round(srs[standing["team"]], 1)
        standing["sos"] = round(srs[standing["team"]] - standing["pt_diff"], 1)


def enrich_standings(standings: list[dict]) -> list[dict]:
    """Add scoring averages, point differential, SOS, and SRS to each entry."""
    for standing in standings:
        _set_scoring_averages(standing)
    srs = _solve_srs(standings)
    _set_strength_ratings(standings, srs)
    return standings


# ── Step 4: Parse schedule + box scores ───────────────────────────────────────

def parse_result(result_text: str) -> dict:
    result_text = result_text.strip()
    # Forfeit: "W F-2" or "L F-2"
    if re.match(r"[WL]\s+F-", result_text, re.IGNORECASE):
        outcome = result_text[0].upper()
        return {"outcome": outcome, "team_score": None, "opp_score": None, "forfeit": True}
    m = re.search(r"([WL])\s*(\d+)-(\d+)", result_text)
    if m:
        outcome = m.group(1)
        score_a, score_b = int(m.group(2)), int(m.group(3))
        if outcome == "W":
            pts_for, pts_against = score_a, score_b
        else:
            pts_for, pts_against = score_b, score_a
        return {"outcome": outcome, "team_score": pts_for, "opp_score": pts_against, "forfeit": False}
    return {"outcome": "", "team_score": None, "opp_score": None, "forfeit": False}


def parse_schedule_from_results_table(table) -> list[dict]:
    """
    Parse the schedule table: Date | Location | Time | Opponent | Results
    The opponent and location cells contain popup HTML — extract just the key info.
    """
    from urllib.parse import urlparse, parse_qs, unquote_plus

    schedule = []
    rows = table.find_all("tr", recursive=False)
    for row in rows[1:]:  # skip header
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        date = cells[0].get_text(strip=True)
        time = cells[2].get_text(strip=True)
        # Opponent: first text before "arrow"
        opp_text = cells[3].get_text(" ", strip=True).split("arrow")[0].strip()
        result_raw = cells[4].get_text(strip=True)

        if not date or opp_text in ("*** No Game This Week", ""):
            continue

        # Location: code, gym name, address from popup
        loc_cell = cells[1]
        a_tag = loc_cell.find("a")
        loc_code = a_tag.get_text(strip=True) if a_tag else loc_cell.get_text(" ", strip=True).split("arrow")[0].strip()
        gym_name = ""
        gym_address = ""
        if a_tag and a_tag.get("href"):
            qs = parse_qs(urlparse(a_tag["href"]).query)
            gym_address = unquote_plus(qs.get("address", [""])[0])
        popup = loc_cell.find(id="popup")
        if popup:
            texts = [t.strip() for t in popup.stripped_strings]
            gym_name = next((t for t in texts if t and t != "arrow"), "")

        parsed = parse_result(result_raw)
        schedule.append({
            "date": date,
            "time": time,
            "location": loc_code,
            "gym_name": gym_name,
            "gym_address": gym_address,
            "opponent": opp_text,
            "result": result_raw,
            **parsed,
        })
    return schedule


def _parse_boxscore_player_row(row, headers: list[str]) -> dict | None:
    cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
    if len(cells) < 4:
        return None
    player = dict(zip(headers, cells))
    _convert_player_stats(player, ("fg", "3pts", "ft", "total"), int)
    return player


def _parse_boxscore_players(box_row) -> list[dict]:
    for nested_table in box_row.find_all("table"):
        headers = [
            header.get_text(strip=True).lower()
            for header in nested_table.find_all("th")
        ]
        if "fg" not in headers or "total" not in headers:
            continue
        players = []
        for row in nested_table.find_all("tr")[1:]:
            player = _parse_boxscore_player_row(row, headers)
            if player:
                players.append(player)
        return players
    return []


def _boxscore_game_info(row, result_lookup: dict) -> dict | None:
    cells = row.find_all("td", recursive=False)
    if len(cells) < 4 or cells[0].get_text(strip=True) != "+-":
        return None
    date, gym, opponent = [cell.get_text(strip=True) for cell in cells[1:4]]
    result = result_lookup.get(opponent, {}).get("result", "")
    return {
        "date": date,
        "gym": gym,
        "opponent": opponent,
        "result": result,
        **parse_result(result),
    }


def parse_boxscores_from_combo_table(table, schedule: list) -> list[dict]:
    """Parse alternating game-header and box-score rows into per-game stats."""
    rows = table.find_all("tr", recursive=False)
    result_lookup = {game["opponent"]: game for game in schedule}
    box_scores = []
    row_index = 0
    while row_index < len(rows):
        game_info = _boxscore_game_info(rows[row_index], result_lookup)
        if not game_info:
            row_index += 1
            continue
        if row_index + 1 == len(rows):
            row_index += 1
            continue
        players = _parse_boxscore_players(rows[row_index + 1])
        if players:
            box_scores.append({**game_info, "players": players})
        row_index += 2
    return box_scores


# ── Step 5: Parse season totals table ─────────────────────────────────────────

def parse_season_totals(table) -> list[dict]:
    """Season totals table: No. | Name | FG | 3Pts | FT | Tot. | D.Rank | GP | Avg. | Rank"""
    rows = table.find_all("tr", recursive=False)
    for index, row in enumerate(rows):
        if row.get_text(" ", strip=True).lower() == "season totals" and index + 1 < len(rows):
            header_cells = rows[index + 1].find_all(["th", "td"], recursive=False)
            headers = [
                part.lower()
                for cell in header_cells
                for part in cell.get_text(" ", strip=True).split()
            ]
            return _parse_season_total_rows(rows[index + 2:], headers)

    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    if not ("fg" in headers and "gp" in headers and "avg." in headers):
        return []
    return _parse_season_total_rows(table.find_all("tr")[1:], headers)


def _parse_season_total_rows(rows, headers: list[str]) -> list[dict]:
    players = []
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if cells and cells[0].lower() == "team":
            break
        if len(cells) < 4:
            continue
        player = dict(zip(headers, cells))
        _convert_player_stats(player, ("fg", "3pts", "ft", "tot.", "gp"), int)
        _convert_player_stats(player, ("avg.", "avg"), float)
        players.append(player)
    return players


def _convert_player_stats(player: dict, fields: tuple[str, ...], converter) -> None:
    for field in fields:
        if field in player:
            try:
                player[field] = converter(player[field])
            except (ValueError, TypeError):
                pass


# ── Step 6: Parse division leaders ────────────────────────────────────────────

def parse_division_leaders(tables: list) -> dict:
    category_names = ("total_points", "scoring_avg", "three_pointers", "free_throws")
    leader_rows = [
        rows for rows in (_division_leader_rows(table) for table in tables) if rows
    ]
    return dict(zip(category_names, leader_rows[:len(category_names)]))


def _division_leader_rows(table) -> list[dict]:
    headers = [header.get_text(strip=True).lower() for header in table.find_all("th")]
    if not {"rank", "team", "no."}.issubset(headers):
        return []
    rows = []
    for row in table.find_all("tr")[1:]:
        cells = [cell.get_text(strip=True) for cell in row.find_all("td")]
        if len(cells) >= 3:
            rows.append(dict(zip(headers, cells)))
    return rows



# ── Main scrape function ───────────────────────────────────────────────────────

MONTH_TO_SEASON = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer",
    9: "Fall", 10: "Fall", 11: "Fall",
}

def infer_season_label(schedule: list, fallback_year: int | None = None) -> str:
    """Derive a season label like 'Spring 2025' from the first game date.

    Prefer a real year from the site metadata when available; this fallback only
    handles cases where the page does not expose a season label.
    """
    year_hint = fallback_year if fallback_year is not None else datetime.now().year

    for game in schedule:
        date_str = game.get("date", "")
        m = re.search(r"(\d{2})/(\d{2})", date_str)
        if m:
            month = int(m.group(1))
            year = year_hint
            if month > 12:
                continue
            season = MONTH_TO_SEASON.get(month, "Season")
            return f"{season} {year}"
    return "Unknown Season"


def season_matches_filter(season_label: str, filter_label: str) -> bool:
    """Match base season labels like 'Fall 2026' even when disambiguated with division text."""
    if not filter_label:
        return True
    normalized_filter = filter_label.strip()
    normalized_label = season_label.strip()
    if normalized_label == normalized_filter:
        return True
    if normalized_label.startswith(f"{normalized_filter} · "):
        return True
    if normalized_label.startswith(f"{normalized_filter} ("):
        return True
    return False


def _find_division(soup) -> str:
    for tag in soup.find_all(["h1", "h2", "h3", "p", "span", "div", "td"]):
        text = tag.get_text(strip=True)
        if re.match(r"^division:\s*\S", text, re.IGNORECASE) and len(text) < 60:
            return re.sub(r"^division:\s*", "", text, flags=re.IGNORECASE).strip()
    return ""


def _team_standings(tables: list, team_name: str) -> tuple[list[dict], dict]:
    if not tables:
        return [], {"wins": 0, "losses": 0}
    standings = enrich_standings(parse_standings(tables[0]))
    record = {"wins": 0, "losses": 0}
    for entry in standings:
        if entry["team"] == team_name:
            record = {"wins": entry["wins"], "losses": entry["losses"]}
            break
    return standings, record


def _find_schedule_table(tables: list):
    best_schedule_table = None
    best_row_count = 0
    for table in tables:
        first_row = table.find("tr")
        if not first_row:
            continue
        first_row_texts = [td.get_text(strip=True) for td in first_row.find_all("td")]
        if first_row_texts[:5] == ["Date", "Location", "Time", "Opponent", "Results"]:
            row_count = len(table.find_all("tr", recursive=False))
            if row_count > best_row_count:
                best_row_count = row_count
                best_schedule_table = table
    return best_schedule_table


def _team_box_scores(tables: list, schedule: list) -> list[dict]:
    for table in tables:
        headers = {header.get_text(strip=True) for header in table.find_all("th")}
        if {"Date", "Gym Location", "Opponent"}.issubset(headers):
            return parse_boxscores_from_combo_table(table, schedule)
    return []


def _team_season_totals(tables: list) -> list[dict]:
    for table in tables:
        totals = parse_season_totals(table)
        if totals:
            return totals
    return []


def scrape_team(session: requests.Session, team: dict) -> dict:
    print(f"Step 3: Scraping '{team['name']}'...")
    resp = get(session, team["url"])
    soup = BeautifulSoup(resp.text, HTML_PARSER)
    tables = soup.find_all("table")
    standings, record = _team_standings(tables, team["name"])
    schedule_table = _find_schedule_table(tables)
    schedule = parse_schedule_from_results_table(schedule_table) if schedule_table else []
    season_label = team.get("season_label", "") or infer_season_label(schedule)
    data = {
        "team_name": team["name"],
        "team_id": team["team_id"],
        "team_url": team["url"],
        "season_label": season_label,
        "division": _find_division(soup),
        "record": record,
        "schedule": schedule,
        "box_scores": _team_box_scores(tables, schedule),
        "season_totals": _team_season_totals(tables),
        "standings": standings,
        "division_leaders": parse_division_leaders(tables),
        "scraped_at": datetime.now().isoformat(),
    }

    print(f"  ✓ Season: {data['season_label']}  Division: {data['division']}")
    print(f"  ✓ Record: {data['record']['wins']}-{data['record']['losses']}")
    print(f"  ✓ Standings: {len(data['standings'])} teams")
    print(f"  ✓ Schedule: {len(data['schedule'])} games")
    print(f"  ✓ Box scores: {len(data['box_scores'])} games")
    print(f"  ✓ Season totals: {len(data['season_totals'])} players")
    print(f"  ✓ Leader categories: {list(data['division_leaders'].keys())}")

    return data


# ── Main ───────────────────────────────────────────────────────────────────────

PRIVATE_KEYS = {"name", "player", "player_name"}


def anonymize_output(value):
    """Recursively strip player-name fields so published data.json has no names.

    Jersey numbers remain so rows can still be told apart.
    """
    if isinstance(value, dict):
        return {k: anonymize_output(v) for k, v in value.items()
                if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [anonymize_output(v) for v in value]
    return value


def _parse_args():
    parser = argparse.ArgumentParser(description="Scrape NYUrban stats and write JSON for a season or all seasons.")
    parser.add_argument("--season", default=os.environ.get("NYURBAN_SEASON"), help="Only include seasons matching this label, e.g. 'Fall 2026'.")
    return parser.parse_args()


def _create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    })
    return session


def _scrape_seasons(session: requests.Session, teams: list[dict], season_filter: str | None) -> list[dict]:
    all_season_data = []
    for team in teams:
        team_data = scrape_team(session, team)
        if season_filter and not season_matches_filter(
            team_data.get("season_label", ""), season_filter
        ):
            continue
        all_season_data.append(team_data)
    return all_season_data


def _disambiguate_season_labels(seasons: list[dict]) -> None:
    label_counts = {}
    for season in seasons:
        label = season["season_label"]
        label_counts[label] = label_counts.get(label, 0) + 1

    label_seen = {}
    for season in seasons:
        label = season["season_label"]
        if label_counts[label] <= 1:
            continue
        label_seen[label] = label_seen.get(label, 0) + 1
        division = season.get("division", "")
        season["season_label"] = (
            f"{label} · {division}" if division else f"{label} ({label_seen[label]})"
        )


def _group_seasons_by_team(seasons: list[dict]) -> list[dict]:
    grouped: dict[str, list] = {}
    for season in seasons:
        name = season["team_name"]
        grouped.setdefault(name, []).append(season)
    teams_output = []
    for team_name, seasons in grouped.items():
        _disambiguate_season_labels(seasons)
        teams_output.append({"team_name": team_name, "seasons": seasons})
    return teams_output


def main():
    args = _parse_args()
    session = _create_session()
    if not login(session):
        print("Aborting — login failed.")
        return

    teams = get_team_links(session)
    if not teams:
        print("No teams found.")
        return

    all_season_data = _scrape_seasons(session, teams, args.season)
    if args.season and not all_season_data:
        print(f"No seasons matched '{args.season}'.")
        return

    output = anonymize_output({
        "teams": _group_seasons_by_team(all_season_data),
        "scraped_at": datetime.now().isoformat(),
    })
    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_FILE)), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Done! Data saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
