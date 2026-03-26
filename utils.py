"""
Shared utility functions — logging setup, config loading, date helpers.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


# ── Logging ─────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the application-wide logger."""
    logger = logging.getLogger("fantasy_manager")
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


log = setup_logging()


# ── Configuration ───────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict[str, Any]:
    """Load the YAML configuration file and return it as a dict."""
    config_path = Path(path)
    if not config_path.exists():
        log.error(
            f"Config file not found at {config_path.resolve()}. "
            "Copy config_example.yaml to config.yaml and fill in your values."
        )
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    """Ensure all required config sections are present."""
    required_sections = ["yahoo", "google_drive", "roto_categories", "analysis"]
    for section in required_sections:
        if section not in config:
            log.error(f"Missing required config section: '{section}'")
            sys.exit(1)

    yahoo = config["yahoo"]
    for key in ("client_id", "client_secret", "league_id"):
        val = yahoo.get(key, "")
        if not val or val.startswith("YOUR_"):
            log.error(
                f"Yahoo config '{key}' is not set. "
                "Edit config.yaml with your real credentials."
            )
            sys.exit(1)


# ── Date Helpers ────────────────────────────────────────────────────────

def today() -> date:
    """Return today's date (makes testing easier to mock)."""
    return date.today()


def today_str(fmt: str = "%Y-%m-%d") -> str:
    return today().strftime(fmt)


def today_display() -> str:
    """Human-friendly date, e.g. 'Wednesday, March 25, 2026'."""
    return today().strftime("%A, %B %d, %Y")


def days_ago(n: int) -> date:
    return today() - timedelta(days=n)


def days_ago_str(n: int, fmt: str = "%Y-%m-%d") -> str:
    return days_ago(n).strftime(fmt)


# ── Stat Formatting ─────────────────────────────────────────────────────

def fmt_avg(value: float) -> str:
    """Format a batting average / rate stat to 3 decimal places."""
    if value == 0:
        return ".000"
    return f"{value:.3f}".lstrip("0") if value < 1 else f"{value:.3f}"


def fmt_era(value: float) -> str:
    return f"{value:.2f}"


def fmt_ip(innings: float) -> str:
    """Format innings pitched (e.g., 6.33333 → '6.1')."""
    whole = int(innings)
    fraction = innings - whole
    # MLB convention: .1 = 1/3, .2 = 2/3
    outs = round(fraction * 3)
    return f"{whole}.{outs}"


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Divide without ZeroDivisionError."""
    if denominator == 0:
        return default
    return numerator / denominator


# ── Category Mapping ────────────────────────────────────────────────────

# Maps config stat keys → the attribute names on our HittingStats / PitchingStats
HITTING_STAT_MAP: dict[str, str] = {
    "R": "runs",
    "HR": "home_runs",
    "RBI": "rbi",
    "SB": "stolen_bases",
    "AVG": "batting_avg",
    "OBP": "on_base_pct",
    "SLG": "slugging_pct",
    "OPS": "ops",
    "H": "hits",
    "2B": "doubles",
    "3B": "triples",
    "BB": "walks",
    "SO": "strikeouts",
}

PITCHING_STAT_MAP: dict[str, str] = {
    "W": "wins",
    "K": "strikeouts",
    "ERA": "era",
    "WHIP": "whip",
    "SV": "saves",
    "HLD": "holds",
    "QS": "quality_starts",
    "IP": "innings_pitched",
    "L": "losses",
    "CG": "complete_games",
    "K9": "k_per_9",
}


def get_stat_value(player, stat_key: str, period: str = "season") -> float:
    """
    Pull a stat value from a Player object by the config stat key.

    Args:
        player: A Player instance with populated stats.
        stat_key: The short key from config (e.g. "HR", "ERA").
        period: "season" or "recent".

    Returns:
        The numeric stat value, or 0.0 if unavailable.
    """
    from models import PlayerType

    if stat_key in HITTING_STAT_MAP:
        stats_obj = (
            player.recent_hitting if period == "recent" else player.season_hitting
        )
        if stats_obj is None:
            return 0.0
        attr = HITTING_STAT_MAP[stat_key]
        return getattr(stats_obj, attr, 0.0)

    if stat_key in PITCHING_STAT_MAP:
        stats_obj = (
            player.recent_pitching if period == "recent" else player.season_pitching
        )
        if stats_obj is None:
            return 0.0
        attr = PITCHING_STAT_MAP[stat_key]
        # Handle property (k_per_9)
        val = getattr(stats_obj, attr, 0.0)
        return val() if callable(val) else val

    return 0.0
