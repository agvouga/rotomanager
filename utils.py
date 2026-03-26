"""
Shared utility functions — logging, config loading, date helpers, stat mapping.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml


# ── Logging ─────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("fantasy_manager")
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)
    return logger


log = setup_logging()


# ── Configuration ───────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        log.error(
            f"Config file not found at {config_path.resolve()}. "
            "Copy config_example.yaml → config.yaml and fill in your values."
        )
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    _validate_config(config)
    return config


def _validate_config(config: dict) -> None:
    for section in ("yahoo", "roto_categories", "analysis"):
        if section not in config:
            log.error(f"Missing required config section: '{section}'")
            sys.exit(1)

    yahoo = config["yahoo"]
    for key in ("client_id", "client_secret", "league_id"):
        val = yahoo.get(key, "")
        if not val or val.startswith("YOUR_"):
            log.error(f"Yahoo config '{key}' is not set. Edit config.yaml.")
            sys.exit(1)


# ── Date Helpers ────────────────────────────────────────────────────────

def today() -> date:
    return date.today()

def today_str(fmt: str = "%Y-%m-%d") -> str:
    return today().strftime(fmt)

def today_display() -> str:
    return today().strftime("%A, %B %-d, %Y")

def days_ago(n: int) -> date:
    return today() - timedelta(days=n)

def days_ago_str(n: int, fmt: str = "%Y-%m-%d") -> str:
    return days_ago(n).strftime(fmt)


# ── Stat Formatting ─────────────────────────────────────────────────────

def fmt_rate(value: float) -> str:
    """Format a rate stat (.345, .812, etc.)."""
    if value == 0:
        return ".000"
    s = f"{value:.3f}"
    return s.lstrip("0") if value < 1 else s

def fmt_era(value: float) -> str:
    return f"{value:.2f}"

def fmt_ip(innings: float) -> str:
    whole = int(innings)
    outs = round((innings - whole) * 3)
    return f"{whole}.{outs}"

def safe_divide(num: float, den: float, default: float = 0.0) -> float:
    return num / den if den != 0 else default


# ── Category → Attribute Mapping ────────────────────────────────────────
# Maps the short stat keys in config.yaml to attribute names on
# HittingStats / PitchingStats so the analyzer can pull values generically.

HITTING_STAT_MAP: dict[str, str] = {
    "R":   "runs",
    "HR":  "home_runs",
    "RBI": "rbi",
    "SB":  "stolen_bases",
    "AVG": "batting_avg",
    "OBP": "on_base_pct",
    "SLG": "slugging_pct",
    "OPS": "ops",
    "H":   "hits",
    "2B":  "doubles",
    "3B":  "triples",
    "BB":  "walks",
    "SO":  "strikeouts",
}

PITCHING_STAT_MAP: dict[str, str] = {
    "W":   "wins",
    "K":   "strikeouts",
    "ERA": "era",
    "WHIP":"whip",
    "SV":  "saves",
    "HLD": "holds",
    "QS":  "quality_starts",
    "IP":  "innings_pitched",
    "L":   "losses",
    "CG":  "complete_games",
    "K9":  "k_per_9",
}


def get_stat_value(player, stat_key: str, period: str = "season") -> float:
    """
    Pull a stat value from a Player by the config stat key.
    period: "season" or "recent"
    """
    from models import PlayerType  # avoid circular import

    if stat_key in HITTING_STAT_MAP:
        obj = player.recent_hitting if period == "recent" else player.season_hitting
        if obj is None:
            return 0.0
        attr = HITTING_STAT_MAP[stat_key]
        return float(getattr(obj, attr, 0.0))

    if stat_key in PITCHING_STAT_MAP:
        obj = player.recent_pitching if period == "recent" else player.season_pitching
        if obj is None:
            return 0.0
        attr = PITCHING_STAT_MAP[stat_key]
        val = getattr(obj, attr, 0.0)
        return float(val() if callable(val) else val)

    return 0.0
