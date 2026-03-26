"""
ROTO Analysis Engine.

This is the brain of the daily manager. It takes raw roster data, free-agent
lists, and today's schedule, then produces ranked recommendations for:
  - Waiver wire pickups (with drop candidates)
  - Trade targets
  - Start / sit decisions

The scoring is built around expert-consensus ROTO strategy:
  - Identify your weakest categories and target players who boost them.
  - Favor "two-category" contributors who help in multiple areas.
  - Weight recent performance (hot streaks) alongside season-long reliability.
  - For a beginner: prioritize high-floor, everyday players over volatile upside.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from models import (
    DailyReport,
    GameMatchup,
    HittingStats,
    PitchingStats,
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
    safe_divide,
    fmt_avg,
    fmt_era,
    HITTING_STAT_MAP,
    PITCHING_STAT_MAP,
)


# ── Category Need Scoring ───────────────────────────────────────────────

@dataclass
class CategoryNeed:
    """Represents how badly you need improvement in one ROTO category."""
    stat: str
    name: str
    current_rank: int            # 1 = best, N = worst
    total_teams: int
    higher_is_better: bool
    need_score: float = 0.0      # 0–1, higher = more urgent need


def compute_category_needs(
    category_rankings: dict[str, int],
    roto_categories: list[dict],
    total_teams: int = 12,
) -> list[CategoryNeed]:
    """
    Score each ROTO category by how much you need to improve in it.

    A bottom-3 category is an urgent need; a top-3 category is fine.
    """
    needs = []
    for cat in roto_categories:
        stat = cat["stat"]
        rank = category_rankings.get(stat, total_teams // 2)
        # Normalize: rank 1 → need 0.0, rank N → need 1.0
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


# ── Player Scoring ──────────────────────────────────────────────────────

def score_player(
    player: Player,
    needs: list[CategoryNeed],
    config: dict,
) -> float:
    """
    Compute a composite score for a player based on how much they address
    your category needs.

    Higher score = better pickup / more valuable.

    The formula blends:
      1. Category contribution weighted by your need in that category.
      2. Season stats (reliability) blended with recent stats (momentum).
      3. Bonus for multi-category contributors.
      4. Penalty for injured or not-playing-today players.
    """
    recency_weight = config.get("analysis", {}).get("recency_weight", 0.6)
    season_weight = 1.0 - recency_weight

    score = 0.0
    categories_helped = 0

    for need in needs:
        stat = need.stat

        # Get the player's production in this category
        season_val = get_stat_value(player, stat, "season")
        recent_val = get_stat_value(player, stat, "recent")

        # Blend season and recent
        blended = (season_val * season_weight) + (recent_val * recency_weight)

        if blended == 0:
            continue

        # For rate stats (AVG, ERA, WHIP), normalize differently
        if stat in ("AVG", "OBP", "SLG", "OPS"):
            # Higher batting avg → better. Scale to ~0–1 range.
            contribution = blended / 0.350  # .350 as a rough ceiling
        elif stat in ("ERA",):
            # Lower ERA → better. Invert so lower = higher score.
            if blended > 0:
                contribution = max(0, (5.0 - blended) / 5.0)
            else:
                contribution = 0.5
        elif stat in ("WHIP",):
            if blended > 0:
                contribution = max(0, (1.8 - blended) / 1.8)
            else:
                contribution = 0.5
        else:
            # Counting stats: use a reasonable per-game rate
            games = 1
            if player.player_type == PlayerType.HITTER:
                if player.season_hitting:
                    games = max(player.season_hitting.games, 1)
            else:
                if player.season_pitching:
                    games = max(player.season_pitching.games, 1)

            per_game = blended / games
            # Normalize counting stats to a 0–1 scale
            # These ceilings are rough expert benchmarks per game
            ceilings = {
                "R": 0.7, "HR": 0.25, "RBI": 0.8, "SB": 0.2,
                "W": 0.2, "K": 7.0, "SV": 0.3, "HLD": 0.3,
                "QS": 0.2, "H": 1.2,
            }
            ceiling = ceilings.get(stat, 1.0)
            contribution = min(per_game / ceiling, 1.5)

        # Weight by your need in this category
        weighted = contribution * need.need_score
        score += weighted

        if contribution > 0.3:
            categories_helped += 1

    # Multi-category bonus: players helping 3+ categories are extra valuable
    if categories_helped >= 3:
        score *= 1.25
    elif categories_helped >= 2:
        score *= 1.10

    # Penalty for injured players
    if player.is_injured:
        score *= 0.3

    # Small bonus for players who are playing today (actionable now)
    if player.is_playing_today:
        score *= 1.05

    # Ownership bonus — higher-owned players are consensus-valued
    ownership_bonus = min(player.ownership_pct / 100.0, 1.0) * 0.1
    score += ownership_bonus

    return round(score, 4)


# ── Waiver Wire Recommendations ────────────────────────────────────────

def find_waiver_adds(
    free_agents: list[Player],
    my_roster: list[Player],
    needs: list[CategoryNeed],
    config: dict,
) -> list[Recommendation]:
    """
    Rank free agents by how much they'd improve your weakest categories,
    paired with a suggested drop candidate from your roster.
    """
    analysis_cfg = config.get("analysis", {})
    min_ownership = analysis_cfg.get("min_ownership_pct", 5)
    max_suggestions = analysis_cfg.get("max_waiver_suggestions", 10)

    # Filter free agents
    viable = [
        fa for fa in free_agents
        if fa.ownership_pct >= min_ownership and not fa.is_injured
    ]

    # Score each free agent
    scored = []
    for fa in viable:
        fa_score = score_player(fa, needs, config)
        scored.append((fa, fa_score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Find drop candidates (lowest-scoring players on your bench)
    bench = [p for p in my_roster if p.roster_status == RosterStatus.BENCH]
    bench_scored = [(p, score_player(p, needs, config)) for p in bench]
    bench_scored.sort(key=lambda x: x[1])

    recommendations = []
    for fa, fa_score in scored[:max_suggestions]:
        # Suggest dropping the weakest bench player of the same type
        drop_candidates = [
            (p, s) for p, s in bench_scored
            if p.player_type == fa.player_type
        ]
        drop_player = drop_candidates[0][0] if drop_candidates else None

        # Build explanation
        impact = _describe_category_impact(fa, needs)
        explanation = _build_waiver_explanation(fa, drop_player, needs, config)

        rec = Recommendation(
            rec_type=RecommendationType.WAIVER_ADD,
            player=fa,
            paired_player=drop_player,
            urgency=_urgency_from_score(fa_score),
            headline=f"Add {fa.name} ({fa.team}, {fa.primary_position})",
            explanation=explanation,
            category_impact=impact,
            score=fa_score,
        )
        recommendations.append(rec)

    return recommendations


# ── Trade Recommendations ───────────────────────────────────────────────

def find_trade_targets(
    my_roster: list[Player],
    needs: list[CategoryNeed],
    config: dict,
) -> list[Recommendation]:
    """
    Identify players on your roster who are strong in categories you
    already dominate, and suggest trading them for help in weak categories.

    This is a simplified approach — it identifies "sell high" candidates
    rather than specific trade partners (which would require league-wide data).
    """
    max_suggestions = config.get("analysis", {}).get("max_trade_suggestions", 5)

    # Find your strongest categories (rank 1–3)
    strong_cats = [n for n in needs if n.need_score < 0.3]
    # Find your weakest categories (bottom third)
    weak_cats = [n for n in needs if n.need_score > 0.6]

    if not strong_cats or not weak_cats:
        return []

    trade_away_candidates = []
    for player in my_roster:
        if player.roster_status == RosterStatus.INJURED:
            continue

        # How much does this player contribute to categories we're already
        # winning? If a lot, they're a trade chip.
        strong_contribution = sum(
            get_stat_value(player, cat.stat, "season")
            for cat in strong_cats
        )
        # How much do they help in categories we need?
        weak_contribution = sum(
            get_stat_value(player, cat.stat, "season")
            for cat in weak_cats
        )

        # Good trade candidate = high value in strong cats, low in weak cats
        if strong_contribution > 0 and weak_contribution == 0:
            surplus_score = strong_contribution
            trade_away_candidates.append((player, surplus_score))

    trade_away_candidates.sort(key=lambda x: x[1], reverse=True)

    recommendations = []
    for player, surplus in trade_away_candidates[:max_suggestions]:
        weak_cat_names = ", ".join(c.name for c in weak_cats[:3])
        strong_cat_names = ", ".join(c.name for c in strong_cats[:2])

        explanation = (
            f"{player.name} is helping you most in {strong_cat_names}, where "
            f"you're already near the top of the league. Consider trading them "
            f"to a team that needs those categories, and target a return that "
            f"boosts your {weak_cat_names} — areas where you're falling behind."
        )

        rec = Recommendation(
            rec_type=RecommendationType.TRADE_AWAY,
            player=player,
            urgency=UrgencyLevel.LOW,
            headline=f"Trade chip: {player.name} ({player.primary_position})",
            explanation=explanation,
            score=surplus,
        )
        recommendations.append(rec)

    return recommendations


# ── Start / Sit Decisions ───────────────────────────────────────────────

def make_start_sit_decisions(
    roster: list[Player],
    games: list[GameMatchup],
    needs: list[CategoryNeed],
    config: dict,
) -> list[StartSitDecision]:
    """
    For each player on the roster, decide whether to start or sit them today.

    Key factors:
      - Is the player's team playing today?
      - Matchup quality (opposing pitcher ERA, handedness, park factor)
      - Recent performance (hot/cold streak)
      - Category needs (a SB specialist matters more if you need SB)
    """
    # Build a quick lookup: team → today's game
    team_game_map: dict[str, GameMatchup] = {}
    for game in games:
        team_game_map[game.home_team] = game
        team_game_map[game.away_team] = game

    decisions = []
    for player in roster:
        if player.roster_status == RosterStatus.INJURED:
            continue

        game = _find_player_game(player, team_game_map)

        if game is None:
            decisions.append(StartSitDecision(
                player=player,
                decision="SIT",
                confidence="High",
                reason="Team is not playing today (off day or not scheduled).",
                matchup_score=0,
            ))
            continue

        # Mark as playing today
        player.is_playing_today = True
        opponent = _get_opponent(player, game)
        player.opponent_today = opponent

        # Evaluate matchup
        matchup_score = _evaluate_matchup(player, game, needs)

        if matchup_score >= 65:
            decision = "START"
            confidence = "High" if matchup_score >= 80 else "Medium"
        elif matchup_score >= 45:
            decision = "START"
            confidence = "Low"
        else:
            decision = "SIT"
            confidence = "High" if matchup_score < 25 else "Medium"

        reason = _build_start_sit_reason(player, game, matchup_score, needs)

        decisions.append(StartSitDecision(
            player=player,
            decision=decision,
            confidence=confidence,
            reason=reason,
            opponent=opponent,
            matchup_score=matchup_score,
        ))

    # Sort: starters first, then by matchup score descending
    decisions.sort(
        key=lambda d: (0 if d.decision == "START" else 1, -d.matchup_score)
    )
    return decisions


# ── Executive Summary Generator ─────────────────────────────────────────

def generate_executive_summary(report: DailyReport, config: dict) -> str:
    """
    Write a plain-English summary of today's key actions, designed for a
    beginner who doesn't know ROTO strategy yet.
    """
    lines = []

    # Games
    n_games = len(report.games_today)
    active_count = sum(1 for d in report.start_sit if d.decision == "START")
    lines.append(
        f"There are {n_games} MLB games today. "
        f"You have {active_count} players in action."
    )

    # Top waiver move
    if report.waiver_adds:
        top = report.waiver_adds[0]
        lines.append(
            f"\n🔥 TOP MOVE: {top.headline}. "
            f"{'This is urgent!' if top.urgency == UrgencyLevel.HIGH else 'Worth considering.'}"
        )

    # Weak categories
    if report.category_standings:
        worst_cats = sorted(
            report.category_standings.items(), key=lambda x: x[1], reverse=True
        )[:3]
        cat_names = ", ".join(f"{cat} (rank {rank})" for cat, rank in worst_cats)
        lines.append(
            f"\n📊 CATEGORY WATCH: Your weakest areas are {cat_names}. "
            f"Today's suggestions focus on improving these."
        )

    # Start/sit highlight
    sits = [d for d in report.start_sit if d.decision == "SIT" and d.confidence == "High"]
    if sits:
        sit_names = ", ".join(d.player.name for d in sits[:3])
        lines.append(f"\n🪑 BENCH TODAY: {sit_names}.")

    return "\n".join(lines)


# ── Private Helpers ─────────────────────────────────────────────────────

def _find_player_game(
    player: Player, team_game_map: dict[str, GameMatchup]
) -> GameMatchup | None:
    """Find today's game for a player's team, handling name mismatches."""
    team = player.team.upper()
    if team in team_game_map:
        return team_game_map[team]

    # Fuzzy match: try partial team name matching
    for key, game in team_game_map.items():
        if team in key.upper() or key.upper() in team:
            return game

    return None


def _get_opponent(player: Player, game: GameMatchup) -> str:
    """Determine the opponent team name."""
    if player.team.upper() in game.home_team.upper():
        return game.away_team
    return game.home_team


def _evaluate_matchup(
    player: Player, game: GameMatchup, needs: list[CategoryNeed]
) -> float:
    """
    Score a matchup from 0–100.

    For hitters: favorable = facing a high-ERA pitcher.
    For pitchers: favorable = facing a low-scoring team (approximated by
    opposing pitcher quality as a proxy for team strength).
    """
    score = 50.0  # neutral baseline

    if player.player_type == PlayerType.HITTER:
        # Check opposing pitcher ERA
        is_home = player.team.upper() in game.home_team.upper()
        opp_era = game.away_pitcher_era if is_home else game.home_pitcher_era

        if opp_era is not None:
            # High ERA = easier matchup for hitters
            if opp_era >= 5.0:
                score += 25
            elif opp_era >= 4.5:
                score += 15
            elif opp_era >= 4.0:
                score += 5
            elif opp_era <= 2.5:
                score -= 20
            elif opp_era <= 3.0:
                score -= 10

        # Recent performance boost
        if player.recent_hitting and player.recent_hitting.batting_avg >= 0.300:
            score += 10
        elif player.recent_hitting and player.recent_hitting.batting_avg < 0.200:
            score -= 10

    elif player.player_type == PlayerType.PITCHER:
        # Pitchers: just check recent ERA and whether they're a starter today
        if player.season_pitching:
            if player.season_pitching.era <= 3.00:
                score += 20
            elif player.season_pitching.era <= 3.50:
                score += 10
            elif player.season_pitching.era >= 5.00:
                score -= 20

        # Recent form
        if player.recent_pitching and player.recent_pitching.era <= 2.50:
            score += 10

    return max(0, min(100, score))


def _describe_category_impact(
    player: Player, needs: list[CategoryNeed]
) -> dict[str, str]:
    """Map each ROTO category to a short impact description."""
    impact = {}
    for need in needs:
        stat = need.stat
        val = get_stat_value(player, stat, "season")
        if val > 0:
            if need.need_score > 0.6:
                impact[stat] = f"Helps (you need this!)"
            elif need.need_score > 0.3:
                impact[stat] = "Contributes"
            else:
                impact[stat] = "Nice to have"
    return impact


def _build_waiver_explanation(
    add_player: Player,
    drop_player: Player | None,
    needs: list[CategoryNeed],
    config: dict,
) -> str:
    """Build a beginner-friendly explanation for a waiver add."""
    audience = config.get("report", {}).get("audience", "beginner")

    parts = []

    # What the player brings
    helping_cats = []
    for need in needs:
        val = get_stat_value(add_player, need.stat, "season")
        if val > 0 and need.need_score > 0.4:
            helping_cats.append(need.name)

    if helping_cats:
        cats_str = ", ".join(helping_cats[:3])
        parts.append(
            f"{add_player.name} can boost your {cats_str} — "
            f"categories where you're currently behind."
        )

    # Ownership signal
    if add_player.ownership_pct >= 50:
        parts.append(
            f"Owned in {add_player.ownership_pct:.0f}% of leagues, "
            f"so other managers value this player highly."
        )
    elif add_player.ownership_pct >= 20:
        parts.append(f"Owned in {add_player.ownership_pct:.0f}% of leagues — a solid pickup.")

    # Drop context
    if drop_player:
        parts.append(
            f"Consider dropping {drop_player.name} to make room. "
            f"They're contributing less to the categories you need."
        )

    if audience == "beginner":
        parts.append(
            "💡 Tip: In ROTO leagues, you earn points by finishing higher in "
            "each statistical category. Picking up players who help your "
            "weakest categories is the fastest way to climb."
        )

    return " ".join(parts)


def _build_start_sit_reason(
    player: Player,
    game: GameMatchup,
    matchup_score: float,
    needs: list[CategoryNeed],
) -> str:
    """Explain why we recommend starting or sitting a player."""
    parts = []

    opponent = _get_opponent(player, game)
    parts.append(f"Playing against {opponent} today.")

    if player.player_type == PlayerType.HITTER:
        is_home = player.team.upper() in game.home_team.upper()
        opp_pitcher = game.away_probable_pitcher if is_home else game.home_probable_pitcher
        opp_era = game.away_pitcher_era if is_home else game.home_pitcher_era

        if opp_pitcher:
            era_str = f" ({opp_era:.2f} ERA)" if opp_era else ""
            if matchup_score >= 65:
                parts.append(f"Facing {opp_pitcher}{era_str} — a favorable matchup for hitters.")
            elif matchup_score < 40:
                parts.append(f"Facing {opp_pitcher}{era_str} — a tough matchup today.")

        if player.recent_hitting:
            avg = player.recent_hitting.batting_avg
            if avg >= 0.300:
                parts.append(f"Hot streak: hitting {avg:.3f} over the last 2 weeks.")
            elif avg < 0.200:
                parts.append(f"Cold stretch: just {avg:.3f} recently.")

    elif player.player_type == PlayerType.PITCHER:
        if player.season_pitching:
            if player.season_pitching.era <= 3.00:
                parts.append("Elite ERA this season — always start.")
            elif player.season_pitching.era >= 5.00:
                parts.append("Struggling with a high ERA — proceed with caution.")

    return " ".join(parts)


def _urgency_from_score(score: float) -> UrgencyLevel:
    if score >= 0.8:
        return UrgencyLevel.HIGH
    elif score >= 0.4:
        return UrgencyLevel.MEDIUM
    return UrgencyLevel.LOW
