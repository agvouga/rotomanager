"""
MLB Stats API client.

Uses the free, public MLB Stats API (via the statsapi Python package) to pull:
  - Today's game schedule with probable pitchers
  - Player season and recent statistics
  - Team information and rosters

Docs: https://github.com/toddrob99/MLB-StatsAPI
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import statsapi

from models import (
    GameMatchup,
    HittingStats,
    PitchingStats,
    Player,
    PlayerType,
)
from utils import log, today_str, days_ago_str, safe_divide


class MLBClient:
    """Wraps the MLB Stats API for fantasy-relevant data."""

    # ── Schedule ────────────────────────────────────────────────────────

    def get_todays_games(self, game_date: Optional[str] = None) -> list[GameMatchup]:
        """
        Return every MLB game scheduled for today (or the given date).

        Each GameMatchup includes teams, venue, game time, and probable
        starting pitchers with their season ERA.
        """
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
                venue=g.get("venue_name", ""),
                home_probable_pitcher=g.get("home_probable_pitcher", ""),
                away_probable_pitcher=g.get("away_probable_pitcher", ""),
            )

            # Fetch probable pitcher ERAs when available
            for side in ("home", "away"):
                pitcher_name = g.get(f"{side}_probable_pitcher", "")
                pitcher_id = g.get(f"{side}_pitcher_id")  # may not exist
                if pitcher_id:
                    era = self._get_pitcher_era(pitcher_id)
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
        """
        Look up a player by name and return their current-season stats.

        Falls back gracefully if the player isn't found or has no stats.
        """
        player_id = self._lookup_player_id(player_name)
        if not player_id:
            log.debug(f"Could not find MLB ID for '{player_name}'")
            return None

        try:
            raw = statsapi.player_stat_data(
                player_id, group="hitting" if player_type == PlayerType.HITTER else "pitching",
                type="season",
            )
        except Exception as exc:
            log.debug(f"Stats lookup failed for {player_name}: {exc}")
            return None

        stats_list = raw.get("stats", [])
        if not stats_list:
            return None

        s = stats_list[0].get("stats", {})

        if player_type == PlayerType.HITTER:
            return self._parse_hitting(s)
        else:
            return self._parse_pitching(s)

    def get_player_recent_stats(
        self,
        player_name: str,
        player_type: PlayerType,
        days: int = 14,
    ) -> Optional[HittingStats | PitchingStats]:
        """
        Pull a player's stats over the last N days using the 'lastXdays'
        stat type from the MLB API.
        """
        player_id = self._lookup_player_id(player_name)
        if not player_id:
            return None

        # The MLB API supports lastXdays as a game-log filter
        group = "hitting" if player_type == PlayerType.HITTER else "pitching"

        try:
            # Use game log and aggregate manually for the date range
            start = days_ago_str(days)
            end = today_str()
            game_log = statsapi.player_stat_data(
                player_id, group=group, type="byDateRange",
                params={"startDate": start, "endDate": end},
            )
        except Exception as exc:
            log.debug(f"Recent stats lookup failed for {player_name}: {exc}")
            return None

        stats_list = game_log.get("stats", [])
        if not stats_list:
            return None

        s = stats_list[0].get("stats", {})
        if player_type == PlayerType.HITTER:
            return self._parse_hitting(s)
        else:
            return self._parse_pitching(s)

    # ── Helpers ─────────────────────────────────────────────────────────

    def _lookup_player_id(self, name: str) -> Optional[int]:
        """Resolve a player name to their MLB person ID."""
        try:
            results = statsapi.lookup_player(name)
            if results:
                return results[0]["id"]
        except Exception:
            pass
        return None

    def _get_pitcher_era(self, pitcher_id: int) -> Optional[float]:
        """Get a pitcher's season ERA by their ID."""
        try:
            data = statsapi.player_stat_data(
                pitcher_id, group="pitching", type="season"
            )
            stats_list = data.get("stats", [])
            if stats_list:
                era_str = stats_list[0].get("stats", {}).get("era", "0.00")
                return float(era_str)
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_hitting(s: dict) -> HittingStats:
        """Parse a raw stats dict into a HittingStats dataclass."""
        def _int(key: str) -> int:
            try:
                return int(s.get(key, 0))
            except (ValueError, TypeError):
                return 0

        def _float(key: str) -> float:
            try:
                return float(s.get(key, 0.0))
            except (ValueError, TypeError):
                return 0.0

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
            batting_avg=_float("avg"),
            on_base_pct=_float("obp"),
            slugging_pct=_float("slg"),
            ops=_float("ops"),
        )

    @staticmethod
    def _parse_pitching(s: dict) -> PitchingStats:
        """Parse a raw stats dict into a PitchingStats dataclass."""
        def _int(key: str) -> int:
            try:
                return int(s.get(key, 0))
            except (ValueError, TypeError):
                return 0

        def _float(key: str) -> float:
            try:
                return float(s.get(key, 0.0))
            except (ValueError, TypeError):
                return 0.0

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
        """
        Return a mapping of full team names → standard abbreviations.
        Useful for matching Yahoo player teams to MLB schedule teams.
        """
        try:
            teams = statsapi.get("teams", {"sportIds": 1})
            mapping = {}
            for t in teams.get("teams", []):
                mapping[t["name"]] = t.get("abbreviation", t["name"])
                # Also map short name for fuzzy matching
                mapping[t.get("teamName", "")] = t.get("abbreviation", "")
            return mapping
        except Exception as exc:
            log.warning(f"Failed to build team abbreviation map: {exc}")
            return {}
