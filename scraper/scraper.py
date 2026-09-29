"""
NYUrban Basketball Scraper
Logs in via the site's custom form, finds all team pages, and scrapes:
  - Division standings
  - Team schedule & results
  - Per-game player box scores
  - Season totals per player
  - Division scoring leaders
"""

import argparse
import requests
from bs4 import BeautifulSoup
import json
import re
import os
import sqlite3
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
DATABASE_FILE = os.path.join(os.path.dirname(__file__), "nyurban_stats.sqlite3")


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


def get_team_links(session: requests.Session) -> list[dict]:
    print("Step 2: Fetching team list...")
    resp = get(session, TEAM_LIST_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    season_by_team_id = {}

    # The team list page itself does not expose the season label. The authoritative
    # metadata lives in each team-detail page's "my_team_for_image" dropdown,
    # which contains entries like "Euknicks (Fall Basketball 2026)".
    team_urls = []
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "team-details" in href and "team_id=" in href:
            m = re.search(r"team_id=(\d+)", href)
            if m:
                tid = m.group(1)
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    name = a.get_text(strip=True)
                    team_urls.append({
                        "name": name,
                        "url": href,
                        "team_id": tid,
                    })

    for team in team_urls:
        detail_resp = get(session, team["url"])
        detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
        for option in detail_soup.select("select#my_team_for_image option, select[name='my_team_for_image'] option"):
            team_id = str(option.get("value", "")).strip()
            label = option.get_text(" ", strip=True)
            if not team_id or not label:
                continue
            match = re.search(r"\(([^()]+)\)$", label)
            if not match:
                continue
            season_label = _normalize_site_season_label(match.group(1))
            if season_label:
                season_by_team_id[team_id] = season_label

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

def parse_standings(table) -> list[dict]:
    standings = []
    rows = table.find_all("tr", recursive=False)
    for row in rows[1:]:  # skip header
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4:
            continue
        name_cell = cells[1]
        name = ""
        for content in name_cell.children:
            if hasattr(content, "get_text"):
                text = content.get_text(strip=True)
                if text and text != "arrow":
                    if content.name == "div":
                        break
                    name = text
            else:
                text = str(content).strip()
                if text:
                    name = text
                    break

        if not name:
            full = name_cell.get_text(" ", strip=True)
            name = full.split("arrow")[0].strip()

        wins_text = cells[2].get_text(strip=True)
        losses_text = cells[3].get_text(strip=True)
        pct_text = cells[4].get_text(strip=True) if len(cells) > 4 else ""

        try:
            wins = int(wins_text)
            losses = int(losses_text)
        except ValueError:
            continue

        # Extract per-game results from the nested popup table
        # The popup includes playoff games, but standings W/L is regular season only.
        # Cap to wins+losses to keep only regular season games.
        games = []
        nested = name_cell.find("table")
        if nested:
            for gr in nested.find_all("tr")[1:]:
                gcells = [td.get_text(strip=True) for td in gr.find_all("td")]
                if len(gcells) >= 2:
                    opp = gcells[0]
                    result = gcells[1]
                    m = re.match(r"([WL])\s*(\d+)-(\d+)", result)
                    if m:
                        outcome = m.group(1)
                        score_a, score_b = int(m.group(2)), int(m.group(3))
                        if outcome == "W":
                            pts_for, pts_against = score_a, score_b
                        else:
                            pts_for, pts_against = score_b, score_a
                        games.append({
                            "opponent": opp,
                            "outcome": outcome,
                            "pts_for": pts_for,
                            "pts_against": pts_against,
                        })
        # Trim to regular season only (wins + losses from standings row)
        regular_season_gp = wins + losses
        games = games[:regular_season_gp]

        standings.append({
            "team": name,
            "wins": wins,
            "losses": losses,
            "pct": pct_text,
            "games": games,
        })
    return standings


def enrich_standings(standings: list[dict]) -> list[dict]:
    """
    Add PPG, Opp PPG, Point Diff, SOS, and SRS to each standings entry.

    SRS (Simple Rating System) matches Basketball Reference's methodology:
      - SRS = Point Differential + SOS  (both in points per game)
      - SOS = average SRS of opponents faced
      - Solved iteratively until convergence (~20 passes)

    SOS reported is the converged average-opponent-SRS value.
    """
    # Build a name → entry map for quick lookup
    by_name = {s["team"]: s for s in standings}

    # Compute PPG / OppPPG / pt_diff first
    for s in standings:
        games = s.get("games", [])
        gp = len(games)
        if gp == 0:
            s.update({"ppg": None, "opp_ppg": None, "pt_diff": None,
                       "sos": None, "srs": None})
            continue
        pts_for     = sum(g["pts_for"]     for g in games)
        pts_against = sum(g["pts_against"] for g in games)
        s["ppg"]     = round(pts_for     / gp, 1)
        s["opp_ppg"] = round(pts_against / gp, 1)
        s["pt_diff"] = round((pts_for - pts_against) / gp, 1)

    # Iterative SRS solve
    # Seed: SRS = pt_diff
    srs = {s["team"]: (s["pt_diff"] or 0.0) for s in standings}

    for _ in range(50):  # converges well within 20 iterations
        new_srs = {}
        for s in standings:
            games = s.get("games", [])
            if not games:
                new_srs[s["team"]] = 0.0
                continue
            opp_srs_avg = sum(srs.get(g["opponent"], 0.0) for g in games) / len(games)
            new_srs[s["team"]] = (s["pt_diff"] or 0.0) + opp_srs_avg
        # Check convergence
        if all(abs(new_srs[t] - srs[t]) < 0.001 for t in srs):
            srs = new_srs
            break
        srs = new_srs

    # Write back SOS (= SRS - pt_diff) and SRS
    for s in standings:
        if s["pt_diff"] is None:
            s["sos"] = None
            s["srs"] = None
        else:
            s["srs"] = round(srs[s["team"]], 1)
            s["sos"] = round(srs[s["team"]] - s["pt_diff"], 1)

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


def parse_boxscores_from_combo_table(table, schedule: list) -> list[dict]:
    """
    Parse box scores from the combo table (table 12-style).
    Alternating rows: game header (+-|date|gym|opponent) then box score row.
    Merge with schedule to get results.
    """
    box_scores = []
    # Build a lookup from opponent name to result
    result_lookup = {g["opponent"]: g for g in schedule}

    rows = table.find_all("tr", recursive=False)
    i = 0
    while i < len(rows):
        cells = rows[i].find_all("td", recursive=False)
        texts = [c.get_text(strip=True) for c in cells]

        if len(cells) >= 4 and texts[0] == "+-":
            date = texts[1]
            gym = texts[2]
            opponent = texts[3]

            # Look up result from schedule
            game_info = result_lookup.get(opponent, {})
            result_raw = game_info.get("result", "")
            parsed = parse_result(result_raw)

            # Next row = box score
            if i + 1 < len(rows):
                box_row = rows[i + 1]
                nested_tables = box_row.find_all("table")
                players = []
                for nt in nested_tables:
                    nt_headers = [th.get_text(strip=True).lower() for th in nt.find_all("th")]
                    if "fg" in nt_headers and "total" in nt_headers:
                        for pr in nt.find_all("tr")[1:]:
                            pcells = [td.get_text(strip=True) for td in pr.find_all("td")]
                            if len(pcells) >= 4:
                                player = dict(zip(nt_headers, pcells))
                                for field in ["fg", "3pts", "ft", "total"]:
                                    if field in player:
                                        try:
                                            player[field] = int(player[field])
                                        except (ValueError, TypeError):
                                            pass
                                players.append(player)
                        break

                if players:
                    box_scores.append({
                        "date": date,
                        "gym": gym,
                        "opponent": opponent,
                        "result": result_raw,
                        **parsed,
                        "players": players,
                    })
                i += 2
            else:
                i += 1
        else:
            i += 1

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
    leaders = {}
    category_names = ["total_points", "scoring_avg", "three_pointers", "free_throws"]
    found = []
    for table in tables:
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if "rank" in headers and "team" in headers and "no." in headers:
            rows = []
            for row in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if cells and len(cells) >= 3:
                    rows.append(dict(zip(headers, cells)))
            if rows:
                found.append({"headers": headers, "rows": rows})

    for idx, lt in enumerate(found[:4]):
        cat = category_names[idx] if idx < len(category_names) else f"category_{idx}"
        leaders[cat] = lt["rows"]

    return leaders


def _player_name(player: dict) -> str:
    name = str(player.get("name") or player.get("player") or "").strip()
    if name:
        return name
    jersey_number = str(player.get("no.") or player.get("no") or "").strip()
    return f"#{jersey_number}" if jersey_number else ""


def _stat_number(player: dict, *keys: str):
    for key in keys:
        value = player.get(key)
        if isinstance(value, (int, float)):
            return value
        if value is not None:
            try:
                return float(str(value).replace(",", ""))
            except ValueError:
                continue
    return None


def _snapshot_date(recorded_at: str) -> str:
    try:
        return datetime.fromisoformat(recorded_at).date().isoformat()
    except ValueError:
        return datetime.now().date().isoformat()


def _store_game_player(connection, team_id: str, season_label: str, game: dict,
                       player: dict, recorded_at: str) -> bool:
    name = _player_name(player)
    if not name:
        return False
    connection.execute("""
        INSERT INTO player_game_stats (
            team_id, season_label, game_date, opponent, gym, player_name,
            points, field_goals, three_pointers, free_throws, stats_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (team_id, season_label, game_date, opponent, gym, player_name)
        DO UPDATE SET points = excluded.points,
            field_goals = excluded.field_goals,
            three_pointers = excluded.three_pointers,
            free_throws = excluded.free_throws,
            stats_json = excluded.stats_json,
            recorded_at = excluded.recorded_at
    """, (
        team_id, season_label, str(game.get("date", "")),
        str(game.get("opponent", "")), str(game.get("gym", "")), name,
        _stat_number(player, "total", "tot."),
        _stat_number(player, "fg"),
        _stat_number(player, "3pts", "3pt"),
        _stat_number(player, "ft"),
        json.dumps(player, sort_keys=True), recorded_at,
    ))
    return True


def _store_season_snapshot(connection, team_id: str, season_label: str,
                            snapshot_date: str, player: dict,
                            recorded_at: str) -> bool:
    name = _player_name(player)
    if not name:
        return False
    connection.execute("""
        INSERT INTO player_season_snapshots (
            team_id, season_label, snapshot_date, player_name,
            total_points, games_played, scoring_average,
            three_pointers, free_throws, stats_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (team_id, season_label, snapshot_date, player_name)
        DO UPDATE SET total_points = excluded.total_points,
            games_played = excluded.games_played,
            scoring_average = excluded.scoring_average,
            three_pointers = excluded.three_pointers,
            free_throws = excluded.free_throws,
            stats_json = excluded.stats_json,
            recorded_at = excluded.recorded_at
    """, (
        team_id, season_label, snapshot_date, name,
        _stat_number(player, "tot.", "total"),
        _stat_number(player, "gp"),
        _stat_number(player, "avg.", "avg"),
        _stat_number(player, "3pts", "3pt"),
        _stat_number(player, "ft"),
        json.dumps(player, sort_keys=True), recorded_at,
    ))
    return True


def _store_division_leader(connection, season: dict, snapshot_date: str,
                           category: str, leader: dict, recorded_at: str) -> bool:
    team_name = str(leader.get("team", "")).strip()
    jersey_number = str(leader.get("no.") or leader.get("no") or "").strip()
    if not team_name or not jersey_number:
        return False
    connection.execute("""
        INSERT INTO division_leader_snapshots (
            division, season_label, snapshot_date, category, team_name,
            jersey_number, metric_value, games_played, stats_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (division, season_label, snapshot_date, category, team_name, jersey_number)
        DO UPDATE SET metric_value = excluded.metric_value,
            games_played = excluded.games_played,
            stats_json = excluded.stats_json,
            recorded_at = excluded.recorded_at
    """, (
        str(season.get("division", "")), str(season.get("season_label", "")),
        snapshot_date, category, team_name, jersey_number,
        _stat_number(leader, "total", "avg", "3 pts", "3pts", "ft"),
        _stat_number(leader, "gp"),
        json.dumps(leader, sort_keys=True), recorded_at,
    ))
    return True


def persist_player_history(seasons: list[dict]) -> tuple[int, int, int]:
    """Store observed box scores and daily season-stat snapshots in SQLite."""
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE_FILE)), exist_ok=True)
    connection = sqlite3.connect(DATABASE_FILE)
    game_rows = 0
    snapshot_rows = 0
    leader_rows = 0
    try:
        with connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS player_game_stats (
                    team_id TEXT NOT NULL,
                    season_label TEXT NOT NULL,
                    game_date TEXT NOT NULL,
                    opponent TEXT NOT NULL,
                    gym TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    points REAL,
                    field_goals REAL,
                    three_pointers REAL,
                    free_throws REAL,
                    stats_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (team_id, season_label, game_date, opponent, gym, player_name)
                );
                CREATE TABLE IF NOT EXISTS player_season_snapshots (
                    team_id TEXT NOT NULL,
                    season_label TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    total_points REAL,
                    games_played REAL,
                    scoring_average REAL,
                    three_pointers REAL,
                    free_throws REAL,
                    stats_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (team_id, season_label, snapshot_date, player_name)
                );
                CREATE INDEX IF NOT EXISTS player_snapshot_history
                    ON player_season_snapshots (team_id, season_label, player_name, snapshot_date);
                CREATE TABLE IF NOT EXISTS division_leader_snapshots (
                    division TEXT NOT NULL,
                    season_label TEXT NOT NULL,
                    snapshot_date TEXT NOT NULL,
                    category TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    jersey_number TEXT NOT NULL,
                    metric_value REAL,
                    games_played REAL,
                    stats_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (division, season_label, snapshot_date, category, team_name, jersey_number)
                );
                CREATE INDEX IF NOT EXISTS division_leader_history
                    ON division_leader_snapshots (division, season_label, category, team_name, jersey_number, snapshot_date);
            """)
            for season in seasons:
                team_id = str(season.get("team_id", ""))
                season_label = str(season.get("season_label", ""))
                recorded_at = season.get("scraped_at") or datetime.now().isoformat()
                snapshot_date = _snapshot_date(recorded_at)

                for game in season.get("box_scores", []):
                    for player in game.get("players", []):
                        game_rows += _store_game_player(
                            connection, team_id, season_label, game, player, recorded_at
                        )

                for player in season.get("season_totals", []):
                    snapshot_rows += _store_season_snapshot(
                        connection, team_id, season_label, snapshot_date, player, recorded_at
                    )

                for category, leaders in season.get("division_leaders", {}).items():
                    for leader in leaders:
                        leader_rows += _store_division_leader(
                            connection, season, snapshot_date, category, leader, recorded_at
                        )
    finally:
        connection.close()
    return game_rows, snapshot_rows, leader_rows


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
            day = int(m.group(2))
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


def scrape_team(session: requests.Session, team: dict) -> dict:
    print(f"Step 3: Scraping '{team['name']}'...")
    resp = get(session, team["url"])
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    data = {
        "team_name": team["name"],
        "team_id": team["team_id"],
        "team_url": team["url"],
        "season_label": team.get("season_label", ""),
        "division": "",
        "record": {"wins": 0, "losses": 0},
        "schedule": [],
        "box_scores": [],
        "season_totals": [],
        "standings": [],
        "division_leaders": {},
        "scraped_at": datetime.now().isoformat(),
    }

    # Division name — look for "Division: X" in h1/h2 tags
    for tag in soup.find_all(["h1", "h2", "h3", "p", "span", "div", "td"]):
        text = tag.get_text(strip=True)
        if re.match(r"^division:\s*\S", text, re.IGNORECASE) and len(text) < 60:
            data["division"] = re.sub(r"^division:\s*", "", text, flags=re.IGNORECASE).strip()
            break

    # Standings (table 0)
    if tables:
        data["standings"] = enrich_standings(parse_standings(tables[0]))
        for entry in data["standings"]:
            if entry["team"] == team["name"]:
                data["record"] = {"wins": entry["wins"], "losses": entry["losses"]}
                break

    # Schedule (table with Date | Location | Time | Opponent | Results, no <th> tags)
    # Pick the largest matching table (regular season, not playoffs)
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
    if best_schedule_table:
        data["schedule"] = parse_schedule_from_results_table(best_schedule_table)

    # Box scores (combo table with +- header rows)
    for table in tables:
        ths = [th.get_text(strip=True) for th in table.find_all("th")]
        if "Date" in ths and "Gym Location" in ths and "Opponent" in ths:
            data["box_scores"] = parse_boxscores_from_combo_table(table, data["schedule"])
            break

    # Season totals appear in the combo table after the per-game box scores.
    for table in tables:
        data["season_totals"] = parse_season_totals(table)
        if data["season_totals"]:
            break

    # Division leaders
    data["division_leaders"] = parse_division_leaders(tables)

    # Prefer the actual site season label when the team list exposes it.
    # Otherwise fall back to the schedule-derived heuristic.
    if not data["season_label"]:
        data["season_label"] = infer_season_label(data["schedule"])

    print(f"  ✓ Season: {data['season_label']}  Division: {data['division']}")
    print(f"  ✓ Record: {data['record']['wins']}-{data['record']['losses']}")
    print(f"  ✓ Standings: {len(data['standings'])} teams")
    print(f"  ✓ Schedule: {len(data['schedule'])} games")
    print(f"  ✓ Box scores: {len(data['box_scores'])} games")
    print(f"  ✓ Season totals: {len(data['season_totals'])} players")
    print(f"  ✓ Leader categories: {list(data['division_leaders'].keys())}")

    return data


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape NYUrban stats and write JSON for a season or all seasons.")
    parser.add_argument("--season", default=os.environ.get("NYURBAN_SEASON"), help="Only include seasons matching this label, e.g. 'Fall 2026'.")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"
    })

    if not login(session):
        print("Aborting — login failed.")
        return

    teams = get_team_links(session)
    if not teams:
        print("No teams found.")
        return

    all_season_data = []
    for team in teams:
        team_data = scrape_team(session, team)
        if args.season and not season_matches_filter(team_data.get("season_label", ""), args.season):
            continue
        all_season_data.append(team_data)

    if args.season and not all_season_data:
        print(f"No seasons matched '{args.season}'.")
        return

    # Group seasons by team name
    grouped: dict[str, list] = {}
    for season in all_season_data:
        name = season["team_name"]
        grouped.setdefault(name, []).append(season)

    # Build output: one entry per team, with a list of seasons
    teams_output = []
    for team_name, seasons in grouped.items():
        # Deduplicate season labels (e.g. two "Spring 2026" entries get suffixes)
        label_counts: dict[str, int] = {}
        for s in seasons:
            lbl = s["season_label"]
            label_counts[lbl] = label_counts.get(lbl, 0) + 1

        label_seen: dict[str, int] = {}
        for s in seasons:
            lbl = s["season_label"]
            if label_counts[lbl] > 1:
                label_seen[lbl] = label_seen.get(lbl, 0) + 1
                # Append division to disambiguate
                div = s.get("division", "")
                s["season_label"] = f"{lbl} · {div}" if div else f"{lbl} ({label_seen[lbl]})"

        teams_output.append({
            "team_name": team_name,
            "seasons": seasons,
        })

    output = {
        "teams": teams_output,
        "scraped_at": datetime.now().isoformat(),
    }

    game_rows, snapshot_rows, leader_rows = persist_player_history(all_season_data)
    print(f"  ✓ History saved: {game_rows} game-player rows, {snapshot_rows} daily player snapshots, {leader_rows} division leader rows")

    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_FILE)), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Done! Data saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
