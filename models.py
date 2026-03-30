"""
Data models for the Fantasy Baseball ROTO Daily Manager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# ── Enums ────────────────────────────────────────────────────────────────

class PlayerType(Enum):
    HITTER = "hitter"
    PITCHER = "pitcher"


class RosterStatus(Enum):
    ACTIVE = "active"
    BENCH = "bench"
    INJURED = "injured"
    NOT_AVAILABLE = "na"


class RecommendationType(Enum):
    WAIVER_ADD = "waiver_add"
    WAIVER_DROP = "waiver_drop"
    TRADE_TARGET = "trade_target"
    TRADE_AWAY = "trade_away"
    START = "start"
    SIT = "sit"


class UrgencyLevel(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ── Player & Stats ──────────────────────────────────────────────────────

@dataclass
class HittingStats:
    """Batting statistics for a defined period."""
    games: int = 0
    at_bats: int = 0
    runs: int = 0
    hits: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    rbi: int = 0
    stolen_bases: int = 0
    caught_stealing: int = 0
    walks: int = 0
    strikeouts: int = 0
    hit_by_pitch: int = 0
    sacrifice_flies: int = 0
    batting_avg: float = 0.0
    on_base_pct: float = 0.0
    slugging_pct: float = 0.0
    ops: float = 0.0

    @property
    def plate_appearances(self) -> int:
        return self.at_bats + self.walks + self.hit_by_pitch + self.sacrifice_flies


@dataclass
class PitchingStats:
    """Pitching statistics for a defined period."""
    games: int = 0
    games_started: int = 0
    wins: int = 0
    losses: int = 0
    saves: int = 0
    holds: int = 0
    innings_pitched: float = 0.0
    hits_allowed: int = 0
    runs_allowed: int = 0
    earned_runs: int = 0
    walks_allowed: int = 0
    strikeouts: int = 0
    home_runs_allowed: int = 0
    era: float = 0.0
    whip: float = 0.0
    quality_starts: int = 0
    complete_games: int = 0

    @property
    def k_per_9(self) -> float:
        if self.innings_pitched == 0:
            return 0.0
        return (self.strikeouts / self.innings_pitched) * 9


@dataclass
class Player:
    """A fantasy-relevant MLB player."""
    player_id: str
    name: str
    team: str
    positions: list[str] = field(default_factory=list)
    player_type: PlayerType = PlayerType.HITTER
    roster_status: RosterStatus = RosterStatus.NOT_AVAILABLE
    injury_status: Optional[str] = None
    ownership_pct: float = 0.0

    season_hitting: Optional[HittingStats] = None
    recent_hitting: Optional[HittingStats] = None
    season_pitching: Optional[PitchingStats] = None
    recent_pitching: Optional[PitchingStats] = None

    opponent_today: Optional[str] = None
    is_playing_today: bool = False
    probable_starter_against: Optional[str] = None

    @property
    def primary_position(self) -> str:
        return self.positions[0] if self.positions else "Util"

    @property
    def is_injured(self) -> bool:
        return self.injury_status is not None


# ── Matchup ─────────────────────────────────────────────────────────────

@dataclass
class GameMatchup:
    """A single MLB game scheduled for today."""
    game_id: int
    home_team: str
    away_team: str
    game_time: str = ""
    venue: str = ""
    home_probable_pitcher: Optional[str] = None
    away_probable_pitcher: Optional[str] = None
    home_pitcher_era: Optional[float] = None
    away_pitcher_era: Optional[float] = None

    @property
    def matchup_label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"


# ── Recommendations ─────────────────────────────────────────────────────

@dataclass
class Recommendation:
    """A single actionable suggestion."""
    rec_type: RecommendationType
    player: Player
    urgency: UrgencyLevel = UrgencyLevel.MEDIUM
    headline: str = ""
    explanation: str = ""
    category_impact: dict[str, str] = field(default_factory=dict)
    paired_player: Optional[Player] = None
    score: float = 0.0


@dataclass
class StartSitDecision:
    """Start or sit recommendation for a rostered player today."""
    player: Player
    decision: str               # "START" or "SIT"
    confidence: str             # "High", "Medium", "Low"
    reason: str
    opponent: str = ""
    matchup_score: float = 0.0


# ── Daily Report ────────────────────────────────────────────────────────

@dataclass
class DailyReport:
    """The complete daily output."""
    report_date: date
    league_name: str = ""
    games_today: list[GameMatchup] = field(default_factory=list)
    games_tomorrow: list[GameMatchup] = field(default_factory=list)
    my_roster: list[Player] = field(default_factory=list)
    open_roster_spots: int = 0
    waiver_adds: list[Recommendation] = field(default_factory=list)
    trade_targets: list[Recommendation] = field(default_factory=list)
    start_sit: list[StartSitDecision] = field(default_factory=list)
    executive_summary: str = ""
    category_standings: dict[str, int] = field(default_factory=dict)
