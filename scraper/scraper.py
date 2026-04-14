"""
NYUrban Basketball Scraper
Logs in via the site's custom form, finds all team pages, and scrapes:
  - Division standings
  - Team schedule & results
  - Per-game player box scores
  - Season totals per player
  - Division scoring leaders
"""

import requests
from bs4 import BeautifulSoup
import json
import re
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.nyurban.com"
LOGIN_URL = f"{BASE_URL}/basketball/new-scoring-stats/"
TEAM_LIST_URL = f"{BASE_URL}/waiver-team-listing/"

USERNAME = os.environ["NYURBAN_USERNAME"]
PASSWORD = os.environ["NYURBAN_PASSWORD"]

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "../web/data.json")


# ── Step 1: Login ─────────────────────────────────────────────────────────────

def login(session: requests.Session) -> bool:
    print("Step 1: Logging in...")
    resp = session.post(
        LOGIN_URL,
        data={"ny_username": USERNAME, "ny_password": PASSWORD, "submit": "login"},
        allow_redirects=True,
    )
    if "logout" in resp.text.lower() or "welcome" in resp.text.lower():
        print("  ✓ Login successful")
        return True
    print("  ✗ Login failed")
    return False


# ── Step 2: Get team list (all seasons) ───────────────────────────────────────

def get_team_links(session: requests.Session) -> list[dict]:
    print("Step 2: Fetching team list...")
    resp = session.get(TEAM_LIST_URL)
    soup = BeautifulSoup(resp.text, "html.parser")
    teams = []
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
                    teams.append({"name": name, "url": href, "team_id": tid})
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
                        # Score format is always winner-loser, so:
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
    m = re.search(r"([WL])\s*(\d+)-(\d+)", result_text.strip())
    if m:
        return {
            "outcome": m.group(1),
            "team_score": int(m.group(2)),
            "opp_score": int(m.group(3)),
        }
    return {"outcome": "", "team_score": None, "opp_score": None}


def parse_schedule_from_results_table(table) -> list[dict]:
    """
    Parse the schedule table: Date | Location | Time | Opponent | Results
    The opponent cell contains popup HTML — extract just the team name.
    """
    schedule = []
    rows = table.find_all("tr", recursive=False)
    for row in rows[1:]:  # skip header
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        date = cells[0].get_text(strip=True)
        # Location: first text node before the popup
        loc_text = cells[1].get_text(" ", strip=True).split("arrow")[0].strip()
        time = cells[2].get_text(strip=True)
        # Opponent: first text before "arrow"
        opp_text = cells[3].get_text(" ", strip=True).split("arrow")[0].strip()
        result_raw = cells[4].get_text(strip=True)

        if not date or opp_text in ("*** No Game This Week", ""):
            continue

        parsed = parse_result(result_raw)
        schedule.append({
            "date": date,
            "location": loc_text,
            "time": time,
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
    headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
    if not ("fg" in headers and "gp" in headers):
        return []
    players = []
    for row in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 4:
            continue
        player = dict(zip(headers, cells))
        for field in ["fg", "3pts", "ft", "tot.", "gp"]:
            if field in player:
                try:
                    player[field] = int(player[field])
                except (ValueError, TypeError):
                    pass
        for field in ["avg.", "avg"]:
            if field in player:
                try:
                    player[field] = float(player[field])
                except (ValueError, TypeError):
                    pass
        players.append(player)
    return players


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


# ── Main scrape function ───────────────────────────────────────────────────────

MONTH_TO_SEASON = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer",
    9: "Fall", 10: "Fall", 11: "Fall",
}

def infer_season_label(schedule: list) -> str:
    """Derive a season label like 'Spring 2025' from the first game date."""
    for game in schedule:
        date_str = game.get("date", "")
        # Format: "Thu 04/03" or "Mon 04/13"
        m = re.search(r"(\d{2})/(\d{2})", date_str)
        if m:
            month = int(m.group(1))
            day = int(m.group(2))
            # Year is ambiguous — use current year heuristic:
            # if month >= current month we're probably in the same year,
            # otherwise it could be last year. We'll just use the scraped_at year
            # adjusted: if month > 6 and scraped in early year, subtract 1.
            now = datetime.now()
            year = now.year
            # If the game month is much later than now, it's probably last year
            if month > now.month + 3:
                year -= 1
            season = MONTH_TO_SEASON.get(month, "Season")
            return f"{season} {year}"
    return "Unknown Season"

def scrape_team(session: requests.Session, team: dict) -> dict:
    print(f"Step 3: Scraping '{team['name']}'...")
    resp = session.get(team["url"])
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    data = {
        "team_name": team["name"],
        "team_id": team["team_id"],
        "team_url": team["url"],
        "season_label": "",
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

    # Season totals (table with GP and Avg columns)
    for table in tables:
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if "fg" in headers and "gp" in headers and "avg." in headers:
            data["season_totals"] = parse_season_totals(table)
            break

    # Division leaders
    data["division_leaders"] = parse_division_leaders(tables)

    # Infer season label from schedule dates
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
        all_season_data.append(team_data)

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

    os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_FILE)), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✓ Done! Data saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
