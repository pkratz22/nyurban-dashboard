"""Post-processing and persistence: season filtering/grouping, anonymizing, JSON output."""

from __future__ import annotations

import json
import os

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "../web/data.json")

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


def group_seasons_by_team(seasons: list[dict]) -> list[dict]:
    grouped: dict[str, list] = {}
    for season in seasons:
        grouped.setdefault(season["team_name"], []).append(season)
    teams_output = []
    for team_name, team_seasons in grouped.items():
        _disambiguate_season_labels(team_seasons)
        teams_output.append({"team_name": team_name, "seasons": team_seasons})
    return teams_output


def write_output(data: dict, path: str | None = None) -> str:
    """Write JSON to `path` (default OUTPUT_FILE, looked up at call time)."""
    path = path or OUTPUT_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path
