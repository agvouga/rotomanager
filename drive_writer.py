"""
Google Drive Report Writer.

Generates a structured daily report as a Google Doc and uploads it to
a designated Google Drive folder. The report is formatted for readability
by a beginner fantasy manager.
"""

from __future__ import annotations

import io
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from models import (
    DailyReport,
    Recommendation,
    RecommendationType,
    StartSitDecision,
    UrgencyLevel,
)
from utils import log, today_str, today_display, fmt_avg, fmt_era


# ── Scopes ──────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
]


class DriveWriter:
    """Writes daily fantasy reports to Google Drive as Google Docs."""

    def __init__(self, config: dict):
        self._cfg = config["google_drive"]
        self._drive_service = None
        self._docs_service = None

    # ── Auth ────────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        """Set up Google API clients using the service account."""
        sa_file = self._cfg["service_account_file"]
        log.info(f"Authenticating with Google Drive (service account: {sa_file})")

        credentials = service_account.Credentials.from_service_account_file(
            sa_file, scopes=SCOPES
        )
        self._drive_service = build("drive", "v3", credentials=credentials)
        self._docs_service = build("docs", "v1", credentials=credentials)
        log.info("Google Drive authentication successful")

    # ── Public API ──────────────────────────────────────────────────────

    def write_report(self, report: DailyReport) -> str:
        """
        Generate the daily report and upload it to Google Drive.

        Returns the URL of the created Google Doc.
        """
        title = self._build_title(report)
        content = self._format_report(report)

        # Create the Doc
        doc_id = self._create_google_doc(title)

        # Write content to the Doc
        self._populate_doc(doc_id, content)

        # Move to the target folder
        self._move_to_folder(doc_id)

        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        log.info(f"Report written: {url}")
        return url

    def write_report_as_text(self, report: DailyReport) -> str:
        """
        Alternative: upload a plain-text file instead of a Google Doc.
        Useful if you don't want to grant Docs API scope.

        Returns the URL of the uploaded file.
        """
        title = self._build_title(report) + ".txt"
        content = self._format_report_plaintext(report)

        file_metadata = {
            "name": title,
            "parents": [self._cfg["folder_id"]],
            "mimeType": "text/plain",
        }

        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype="text/plain",
        )

        result = self._drive_service.files().create(
            body=file_metadata, media_body=media, fields="id,webViewLink"
        ).execute()

        url = result.get("webViewLink", f"https://drive.google.com/file/d/{result['id']}")
        log.info(f"Text report uploaded: {url}")
        return url

    # ── Formatting ──────────────────────────────────────────────────────

    def _build_title(self, report: DailyReport) -> str:
        prefix = "Fantasy Daily Report"
        date_str = report.report_date.strftime("%Y-%m-%d")
        return f"{prefix} — {date_str}"

    def _format_report(self, report: DailyReport) -> list[dict]:
        """
        Build a list of Google Docs API 'requests' that insert formatted
        text into the document.

        Each request is an insertText or updateParagraphStyle call.
        """
        requests: list[dict] = []
        idx = 1  # cursor position (Google Docs uses 1-based index)

        # ── Title ───────────────────────────────────────────────────────
        title_text = f"Fantasy Baseball Daily Report\n{today_display()}\n\n"
        requests.append(self._insert_text(idx, title_text))
        requests.append(self._style_heading(idx, idx + len("Fantasy Baseball Daily Report"), "HEADING_1"))
        idx += len(title_text)

        # ── Executive Summary ───────────────────────────────────────────
        if report.executive_summary:
            header = "Today's Overview\n"
            requests.append(self._insert_text(idx, header))
            requests.append(self._style_heading(idx, idx + len(header) - 1, "HEADING_2"))
            idx += len(header)

            body = report.executive_summary + "\n\n"
            requests.append(self._insert_text(idx, body))
            idx += len(body)

        # ── Today's Games ───────────────────────────────────────────────
        header = "Today's MLB Games\n"
        requests.append(self._insert_text(idx, header))
        requests.append(self._style_heading(idx, idx + len(header) - 1, "HEADING_2"))
        idx += len(header)

        if report.games_today:
            for game in report.games_today:
                line = f"• {game.matchup_label}"
                if game.home_probable_pitcher or game.away_probable_pitcher:
                    line += f"  |  {game.away_probable_pitcher or 'TBD'} vs {game.home_probable_pitcher or 'TBD'}"
                line += "\n"
                requests.append(self._insert_text(idx, line))
                idx += len(line)
        else:
            line = "No games scheduled today.\n"
            requests.append(self._insert_text(idx, line))
            idx += len(line)

        requests.append(self._insert_text(idx, "\n"))
        idx += 1

        # ── Start / Sit ────────────────────────────────────────────────
        header = "Start / Sit Recommendations\n"
        requests.append(self._insert_text(idx, header))
        requests.append(self._style_heading(idx, idx + len(header) - 1, "HEADING_2"))
        idx += len(header)

        if report.start_sit:
            for decision in report.start_sit:
                emoji = "✅" if decision.decision == "START" else "🔴"
                line = (
                    f"{emoji} {decision.decision} — {decision.player.name} "
                    f"({decision.player.primary_position}, {decision.player.team})"
                    f"  [{decision.confidence} confidence]\n"
                )
                requests.append(self._insert_text(idx, line))
                idx += len(line)

                reason_line = f"    {decision.reason}\n"
                requests.append(self._insert_text(idx, reason_line))
                idx += len(reason_line)
        else:
            line = "No active decisions today.\n"
            requests.append(self._insert_text(idx, line))
            idx += len(line)

        requests.append(self._insert_text(idx, "\n"))
        idx += 1

        # ── Waiver Wire Pickups ─────────────────────────────────────────
        header = "Waiver Wire Recommendations\n"
        requests.append(self._insert_text(idx, header))
        requests.append(self._style_heading(idx, idx + len(header) - 1, "HEADING_2"))
        idx += len(header)

        if report.waiver_adds:
            for i, rec in enumerate(report.waiver_adds, 1):
                urgency_icon = {"high": "🔥", "medium": "⚡", "low": "💤"}
                icon = urgency_icon.get(rec.urgency.value, "")

                line = f"\n{i}. {icon} {rec.headline}\n"
                requests.append(self._insert_text(idx, line))
                idx += len(line)

                if rec.explanation:
                    exp_line = f"   {rec.explanation}\n"
                    requests.append(self._insert_text(idx, exp_line))
                    idx += len(exp_line)

                if rec.category_impact:
                    cats = ", ".join(
                        f"{cat}: {desc}" for cat, desc in rec.category_impact.items()
                    )
                    cat_line = f"   Category impact: {cats}\n"
                    requests.append(self._insert_text(idx, cat_line))
                    idx += len(cat_line)
        else:
            line = "No waiver moves recommended today.\n"
            requests.append(self._insert_text(idx, line))
            idx += len(line)

        requests.append(self._insert_text(idx, "\n"))
        idx += 1

        # ── Trade Suggestions ───────────────────────────────────────────
        header = "Trade Ideas\n"
        requests.append(self._insert_text(idx, header))
        requests.append(self._style_heading(idx, idx + len(header) - 1, "HEADING_2"))
        idx += len(header)

        if report.trade_targets:
            for i, rec in enumerate(report.trade_targets, 1):
                line = f"\n{i}. {rec.headline}\n"
                requests.append(self._insert_text(idx, line))
                idx += len(line)

                if rec.explanation:
                    exp_line = f"   {rec.explanation}\n"
                    requests.append(self._insert_text(idx, exp_line))
                    idx += len(exp_line)
        else:
            line = "No trade suggestions today — hold steady.\n"
            requests.append(self._insert_text(idx, line))
            idx += len(line)

        # ── Footer ──────────────────────────────────────────────────────
        requests.append(self._insert_text(idx, "\n"))
        idx += 1
        footer = (
            "───────────────────────────────────\n"
            "Generated by Fantasy Baseball ROTO Daily Manager\n"
            "Recommendations are based on statistical analysis and expert "
            "ROTO strategy. Always double-check injury reports before making moves.\n"
        )
        requests.append(self._insert_text(idx, footer))

        return requests

    def _format_report_plaintext(self, report: DailyReport) -> str:
        """Generate a plain-text version of the report."""
        lines = []
        lines.append("=" * 60)
        lines.append(f"  FANTASY BASEBALL DAILY REPORT — {today_display()}")
        lines.append("=" * 60)
        lines.append("")

        if report.executive_summary:
            lines.append("TODAY'S OVERVIEW")
            lines.append("-" * 40)
            lines.append(report.executive_summary)
            lines.append("")

        lines.append("TODAY'S MLB GAMES")
        lines.append("-" * 40)
        for game in report.games_today:
            pitchers = ""
            if game.away_probable_pitcher or game.home_probable_pitcher:
                pitchers = f"  |  {game.away_probable_pitcher or 'TBD'} vs {game.home_probable_pitcher or 'TBD'}"
            lines.append(f"  {game.matchup_label}{pitchers}")
        if not report.games_today:
            lines.append("  No games scheduled.")
        lines.append("")

        lines.append("START / SIT")
        lines.append("-" * 40)
        for d in report.start_sit:
            tag = "[START]" if d.decision == "START" else "[ SIT ]"
            lines.append(
                f"  {tag} {d.player.name} ({d.player.primary_position}, "
                f"{d.player.team}) — {d.confidence} confidence"
            )
            lines.append(f"         {d.reason}")
        lines.append("")

        lines.append("WAIVER WIRE PICKS")
        lines.append("-" * 40)
        for i, rec in enumerate(report.waiver_adds, 1):
            urgency = rec.urgency.value.upper()
            lines.append(f"  {i}. [{urgency}] {rec.headline}")
            if rec.explanation:
                # Word-wrap explanation
                lines.append(f"     {rec.explanation}")
            lines.append("")
        if not report.waiver_adds:
            lines.append("  No waiver moves recommended.")
        lines.append("")

        lines.append("TRADE IDEAS")
        lines.append("-" * 40)
        for i, rec in enumerate(report.trade_targets, 1):
            lines.append(f"  {i}. {rec.headline}")
            if rec.explanation:
                lines.append(f"     {rec.explanation}")
            lines.append("")
        if not report.trade_targets:
            lines.append("  No trade suggestions today.")
        lines.append("")

        lines.append("-" * 60)
        lines.append("Generated by Fantasy Baseball ROTO Daily Manager")
        lines.append(
            "Recommendations based on statistical analysis and expert ROTO strategy."
        )
        lines.append("Always check injury reports before making moves.")

        return "\n".join(lines)

    # ── Google Docs API Helpers ─────────────────────────────────────────

    def _create_google_doc(self, title: str) -> str:
        """Create an empty Google Doc and return its ID."""
        body = {"title": title}
        doc = self._docs_service.documents().create(body=body).execute()
        doc_id = doc["documentId"]
        log.debug(f"Created Google Doc: {doc_id}")
        return doc_id

    def _populate_doc(self, doc_id: str, requests: list[dict]) -> None:
        """Send batch update requests to populate the doc."""
        if not requests:
            return
        self._docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute()

    def _move_to_folder(self, file_id: str) -> None:
        """Move a file into the configured Drive folder."""
        folder_id = self._cfg["folder_id"]
        try:
            # Get current parent(s)
            file = self._drive_service.files().get(
                fileId=file_id, fields="parents"
            ).execute()
            previous_parents = ",".join(file.get("parents", []))

            self._drive_service.files().update(
                fileId=file_id,
                addParents=folder_id,
                removeParents=previous_parents,
                fields="id, parents",
            ).execute()
        except Exception as exc:
            log.warning(f"Could not move doc to folder: {exc}")

    @staticmethod
    def _insert_text(index: int, text: str) -> dict:
        return {
            "insertText": {
                "location": {"index": index},
                "text": text,
            }
        }

    @staticmethod
    def _style_heading(start: int, end: int, style: str) -> dict:
        return {
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": style},
                "fields": "namedStyleType",
            }
        }
