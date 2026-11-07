"""Pure HTML parsers for nyurban.com pages.

Nothing in this module touches the network or the filesystem: every function
takes already-fetched HTML (or a BeautifulSoup node) and returns plain data.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, unquote_plus, urlparse

from bs4 import BeautifulSoup

HTML_PARSER = "html.parser"


# ── Team list + season labels ─────────────────────────────────────────────────

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



def parse_team_list(html: str) -> list[dict]:
    """Team-listing page -> [{name, url, team_id}] (deduplicated by team_id)."""
    return _extract_team_urls(BeautifulSoup(html, HTML_PARSER))


def parse_season_labels(html: str) -> dict[str, str]:
    """Team-detail page -> {team_id: 'Fall 2026'} from the team <select>."""
    soup = BeautifulSoup(html, HTML_PARSER)
    options = soup.select(
        "select#my_team_for_image option, select[name='my_team_for_image'] option"
    )
    labels = {}
    for option in options:
        team_id, season_label = _season_label_from_option(option)
        if team_id and season_label:
            labels[team_id] = season_label
    return labels


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


def parse_team_page(html: str, team: dict) -> dict:
    """Team-detail page -> the full per-season record for that team."""
    soup = BeautifulSoup(html, HTML_PARSER)
    tables = soup.find_all("table")
    standings, record = _team_standings(tables, team["name"])
    schedule_table = _find_schedule_table(tables)
    schedule = parse_schedule_from_results_table(schedule_table) if schedule_table else []
    return {
        "team_name": team["name"],
        "team_id": team["team_id"],
        "team_url": team["url"],
        "season_label": team.get("season_label", "") or infer_season_label(schedule),
        "division": _find_division(soup),
        "record": record,
        "schedule": schedule,
        "box_scores": _team_box_scores(tables, schedule),
        "season_totals": _team_season_totals(tables),
        "standings": standings,
        "division_leaders": parse_division_leaders(tables),
        "scraped_at": datetime.now().isoformat(),
    }
