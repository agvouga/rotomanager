"""
ROTO Analysis Engine (6×6).

Hitting:  R, HR, RBI, SB, OBP, OPS
Pitching: W, SV, K, HLD, ERA, WHIP

Fixes addressed in this version:
  - Waivers: Always produces recommendations when free agents have stats.
    Handles empty bench spots (no drop needed). Lowered score thresholds.
  - Start/Sit: Only recommends SIT when there's a bench alternative who
    can fill the position. An empty lineup slot is always worse than a
    bad matchup.
  - Trades: Works at mid-pack rankings by looking at relative strength
    across your own categories rather than requiring extreme top/bottom.
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
    Composite score: how much does this player produce across your ROTO
    categories, weighted by how badly you need each one?

    Returns a score >= 0. A player with ANY positive stats will score > 0.
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

        contribution = _normalize_stat(stat, blended, player)

        # Weight by need — but give a baseline even for categories you're
        # winning so that productive players always score above zero.
        # need_score ranges 0–1; we add a floor of 0.2 so strong categories
        # still count for something.
        weight = max(need.need_score, 0.2)
        score += contribution * weight

        if contribution > 0.2:  # lowered from 0.3
            categories_helped += 1

    # Multi-category bonus
    if categories_helped >= 4:
        score *= 1.35
    elif categories_helped >= 3:
        score *= 1.20
    elif categories_helped >= 2:
        score *= 1.10

    # OBP/OPS league bonus: reward plate discipline
    if player.player_type == PlayerType.HITTER and player.season_hitting:
        h = player.season_hitting
        if h.plate_appearances > 0:
            walk_rate = h.walks / h.plate_appearances
            if walk_rate >= 0.12:
                score *= 1.10
            elif walk_rate >= 0.09:
                score *= 1.05

    # Holds league bonus: value setup relievers
    if player.player_type == PlayerType.PITCHER and player.season_pitching:
        p = player.season_pitching
        if p.holds >= 5 and p.saves >= 3:
            score *= 1.15
        elif p.holds >= 3:
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
        return value / 0.400
    if stat == "OPS":
        return value / 1.000
    if stat == "AVG":
        return value / 0.350
    if stat == "SLG":
        return value / 0.600
    if stat == "ERA":
        return max(0, (5.0 - value) / 5.0)
    if stat == "WHIP":
        return max(0, (1.8 - value) / 1.8)

    # Counting stats: use raw total (not per-game) for season-length
    # comparison, then normalize against reasonable season totals.
    # This avoids the per-game division that crushes scores for players
    # with many games played.
    season_ceilings = {
        "R": 100, "HR": 40, "RBI": 110, "SB": 40,
        "W": 15, "K": 220, "SV": 35, "HLD": 30,
        "H": 180, "BB": 80, "QS": 20,
    }
    ceiling = season_ceilings.get(stat)
    if ceiling:
        return min(value / ceiling, 1.5)

    return min(value, 1.5)


# ── Waiver Wire ─────────────────────────────────────────────────────────

def find_waiver_adds(
    free_agents: list[Player],
    my_roster: list[Player],
    needs: list[CategoryNeed],
    config: dict,
) -> list[Recommendation]:
    """
    Rank free agents and always return the top N suggestions as long as
    they have any stats at all.

    Handles empty bench spots: if you have open roster spots, no drop
    is needed. Otherwise pairs each add with the weakest bench player.
    """
    analysis_cfg = config.get("analysis", {})
    min_ownership = analysis_cfg.get("min_ownership_pct", 0)  # default to 0 so we don't filter too aggressively
    max_suggestions = analysis_cfg.get("max_waiver_suggestions", 10)

    # Filter: not injured, has at least some stats to evaluate
    viable = []
    for fa in free_agents:
        if fa.is_injured:
            continue
        if fa.ownership_pct < min_ownership:
            continue
        # Must have SOME stats to score
        has_stats = (
            (fa.season_hitting is not None and fa.season_hitting.games > 0) or
            (fa.season_pitching is not None and fa.season_pitching.games > 0) or
            (fa.recent_hitting is not None and fa.recent_hitting.games > 0) or
            (fa.recent_pitching is not None and fa.recent_pitching.games > 0)
        )
        if has_stats:
            viable.append(fa)

    log.info(f"Waiver evaluation: {len(viable)} viable free agents (of {len(free_agents)} total)")

    # Score each free agent
    scored = [(fa, score_player(fa, needs, config)) for fa in viable]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Count open roster spots (bench slots not occupied)
    bench_capacity = config.get("roster_positions", {}).get("bench", 4)
    bench_players = [p for p in my_roster if p.roster_status == RosterStatus.BENCH]
    open_spots = max(0, bench_capacity - len(bench_players))

    log.info(f"Roster: {len(bench_players)} bench players, {open_spots} open spots")

    # Score bench players for drop candidates (weakest first)
    bench_scored = sorted(
        [(p, score_player(p, needs, config)) for p in bench_players],
        key=lambda x: x[1],
    )

    recommendations = []
    drops_used = set()  # don't suggest dropping the same player twice

    for fa, fa_score in scored[:max_suggestions]:
        if fa_score <= 0:
            continue

        drop_player = None

        if open_spots > 0:
            # Free slot available — just add, no drop needed
            open_spots -= 1
        else:
            # Find the weakest bench player of same type not already suggested
            for bp, bp_score in bench_scored:
                if bp.player_id in drops_used:
                    continue
                if bp.player_type == fa.player_type and fa_score > bp_score:
                    drop_player = bp
                    drops_used.add(bp.player_id)
                    break

            # If no same-type drop, try any bench player
            if drop_player is None:
                for bp, bp_score in bench_scored:
                    if bp.player_id in drops_used:
                        continue
                    if fa_score > bp_score:
                        drop_player = bp
                        drops_used.add(bp.player_id)
                        break

            # If the free agent doesn't beat ANY bench player, skip
            if drop_player is None and open_spots <= 0:
                continue

        impact = _describe_category_impact(fa, needs)
        explanation = _build_waiver_explanation(fa, drop_player, needs, config, open_spots_remain=(drop_player is None))

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

    log.info(f"Waiver recommendations generated: {len(recommendations)}")
    return recommendations


# ── Trade Ideas ─────────────────────────────────────────────────────────

def find_trade_targets(
    my_roster: list[Player],
    needs: list[CategoryNeed],
    config: dict,
) -> list[Recommendation]:
    """
    Find "sell high" candidates: players producing heavily in categories
    you're relatively strong in, who could be traded for help in weaker areas.

    Uses relative comparison across YOUR categories rather than requiring
    extreme top-3/bottom-3 splits, so it works at mid-pack standings too.
    """
    max_suggestions = config.get("analysis", {}).get("max_trade_suggestions", 5)

    if len(needs) < 4:
        return []

    # Split into stronger half and weaker half of YOUR categories
    midpoint = len(needs) // 2
    weak_cats = needs[:midpoint]    # sorted by need_score descending, so top = weakest
    strong_cats = needs[midpoint:]  # bottom = strongest

    candidates = []
    for player in my_roster:
        if player.roster_status == RosterStatus.INJURED:
            continue
        if player.roster_status == RosterStatus.BENCH:
            continue  # bench players aren't trade chips

        # How much does this player contribute to your strong categories?
        strong_val = 0.0
        weak_val = 0.0
        for cat in strong_cats:
            v = get_stat_value(player, cat.stat, "season")
            strong_val += _normalize_stat(cat.stat, v, player) if v > 0 else 0
        for cat in weak_cats:
            v = get_stat_value(player, cat.stat, "season")
            weak_val += _normalize_stat(cat.stat, v, player) if v > 0 else 0

        # Good trade chip = high production in strong cats, low in weak cats
        if strong_val > 0.3 and (weak_val < 0.15 or strong_val > weak_val * 2):
            candidates.append((player, strong_val, weak_val))

    candidates.sort(key=lambda x: x[1], reverse=True)

    recs = []
    for player, strong_v, weak_v in candidates[:max_suggestions]:
        weak_names = ", ".join(c.name for c in weak_cats[:3])
        strong_names = ", ".join(c.name for c in strong_cats[:2])

        explanation = (
            f"{player.name} is producing most in {strong_names}, where you're "
            f"relatively strong. Trade them to a team that needs those stats "
            f"and target a return that helps your {weak_names}."
        )

        recs.append(Recommendation(
            rec_type=RecommendationType.TRADE_AWAY,
            player=player,
            urgency=UrgencyLevel.MEDIUM if strong_v > 0.6 else UrgencyLevel.LOW,
            headline=f"Sell high: {player.name} ({player.primary_position})",
            explanation=explanation,
            score=strong_v,
        ))

    return recs


# ── Start / Sit ─────────────────────────────────────────────────────────

def make_start_sit_decisions(
    roster: list[Player],
    games: list[GameMatchup],
    needs: list[CategoryNeed],
    config: dict,
) -> list[StartSitDecision]:
    """
    Start/sit decisions with a critical rule: NEVER recommend sitting a
    player if there's no bench alternative who can fill that roster slot.
    An empty lineup spot always scores zero, which is worse than any
    bad matchup.
    """
    team_game: dict[str, GameMatchup] = {}
    for game in games:
        team_game[game.home_team] = game
        team_game[game.away_team] = game

    # First pass: score every player's matchup
    player_matchups: list[tuple[Player, GameMatchup | None, float]] = []

    for player in roster:
        if player.roster_status == RosterStatus.INJURED:
            continue

        game = _find_game(player, team_game)

        if game is None:
            player_matchups.append((player, None, 0.0))
        else:
            player.is_playing_today = True
            player.opponent_today = _get_opponent(player, game)
            ms = _evaluate_matchup(player, game, needs)
            player_matchups.append((player, game, ms))

    # Identify bench players available as substitutes
    bench_players = [
        (p, g, ms) for p, g, ms in player_matchups
        if p.roster_status == RosterStatus.BENCH and g is not None
    ]

    # Build the position → bench alternatives map
    def _positions_for(player: Player) -> set[str]:
        """All positions this player is eligible for."""
        pos = set(player.positions)
        # OF covers LF/CF/RF and vice versa
        if pos & {"LF", "CF", "RF", "OF"}:
            pos.add("OF")
        pos.add("Util")
        return pos

    decisions = []

    for player, game, ms in player_matchups:
        if player.roster_status == RosterStatus.BENCH:
            # Bench players: just note their matchup for context
            if game:
                reason = _build_start_sit_reason(player, game, ms)
                decisions.append(StartSitDecision(
                    player=player,
                    decision="BENCH",
                    confidence="—",
                    reason=f"On bench. {reason}",
                    opponent=player.opponent_today or "",
                    matchup_score=ms,
                ))
            continue

        # Active roster player
        if game is None:
            # No game today — but only flag as SIT if a bench player could
            # take this slot
            can_replace = any(
                _positions_for(bp) & _positions_for(player)
                for bp, bg, bms in bench_players
            )
            if can_replace:
                decisions.append(StartSitDecision(
                    player=player,
                    decision="SIT",
                    confidence="High",
                    reason="No game today. A bench player can fill this slot.",
                    matchup_score=0,
                ))
            else:
                decisions.append(StartSitDecision(
                    player=player,
                    decision="START",
                    confidence="—",
                    reason="No game today, but no bench alternative for this position.",
                    matchup_score=0,
                ))
            continue

        # Has a game — evaluate matchup
        if ms >= 65:
            decision = "START"
            confidence = "High" if ms >= 80 else "Medium"
            reason = _build_start_sit_reason(player, game, ms)
        elif ms >= 45:
            decision = "START"
            confidence = "Low"
            reason = _build_start_sit_reason(player, game, ms)
        else:
            # Bad matchup — but can we actually sit them?
            better_bench = [
                (bp, bms) for bp, bg, bms in bench_players
                if _positions_for(bp) & _positions_for(player) and bms > ms
            ]
            if better_bench:
                best_alt, best_ms = max(better_bench, key=lambda x: x[1])
                decision = "SIT"
                confidence = "High" if ms < 25 else "Medium"
                reason = (
                    _build_start_sit_reason(player, game, ms) +
                    f" Consider starting {best_alt.name} instead "
                    f"(matchup score {best_ms:.0f} vs {ms:.0f})."
                )
            else:
                # No better alternative — start them despite the bad matchup
                decision = "START"
                confidence = "Low"
                reason = (
                    _build_start_sit_reason(player, game, ms) +
                    " Tough matchup, but no better bench option at this position."
                )

        decisions.append(StartSitDecision(
            player=player,
            decision=decision,
            confidence=confidence,
            reason=reason,
            opponent=player.opponent_today or "",
            matchup_score=ms,
        ))

    # Sort: starters first, then sits, then bench notes
    order = {"START": 0, "SIT": 1, "BENCH": 2}
    decisions.sort(key=lambda d: (order.get(d.decision, 3), -d.matchup_score))
    return decisions


# ── Executive Summary ───────────────────────────────────────────────────

def generate_executive_summary(report: DailyReport, config: dict) -> str:
    lines = []

    n_games = len(report.games_today)
    active = sum(1 for d in report.start_sit if d.decision == "START")
    lines.append(f"**{n_games} MLB games today.** You have **{active} players** to start.")

    if report.open_roster_spots > 0:
        lines.append(
            f"\n**You have {report.open_roster_spots} open roster spot(s)!** "
            f"Pick up a free agent — there's no reason to leave spots empty."
        )

    if report.waiver_adds:
        top = report.waiver_adds[0]
        urgency_tag = " **Act fast — high priority.**" if top.urgency == UrgencyLevel.HIGH else ""
        lines.append(f"\n**Top move:** {top.headline}.{urgency_tag}")

    if report.category_standings:
        worst = sorted(report.category_standings.items(), key=lambda x: x[1], reverse=True)[:3]
        cats = ", ".join(f"{cat} (#{rank})" for cat, rank in worst)
        lines.append(f"\n**Category watch:** Your weakest areas are {cats}. Today's suggestions target these.")

    sits = [d for d in report.start_sit if d.decision == "SIT"]
    if sits:
        names = ", ".join(d.player.name for d in sits[:3])
        lines.append(f"\n**Bench today:** {names}.")

    n_tomorrow = len(report.games_tomorrow)
    if n_tomorrow > 0:
        lines.append(f"\n**Tomorrow:** {n_tomorrow} games on the schedule — waiver claims below target tomorrow's action.")

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
            else:
                parts.append(f"Facing {opp_p}{era_s}.")

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
            if need.need_score > 0.5:
                impact[need.stat] = "Helps a weak category!"
            elif need.need_score > 0.25:
                impact[need.stat] = "Contributes"
            else:
                impact[need.stat] = "Nice to have"
    return impact


def _build_waiver_explanation(
    add: Player, drop: Player | None, needs: list[CategoryNeed],
    config: dict, open_spots_remain: bool = False,
) -> str:
    audience = config.get("report", {}).get("audience", "beginner")
    parts = []

    if open_spots_remain:
        parts.append("**You have an open roster spot — no drop needed!**")

    # What categories does this player help?
    helping = [n.name for n in needs if get_stat_value(add, n.stat, "season") > 0 and n.need_score > 0.25]
    if helping:
        parts.append(f"{add.name} boosts your {', '.join(helping[:4])}.")

    if add.ownership_pct >= 50:
        parts.append(f"Owned in {add.ownership_pct:.0f}% of leagues (highly valued).")
    elif add.ownership_pct >= 20:
        parts.append(f"Owned in {add.ownership_pct:.0f}% of leagues.")

    if add.player_type == PlayerType.HITTER and add.season_hitting:
        h = add.season_hitting
        if h.on_base_pct >= 0.350:
            parts.append(f"Strong {h.on_base_pct:.3f} OBP — valuable in your OBP league.")
        if h.ops >= 0.850:
            parts.append(f"Excellent {h.ops:.3f} OPS.")

    if add.player_type == PlayerType.PITCHER and add.season_pitching:
        p = add.season_pitching
        if p.holds >= 3:
            parts.append(f"Already has {p.holds} holds — real asset in a holds league.")
        if p.saves >= 3 and p.holds >= 3:
            parts.append("Rare SV+HLD dual contributor.")

    if drop:
        parts.append(f"Drop {drop.name} to make room.")

    if audience == "beginner":
        parts.append(
            "*(In ROTO, you earn points by ranking higher in each stat category. "
            "Picking up players who help your weakest categories is the fastest way to climb.)*"
        )

    return " ".join(parts)


def _urgency_from_score(score: float) -> UrgencyLevel:
    if score >= 1.5:
        return UrgencyLevel.HIGH
    if score >= 0.5:
        return UrgencyLevel.MEDIUM
    return UrgencyLevel.LOW
