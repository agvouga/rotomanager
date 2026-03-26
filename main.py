#!/usr/bin/env python3
"""
Fantasy Baseball ROTO Daily Manager — Main Entry Point.

This script orchestrates the full daily workflow:
  1. Authenticate with Yahoo Fantasy API and Google Drive.
  2. Pull today's MLB schedule.
  3. Load your roster and available free agents from Yahoo.
  4. Enrich players with season + recent stats from the MLB API.
  5. Run the ROTO analysis engine to generate recommendations.
  6. Write the daily report to Google Drive.

Usage:
    python main.py                    # Run once for today
    python main.py --date 2026-04-15  # Run for a specific date
    python main.py --dry-run          # Print report to console (no Drive upload)
    python main.py --text-only        # Upload as plain text instead of Google Doc
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from models import DailyReport, PlayerType
from utils import load_config, log, today, today_str
from yahoo_client import YahooClient
from mlb_client import MLBClient
from analyzer import (
    compute_category_needs,
    find_trade_targets,
    find_waiver_adds,
    generate_executive_summary,
    make_start_sit_decisions,
    score_player,
)
from drive_writer import DriveWriter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fantasy Baseball ROTO Daily Manager"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--date", default=None,
        help="Run for a specific date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the report to console without uploading to Drive.",
    )
    parser.add_argument(
        "--text-only", action="store_true",
        help="Upload as plain text file instead of a Google Doc.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else today()
    )
    date_str = target_date.strftime("%Y-%m-%d")

    log.info(f"=== Fantasy Baseball Daily Manager — {date_str} ===")

    # ── Step 1: Authenticate ────────────────────────────────────────────
    log.info("Step 1/6: Authenticating …")

    yahoo = YahooClient(config)
    yahoo.authenticate()

    mlb = MLBClient()

    if not args.dry_run:
        drive = DriveWriter(config)
        drive.authenticate()

    # ── Step 2: Fetch today's MLB schedule ──────────────────────────────
    log.info("Step 2/6: Fetching today's MLB schedule …")
    games_today = mlb.get_todays_games(game_date=date_str)

    # ── Step 3: Load roster and free agents from Yahoo ──────────────────
    log.info("Step 3/6: Loading your roster and waiver wire …")
    my_roster = yahoo.get_my_roster()
    free_agents = yahoo.get_free_agents(position="ALL", count=75)

    # ── Step 4: Enrich with MLB stats ───────────────────────────────────
    log.info("Step 4/6: Enriching players with MLB stats …")
    hot_streak_days = config.get("analysis", {}).get("hot_streak_days", 14)

    all_players = my_roster + free_agents
    enriched_count = 0
    for player in all_players:
        try:
            if player.player_type == PlayerType.HITTER:
                player.season_hitting = mlb.get_player_season_stats(
                    player.name, PlayerType.HITTER
                )
                player.recent_hitting = mlb.get_player_recent_stats(
                    player.name, PlayerType.HITTER, days=hot_streak_days
                )
            else:
                player.season_pitching = mlb.get_player_season_stats(
                    player.name, PlayerType.PITCHER
                )
                player.recent_pitching = mlb.get_player_recent_stats(
                    player.name, PlayerType.PITCHER, days=hot_streak_days
                )
            enriched_count += 1
        except Exception as exc:
            log.debug(f"Could not enrich {player.name}: {exc}")

    log.info(f"Enriched {enriched_count}/{len(all_players)} players with MLB stats")

    # ── Step 5: Run the analysis engine ─────────────────────────────────
    log.info("Step 5/6: Running ROTO analysis …")

    # Gather all configured categories
    all_categories = (
        config["roto_categories"].get("hitting", [])
        + config["roto_categories"].get("pitching", [])
    )

    # Get your current category rankings
    category_rankings = yahoo.get_my_category_rankings()

    # If we couldn't get real rankings, use mid-pack defaults
    if not category_rankings:
        log.warning(
            "Could not fetch category rankings — using mid-pack estimates. "
            "The app will still work, but recommendations will be less targeted."
        )
        category_rankings = {cat["stat"]: 6 for cat in all_categories}

    # Compute needs
    needs = compute_category_needs(
        category_rankings, all_categories, total_teams=12
    )

    log.info("Category needs (most urgent first):")
    for need in needs[:5]:
        log.info(f"  {need.name} ({need.stat}): rank {need.current_rank}, need={need.need_score:.2f}")

    # Waiver recommendations
    waiver_adds = find_waiver_adds(free_agents, my_roster, needs, config)
    log.info(f"Generated {len(waiver_adds)} waiver recommendations")

    # Trade recommendations
    trade_targets = find_trade_targets(my_roster, needs, config)
    log.info(f"Generated {len(trade_targets)} trade ideas")

    # Start/sit decisions
    start_sit = make_start_sit_decisions(my_roster, games_today, needs, config)
    starts = sum(1 for d in start_sit if d.decision == "START")
    sits = sum(1 for d in start_sit if d.decision == "SIT")
    log.info(f"Start/sit: {starts} starts, {sits} sits")

    # ── Build the report ────────────────────────────────────────────────
    report = DailyReport(
        report_date=target_date,
        league_name=yahoo.get_league_name(),
        games_today=games_today,
        my_roster=my_roster,
        waiver_adds=waiver_adds,
        trade_targets=trade_targets,
        start_sit=start_sit,
        category_standings=category_rankings,
    )

    # Generate the executive summary
    report.executive_summary = generate_executive_summary(report, config)

    # ── Step 6: Output ──────────────────────────────────────────────────
    if args.dry_run:
        log.info("Step 6/6: Dry run — printing report to console …")
        temp_writer = DriveWriter(config)
        plaintext = temp_writer._format_report_plaintext(report)
        print("\n")
        print(plaintext)
    else:
        log.info("Step 6/6: Writing report to Google Drive …")
        if args.text_only:
            url = drive.write_report_as_text(report)
        else:
            url = drive.write_report(report)
        log.info(f"✅ Report ready: {url}")

    log.info("=== Daily Manager complete ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        sys.exit(0)
    except Exception as exc:
        log.error(f"Fatal error: {exc}", exc_info=True)
        sys.exit(1)
