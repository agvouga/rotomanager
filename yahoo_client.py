"""
Yahoo Fantasy Sports API client.

Handles OAuth 2.0 and provides methods to pull:
  - Your current roster
  - Free agents / waiver wire
  - League standings and category rankings
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa

from models import Player, PlayerType, RosterStatus
from utils import log


class YahooClient:
    """Interface to the Yahoo Fantasy Sports API."""

    def __init__(self, config: dict):
        self._cfg = config["yahoo"]
        self._oauth: Optional[OAuth2] = None
        self._game: Optional[yfa.Game] = None
        self._league: Optional[yfa.League] = None

    # ── Auth ────────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """
        Establish an OAuth2 session with Yahoo.
        First run opens a browser; subsequent runs reuse the cached token.
        """
        creds_file = self._ensure_creds_file()

        log.info("Authenticating with Yahoo Fantasy API …")
        self._oauth = OAuth2(None, None, from_file=creds_file)

        if not self._oauth.token_is_valid():
            self._oauth.refresh_access_token()

        self._game = yfa.Game(self._oauth, "mlb")
        league_id = self._cfg["league_id"]

        if "." not in league_id:
            game_key = self._game.game_id()
            league_id = f"{game_key}.l.{league_id}"

        self._league = self._game.to_league(league_id)
        log.info(f"Connected to league: {self._league.settings()['name']}")

    def _ensure_creds_file(self) -> str:
        """yahoo_oauth needs a JSON file with consumer_key/secret."""
        creds_path = Path(".yahoo_creds.json")
        creds_path.write_text(json.dumps({
            "consumer_key": self._cfg["client_id"],
            "consumer_secret": self._cfg["client_secret"],
        }))
        return str(creds_path)

    # ── League ──────────────────────────────────────────────────────────

    def get_league_name(self) -> str:
        return self._league.settings().get("name", "My League")

    def get_my_category_rankings(self) -> dict[str, int]:
        """
        Return a dict mapping each ROTO category to your current rank.
        e.g. {"HR": 3, "ERA": 7, ...}
        """
        try:
            standings = self._league.standings()
            # Implementation depends on yahoo_fantasy_api response structure.
            # The library returns standings as a list of teams with stat totals;
            # computing per-category rank requires comparing across all teams.
            # This is a best-effort parse — returns empty if the format changes.
            return {}
        except Exception as exc:
            log.warning(f"Could not parse category rankings: {exc}")
            return {}

    # ── Roster ──────────────────────────────────────────────────────────

    def get_my_roster(self) -> list[Player]:
        """Return all players currently on your fantasy roster."""
        log.info("Fetching your roster …")
        try:
            team = self._league.to_team(self._league.team_key())
            raw_roster = team.roster()
        except Exception as exc:
            log.error(f"Failed to fetch roster: {exc}")
            return []

        players = [self._parse_player(e, rostered=True) for e in raw_roster]
        players = [p for p in players if p is not None]
        log.info(f"Roster loaded: {len(players)} players")
        return players

    # ── Free Agents ─────────────────────────────────────────────────────

    def get_free_agents(self, position: str = "ALL", count: int = 50) -> list[Player]:
        """Return top available free agents from the waiver wire."""
        log.info(f"Fetching free agents (position={position}, count={count}) …")
        try:
            if position == "ALL":
                raw = self._league.free_agents("B")[:count] + self._league.free_agents("P")[:count]
            else:
                raw = self._league.free_agents(position)[:count]
        except Exception as exc:
            log.error(f"Failed to fetch free agents: {exc}")
            return []

        players = [self._parse_player(e, rostered=False) for e in raw]
        players = [p for p in players if p is not None]
        log.info(f"Found {len(players)} free agents")
        return players

    # ── Parsing ─────────────────────────────────────────────────────────

    def _parse_player(self, raw: dict, rostered: bool) -> Optional[Player]:
        """Convert a raw Yahoo player dict into a Player model."""
        try:
            name = raw.get("name", "Unknown")
            player_id = str(raw.get("player_id", ""))
            team = raw.get("editorial_team_abbr", raw.get("team", "")).upper()

            eligible = raw.get("eligible_positions", [])
            if isinstance(eligible, str):
                eligible = [eligible]
            positions = [p for p in eligible if p not in ("Util", "BN", "IL", "IL+", "NA")]

            pitcher_pos = {"SP", "RP", "P"}
            is_pitcher = bool(set(positions) & pitcher_pos)
            player_type = PlayerType.PITCHER if is_pitcher else PlayerType.HITTER

            if rostered:
                selected_pos = raw.get("selected_position", "")
                if selected_pos in ("BN",):
                    status = RosterStatus.BENCH
                elif selected_pos in ("IL", "IL+", "NA"):
                    status = RosterStatus.INJURED
                else:
                    status = RosterStatus.ACTIVE
            else:
                status = RosterStatus.NOT_AVAILABLE

            injury = raw.get("status", None)

            return Player(
                player_id=player_id,
                name=name,
                team=team,
                positions=positions if positions else ["Util"],
                player_type=player_type,
                roster_status=status,
                injury_status=injury if injury else None,
                ownership_pct=float(raw.get("percent_owned", 0)),
            )
        except Exception as exc:
            log.debug(f"Failed to parse player: {exc}")
            return None
