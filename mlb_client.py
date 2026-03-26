"""
MLB Stats API client.

Uses the free, public MLB Stats API (statsapi package) to pull:
  - Today's game schedule with probable pitchers
  - Player season and recent statistics
  - Team info

Docs: https://github.com/toddrob99/MLB-StatsAPI
"""

from __future__ import annotations

from typing import Optional

import statsapi

from models import (
    GameMatchup,
    HittingStats,
    PitchingStats,
    PlayerType,
)
from utils import log, today_str, days_ago_str


class MLBClient:
    """Wraps the MLB Stats API for fantasy-relevant data."""

    # ── Schedule ────────────────────────────────────────────────────────

    def get_todays_games(self, game_date: Optional[str] = None) -> list[GameMatchup]:
        """Return every MLB game scheduled for the given date."""
        date_str = game_date or today_str()
        log.info(f"Fetching MLB schedule for {date_str}")

        try:
            schedule = statsapi.schedule(date=date_str)
        except Exception as exc:
            log.error(f"Failed to fetch MLB schedule: {exc}")
            return []

        games: list[GameMatchup] = []
        for g in schedule:
            matchup = GameMatchup(
                game_id=g.get("game_id", 0),
                home_team=g.get("home_name", ""),
                away_team=g.get("away_name", ""),
                game_time=g.get("game_datetime", ""),
                venue=g.get("venue_name", ""),
                home_probable_pitcher=g.get("home_probable_pitcher", ""),
                away_probable_pitcher=g.get("away_probable_pitcher", ""),
            )

            # Look up probable pitcher ERAs
            for side in ("home", "away"):
                pid = g.get(f"{side}_pitcher_id")
                if pid:
                    era = self._get_pitcher_era(pid)
                    if side == "home":
                        matchup.home_pitcher_era = era
                    else:
                        matchup.away_pitcher_era = era

            games.append(matchup)

        log.info(f"Found {len(games)} games scheduled")
        return games

    # ── Player Stats ────────────────────────────────────────────────────

    def get_player_season_stats(
        self, player_name: str, player_type: PlayerType
    ) -> Optional[HittingStats | PitchingStats]:
        """Look up a player by name and return current-season stats."""
        player_id = self._lookup_player_id(player_name)
        if not player_id:
            return None

        group = "hitting" if player_type == PlayerType.HITTER else "pitching"
        try:
            raw = statsapi.player_stat_data(player_id, group=group, type="season")
        except Exception as exc:
            log.debug(f"Stats lookup failed for {player_name}: {exc}")
            return None

        stats_list = raw.get("stats", [])
        if not stats_list:
            return None

        s = stats_list[0].get("stats", {})
        return self._parse_hitting(s) if player_type == PlayerType.HITTER else self._parse_pitching(s)

    def get_player_recent_stats(
        self, player_name: str, player_type: PlayerType, days: int = 14,
    ) -> Optional[HittingStats | PitchingStats]:
        """Pull a player's stats over the last N days."""
        player_id = self._lookup_player_id(player_name)
        if not player_id:
            return None

        group = "hitting" if player_type == PlayerType.HITTER else "pitching"
        start = days_ago_str(days)
        end = today_str()

        try:
            raw = statsapi.player_stat_data(
                player_id, group=group, type="byDateRange",
                params={"startDate": start, "endDate": end},
            )
        except Exception as exc:
            log.debug(f"Recent stats lookup failed for {player_name}: {exc}")
            return None

        stats_list = raw.get("stats", [])
        if not stats_list:
            return None

        s = stats_list[0].get("stats", {})
        return self._parse_hitting(s) if player_type == PlayerType.HITTER else self._parse_pitching(s)

    # ── Internal Helpers ────────────────────────────────────────────────

    def _lookup_player_id(self, name: str) -> Optional[int]:
        try:
            results = statsapi.lookup_player(name)
            if results:
                return results[0]["id"]
        except Exception:
            pass
        return None

    def _get_pitcher_era(self, pitcher_id: int) -> Optional[float]:
        try:
            data = statsapi.player_stat_data(pitcher_id, group="pitching", type="season")
            stats_list = data.get("stats", [])
            if stats_list:
                return float(stats_list[0].get("stats", {}).get("era", "0.00"))
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_hitting(s: dict) -> HittingStats:
        def _int(k): return int(s.get(k, 0) or 0)
        def _float(k): return float(s.get(k, 0.0) or 0.0)

        return HittingStats(
            games=_int("gamesPlayed"),
            at_bats=_int("atBats"),
            runs=_int("runs"),
            hits=_int("hits"),
            doubles=_int("doubles"),
            triples=_int("triples"),
            home_runs=_int("homeRuns"),
            rbi=_int("rbi"),
            stolen_bases=_int("stolenBases"),
            caught_stealing=_int("caughtStealing"),
            walks=_int("baseOnBalls"),
            strikeouts=_int("strikeOuts"),
            hit_by_pitch=_int("hitByPitch"),
            sacrifice_flies=_int("sacFlies"),
            batting_avg=_float("avg"),
            on_base_pct=_float("obp"),
            slugging_pct=_float("slg"),
            ops=_float("ops"),
        )

    @staticmethod
    def _parse_pitching(s: dict) -> PitchingStats:
        def _int(k): return int(s.get(k, 0) or 0)
        def _float(k): return float(s.get(k, 0.0) or 0.0)

        return PitchingStats(
            games=_int("gamesPlayed"),
            games_started=_int("gamesStarted"),
            wins=_int("wins"),
            losses=_int("losses"),
            saves=_int("saves"),
            holds=_int("holds"),
            innings_pitched=_float("inningsPitched"),
            hits_allowed=_int("hits"),
            runs_allowed=_int("runs"),
            earned_runs=_int("earnedRuns"),
            walks_allowed=_int("baseOnBalls"),
            strikeouts=_int("strikeOuts"),
            home_runs_allowed=_int("homeRuns"),
            era=_float("era"),
            whip=_float("whip"),
            quality_starts=_int("qualityStarts") if "qualityStarts" in s else 0,
            complete_games=_int("completeGames"),
        )

    def get_team_abbreviation_map(self) -> dict[str, str]:
        """Map full team names → abbreviations for cross-API matching."""
        try:
            teams = statsapi.get("teams", {"sportIds": 1})
            mapping = {}
            for t in teams.get("teams", []):
                mapping[t["name"]] = t.get("abbreviation", t["name"])
                mapping[t.get("teamName", "")] = t.get("abbreviation", "")
            return mapping
        except Exception:
            return {}
