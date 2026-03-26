"""
Yahoo Fantasy Sports API client.

Handles OAuth 2.0 authentication and provides methods to pull:
  - Your current roster (active, bench, IL)
  - Free agents / waiver wire
  - League standings and category rankings
  - Player ownership and availability info

Requires: yahoo_oauth, yahoo_fantasy_api
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa

from models import (
    HittingStats,
    PitchingStats,
    Player,
    PlayerType,
    RosterStatus,
)
from utils import log


class YahooClient:
    """Interface to the Yahoo Fantasy Sports API."""

    def __init__(self, config: dict):
        self._cfg = config["yahoo"]
        self._oauth: Optional[OAuth2] = None
        self._game: Optional[yfa.Game] = None
        self._league: Optional[yfa.League] = None

    # ── Authentication ──────────────────────────────────────────────────

    def authenticate(self) -> None:
        """
        Establish an OAuth2 session with Yahoo.

        On first run this opens a browser for user authorization. Subsequent
        runs reuse the cached token from the token_file.
        """
        token_file = self._cfg.get("token_file", ".yahoo_token.json")
        creds_file = self._ensure_creds_file()

        log.info("Authenticating with Yahoo Fantasy API …")
        self._oauth = OAuth2(None, None, from_file=creds_file)

        if not self._oauth.token_is_valid():
            self._oauth.refresh_access_token()

        self._game = yfa.Game(self._oauth, "mlb")
        league_id = self._cfg["league_id"]

        # If league_id doesn't include game key, resolve it
        if "." not in league_id:
            game_keys = self._game.game_key()  # current season key
            league_id = f"{game_keys}.l.{league_id}"

        self._league = self._game.to_league(league_id)
        log.info(f"Connected to league: {self._league.settings()['name']}")

    def _ensure_creds_file(self) -> str:
        """
        yahoo_oauth expects a JSON file with consumer_key and consumer_secret.
        We generate it from config.yaml values.
        """
        creds_path = Path(".yahoo_creds.json")
        creds = {
            "consumer_key": self._cfg["client_id"],
            "consumer_secret": self._cfg["client_secret"],
        }
        creds_path.write_text(json.dumps(creds))
        return str(creds_path)

    # ── League Info ─────────────────────────────────────────────────────

    def get_league_name(self) -> str:
        return self._league.settings().get("name", "My League")

    def get_standings(self) -> list[dict]:
        """
        Return current league standings.

        Each entry: {
            "team_name": str,
            "rank": int,
            "points": float,
            "categories": {"HR": rank_int, "ERA": rank_int, ...}
        }
        """
        try:
            raw = self._league.standings()
            standings = []
            for i, team in enumerate(raw, 1):
                team_data = team.get("team", team)
                entry = {
                    "team_name": self._extract_team_name(team_data),
                    "rank": i,
                }
                standings.append(entry)
            return standings
        except Exception as exc:
            log.error(f"Failed to fetch standings: {exc}")
            return []

    def get_my_category_rankings(self) -> dict[str, int]:
        """
        Return a dict mapping each ROTO category to your current rank.
        e.g. {"HR": 3, "ERA": 7, "SB": 1, ...}
        """
        try:
            standings = self._league.standings()
            # Find our team in the standings and extract stat rankings
            settings = self._league.settings()
            my_team_key = self._league.team_key()

            # This is simplified — the actual implementation depends on
            # how the yahoo_fantasy_api structures standings data.
            # You may need to parse XML or JSON responses directly.
            return {}
        except Exception as exc:
            log.warning(f"Could not parse category rankings: {exc}")
            return {}

    # ── Roster ──────────────────────────────────────────────────────────

    def get_my_roster(self) -> list[Player]:
        """
        Return all players currently on your fantasy roster.
        """
        log.info("Fetching your roster …")
        try:
            team = self._league.to_team(self._league.team_key())
            raw_roster = team.roster()
        except Exception as exc:
            log.error(f"Failed to fetch roster: {exc}")
            return []

        players = []
        for entry in raw_roster:
            player = self._parse_roster_player(entry)
            if player:
                players.append(player)

        log.info(f"Roster loaded: {len(players)} players")
        return players

    def _parse_roster_player(self, raw: dict) -> Optional[Player]:
        """Convert a raw Yahoo roster entry into a Player."""
        try:
            name = raw.get("name", "Unknown")
            player_id = str(raw.get("player_id", ""))
            team = raw.get("editorial_team_abbr", raw.get("team", ""))

            # Determine positions
            eligible = raw.get("eligible_positions", [])
            if isinstance(eligible, str):
                eligible = [eligible]
            positions = [p for p in eligible if p not in ("Util", "BN", "IL", "IL+", "NA")]

            # Determine hitter vs pitcher
            pitcher_pos = {"SP", "RP", "P"}
            is_pitcher = bool(set(positions) & pitcher_pos)
            player_type = PlayerType.PITCHER if is_pitcher else PlayerType.HITTER

            # Roster status
            selected_pos = raw.get("selected_position", "")
            if selected_pos in ("BN",):
                status = RosterStatus.BENCH
            elif selected_pos in ("IL", "IL+", "NA"):
                status = RosterStatus.INJURED
            else:
                status = RosterStatus.ACTIVE

            # Injury note
            injury = raw.get("status", None)  # e.g. "IL10", "DTD"

            return Player(
                player_id=player_id,
                name=name,
                team=team.upper(),
                positions=positions if positions else [selected_pos],
                player_type=player_type,
                roster_status=status,
                injury_status=injury if injury else None,
                ownership_pct=float(raw.get("percent_owned", 0)),
            )
        except Exception as exc:
            log.debug(f"Failed to parse roster player: {exc}")
            return None

    # ── Free Agents ─────────────────────────────────────────────────────

    def get_free_agents(
        self, position: str = "ALL", count: int = 50
    ) -> list[Player]:
        """
        Return top available free agents from the waiver wire.

        Args:
            position: Filter by position ("ALL", "C", "SP", etc.)
            count: Max number of players to return.
        """
        log.info(f"Fetching free agents (position={position}, count={count}) …")
        try:
            if position == "ALL":
                # Pull batters and pitchers separately for better coverage
                raw_b = self._league.free_agents("B")[:count]
                raw_p = self._league.free_agents("P")[:count]
                raw = raw_b + raw_p
            else:
                raw = self._league.free_agents(position)[:count]
        except Exception as exc:
            log.error(f"Failed to fetch free agents: {exc}")
            return []

        players = []
        for entry in raw:
            player = self._parse_free_agent(entry)
            if player:
                players.append(player)

        log.info(f"Found {len(players)} free agents")
        return players

    def _parse_free_agent(self, raw: dict) -> Optional[Player]:
        """Convert a raw Yahoo free-agent entry into a Player."""
        try:
            name = raw.get("name", "Unknown")
            player_id = str(raw.get("player_id", ""))
            team = raw.get("editorial_team_abbr", raw.get("team", ""))

            eligible = raw.get("eligible_positions", [])
            if isinstance(eligible, str):
                eligible = [eligible]
            positions = [p for p in eligible if p not in ("Util", "BN", "IL", "IL+", "NA")]

            pitcher_pos = {"SP", "RP", "P"}
            is_pitcher = bool(set(positions) & pitcher_pos)
            player_type = PlayerType.PITCHER if is_pitcher else PlayerType.HITTER

            ownership = float(raw.get("percent_owned", 0))

            return Player(
                player_id=player_id,
                name=name,
                team=team.upper(),
                positions=positions if positions else ["Util"],
                player_type=player_type,
                roster_status=RosterStatus.NOT_AVAILABLE,
                ownership_pct=ownership,
            )
        except Exception as exc:
            log.debug(f"Failed to parse free agent: {exc}")
            return None

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_team_name(team_data: Any) -> str:
        """Pull team name from various Yahoo response formats."""
        if isinstance(team_data, dict):
            return team_data.get("name", "Unknown")
        if isinstance(team_data, list):
            for item in team_data:
                if isinstance(item, dict) and "name" in item:
                    return item["name"]
        return "Unknown"
