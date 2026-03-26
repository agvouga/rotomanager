"""
ROTO Analysis Engine (6×6).

Produces ranked recommendations for waivers, trades, and start/sit
decisions based on your league's categories:

  Hitting:  R, HR, RBI, SB, OBP, OPS
  Pitching: W, SV, K, HLD, ERA, WHIP

Key strategy concepts built in:
  - Identify your weakest categories and target players who improve them.
  - Two-category contributors (e.g. a hitter with SB + OBP) rank higher.
  - Holds league → relievers with HLD upside are real assets, not afterthoughts.
  - OBP + OPS league → patient hitters (high walk rate) are more valuable
    than free-swingers with comparable AVG.
  - Recency weighting surfaces hot streaks for actionable daily moves.
  - Beginner-friendly: prefer high-floor everyday players over volatile upside.
"""

from __future__ import annotations

from dataclasses import dataclass

from models import (
    DailyReport,
    GameMatchup,
    Player,
    PlayerType,
    Recommendation,
    RecommendationType,
    RosterStatus,
    StartSitDecision,
    UrgencyLevel,
)
from utils import (
    log,
    get_stat_value,
    HITTING_STAT_MAP,
    PITCHING_STAT_MAP,
)


# ── Category Need Scoring ───────────────────────────────────────────────

@dataclass
class CategoryNeed:
    stat: str
    name: str
    current_rank: int
    total_teams: int
    higher_is_better: bool
    need_score: float = 0.0      # 0–1, higher = more urgent


