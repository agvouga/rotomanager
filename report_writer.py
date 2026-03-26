"""
Local Markdown Report Writer.

Writes a clean, phone-readable .md file to a local folder (typically one
synced to Google Drive, Dropbox, or OneDrive). No API keys, no OAuth,
no service accounts — just a file on disk.
"""

from __future__ import annotations

from pathlib import Path

from models import (
    DailyReport,
    Recommendation,
    StartSitDecision,
    UrgencyLevel,
)
from utils import log, today_str, today_display


class ReportWriter:
    """Writes the daily report as a local Markdown file."""

    def __init__(self, config: dict):
        output_cfg = config.get("output", {})
        raw_dir = output_cfg.get("directory", "~/Fantasy Baseball Reports")
        self._directory = Path(raw_dir).expanduser()
        self._mode = output_cfg.get("mode", "overwrite")
        self._filename = output_cfg.get("filename", "daily_report.md")

    def write(self, report: DailyReport) -> Path:
        """
        Render the report to Markdown and write it to disk.
        Returns the path of the written file.
        """
        self._directory.mkdir(parents=True, exist_ok=True)

        if self._mode == "dated":
            date_str = report.report_date.strftime("%Y-%m-%d")
            filename = f"report_{date_str}.md"
        else:
            filename = self._filename

        filepath = self._directory / filename
        content = self._render(report)
        filepath.write_text(content, encoding="utf-8")

        log.info(f"Report written → {filepath}")
        return filepath

    # ── Markdown Rendering ──────────────────────────────────────────────

    def _render(self, report: DailyReport) -> str:
        sections = [
            self._header(report),
            self._summary(report),
            self._schedule(report),
            self._start_sit(report),
            self._waivers(report),
            self._trades(report),
            self._footer(),
        ]
        return "\n".join(sections)

    # ── Sections ────────────────────────────────────────────────────────

    def _header(self, report: DailyReport) -> str:
        date_display = f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"
        league = report.league_name or "My League"
        return (
            f"# Fantasy Baseball Daily Report\n"
            f"### {date_display} — {league}\n"
            f"---\n"
        )

    def _summary(self, report: DailyReport) -> str:
        if not report.executive_summary:
            return ""
        return (
            f"## Today at a Glance\n\n"
            f"{report.executive_summary}\n\n"
            f"---\n"
        )

    def _schedule(self, report: DailyReport) -> str:
        lines = ["## Today's Games\n"]

        if not report.games_today:
            lines.append("No MLB games scheduled today.\n")
            return "\n".join(lines) + "\n---\n"

        # Table header
        lines.append("| Matchup | Away SP | Home SP |")
        lines.append("|---------|---------|---------|")

        for g in report.games_today:
            away_sp = g.away_probable_pitcher or "TBD"
            home_sp = g.home_probable_pitcher or "TBD"
            if g.away_pitcher_era is not None:
                away_sp += f" ({g.away_pitcher_era:.2f})"
            if g.home_pitcher_era is not None:
                home_sp += f" ({g.home_pitcher_era:.2f})"
            lines.append(f"| {g.matchup_label} | {away_sp} | {home_sp} |")

        lines.append("")
        return "\n".join(lines) + "\n---\n"

    def _start_sit(self, report: DailyReport) -> str:
        lines = ["## Start / Sit\n"]

        if not report.start_sit:
            lines.append("No decisions to make today.\n")
            return "\n".join(lines) + "\n---\n"

        # Group by decision
        starters = [d for d in report.start_sit if d.decision == "START"]
        sitters = [d for d in report.start_sit if d.decision == "SIT"]

        if starters:
            lines.append("### Start\n")
            for d in starters:
                icon = self._confidence_icon(d.confidence)
                lines.append(
                    f"- {icon} **{d.player.name}** "
                    f"({d.player.primary_position}, {d.player.team}) "
                    f"— {d.confidence} confidence"
                )
                lines.append(f"  - {d.reason}")
            lines.append("")

        if sitters:
            lines.append("### Sit\n")
            for d in sitters:
                lines.append(
                    f"- **{d.player.name}** "
                    f"({d.player.primary_position}, {d.player.team}) "
                    f"— {d.confidence} confidence"
                )
                lines.append(f"  - {d.reason}")
            lines.append("")

        return "\n".join(lines) + "\n---\n"

    def _waivers(self, report: DailyReport) -> str:
        lines = ["## Waiver Wire Picks\n"]

        if not report.waiver_adds:
            lines.append("No waiver moves recommended today. Your roster is solid.\n")
            return "\n".join(lines) + "\n---\n"

        for i, rec in enumerate(report.waiver_adds, 1):
            icon = self._urgency_icon(rec.urgency)
            lines.append(f"### {i}. {icon} {rec.headline}\n")

            if rec.explanation:
                lines.append(f"{rec.explanation}\n")

            if rec.category_impact:
                impact_parts = [f"**{cat}**: {desc}" for cat, desc in rec.category_impact.items()]
                lines.append(f"Category impact: {' · '.join(impact_parts)}\n")

            if rec.paired_player:
                lines.append(f"> **Drop candidate:** {rec.paired_player.name}\n")

            lines.append("")

        return "\n".join(lines) + "---\n"

    def _trades(self, report: DailyReport) -> str:
        lines = ["## Trade Ideas\n"]

        if not report.trade_targets:
            lines.append("No trade suggestions today — hold your roster.\n")
            return "\n".join(lines) + "\n---\n"

        for i, rec in enumerate(report.trade_targets, 1):
            lines.append(f"### {i}. {rec.headline}\n")
            if rec.explanation:
                lines.append(f"{rec.explanation}\n")
            lines.append("")

        return "\n".join(lines) + "---\n"

    def _footer(self) -> str:
        return (
            "\n*Generated by Fantasy Baseball ROTO Daily Manager. "
            "Recommendations are based on statistical analysis and expert "
            "ROTO strategy. Always double-check injury news before making moves.*\n"
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _urgency_icon(urgency: UrgencyLevel) -> str:
        return {"high": "🔥", "medium": "⚡", "low": "💤"}.get(urgency.value, "")

    @staticmethod
    def _confidence_icon(confidence: str) -> str:
        return {"High": "✅", "Medium": "🟡", "Low": "🟠"}.get(confidence, "")
