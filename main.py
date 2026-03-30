#!/usr/bin/env python3
"""
Fantasy Baseball ROTO Daily Manager — Main Entry Point.

Workflow:
  1. Authenticate with Yahoo Fantasy API.
  2. Pull today's MLB schedule (free public API, no auth).
  3. Load your roster and available free agents from Yahoo.
  4. Enrich players with season + recent stats from the MLB API.
  5. Run the ROTO analysis engine to produce recommendations.
  6. Write a Markdown report to your local synced folder.

Usage:
    python main.py                    # Run for today
    python main.py --date 2026-04-15  # Run for a specific date
    python main.py --dry-run          # Print to console, don't write file
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from models import DailyReport, PlayerType, RosterStatus
from utils import load_config, log, today, today_str
from yahoo_client import YahooClient
from mlb_client import MLBClient
from analyzer import (
    compute_category_needs,
    find_trade_targets,
    find_waiver_adds,
    generate_executive_summary,
    make_start_sit_decisions,
)
from report_writer import ReportWriter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fantasy Baseball ROTO Daily Manager")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--date", default=None, help="Run for a specific date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print report to console only")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else today()
    )
    date_str = target_date.strftime("%Y-%m-%d")

    log.info(f"=== Fantasy Baseball Daily Manager — {date_str} ===")

    # ── 1. Authenticate with Yahoo ──────────────────────────────────────
    log.info("Step 1/5: Authenticating with Yahoo …")
    yahoo = YahooClient(config)
    yahoo.authenticate()

    mlb = MLBClient()

    # ── 2. MLB schedule: today (start/sit) + tomorrow (waiver targets) ──
    log.info("Step 2/5: Fetching MLB schedule (today + tomorrow) …")
    games_today = mlb.get_todays_games(game_date=date_str)

    tomorrow = target_date + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")
    games_tomorrow = mlb.get_todays_games(game_date=tomorrow_str)
    log.info(f"  Today: {len(games_today)} games | Tomorrow: {len(games_tomorrow)} games")

    # ── 3. Roster + free agents from Yahoo ──────────────────────────────
    log.info("Step 3/5: Loading roster and waiver wire …")
    my_roster = yahoo.get_my_roster()
    free_agents = yahoo.get_free_agents(position="ALL", count=75)

    # ── 4. Enrich with MLB stats ────────────────────────────────────────
    log.info("Step 4/5: Enriching players with MLB stats …")
    hot_days = config.get("analysis", {}).get("hot_streak_days", 14)

    all_players = my_roster + free_agents
    enriched = 0
    for player in all_players:
        try:
            if player.player_type == PlayerType.HITTER:
                player.season_hitting = mlb.get_player_season_stats(player.name, PlayerType.HITTER)
                player.recent_hitting = mlb.get_player_recent_stats(player.name, PlayerType.HITTER, days=hot_days)
            else:
                player.season_pitching = mlb.get_player_season_stats(player.name, PlayerType.PITCHER)
                player.recent_pitching = mlb.get_player_recent_stats(player.name, PlayerType.PITCHER, days=hot_days)
            enriched += 1
        except Exception as exc:
            log.debug(f"Could not enrich {player.name}: {exc}")

    log.info(f"Enriched {enriched}/{len(all_players)} players")

    # ── 5. Run the analysis ─────────────────────────────────────────────
    log.info("Step 5/5: Running ROTO analysis …")

    all_categories = (
        config["roto_categories"].get("hitting", [])
        + config["roto_categories"].get("pitching", [])
    )

    category_rankings = yahoo.get_my_category_rankings()
    if not category_rankings:
        log.warning("Could not fetch category rankings — using mid-pack estimates.")
        category_rankings = {cat["stat"]: 6 for cat in all_categories}

    needs = compute_category_needs(category_rankings, all_categories, total_teams=12)

    log.info("Category needs (most urgent first):")
    for need in needs[:5]:
        log.info(f"  {need.name}: rank #{need.current_rank}, need={need.need_score:.2f}")

    waiver_adds = find_waiver_adds(free_agents, my_roster, needs, config)
    trade_targets = find_trade_targets(my_roster, needs, config)
    start_sit = make_start_sit_decisions(my_roster, games_today, needs, config)

    starts = sum(1 for d in start_sit if d.decision == "START")
    sits = sum(1 for d in start_sit if d.decision == "SIT")
    log.info(f"Results: {len(waiver_adds)} waiver picks, {len(trade_targets)} trade ideas, {starts} starts / {sits} sits")

    # Count open roster spots
    bench_capacity = config.get("roster_positions", {}).get("bench", 4)
    bench_count = sum(1 for p in my_roster if p.roster_status == RosterStatus.BENCH)
    open_spots = max(0, bench_capacity - bench_count)
    if open_spots > 0:
        log.info(f"  ⚠ {open_spots} open roster spot(s) detected!")

    # ── Build the report ────────────────────────────────────────────────
    report = DailyReport(
        report_date=target_date,
        league_name=yahoo.get_league_name(),
        games_today=games_today,
        games_tomorrow=games_tomorrow,
        my_roster=my_roster,
        open_roster_spots=open_spots,
        waiver_adds=waiver_adds,
        trade_targets=trade_targets,
        start_sit=start_sit,
        category_standings=category_rankings,
    )
    report.executive_summary = generate_executive_summary(report, config)

    # ── Output ──────────────────────────────────────────────────────────
    if args.dry_run:
        writer = ReportWriter(config)
        content = writer._render(report)
        print("\n" + content)
        log.info("Dry run complete — nothing written to disk.")
    else:
        writer = ReportWriter(config)
        path = writer.write(report)
        log.info(f"✅ Done! Report saved to: {path}")

    log.info("=== Daily Manager complete ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted")
        sys.exit(0)
    except Exception as exc:
        log.error(f"Fatal error: {exc}", exc_info=True)
        sys.exit(1)