def compute_category_needs(
    category_rankings: dict[str, int],
    roto_categories: list[dict],
    total_teams: int = 12,
) -> list[CategoryNeed]:
    needs = []
    for cat in roto_categories:
        stat = cat["stat"]
        rank = category_rankings.get(stat, total_teams // 2)
        need_score = (rank - 1) / max(total_teams - 1, 1)
        needs.append(CategoryNeed(
            stat=stat,
            name=cat["name"],
            current_rank=rank,
            total_teams=total_teams,
            higher_is_better=cat.get("higher_is_better", True),
            need_score=need_score,
        ))
    needs.sort(key=lambda n: n.need_score, reverse=True)
    return needs


# ── Player Composite Scoring ────────────────────────────────────────────

def score_player(
    player: Player,
    needs: list[CategoryNeed],
    config: dict,
) -> float:
    """
    Composite score: how much does this player address your weakest categories?

    Special handling for your 6×6 format:
      - OBP/OPS: patient hitters (high BB rate) get a bonus.
      - HLD: middle relievers with holds upside are scored properly.
      - SV+HLD: a reliever contributing to both is very valuable.
    """
    recency_weight = config.get("analysis", {}).get("recency_weight", 0.6)
    season_weight = 1.0 - recency_weight

    score = 0.0
    categories_helped = 0

    for need in needs:
        stat = need.stat
        season_val = get_stat_value(player, stat, "season")
        recent_val = get_stat_value(player, stat, "recent")
        blended = (season_val * season_weight) + (recent_val * recency_weight)

        if blended == 0:
            continue

        # Normalize to a ~0–1 contribution scale depending on stat type
        contribution = _normalize_stat(stat, blended, player)

        # Weight by how badly you need this category
        score += contribution * need.need_score

        if contribution > 0.3:
            categories_helped += 1

    # Multi-category bonus
    if categories_helped >= 4:
        score *= 1.35
    elif categories_helped >= 3:
        score *= 1.20
    elif categories_helped >= 2:
        score *= 1.10

    # ── OBP/OPS league bonus: reward plate discipline ───────────────
    if player.player_type == PlayerType.HITTER and player.season_hitting:
        h = player.season_hitting
        if h.plate_appearances > 0:
            walk_rate = h.walks / h.plate_appearances
            if walk_rate >= 0.12:       # elite walk rate
                score *= 1.10
            elif walk_rate >= 0.09:     # above-average
                score *= 1.05

    # ── Holds league bonus: value setup relievers ───────────────────
    if player.player_type == PlayerType.PITCHER and player.season_pitching:
        p = player.season_pitching
        if p.holds >= 5 and p.saves >= 3:
            # Dual SV+HLD contributor — rare and very valuable in your format
            score *= 1.15
        elif p.holds >= 5:
            score *= 1.05

    # Penalties & bonuses
    if player.is_injured:
        score *= 0.3
    if player.is_playing_today:
        score *= 1.05

    # Ownership signal
    score += min(player.ownership_pct / 100.0, 1.0) * 0.1

    return round(score, 4)


def _normalize_stat(stat: str, value: float, player: Player) -> float:
    """Scale a stat value to roughly 0–1 for cross-category comparison."""

    # Rate stats
    if stat == "OBP":
        return value / 0.400           # .400 OBP ≈ elite ceiling
    if stat == "OPS":
        return value / 1.000           # 1.000 OPS ≈ elite ceiling
    if stat == "AVG":
        return value / 0.350
    if stat == "SLG":
        return value / 0.600
    if stat == "ERA":
        return max(0, (5.0 - value) / 5.0)   # lower is better
    if stat == "WHIP":
        return max(0, (1.8 - value) / 1.8)

    # Counting stats: convert to per-game rate, then normalize
    games = 1
    if player.player_type == PlayerType.HITTER and player.season_hitting:
        games = max(player.season_hitting.games, 1)
    elif player.player_type == PlayerType.PITCHER and player.season_pitching:
        games = max(player.season_pitching.games, 1)

    per_game = value / games

    ceilings = {
        "R": 0.7, "HR": 0.25, "RBI": 0.8, "SB": 0.20,
        "W": 0.20, "K": 7.0, "SV": 0.35, "HLD": 0.35,
        "H": 1.2, "BB": 0.5, "QS": 0.20,
    }
    ceiling = ceilings.get(stat, 1.0)
    return min(per_game / ceiling, 1.5)


# ── Waiver Wire ─────────────────────────────────────────────────────────

def find_waiver_adds(
    free_agents: list[Player],
    my_roster: list[Player],
    needs: list[CategoryNeed],
    config: dict,
) -> list[Recommendation]:
    analysis_cfg = config.get("analysis", {})
    min_ownership = analysis_cfg.get("min_ownership_pct", 5)
    max_suggestions = analysis_cfg.get("max_waiver_suggestions", 10)

    viable = [fa for fa in free_agents if fa.ownership_pct >= min_ownership and not fa.is_injured]

    scored = [(fa, score_player(fa, needs, config)) for fa in viable]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Drop candidates: weakest bench players
    bench = [p for p in my_roster if p.roster_status == RosterStatus.BENCH]
    bench_scored = sorted(
        [(p, score_player(p, needs, config)) for p in bench],
        key=lambda x: x[1],
    )

    recommendations = []
    for fa, fa_score in scored[:max_suggestions]:
        # Match drop candidate by player type
        drops = [p for p, s in bench_scored if p.player_type == fa.player_type]
        drop_player = drops[0] if drops else None

        impact = _describe_category_impact(fa, needs)
        explanation = _build_waiver_explanation(fa, drop_player, needs, config)

        recommendations.append(Recommendation(
            rec_type=RecommendationType.WAIVER_ADD,
            player=fa,
            paired_player=drop_player,
            urgency=_urgency_from_score(fa_score),
            headline=f"Add {fa.name} ({fa.team}, {fa.primary_position})",
            explanation=explanation,
            category_impact=impact,
            score=fa_score,
        ))

    return recommendations


# ── Trade Ideas ─────────────────────────────────────────────────────────

def find_trade_targets(
    my_roster: list[Player],
    needs: list[CategoryNeed],
    config: dict,
) -> list[Recommendation]:
    max_suggestions = config.get("analysis", {}).get("max_trade_suggestions", 5)

    strong_cats = [n for n in needs if n.need_score < 0.3]
    weak_cats = [n for n in needs if n.need_score > 0.6]

    if not strong_cats or not weak_cats:
        return []

    candidates = []
    for player in my_roster:
        if player.roster_status == RosterStatus.INJURED:
            continue

        strong_val = sum(get_stat_value(player, c.stat, "season") for c in strong_cats)
        weak_val = sum(get_stat_value(player, c.stat, "season") for c in weak_cats)

        if strong_val > 0 and weak_val == 0:
            candidates.append((player, strong_val))

    candidates.sort(key=lambda x: x[1], reverse=True)

    recs = []
    for player, surplus in candidates[:max_suggestions]:
        weak_names = ", ".join(c.name for c in weak_cats[:3])
        strong_names = ", ".join(c.name for c in strong_cats[:2])

        explanation = (
            f"{player.name} is producing most in {strong_names}, where you're "
            f"already near the top. Trade them to a team that needs those stats "
            f"and target a return that helps your {weak_names}."
        )

        recs.append(Recommendation(
            rec_type=RecommendationType.TRADE_AWAY,
            player=player,
            urgency=UrgencyLevel.LOW,
            headline=f"Sell high: {player.name} ({player.primary_position})",
            explanation=explanation,
            score=surplus,
        ))

    return recs


# ── Start / Sit ─────────────────────────────────────────────────────────

def make_start_sit_decisions(
    roster: list[Player],
    games: list[GameMatchup],
    needs: list[CategoryNeed],
    config: dict,
) -> list[StartSitDecision]:
    team_game: dict[str, GameMatchup] = {}
    for game in games:
        team_game[game.home_team] = game
        team_game[game.away_team] = game

    decisions = []
    for player in roster:
        if player.roster_status == RosterStatus.INJURED:
            continue

        game = _find_game(player, team_game)

        if game is None:
            decisions.append(StartSitDecision(
                player=player,
                decision="SIT",
                confidence="High",
                reason="Team has no game scheduled today.",
                matchup_score=0,
            ))
            continue

        player.is_playing_today = True
        opponent = _get_opponent(player, game)
        player.opponent_today = opponent

        ms = _evaluate_matchup(player, game, needs)

        if ms >= 65:
            decision, confidence = "START", ("High" if ms >= 80 else "Medium")
        elif ms >= 45:
            decision, confidence = "START", "Low"
        else:
            decision, confidence = "SIT", ("High" if ms < 25 else "Medium")

        reason = _build_start_sit_reason(player, game, ms)

        decisions.append(StartSitDecision(
            player=player,
            decision=decision,
            confidence=confidence,
            reason=reason,
            opponent=opponent,
            matchup_score=ms,
        ))

    decisions.sort(key=lambda d: (0 if d.decision == "START" else 1, -d.matchup_score))
    return decisions


# ── Executive Summary ───────────────────────────────────────────────────

def generate_executive_summary(report: DailyReport, config: dict) -> str:
    lines = []

    n_games = len(report.games_today)
    active = sum(1 for d in report.start_sit if d.decision == "START")
    lines.append(
        f"**{n_games} MLB games today.** You have **{active} players** in action."
    )

    if report.waiver_adds:
        top = report.waiver_adds[0]
        urgency_tag = " **Act fast — high priority.**" if top.urgency == UrgencyLevel.HIGH else ""
        lines.append(f"\n**Top move:** {top.headline}.{urgency_tag}")

    if report.category_standings:
        worst = sorted(report.category_standings.items(), key=lambda x: x[1], reverse=True)[:3]
        cats = ", ".join(f"{cat} (#{rank})" for cat, rank in worst)
        lines.append(f"\n**Category watch:** Your weakest areas are {cats}. Today's suggestions target these.")

    sits = [d for d in report.start_sit if d.decision == "SIT" and d.confidence == "High"]
    if sits:
        names = ", ".join(d.player.name for d in sits[:3])
        lines.append(f"\n**Bench today:** {names}.")

    return "\n".join(lines)


# ── Private Helpers ─────────────────────────────────────────────────────

def _find_game(player: Player, team_game: dict[str, GameMatchup]) -> GameMatchup | None:
    team = player.team.upper()
    if team in team_game:
        return team_game[team]
    for key, game in team_game.items():
        if team in key.upper() or key.upper() in team:
            return game
    return None


def _get_opponent(player: Player, game: GameMatchup) -> str:
    if player.team.upper() in game.home_team.upper():
        return game.away_team
    return game.home_team


def _evaluate_matchup(player: Player, game: GameMatchup, needs: list[CategoryNeed]) -> float:
    score = 50.0

    if player.player_type == PlayerType.HITTER:
        is_home = player.team.upper() in game.home_team.upper()
        opp_era = game.away_pitcher_era if is_home else game.home_pitcher_era
        if opp_era is not None:
            if opp_era >= 5.0: score += 25
            elif opp_era >= 4.5: score += 15
            elif opp_era >= 4.0: score += 5
            elif opp_era <= 2.5: score -= 20
            elif opp_era <= 3.0: score -= 10

        if player.recent_hitting:
            if player.recent_hitting.ops >= 0.900: score += 12
            elif player.recent_hitting.ops >= 0.800: score += 5
            elif player.recent_hitting.ops < 0.600: score -= 10

    elif player.player_type == PlayerType.PITCHER:
        if player.season_pitching:
            if player.season_pitching.era <= 3.00: score += 20
            elif player.season_pitching.era <= 3.50: score += 10
            elif player.season_pitching.era >= 5.00: score -= 20
        if player.recent_pitching and player.recent_pitching.era <= 2.50:
            score += 10

    return max(0, min(100, score))


def _build_start_sit_reason(player: Player, game: GameMatchup, ms: float) -> str:
    parts = []
    opponent = _get_opponent(player, game)
    parts.append(f"vs {opponent}.")

    if player.player_type == PlayerType.HITTER:
        is_home = player.team.upper() in game.home_team.upper()
        opp_p = game.away_probable_pitcher if is_home else game.home_probable_pitcher
        opp_era = game.away_pitcher_era if is_home else game.home_pitcher_era
        if opp_p:
            era_s = f" ({opp_era:.2f} ERA)" if opp_era else ""
            if ms >= 65:
                parts.append(f"Facing {opp_p}{era_s} — favorable matchup.")
            elif ms < 40:
                parts.append(f"Facing {opp_p}{era_s} — tough matchup.")

        if player.recent_hitting:
            ops = player.recent_hitting.ops
            if ops >= 0.900:
                parts.append(f"Hot streak: {ops:.3f} OPS last 2 weeks.")
            elif ops < 0.550:
                parts.append(f"Cold: {ops:.3f} OPS recently.")

    elif player.player_type == PlayerType.PITCHER:
        if player.season_pitching:
            if player.season_pitching.era <= 3.00:
                parts.append("Elite ERA — always start.")
            elif player.season_pitching.era >= 5.00:
                parts.append("High ERA this season — risky start.")

    return " ".join(parts)


def _describe_category_impact(player: Player, needs: list[CategoryNeed]) -> dict[str, str]:
    impact = {}
    for need in needs:
        val = get_stat_value(player, need.stat, "season")
        if val > 0:
            if need.need_score > 0.6:
                impact[need.stat] = "Helps a weak category!"
            elif need.need_score > 0.3:
                impact[need.stat] = "Contributes"
            else:
                impact[need.stat] = "Nice to have"
    return impact


def _build_waiver_explanation(
    add: Player, drop: Player | None, needs: list[CategoryNeed], config: dict,
) -> str:
    audience = config.get("report", {}).get("audience", "beginner")
    parts = []

    helping = [n.name for n in needs if get_stat_value(add, n.stat, "season") > 0 and n.need_score > 0.4]
    if helping:
        parts.append(f"{add.name} boosts your {', '.join(helping[:3])} — categories where you're behind.")

    if add.ownership_pct >= 50:
        parts.append(f"Owned in {add.ownership_pct:.0f}% of leagues (highly valued).")
    elif add.ownership_pct >= 20:
        parts.append(f"Owned in {add.ownership_pct:.0f}% of leagues.")

    # OBP/OPS-specific note
    if add.player_type == PlayerType.HITTER and add.season_hitting:
        h = add.season_hitting
        if h.on_base_pct >= 0.350:
            parts.append(f"Strong {h.on_base_pct:.3f} OBP — valuable in your OBP league.")
        if h.ops >= 0.850:
            parts.append(f"Excellent {h.ops:.3f} OPS.")

    # Holds-specific note
    if add.player_type == PlayerType.PITCHER and add.season_pitching:
        p = add.season_pitching
        if p.holds >= 3:
            parts.append(f"Already has {p.holds} holds — a real asset in your holds league.")
        if p.saves >= 3 and p.holds >= 3:
            parts.append("Rare SV+HLD dual contributor.")

    if drop:
        parts.append(f"Consider dropping {drop.name} to make room.")

    if audience == "beginner":
        parts.append(
            "*(In ROTO, you earn points by ranking higher in each stat category. "
            "Picking up players who help your weakest categories is the fastest way to climb.)*"
        )

    return " ".join(parts)


def _urgency_from_score(score: float) -> UrgencyLevel:
    if score >= 0.8:
        return UrgencyLevel.HIGH
    if score >= 0.4:
        return UrgencyLevel.MEDIUM
    return UrgencyLevel.LOW
