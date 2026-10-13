"""Delete server messages containing configured insults or profanity."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import struct
import tempfile
import time
import unicodedata
import uuid
import zipfile
import zlib
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands


logger = logging.getLogger(__name__)
WARNING_SECONDS = 5

# Keep this list to direct insults and profanity. Neutral animal names, identity
# terms, and context-dependent words such as "جاي" are intentionally excluded.
BAD_WORDS = (
    "غبي",
    "غبية",
    "أغبياء",
    "أحمق",
    "حمقاء",
    "جاهل",
    "جاهلة",
    "فاشل",
    "فاشلة",
    "تافه",
    "تافهة",
    "قذر",
    "قذرة",
    "وسخ",
    "وسخة",
    "حقير",
    "حقيرة",
    "ذليل",
    "ذليلة",
    "منحط",
    "منحطة",
    "زبالة",
    "زباله",
    "خرا",
    "خراء",
    "زق",
    "عبيط",
    "بليد",
    "معتوه",
    "متخلف",
    "متخلفة",
    "idiot",
    "stupid",
    "dumb",
    "fool",
    "moron",
    "loser",
    "لعنة عليك",
    "يلعن",
    "الله يلعن",
    "قبحك",
    "اخرس",
    "تفو",
    "تفو عليك",
)

MAX_CONFIGURED_TERMS = 250
MAX_TERM_LENGTH = 80
MAX_TERM_WORDS = 5
MIN_SERVER_GROUP_SIZE = 3
MAX_SERVER_GROUP_SIZE = 25
MAX_SERVER_GROUPS = 50
MAX_SERVER_GROUP_NAME_LENGTH = 50
SERVER_GROUP_HISTORY_PAGE_SIZE = 20
MAX_SERVER_GROUP_HISTORY_PAGE = 1_000_000
MAX_RECOVERY_FILE_BYTES = 8 * 1024 * 1024
# Leave headroom below Discord's ordinary attachment ceiling for request overhead.
MAX_COMPARISON_CSV_PART_BYTES = 7 * 1024 * 1024
MAX_COMPARISON_CSV_ARCHIVE_ATTACHMENT_BYTES = 8 * 1024 * 1024
COMPARISON_CSV_RETRY_TTL_SECONDS = 10 * 60
MAX_COMPARISON_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_COMPARISON_ARCHIVE_PARTS = 100
MAX_COMPARISON_CHECKSUM_DIAGNOSTIC_FILES = 8
_ARABIC_DIACRITICS = {
    chr(codepoint)
    for codepoint in (*range(0x064B, 0x0660), 0x0670, 0x0640)
}

_ARABIC_MARKS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_ARABIC_TRANSLATION = str.maketrans(
    {
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ؤ": "و",
        "ئ": "ي",
        "ى": "ي",
    }
)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _ARABIC_MARKS.sub("", text).translate(_ARABIC_TRANSLATION)
    text = _PUNCTUATION.sub(" ", text)
    return " ".join(text.split())


def _format_comparison_generated_at_utc(value: object) -> str | None:
    """Format an optional timezone-aware comparison timestamp as UTC."""
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        return (
            timestamp.astimezone(timezone.utc)
            .isoformat(timespec="auto")
            .replace("+00:00", "Z")
        )
    except (OverflowError, ValueError):
        return None


def _validate_server_comparison_report(payload: object) -> dict[str, object]:
    """Validate the read-only JSON format produced by /filter server-compare."""

    def terms(value: object, label: str) -> list[str]:
        if not isinstance(value, list) or any(
            not isinstance(term, str) or not term.strip() for term in value
        ):
            raise ValueError(f"قائمة {label} غير صالحة.")
        if any(len(term) > MAX_TERM_LENGTH for term in value):
            raise ValueError(
                f"أحد مصطلحات {label} يتجاوز الحد الأقصى للطول."
            )
        return value

    if (
        not isinstance(payload, dict)
        or payload.get("title") != "Server filter comparison"
    ):
        raise ValueError("هذا ليس تقرير مقارنة مرشح لغة معروفاً.")
    saved_group = payload.get("saved_group")
    if saved_group is not None and not isinstance(saved_group, str):
        raise ValueError("اسم المجموعة المحفوظة في التقرير غير صالح.")
    terms(payload.get("global_terms"), "الكلمات العامة")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise ValueError("قائمة السيرفرات في التقرير غير صالحة.")

    seen_ids: set[int] = set()
    for server in servers:
        if not isinstance(server, dict):
            raise ValueError("بيانات أحد السيرفرات غير صالحة.")
        server_id = server.get("id")
        if (
            isinstance(server_id, bool)
            or not isinstance(server_id, int)
            or server_id <= 0
            or server_id in seen_ids
        ):
            raise ValueError("معرّف أحد السيرفرات غير صالح أو مكرر.")
        seen_ids.add(server_id)
        if not isinstance(server.get("name"), str) or len(server["name"]) > 100:
            raise ValueError("اسم أحد السيرفرات غير صالح.")
        terms(server.get("effective_terms"), "الكلمات الفعالة")
        overrides = server.get("overrides")
        if not isinstance(overrides, dict):
            raise ValueError("تعديلات أحد السيرفرات غير صالحة.")
        for status in ("effective", "retained"):
            status_overrides = overrides.get(status)
            if not isinstance(status_overrides, dict):
                raise ValueError("تفاصيل تعديلات أحد السيرفرات غير مكتملة.")
            for override_type in ("additions", "exclusions"):
                terms(
                    status_overrides.get(override_type),
                    "الإضافات والاستثناءات",
                )
    server_count = len(seen_ids)
    if not MIN_SERVER_GROUP_SIZE <= server_count <= MAX_SERVER_GROUP_SIZE:
        raise ValueError(
            "يجب أن يحتوي التقرير على مقارنة تشمل من "
            f"{MIN_SERVER_GROUP_SIZE} إلى {MAX_SERVER_GROUP_SIZE} سيرفراً "
            "مختلفاً؛ عدد السيرفرات الموجودة: "
            f"{server_count}."
        )
    return payload


def _format_archived_saved_group_label(
    report: dict[str, object],
    report_label: str | None = None,
) -> str | None:
    saved_group = report.get("saved_group")
    if not isinstance(saved_group, str) or not saved_group.strip():
        return None
    safe_name = discord.utils.escape_markdown(
        saved_group.replace("\r", " ").replace("\n", " ")
    )
    label = (
        f"المجموعة المحفوظة في {report_label}"
        if report_label is not None
        else "المجموعة المحفوظة"
    )
    return f"{label}: {safe_name}"


def _diff_archived_server_comparison_reports(
    first_report: dict[str, object],
    second_report: dict[str, object],
) -> str:
    """Format a read-only diff between two validated archived JSON reports."""

    def diff_terms(
        first: list[str],
        second: list[str],
    ) -> tuple[list[str], list[str]]:
        first_keys = {_normalize(term) for term in first}
        second_keys = {_normalize(term) for term in second}
        added = [
            term for term in second if _normalize(term) not in first_keys
        ]
        removed = [
            term for term in first if _normalize(term) not in second_keys
        ]
        return added, removed

    def format_terms(terms: list[str]) -> str:
        return ", ".join(
            discord.utils.escape_markdown(term) for term in terms
        ) or "لا شيء"

    def append_change(
        lines: list[str],
        label: str,
        first: list[str],
        second: list[str],
    ) -> None:
        added, removed = diff_terms(first, second)
        compact_line = (
            f"{label}: أُضيفت ({len(added)}): {format_terms(added)}؛ "
            f"أُزيلت ({len(removed)}): {format_terms(removed)}"
        )
        if len(compact_line) <= 1500:
            lines.append(compact_line)
            return

        lines.append(f"{label}:")
        for change_label, changed_terms in (
            ("أُضيفت", added),
            ("أُزيلت", removed),
        ):
            if not changed_terms:
                lines.append(f"  {change_label} (0): لا شيء")
                continue
            lines.append(f"  {change_label} ({len(changed_terms)}):")
            current = ""
            for term in changed_terms:
                escaped_term = discord.utils.escape_markdown(term)
                next_text = (
                    escaped_term if not current else f"{current}, {escaped_term}"
                )
                if len(next_text) > 1450 and current:
                    lines.append(f"    {current}")
                    current = escaped_term
                else:
                    current = next_text
            if current:
                lines.append(f"    {current}")

    first_servers = {server["id"]: server for server in first_report["servers"]}
    second_servers = {server["id"]: server for server in second_report["servers"]}
    first_ids = set(first_servers)
    second_ids = set(second_servers)
    first_only_ids = first_ids - second_ids
    second_only_ids = second_ids - first_ids

    def timestamp_label(report: dict[str, object]) -> str:
        return (
            _format_comparison_generated_at_utc(report.get("generated_at_utc"))
            or "غير متاح (مفقود أو غير صالح)"
        )

    if first_ids == second_ids:
        scope_line = f"نطاق السيرفرات متطابق: {len(first_ids)} سيرفرات."
    else:
        scope_line = (
            f"نطاق السيرفرات مختلف: {len(first_ids)} في التقرير الأول، "
            f"و{len(second_ids)} في التقرير الثاني."
        )

    group_labels = [
        label
        for report, report_label in (
            (first_report, "التقرير الأول"),
            (second_report, "التقرير الثاني"),
        )
        if (
            label := _format_archived_saved_group_label(
                report,
                report_label,
            )
        )
        is not None
    ]
    lines = [
        "مقارنة تقريرين محفوظين — قراءة فقط؛ لم تُقرأ إعدادات حية "
        "ولم تُغيّر إعدادات المرشح.",
        f"وقت التقرير الأول (خط الأساس، UTC): {timestamp_label(first_report)}",
        f"وقت التقرير الثاني (UTC): {timestamp_label(second_report)}",
        *group_labels,
        scope_line,
        "",
    ]
    append_change(
        lines,
        "القائمة العامة",
        first_report["global_terms"],
        second_report["global_terms"],
    )

    ordered_ids = [server["id"] for server in first_report["servers"]]
    ordered_ids.extend(
        server["id"]
        for server in second_report["servers"]
        if server["id"] not in first_ids
    )
    for server_id in ordered_ids:
        first_server = first_servers.get(server_id)
        second_server = second_servers.get(server_id)
        if first_server is None or second_server is None:
            server = second_server or first_server
            server_name = discord.utils.escape_markdown(server["name"])
            report_label = "الأول" if server_id in first_only_ids else "الثاني"
            lines.extend(
                [
                    "",
                    f"**{server_name} (ID: {server_id})**",
                    f"موجود في التقرير {report_label} فقط؛ لا توجد لقطة مقابلة "
                    "لحساب التغييرات.",
                ]
            )
            continue

        first_name = discord.utils.escape_markdown(first_server["name"])
        second_name = discord.utils.escape_markdown(second_server["name"])
        label = f"{second_name} (ID: {server_id})"
        if first_name != second_name:
            label += f" — كان اسمه {first_name}"
        lines.extend(["", f"**{label}**"])
        append_change(
            lines,
            "الكلمات الفعالة",
            first_server["effective_terms"],
            second_server["effective_terms"],
        )
        for status, status_label in (
            ("effective", "التعديلات الفعالة"),
            ("retained", "التعديلات المحفوظة غير المؤثرة"),
        ):
            for override_type, override_label in (
                ("additions", "الإضافات"),
                ("exclusions", "الاستثناءات"),
            ):
                append_change(
                    lines,
                    f"{status_label} — {override_label}",
                    first_server["overrides"][status][override_type],
                    second_server["overrides"][status][override_type],
                )

    return "\n".join(lines)


_SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _spreadsheet_safe_text(value: str) -> str:
    """Prefix spreadsheet-sensitive text with an apostrophe for CSV exports."""
    if value.startswith("'") or value.lstrip().startswith(
        _SPREADSHEET_FORMULA_PREFIXES
    ):
        return f"'{value}"
    return value


def _restore_spreadsheet_safe_text(value: str) -> str:
    """Remove the export-only apostrophe from formula-safe CSV text."""
    if len(value) > 1 and value.startswith("'") and (
        value.startswith("''")
        or value[1:].lstrip().startswith(_SPREADSHEET_FORMULA_PREFIXES)
    ):
        return value[1:]
    return value


class ComparisonCsvRowTooLarge(ValueError):
    """Raised when one complete comparison CSV row cannot fit in a part."""


def _encode_comparison_csv_row(row: Iterable[object]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerow(row)
    return buffer.getvalue().encode("utf-8")


def _split_comparison_csv_rows(
    header: tuple[object, ...],
    rows: Iterable[tuple[object, ...]],
    *,
    max_part_bytes: int,
) -> list[bytes]:
    """Encode ordered CSV rows into BOM-prefixed, header-repeating parts."""
    bom = b"\xef\xbb\xbf"
    header_bytes = bom + _encode_comparison_csv_row(header)
    if len(header_bytes) > max_part_bytes:
        raise ComparisonCsvRowTooLarge("CSV header exceeds the part size.")

    parts: list[bytes] = []
    current_part = bytearray(header_bytes)
    for row in rows:
        row_bytes = _encode_comparison_csv_row(row)
        if len(header_bytes) + len(row_bytes) > max_part_bytes:
            raise ComparisonCsvRowTooLarge("CSV row exceeds the part size.")
        if len(current_part) + len(row_bytes) > max_part_bytes:
            parts.append(bytes(current_part))
            current_part = bytearray(header_bytes)
        current_part.extend(row_bytes)

    parts.append(bytes(current_part))
    return parts


def _build_comparison_csv_archive(
    parts: Iterable[tuple[str, bytes]],
) -> bytes | None:
    """Bundle ordered CSV parts when the ZIP is safe to upload and review."""
    part_list = list(parts)
    if len(part_list) < 2 or sum(len(contents) for _, contents in part_list) > (
        MAX_COMPARISON_ARCHIVE_BYTES
    ):
        return None

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for filename, contents in part_list:
            archive.writestr(filename, contents)

    archive_contents = buffer.getvalue()
    if len(archive_contents) > MAX_COMPARISON_CSV_ARCHIVE_ATTACHMENT_BYTES:
        return None
    return archive_contents


class ComparisonCsvRetryView(discord.ui.View):
    """Private, time-limited retry for only the failed and unsent CSV parts."""

    def __init__(
        self,
        pending_parts: list[tuple[int, str, bytes]],
        part_count: int,
        developer_id: int,
    ):
        super().__init__(timeout=COMPARISON_CSV_RETRY_TTL_SECONDS)
        self.pending_parts = list(pending_parts)
        self.part_count = part_count
        self.developer_id = developer_id
        self.expires_at = time.monotonic() + COMPARISON_CSV_RETRY_TTL_SECONDS
        self.finished = False
        self.message = None
        self._retry_lock = asyncio.Lock()

    def _discard(self) -> None:
        self.pending_parts.clear()
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(
        label="إعادة إرسال الأجزاء المتبقية",
        style=discord.ButtonStyle.primary,
    )
    async def retry(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "إعادة المحاولة متاحة لمطور البوت الذي بدأ التصدير فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self.finished or time.monotonic() >= self.expires_at:
            self._discard()
            await interaction.response.send_message(
                "انتهت مهلة إعادة المحاولة وحُذفت الأجزاء المؤقتة. "
                "شغّل المقارنة مجدداً لإنشاء تصدير جديد. لم تتغير إعدادات المرشح.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self._retry_lock.locked():
            await interaction.response.send_message(
                "تجري إعادة إرسال هذه الأجزاء بالفعل.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        async with self._retry_lock:
            if self.finished:
                await interaction.response.send_message(
                    "انتهت مهلة إعادة المحاولة وحُذفت الأجزاء المؤقتة. "
                    "شغّل المقارنة مجدداً لإنشاء تصدير جديد.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            button.disabled = True
            for child in self.children:
                if child is not button:
                    child.disabled = True
            await interaction.response.edit_message(
                content="جارٍ إعادة إرسال الأجزاء المتبقية من التصدير نفسه.",
                view=self,
            )
            pending = list(self.pending_parts)
            for index, (part_number, filename, contents) in enumerate(pending):
                report_file = discord.File(
                    io.BytesIO(contents),
                    filename=filename,
                )
                try:
                    await interaction.followup.send(
                        f"أُعيد إرسال الجزء {part_number} من {self.part_count} "
                        "من تقرير المقارنة الخاص.",
                        file=report_file,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException as error:
                    self.pending_parts = pending[index:]
                    for child in self.children:
                        child.disabled = False
                    if error.status == 413 or error.code == 40005:
                        failure = "رفض Discord هذا الجزء بسبب حد حجم الملفات."
                    else:
                        failure = "تعذر إرسال هذا الجزء إلى Discord."
                    await interaction.message.edit(
                        content=(
                            f"{failure} لم تُعد إرسال الأجزاء السابقة لهذه المحاولة؛ "
                            f"توقف الإرسال عند الجزء {part_number} من "
                            f"{self.part_count}. يمكنك المحاولة مجدداً لفترة قصيرة "
                            "باستخدام الزر أدناه. لم تتغير إعدادات المرشح."
                        ),
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

            self._discard()
            await interaction.message.edit(
                content=(
                    "اكتملت إعادة إرسال الأجزاء التي لم تصل سابقاً. "
                    "احتفظ بالأجزاء مع الملفات التي أُرسلت قبل ذلك؛ "
                    "لم تتغير إعدادات المرشح."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @discord.ui.button(
        label="حذف الأجزاء المتبقية",
        style=discord.ButtonStyle.danger,
    )
    async def discard_pending(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "حذف الأجزاء متاح لمطور البوت الذي بدأ التصدير فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self._retry_lock.locked():
            await interaction.response.send_message(
                "تجري إعادة إرسال الأجزاء الآن. يمكنك حذف المتبقي بعد انتهاء المحاولة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if self.finished:
            await interaction.response.send_message(
                "لم تعد هناك أجزاء متبقية لحذفها.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if time.monotonic() >= self.expires_at:
            self._discard()
            await interaction.response.edit_message(
                content=(
                    "انتهت مهلة إعادة المحاولة وحُذفت الأجزاء المؤقتة. "
                    "شغّل المقارنة مجدداً لإنشاء تصدير جديد. لم تتغير إعدادات المرشح."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        async with self._retry_lock:
            if self.finished:
                await interaction.response.send_message(
                    "لم تعد هناك أجزاء متبقية لحذفها.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            self._discard()
            await interaction.response.edit_message(
                content=(
                    "حُذفت الأجزاء المتبقية من الذاكرة. الأجزاء التي أُرسلت سابقاً "
                    "لم تتأثر، ولم تتغير إعدادات المرشح. شغّل مقارنة جديدة لتصدير كامل."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def on_timeout(self) -> None:
        async with self._retry_lock:
            if self.finished:
                return
            self._discard()
            if self.message is None:
                return
            try:
                await self.message.edit(
                    content=(
                        "انتهت مهلة إعادة المحاولة وحُذفت الأجزاء المؤقتة. "
                        "شغّل المقارنة مجدداً لإنشاء تصدير جديد. "
                        "لم تتغير إعدادات المرشح."
                    ),
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.warning("Could not update an expired CSV comparison retry.")


def _parse_server_comparison_csv(contents: bytes) -> dict[str, object]:
    """Convert the CSV export from /filter server-compare to its JSON shape."""
    expected_headers = [
        "record_type",
        "saved_group_name",
        "server_id",
        "server_name",
        "term",
        "generated_at_utc",
    ]
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("تعذر فك ترميز CSV بترميز UTF-8.") from error

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader)
    except StopIteration as error:
        raise ValueError("رأس CSV مفقود: الملف فارغ أو بلا عناوين أعمدة.") from error
    except csv.Error as error:
        raise ValueError("رأس CSV غير صالح أو تعذر قراءته.") from error
    if headers != expected_headers:
        raise ValueError("رأس CSV لا يطابق عناوين تقرير مقارنة السيرفرات.")

    global_terms: list[str] = []
    servers: dict[int, dict[str, object]] = {}
    csv_server_names: dict[int, str] = {}
    generated_at_utc: str | None = None
    saved_group_name: str | None = None
    row_count = 0

    def parse_server_id(value: str) -> int:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
            raise ValueError("معرّف أحد السيرفرات غير صالح.")
        return int(value)

    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as error:
            raise ValueError(
                f"سجل CSV رقم {row_count + 1} غير صالح: تعذر قراءة صيغة CSV."
            ) from error

        row_count += 1
        try:
            if len(row) != len(expected_headers):
                raise ValueError("أحد صفوف CSV غير مكتمل أو يحتوي أعمدة زائدة.")
            (
                record_type,
                group_name,
                server_id_text,
                server_name,
                term,
                timestamp,
            ) = row
            if (
                not timestamp
                or _format_comparison_generated_at_utc(timestamp) is None
            ):
                raise ValueError("وقت إنشاء تقرير CSV مفقود أو غير صالح.")
            if generated_at_utc is None:
                generated_at_utc = timestamp
                saved_group_name = group_name
            elif timestamp != generated_at_utc or group_name != saved_group_name:
                raise ValueError("بيانات تقرير CSV غير متطابقة بين الصفوف.")

            if record_type in ("global_term", "shared_effective_term"):
                if server_id_text or server_name or not term.strip():
                    raise ValueError("صف مصطلح عام في CSV غير صالح.")
                if record_type == "global_term":
                    global_terms.append(_restore_spreadsheet_safe_text(term))
                continue

            if record_type == "server":
                server_id = parse_server_id(server_id_text)
                if server_id in servers or term:
                    raise ValueError("صف تعريف سيرفر في CSV غير صالح أو مكرر.")
                servers[server_id] = {
                    "id": server_id,
                    "name": server_name,
                    "effective_terms": [],
                    "overrides": {
                        "effective": {"additions": [], "exclusions": []},
                        "retained": {"additions": [], "exclusions": []},
                    },
                }
                csv_server_names[server_id] = server_name
                continue

            server_term_types = {
                "effective_term": ("effective_terms", None, None),
                "effective_addition": ("overrides", "effective", "additions"),
                "effective_exclusion": ("overrides", "effective", "exclusions"),
                "retained_addition": ("overrides", "retained", "additions"),
                "retained_exclusion": ("overrides", "retained", "exclusions"),
            }
            type_mapping = server_term_types.get(record_type)
            if type_mapping is None or not term.strip():
                raise ValueError("نوع سجل أو مصطلح في CSV غير صالح.")
            server_id = parse_server_id(server_id_text)
            server = servers.get(server_id)
            if server is None or server_name != csv_server_names[server_id]:
                raise ValueError("مصطلح CSV لا يطابق تعريف سيرفر معروفاً.")
            term = _restore_spreadsheet_safe_text(term)
            target, status, override_type = type_mapping
            if target == "effective_terms":
                server["effective_terms"].append(term)
            else:
                server["overrides"][status][override_type].append(term)
        except ValueError as error:
            raise ValueError(
                f"سجل CSV رقم {row_count} غير صالح: {error}"
            ) from error

    if row_count == 0:
        raise ValueError("تقرير CSV لا يحتوي أي صفوف.")

    # Remove the export-only spreadsheet-safety prefix when reconstructing names.
    for server in servers.values():
        server["name"] = _restore_spreadsheet_safe_text(server["name"])

    return _validate_server_comparison_report(
        {
            "title": "Server filter comparison",
            "generated_at_utc": generated_at_utc,
            "saved_group": (
                _restore_spreadsheet_safe_text(saved_group_name)
                if saved_group_name
                else None
            ),
            "global_terms": global_terms,
            "servers": list(servers.values()),
        }
    )


def _parse_server_comparison_csv_file(
    contents: bytes,
    filename: str,
) -> dict[str, object]:
    """Validate filename-carried integrity metadata before parsing a CSV file."""
    checksum_prefix = "filter-server-comparison-sha256-"
    checksum_match = re.fullmatch(
        r"filter-server-comparison-sha256-([0-9a-f]{64})\.csv",
        filename,
    )
    if filename.startswith(checksum_prefix):
        if checksum_match is None:
            raise ValueError("اسم ملف CSV أو بصمته غير صالح.")
        expected_digest = checksum_match.group(1)
        actual_digest = hashlib.sha256(contents).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(
                "محتوى ملف CSV لا يطابق بصمته المحددة في اسم الملف "
                f"(الملف: {filename})."
            )
    return _parse_server_comparison_csv(contents)


def _parse_server_comparison_csv_parts(
    parts: list[tuple[str, bytes]],
) -> dict[str, object]:
    """Validate and combine ordered CSV parts from one comparison export."""
    expected_headers = (
        "record_type",
        "saved_group_name",
        "server_id",
        "server_name",
        "term",
        "generated_at_utc",
    )
    header_bytes = b"\xef\xbb\xbf" + _encode_comparison_csv_row(expected_headers)
    if not 2 <= len(parts) <= MAX_COMPARISON_ARCHIVE_PARTS:
        raise ValueError("يجب أن يحتوي ZIP على جزأين إلى 100 جزء من التقرير.")

    parsed_parts: list[
        tuple[int, str, bytes, str | None, str | None, int]
    ] = []
    expected_total: int | None = None
    seen_names: set[str] = set()
    for filename, contents in parts:
        try:
            if filename in seen_names:
                raise ValueError("يحتوي ZIP على ملف جزء مكرر.")
            seen_names.add(filename)
            match = re.fullmatch(
                r"filter-server-comparison-part-([0-9]{3,})-of-"
                r"([0-9]{3,})(?:-sha256-([0-9a-f]{64})"
                r"(?:-part-sha256-([0-9a-f]{64}))?)?\.csv",
                filename,
            )
            if match is None:
                raise ValueError("اسم أحد أجزاء CSV غير صالح أو تم تغييره.")
            part_number, part_total, digest, member_digest = match.groups()
            part_number_int = int(part_number)
            part_total_int = int(part_total)
            if (
                filename
                != "filter-server-comparison-part-"
                f"{part_number_int:03d}-of-{part_total_int:03d}"
                f"{f'-sha256-{digest}' if digest else ''}"
                f"{f'-part-sha256-{member_digest}' if member_digest else ''}.csv"
            ):
                raise ValueError("اسم أحد أجزاء CSV غير صالح أو تم تغييره.")
            if expected_total is None:
                expected_total = part_total_int
            if part_total_int != expected_total:
                raise ValueError("عدد الأجزاء لا يتطابق بين أسماء الملفات.")
            if not 1 <= part_number_int <= part_total_int:
                raise ValueError("ترقيم أحد أجزاء CSV غير صالح.")
            if len(contents) > MAX_COMPARISON_CSV_PART_BYTES:
                raise ValueError("أحد أجزاء CSV يتجاوز الحد المسموح.")
            if (
                member_digest is not None
                and hashlib.sha256(contents).hexdigest() != member_digest
            ):
                raise ValueError("محتوى أحد أجزاء CSV لا يطابق بصمته الفردية.")
            if not contents.startswith(header_bytes):
                raise ValueError("عناوين أعمدة CSV لا تتطابق بين الأجزاء.")
            text = contents.decode("utf-8-sig")
            reader = csv.reader(io.StringIO(text, newline=""), strict=True)
            if tuple(next(reader)) != expected_headers:
                raise ValueError("عناوين أعمدة CSV لا تتطابق بين الأجزاء.")
            record_count = 0
            try:
                first_record = next(reader, None)
            except csv.Error:
                # Defer malformed-record reporting so the combined parser can
                # attach the record's number across all CSV parts.
                record_count = 1
            else:
                if first_record is None:
                    raise ValueError("أحد أجزاء CSV لا يحتوي صفوف التقرير.")
                record_count = 1
                while True:
                    try:
                        next(reader)
                    except StopIteration:
                        break
                    except csv.Error:
                        # Include the malformed record itself in this part's
                        # range so the combined parser can identify its file.
                        record_count += 1
                        break
                    record_count += 1
            parsed_parts.append(
                (
                    part_number_int,
                    filename,
                    contents,
                    digest,
                    member_digest,
                    record_count,
                )
            )
        except StopIteration as error:
            raise ValueError(
                f"أحد أجزاء CSV فارغ أو بلا عناوين أعمدة. (الجزء: {filename})"
            ) from error
        except UnicodeDecodeError as error:
            raise ValueError(
                f"تعذر فك ترميز أحد الأجزاء بترميز UTF-8. (الجزء: {filename})"
            ) from error
        except ValueError as error:
            raise ValueError(f"{error} (الجزء: {filename})") from error

    if expected_total != len(parsed_parts):
        raise ValueError("هناك أجزاء مفقودة من تقرير CSV.")
    expected_order = list(range(1, len(parsed_parts) + 1))
    if [part_number for part_number, _, _, _, _, _ in parsed_parts] != expected_order:
        raise ValueError("ترتيب أجزاء CSV غير صحيح أو يحتوي أجزاء مكررة.")

    member_digest_presence = {
        member_digest is not None
        for _, _, _, _, member_digest, _ in parsed_parts
    }
    if len(member_digest_presence) != 1:
        missing_member_digests = [
            filename
            for _, filename, _, _, member_digest, _ in parsed_parts
            if member_digest is None
        ]
        raise ValueError(
            "بصمة أحد أجزاء CSV مفقودة من الملفات التالية: "
            f"{'، '.join(missing_member_digests)}."
        )

    digests = {digest for _, _, _, digest, _, _ in parsed_parts}
    if len(digests) != 1:
        digest_counts = {
            digest: sum(
                part_digest == digest
                for _, _, _, part_digest, _, _ in parsed_parts
            )
            for digest in digests
        }
        most_common_count = max(digest_counts.values())
        most_common_digests = {
            digest
            for digest, count in digest_counts.items()
            if count == most_common_count
        }
        if len(most_common_digests) == 1:
            inconsistent_names = [
                filename
                for _, filename, _, part_digest, _, _ in parsed_parts
                if part_digest not in most_common_digests
            ]
            detail = "تختلف تسمية بصمتها عن بقية الأجزاء في الملف التالي: "
        else:
            inconsistent_names = [
                filename for _, filename, _, _, _, _ in parsed_parts
            ]
            detail = "تختلف تسميات البصمات بين هذه الملفات: "
        shown_names = inconsistent_names[
            :MAX_COMPARISON_CHECKSUM_DIAGNOSTIC_FILES
        ]
        if len(inconsistent_names) > len(shown_names):
            shown_names.append(
                f"… وهناك {len(inconsistent_names) - len(shown_names)} أجزاء أخرى"
            )
        raise ValueError(
            "بصمات أجزاء CSV غير متطابقة؛ "
            f"{detail}{'، '.join(shown_names)}."
        )
    digest = next(iter(digests))
    member_names = [filename for _, filename, _, _, _, _ in parsed_parts]
    shown_names = member_names[:MAX_COMPARISON_CHECKSUM_DIAGNOSTIC_FILES]
    if len(member_names) > len(shown_names):
        shown_names.append(
            f"… وهناك {len(member_names) - len(shown_names)} أجزاء أخرى"
        )
    combined_contents = parsed_parts[0][2] + b"".join(
        contents[len(header_bytes) :]
        for _, _, contents, _, _, _ in parsed_parts[1:]
    )
    if digest is not None and hashlib.sha256(combined_contents).hexdigest() != digest:
        raise ValueError(
            "محتوى التقرير المجمع لا يطابق بصمته؛ تعذر تحديد الجزء المسؤول "
            f"من بين الملفات التالية: {'، '.join(shown_names)}."
        )
    try:
        return _parse_server_comparison_csv(combined_contents)
    except ValueError as error:
        record_match = re.search(r"سجل CSV رقم ([0-9]+)", str(error))
        if record_match is None:
            raise
        record_number = int(record_match.group(1))
        preceding_records = 0
        for _, filename, _, _, _, record_count in parsed_parts:
            if record_number <= preceding_records + record_count:
                raise ValueError(
                    f"{error} (الجزء: {filename})"
                ) from error
            preceding_records += record_count
        raise


def _parse_server_comparison_csv_archive(contents: bytes) -> dict[str, object]:
    """Read and validate a ZIP containing all ordered parts of one CSV report."""
    try:
        with zipfile.ZipFile(io.BytesIO(contents)) as archive:
            members = archive.infolist()
            if not 2 <= len(members) <= MAX_COMPARISON_ARCHIVE_PARTS:
                raise ValueError("يجب أن يحتوي ZIP على جزأين إلى 100 جزء من التقرير.")
            expanded_size = 0
            parts: list[tuple[str, bytes]] = []
            for member in members:
                try:
                    if member.is_dir():
                        raise ValueError("يجب أن يحتوي ZIP على أجزاء CSV فقط.")
                    if member.file_size > MAX_COMPARISON_CSV_PART_BYTES:
                        raise ValueError(
                            "أحد أجزاء CSV داخل ZIP يتجاوز الحد المسموح."
                        )
                    expanded_size += member.file_size
                    if expanded_size > MAX_COMPARISON_ARCHIVE_BYTES:
                        raise ValueError(
                            "حجم الأجزاء بعد فك ZIP يتجاوز الحد المسموح."
                        )
                    with archive.open(member) as part_file:
                        part_contents = part_file.read(
                            MAX_COMPARISON_CSV_PART_BYTES + 1
                        )
                    if len(part_contents) != member.file_size:
                        raise ValueError(
                            "حجم أحد أجزاء CSV لا يطابق بيانات ZIP."
                        )
                    parts.append((member.filename, part_contents))
                except (
                    EOFError,
                    OSError,
                    RuntimeError,
                    struct.error,
                    zipfile.BadZipFile,
                    zlib.error,
                ) as error:
                    raise ValueError(
                        "تعذر قراءة ZIP أو أحد أجزاء CSV داخله. "
                        f"(الجزء: {member.filename})"
                    ) from error
                except ValueError as error:
                    raise ValueError(
                        f"{error} (الجزء: {member.filename})"
                    ) from error
    except ValueError:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        struct.error,
        zipfile.BadZipFile,
        zlib.error,
    ) as error:
        raise ValueError("تعذر قراءة ZIP أو أحد أجزاء CSV داخله.") from error
    return _parse_server_comparison_csv_parts(parts)


class ProfanityTermError(ValueError):
    """Raised when a filtered term is invalid or cannot be changed."""


def _search_history_entries(
    history: list[dict[str, object]],
    term: str | None,
    action: str | None,
    allowed_actions: set[str],
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    developer_id: int | None = None,
    include_index: bool = False,
) -> list[dict[str, object]]:
    if developer_id is not None and (
        isinstance(developer_id, bool)
        or not isinstance(developer_id, int)
        or developer_id <= 0
    ):
        raise ProfanityTermError("معرّف المطور المحدد للبحث غير صالح.")

    if term is not None:
        if not isinstance(term, str):
            raise ProfanityTermError("اكتب كلمة أو جزءاً من كلمة للبحث.")
        term = term.strip()
        if not term:
            raise ProfanityTermError("اكتب كلمة أو جزءاً من كلمة للبحث.")
        if len(term) > MAX_TERM_LENGTH:
            raise ProfanityTermError(
                f"لا يمكن أن يتجاوز نص البحث {MAX_TERM_LENGTH} حرفاً."
            )
        query = _normalize(term)
        if not query:
            raise ProfanityTermError("اكتب كلمة أو جزءاً من كلمة صالحاً للبحث.")
    else:
        query = None

    if action is not None and (
        not isinstance(action, str) or action not in allowed_actions
    ):
        raise ProfanityTermError("نوع الإجراء المحدد للبحث غير صالح.")

    def parse_date(value: str | None, label: str) -> date | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProfanityTermError(
                f"تاريخ {label} غير صالح. استخدم الصيغة YYYY-MM-DD."
            )
        normalized = value.strip()
        try:
            parsed = date.fromisoformat(normalized)
        except ValueError as error:
            raise ProfanityTermError(
                f"تاريخ {label} غير صالح. استخدم الصيغة YYYY-MM-DD."
            ) from error
        if parsed.isoformat() != normalized:
            raise ProfanityTermError(
                f"تاريخ {label} غير صالح. استخدم الصيغة YYYY-MM-DD."
            )
        return parsed

    start = parse_date(start_date, "البداية")
    end = parse_date(end_date, "النهاية")
    if start is not None and end is not None and start > end:
        raise ProfanityTermError("تاريخ البداية يجب ألا يكون بعد تاريخ النهاية.")

    results = []
    for history_index, entry in enumerate(history):
        if start is not None or end is not None:
            timestamp = datetime.fromisoformat(
                str(entry.get("timestamp", "")).replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None:
                continue
            entry_date = timestamp.astimezone(timezone.utc).date()
            if start is not None and entry_date < start:
                continue
            if end is not None and entry_date > end:
                continue
        if action is not None and entry.get("action") != action:
            continue
        if developer_id is not None and entry.get("developer_id") != developer_id:
            continue
        matching_added = []
        matching_removed = []
        if query is not None:
            entry_terms = []
            recorded_term = entry.get("term")
            if isinstance(recorded_term, str):
                entry_terms.append(recorded_term)
            for key in ("added", "removed"):
                recorded_terms = entry.get(key, [])
                if isinstance(recorded_terms, list):
                    matching_terms = [
                        value
                        for value in recorded_terms
                        if isinstance(value, str) and query in _normalize(value)
                    ]
                    if key == "added":
                        matching_added = matching_terms
                    else:
                        matching_removed = matching_terms
                    entry_terms.extend(
                        value for value in recorded_terms if isinstance(value, str)
                    )
            if not any(query in _normalize(value) for value in entry_terms):
                continue
        result = deepcopy(entry)
        if include_index:
            result["history_index"] = history_index
        if query is not None and entry.get("action") in {"reset", "restore"}:
            result["matched_added"] = matching_added
            result["matched_removed"] = matching_removed
        results.append(result)
    return results


class ProfanityTermStore:
    """Persist developer-managed additions and removals from the built-in list."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        base_terms: Iterable[str] = BAD_WORDS,
    ):
        self.path = (
            Path(path)
            if path
            else Path(__file__).parent / "data" / "blocked_terms.json"
        )
        self.base_terms = tuple(base_terms)
        self._added: list[str] = []
        self._removed: list[str] = []
        self._history: list[dict[str, object]] = []
        self._load()

    @staticmethod
    def _clean_term(term: str) -> str:
        if not isinstance(term, str):
            raise ProfanityTermError("اكتب كلمة أو عبارة نصية.")
        term = term.strip()
        if not term or len(term) > MAX_TERM_LENGTH:
            raise ProfanityTermError(
                f"يجب أن يكون طول الكلمة أو العبارة بين حرفين و{MAX_TERM_LENGTH} حرفاً."
            )
        if any(
            not (
                character.isalpha()
                or character == " "
                or character in _ARABIC_DIACRITICS
            )
            for character in term
        ):
            raise ProfanityTermError("استخدم الحروف والمسافات فقط، دون رموز أو أرقام.")
        normalized = _normalize(term)
        if len(normalized.replace(" ", "")) < 2:
            raise ProfanityTermError("يجب أن تتكون الكلمة أو العبارة من حرفين على الأقل.")
        if len(normalized.split()) > MAX_TERM_WORDS:
            raise ProfanityTermError(
                f"لا يمكن أن تتجاوز العبارة {MAX_TERM_WORDS} كلمات."
            )
        return normalized

    @staticmethod
    def _key(term: str) -> str:
        return _normalize(term)

    def list_terms(self) -> list[str]:
        removed = {self._key(term) for term in self._removed}
        result = [term for term in self.base_terms if self._key(term) not in removed]
        result.extend(self._added)
        return result

    @staticmethod
    def _validate_developer_id(developer_id: int) -> None:
        if (
            isinstance(developer_id, bool)
            or not isinstance(developer_id, int)
            or developer_id <= 0
        ):
            raise ProfanityTermError("تعذر التحقق من هوية المطور.")

    def list_history(self) -> list[dict[str, object]]:
        """Return a copy of the persisted changes to the configured word list."""
        return [entry.copy() for entry in self._history]

    def batch_history_indexes(self, history_index: int) -> list[int]:
        """Return the source change indexes for a batch, or an empty list."""
        if (
            isinstance(history_index, bool)
            or not isinstance(history_index, int)
            or history_index < 0
            or history_index >= len(self._history)
        ):
            return []
        selected = self._history[history_index]
        if selected.get("action") not in {"add", "remove"}:
            return []

        batch_id = selected.get("batch_id")
        if batch_id is not None:
            start = history_index
            while (
                start > 0
                and self._history[start - 1].get("batch_id") == batch_id
            ):
                start -= 1
            end = history_index + 1
            while (
                end < len(self._history)
                and self._history[end].get("batch_id") == batch_id
            ):
                end += 1
            return list(range(start, end))

        # Older batch records have no explicit marker, but apply_changes wrote
        # their entries contiguously with one shared timestamp and developer.
        timestamp = selected.get("timestamp")
        developer_id = selected.get("developer_id")

        def belongs_to_legacy_batch(entry: dict[str, object]) -> bool:
            return (
                entry.get("action") in {"add", "remove"}
                and "batch_id" not in entry
                and entry.get("timestamp") == timestamp
                and entry.get("developer_id") == developer_id
            )

        start = history_index
        while start > 0 and belongs_to_legacy_batch(self._history[start - 1]):
            start -= 1
        end = history_index + 1
        while end < len(self._history) and belongs_to_legacy_batch(self._history[end]):
            end += 1
        return list(range(start, end)) if end - start > 1 else []

    def batch_number_for_history_index(self, history_index: int) -> int | None:
        """Return the one-based history number that identifies a batch."""
        indexes = self.batch_history_indexes(history_index)
        return indexes[0] + 1 if indexes else None

    def search_history(
        self,
        term: str | None = None,
        action: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        include_index: bool = False,
    ) -> list[dict[str, object]]:
        """Find global change records by term, action, and optional date range."""
        return _search_history_entries(
            self.list_history(),
            term,
            action,
            {"add", "remove", "undo", "redo"},
            start_date,
            end_date,
            include_index=include_index,
        )

    def add(self, term: str, developer_id: int) -> str:
        self._validate_developer_id(developer_id)
        cleaned = self._clean_term(term)
        key = self._key(cleaned)
        if any(self._key(existing) == key for existing in self.list_terms()):
            raise ProfanityTermError("هذه الكلمة أو العبارة موجودة بالفعل.")

        before_added = self._added.copy()
        before_removed = self._removed.copy()
        before_history = self.list_history()
        base_term = next(
            (item for item in self.base_terms if self._key(item) == key),
            None,
        )
        if base_term is not None:
            self._removed = [item for item in self._removed if self._key(item) != key]
        else:
            if len(self.list_terms()) >= MAX_CONFIGURED_TERMS:
                raise ProfanityTermError("وصلت قائمة الكلمات إلى الحد الأقصى.")
            self._added.append(cleaned)
        self._history.append(
            {
                "developer_id": developer_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "add",
                "term": cleaned,
            }
        )

        try:
            self._save()
        except OSError:
            self._added = before_added
            self._removed = before_removed
            self._history = before_history
            raise
        return cleaned

    def remove(self, term: str, developer_id: int) -> str:
        self._validate_developer_id(developer_id)
        cleaned = self._clean_term(term)
        key = self._key(cleaned)
        existing = next(
            (item for item in self.list_terms() if self._key(item) == key),
            None,
        )
        if existing is None:
            raise ProfanityTermError("هذه الكلمة أو العبارة غير موجودة في القائمة.")

        before_added = self._added.copy()
        before_removed = self._removed.copy()
        before_history = self.list_history()
        base_term = next(
            (item for item in self.base_terms if self._key(item) == key),
            None,
        )
        if base_term is not None:
            self._removed.append(base_term)
        else:
            self._added = [item for item in self._added if self._key(item) != key]
        self._history.append(
            {
                "developer_id": developer_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "remove",
                "term": existing,
            }
        )

        try:
            self._save()
        except OSError:
            self._added = before_added
            self._removed = before_removed
            self._history = before_history
            raise
        return existing

    def undo(
        self,
        developer_id: int,
        history_index: int | None = None,
    ) -> dict[str, object]:
        """Reverse the latest eligible change or a specific older add/remove."""
        self._validate_developer_id(developer_id)
        if not self._history:
            raise ProfanityTermError("لا توجد تغييرات مسجلة للتراجع عنها.")

        undone_indexes = {
            entry["undone_history_index"]
            for entry in self._history
            if entry["action"] == "undo"
        }

        def change_action(entry: dict[str, object]) -> object:
            action = entry.get("action")
            if action in {"add", "remove"}:
                return action
            if action == "redo":
                return entry.get("redone_action")
            return None

        if history_index is None:
            target_index = next(
                (
                    index
                    for index in range(len(self._history) - 1, -1, -1)
                    if index not in undone_indexes
                    and change_action(self._history[index]) in {"add", "remove"}
                ),
                None,
            )
            if target_index is None:
                raise ProfanityTermError("لا توجد تغييرات غير متراجع عنها.")
        else:
            if (
                isinstance(history_index, bool)
                or not isinstance(history_index, int)
                or history_index < 0
                or history_index >= len(self._history)
            ):
                raise ProfanityTermError("رقم سجل التغيير المحدد غير صالح.")
            target_index = history_index
            selected_action = change_action(self._history[target_index])
            if selected_action not in {"add", "remove"}:
                raise ProfanityTermError(
                    "السجل المحدد ليس عملية إضافة أو إزالة يمكن التراجع عنها."
                )
            if target_index in undone_indexes:
                raise ProfanityTermError(
                    "تم التراجع عن هذا التغيير بالفعل."
                )
            selected_key = self._key(
                self._clean_term(str(self._history[target_index]["term"]))
            )
            if any(
                self._key(self._clean_term(str(entry["term"]))) == selected_key
                for entry in self._history[target_index + 1 :]
            ):
                raise ProfanityTermError(
                    "تعذر التراجع عن هذا السجل لأن الكلمة أو العبارة تغيّرت بعده."
                )

        original = self._history[target_index]
        action = str(change_action(original))
        term = self._clean_term(str(original["term"]))
        key = self._key(term)
        base_term = next(
            (item for item in self.base_terms if self._key(item) == key),
            None,
        )

        before_added = self._added.copy()
        before_removed = self._removed.copy()
        before_history = self.list_history()
        if action == "add":
            if base_term is not None:
                self._removed.append(base_term)
            else:
                self._added = [
                    item for item in self._added if self._key(item) != key
                ]
        else:
            if len(self.list_terms()) >= MAX_CONFIGURED_TERMS:
                raise ProfanityTermError(
                    "تعذر التراجع لأن استعادة الكلمة ستتجاوز الحد الأقصى للقائمة."
                )
            if base_term is not None:
                self._removed = [
                    item for item in self._removed if self._key(item) != key
                ]
            else:
                self._added.append(term)

        undo_entry = {
            "developer_id": developer_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "undo",
            "term": term,
            "undone_action": action,
            "undone_history_index": target_index,
        }
        self._history.append(undo_entry)
        try:
            self._save()
        except OSError:
            self._added = before_added
            self._removed = before_removed
            self._history = before_history
            raise
        return undo_entry.copy()

    def undo_batch(
        self,
        developer_id: int,
        history_index: int,
    ) -> dict[str, object]:
        """Reverse every still-current change in one saved batch atomically."""
        self._validate_developer_id(developer_id)
        target_indexes = self.batch_history_indexes(history_index)
        if not target_indexes:
            raise ProfanityTermError(
                "السجل المحدد لا ينتمي إلى دفعة متاحة للتراجع عنها."
            )

        undone_indexes = {
            entry["undone_history_index"]
            for entry in self._history
            if entry["action"] == "undo"
        }
        if any(index in undone_indexes for index in target_indexes):
            raise ProfanityTermError("تم التراجع عن جزء من هذه الدفعة بالفعل.")

        current_keys = {self._key(term) for term in self.list_terms()}
        changes: list[tuple[int, str, str]] = []
        for target_index in target_indexes:
            original = self._history[target_index]
            action = original["action"]
            term = self._clean_term(str(original["term"]))
            key = self._key(term)
            if any(
                self._key(self._clean_term(str(later["term"]))) == key
                for later in self._history[target_index + 1 :]
                if later.get("action") in {"add", "remove", "undo", "redo"}
            ):
                raise ProfanityTermError(
                    "تعذر التراجع عن الدفعة لأن إحدى كلماتها تغيّرت بعدها."
                )
            if (action == "add" and key not in current_keys) or (
                action == "remove" and key in current_keys
            ):
                raise ProfanityTermError(
                    "تعذر التراجع عن الدفعة لأن حالة إحدى كلماتها لم تعد تطابقها."
                )
            changes.append((target_index, str(action), term))

        restore_count = sum(action == "remove" for _, action, _ in changes)
        if len(current_keys) + restore_count > MAX_CONFIGURED_TERMS:
            raise ProfanityTermError(
                "تعذر التراجع عن الدفعة لأن استعادة كلماتها ستتجاوز الحد الأقصى للقائمة."
            )

        before_added = self._added.copy()
        before_removed = self._removed.copy()
        before_history = self.list_history()

        for target_index, action, term in changes:
            key = self._key(term)
            base_term = next(
                (item for item in self.base_terms if self._key(item) == key),
                None,
            )
            if action == "add":
                if base_term is not None:
                    self._removed.append(base_term)
                else:
                    self._added = [
                        item for item in self._added if self._key(item) != key
                    ]
            elif base_term is not None:
                self._removed = [
                    item for item in self._removed if self._key(item) != key
                ]
            else:
                self._added.append(term)

            self._history.append(
                {
                    "developer_id": developer_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "action": "undo",
                    "term": term,
                    "undone_action": action,
                    "undone_history_index": target_index,
                }
            )

        try:
            self._save()
        except OSError:
            self._added = before_added
            self._removed = before_removed
            self._history = before_history
            raise

        return {
            "action": "undo_batch",
            "batch_number": target_indexes[0] + 1,
            "undone_history_indexes": target_indexes,
            "changes": [
                {"action": action, "term": term}
                for _, action, term in changes
            ],
        }

    def redo(self, developer_id: int) -> dict[str, object]:
        """Reapply the latest undo that has not already been redone."""
        self._validate_developer_id(developer_id)
        redone_indexes = {
            entry["redone_undo_index"]
            for entry in self._history
            if entry["action"] == "redo"
        }
        target_index = next(
            (
                index
                for index in range(len(self._history) - 1, -1, -1)
                if self._history[index]["action"] == "undo"
                and index not in redone_indexes
            ),
            None,
        )
        if target_index is None:
            if any(entry["action"] == "undo" for entry in self._history):
                raise ProfanityTermError("لا توجد عمليات تراجع غير معادة.")
            raise ProfanityTermError("لا توجد تغييرات متراجع عنها لإعادتها.")

        undo_entry = self._history[target_index]
        action = str(undo_entry["undone_action"])
        term = self._clean_term(str(undo_entry["term"]))
        key = self._key(term)
        if any(
            self._key(str(entry["term"])) == key
            for entry in self._history[target_index + 1 :]
        ):
            raise ProfanityTermError(
                "تعذر الإعادة لأن الكلمة أو العبارة تغيّرت بعد التراجع عنها."
            )

        current_term = next(
            (item for item in self.list_terms() if self._key(item) == key),
            None,
        )
        if (action == "add" and current_term is not None) or (
            action == "remove" and current_term is None
        ):
            raise ProfanityTermError(
                "تعذر الإعادة لأن حالة الكلمة أو العبارة لم تعد تطابق التراجع."
            )
        if (
            action == "add"
            and not any(self._key(item) == key for item in self.base_terms)
            and len(self.list_terms()) >= MAX_CONFIGURED_TERMS
        ):
            raise ProfanityTermError(
                "تعذر إعادة الإضافة لأن القائمة وصلت إلى الحد الأقصى."
            )

        base_term = next(
            (item for item in self.base_terms if self._key(item) == key),
            None,
        )
        before_added = self._added.copy()
        before_removed = self._removed.copy()
        before_history = self.list_history()
        if action == "add":
            if base_term is not None:
                self._removed = [
                    item for item in self._removed if self._key(item) != key
                ]
            else:
                self._added.append(term)
        elif base_term is not None:
            self._removed.append(base_term)
        else:
            self._added = [
                item for item in self._added if self._key(item) != key
            ]

        redo_entry = {
            "developer_id": developer_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "redo",
            "term": term,
            "redone_action": action,
            "redone_undo_index": target_index,
        }
        self._history.append(redo_entry)
        try:
            self._save()
        except OSError:
            self._added = before_added
            self._removed = before_removed
            self._history = before_history
            raise
        return redo_entry.copy()

    def preview_change(
        self, action: str, term: str
    ) -> tuple[str, list[str], list[str]]:
        """Validate a global change and return its before/after lists without saving."""
        cleaned = self._clean_term(term)
        current_terms = self.list_terms()
        key = self._key(cleaned)
        if action == "add":
            if any(self._key(existing) == key for existing in current_terms):
                raise ProfanityTermError("هذه الكلمة أو العبارة موجودة بالفعل.")
            base_term = next(
                (item for item in self.base_terms if self._key(item) == key),
                None,
            )
            if base_term is None and len(current_terms) >= MAX_CONFIGURED_TERMS:
                raise ProfanityTermError("وصلت قائمة الكلمات إلى الحد الأقصى.")
            return cleaned, current_terms, [*current_terms, base_term or cleaned]
        if action == "remove":
            existing = next(
                (item for item in current_terms if self._key(item) == key),
                None,
            )
            if existing is None:
                raise ProfanityTermError("هذه الكلمة أو العبارة غير موجودة في القائمة.")
            return (
                existing,
                current_terms,
                [
                    item
                    for item in current_terms
                    if self._key(item) != key
                ],
            )
        raise ProfanityTermError("نوع التغيير المطلوب غير معروف.")

    def _prepare_changes(
        self,
        add_terms: Iterable[str],
        remove_terms: Iterable[str],
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        additions = [self._clean_term(term) for term in add_terms]
        requested_removals = [self._clean_term(term) for term in remove_terms]
        if not additions and not requested_removals:
            raise ProfanityTermError("أدخل كلمة واحدة على الأقل للإضافة أو الإزالة.")

        requested_keys = [self._key(term) for term in (*additions, *requested_removals)]
        if len(set(requested_keys)) != len(requested_keys):
            raise ProfanityTermError(
                "لا تكرر الكلمة نفسها أو تضعها في الإضافات والإزالات معاً."
            )

        before = self.list_terms()
        after = before.copy()
        validated_additions = []
        for term in additions:
            key = self._key(term)
            if any(self._key(existing) == key for existing in before):
                raise ProfanityTermError(
                    f"الكلمة أو العبارة «{term}» موجودة بالفعل."
                )
            base_term = next(
                (item for item in self.base_terms if self._key(item) == key),
                None,
            )
            canonical_term = base_term or term
            validated_additions.append(canonical_term)

        validated_removals = []
        for term in requested_removals:
            key = self._key(term)
            existing = next(
                (item for item in before if self._key(item) == key),
                None,
            )
            if existing is None:
                raise ProfanityTermError(
                    f"الكلمة أو العبارة «{term}» غير موجودة في القائمة."
                )
            validated_removals.append(existing)

        if (
            len(before) + len(validated_additions) - len(validated_removals)
            > MAX_CONFIGURED_TERMS
        ):
            raise ProfanityTermError("وصلت قائمة الكلمات إلى الحد الأقصى.")
        after.extend(validated_additions)
        removed_keys = {self._key(term) for term in validated_removals}
        after = [term for term in after if self._key(term) not in removed_keys]
        return validated_additions, validated_removals, before, after

    def preview_changes(
        self,
        add_terms: Iterable[str],
        remove_terms: Iterable[str],
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """Validate a batch and return its normalized before/after state."""
        return self._prepare_changes(add_terms, remove_terms)

    def apply_changes(
        self,
        add_terms: Iterable[str],
        remove_terms: Iterable[str],
        developer_id: int,
    ) -> dict[str, list[str]]:
        """Apply a validated batch with one atomic save and per-term history."""
        self._validate_developer_id(developer_id)
        additions, removals, _, _ = self._prepare_changes(add_terms, remove_terms)

        before_added = self._added.copy()
        before_removed = self._removed.copy()
        before_history = self.list_history()
        timestamp = datetime.now(timezone.utc).isoformat()
        batch_id = len(self._history)

        for term in additions:
            key = self._key(term)
            base_term = next(
                (item for item in self.base_terms if self._key(item) == key),
                None,
            )
            if base_term is not None:
                self._removed = [
                    item for item in self._removed if self._key(item) != key
                ]
            else:
                self._added.append(term)
            self._history.append(
                {
                    "developer_id": developer_id,
                    "timestamp": timestamp,
                    "action": "add",
                    "term": term,
                    "batch_id": batch_id,
                }
            )

        for term in removals:
            key = self._key(term)
            base_term = next(
                (item for item in self.base_terms if self._key(item) == key),
                None,
            )
            if base_term is not None:
                self._removed.append(base_term)
            else:
                self._added = [
                    item for item in self._added if self._key(item) != key
                ]
            self._history.append(
                {
                    "developer_id": developer_id,
                    "timestamp": timestamp,
                    "action": "remove",
                    "term": term,
                    "batch_id": batch_id,
                }
            )

        try:
            self._save()
        except OSError:
            self._added = before_added
            self._removed = before_removed
            self._history = before_history
            raise
        return {"added": additions, "removed": removals}

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (
            not isinstance(payload, dict)
            or set(payload) not in (
                {"added", "removed"},
                {"added", "removed", "history"},
            )
            or not isinstance(payload["added"], list)
            or not isinstance(payload["removed"], list)
            or (
                "history" in payload
                and not isinstance(payload["history"], list)
            )
        ):
            raise ValueError(f"Invalid filtered-term data in {self.path}.")

        added = [self._clean_term(term) for term in payload["added"]]
        removed = [self._clean_term(term) for term in payload["removed"]]
        base_keys = {self._key(term) for term in self.base_terms}
        added_keys = [self._key(term) for term in added]
        removed_keys = [self._key(term) for term in removed]
        if (
            len(set(added_keys)) != len(added_keys)
            or len(set(removed_keys)) != len(removed_keys)
            or any(key in base_keys for key in added_keys)
            or any(key not in base_keys for key in removed_keys)
            or len(added) > MAX_CONFIGURED_TERMS
        ):
            raise ValueError(f"Invalid or duplicate filtered terms in {self.path}.")
        history = payload.get("history", [])
        undone_indexes = set()
        batch_indexes: dict[int, list[int]] = {}
        for index, entry in enumerate(history):
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid filtered-term history in {self.path}.")
            action = entry.get("action")
            if isinstance(action, str) and action in {"add", "remove"}:
                base_keys = {"developer_id", "timestamp", "action", "term"}
                if set(entry) not in (base_keys, base_keys | {"batch_id"}):
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    )
                if "batch_id" in entry:
                    batch_id = entry["batch_id"]
                    if (
                        isinstance(batch_id, bool)
                        or not isinstance(batch_id, int)
                        or batch_id < 0
                        or batch_id > index
                    ):
                        raise ValueError(
                            f"Invalid filtered-term history in {self.path}."
                        )
                    batch_indexes.setdefault(batch_id, []).append(index)
            elif action == "undo":
                if set(entry) != {
                    "developer_id",
                    "timestamp",
                    "action",
                    "term",
                    "undone_action",
                    "undone_history_index",
                }:
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    )
                target_index = entry["undone_history_index"]
                if (
                    isinstance(target_index, bool)
                    or not isinstance(target_index, int)
                    or target_index < 0
                    or target_index >= index
                    or target_index in undone_indexes
                    or not isinstance(entry["undone_action"], str)
                    or entry["undone_action"] not in {"add", "remove"}
                ):
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    )
                target = history[target_index]
                target_action = target.get("action")
                if target_action == "redo":
                    target_action = target.get("redone_action")
                try:
                    if (
                        target_action != entry["undone_action"]
                        or self._key(self._clean_term(entry["term"]))
                        != self._key(self._clean_term(target.get("term")))
                    ):
                        raise ValueError(
                            f"Invalid filtered-term history in {self.path}."
                        )
                except (TypeError, ProfanityTermError) as error:
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    ) from error
                undone_indexes.add(target_index)
            elif action == "redo":
                if set(entry) != {
                    "developer_id",
                    "timestamp",
                    "action",
                    "term",
                    "redone_action",
                    "redone_undo_index",
                }:
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    )
                target_index = entry["redone_undo_index"]
                if (
                    isinstance(target_index, bool)
                    or not isinstance(target_index, int)
                    or target_index < 0
                    or target_index >= index
                    or target_index in {
                        previous["redone_undo_index"]
                        for previous in history[:index]
                        if previous.get("action") == "redo"
                    }
                    or not isinstance(entry["redone_action"], str)
                    or entry["redone_action"] not in {"add", "remove"}
                ):
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    )
                target = history[target_index]
                try:
                    if (
                        target.get("action") != "undo"
                        or target.get("undone_action") != entry["redone_action"]
                        or self._key(self._clean_term(entry["term"]))
                        != self._key(self._clean_term(target.get("term")))
                    ):
                        raise ValueError(
                            f"Invalid filtered-term history in {self.path}."
                        )
                except (TypeError, ProfanityTermError) as error:
                    raise ValueError(
                        f"Invalid filtered-term history in {self.path}."
                    ) from error
            else:
                raise ValueError(f"Invalid filtered-term history in {self.path}.")
            if (
                isinstance(entry.get("developer_id"), bool)
                or not isinstance(entry.get("developer_id"), int)
                or entry["developer_id"] <= 0
            ):
                raise ValueError(f"Invalid filtered-term history in {self.path}.")
            try:
                timestamp = datetime.fromisoformat(entry["timestamp"])
                self._clean_term(entry["term"])
            except (TypeError, ValueError, ProfanityTermError) as error:
                raise ValueError(
                    f"Invalid filtered-term history in {self.path}."
                ) from error
            if timestamp.tzinfo is None:
                raise ValueError(f"Invalid filtered-term history in {self.path}.")
        for batch_id, indexes in batch_indexes.items():
            if (
                indexes[0] != batch_id
                or indexes != list(range(indexes[0], indexes[-1] + 1))
                or any(
                    history[index].get("batch_id") != batch_id
                    or history[index].get("developer_id")
                    != history[batch_id].get("developer_id")
                    or history[index].get("timestamp")
                    != history[batch_id].get("timestamp")
                    for index in indexes
                )
            ):
                raise ValueError(f"Invalid filtered-term batch history in {self.path}.")
        self._added = added
        self._removed = removed
        self._history = [entry.copy() for entry in history]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "added": self._added,
            "removed": self._removed,
            "history": self._history,
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temp_path = Path(output.name)
                json.dump(payload, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


class GuildProfanityTermStore:
    """Persist developer-managed filtered-term overrides separately per guild."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.backup_dir = self.path.parent / f"{self.path.stem}_backups"
        self._guilds: dict[str, dict[str, list]] = {}
        self._load()

    @staticmethod
    def _validate_guild_id(guild_id: int) -> None:
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
        ):
            raise ProfanityTermError("تعذر التحقق من هوية السيرفر.")

    @staticmethod
    def _empty_state() -> dict[str, list]:
        return {"added": [], "removed": [], "history": []}

    def snapshot_guild(self, guild_id: int) -> dict[str, list]:
        """Return a deep copy of one guild's complete persisted state."""
        self._validate_guild_id(guild_id)
        return deepcopy(self._guilds.get(str(guild_id), self._empty_state()))

    def parse_recovery_file(
        self, content: bytes | str
    ) -> tuple[int, dict[str, list], str]:
        """Parse and validate a private server-filter recovery export."""
        try:
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            if not isinstance(content, str):
                raise TypeError("recovery file must be UTF-8 JSON")
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise ProfanityTermError("ملف الاستعادة ليس JSON صالحاً بترميز UTF-8.") from error

        if (
            not isinstance(payload, dict)
            or set(payload) != {"guild_id", "created_at", "state"}
            or not isinstance(payload["guild_id"], str)
            or not payload["guild_id"].isascii()
            or not payload["guild_id"].isdecimal()
        ):
            raise ProfanityTermError("بنية ملف استعادة إعدادات السيرفر غير صالحة.")
        try:
            guild_id = int(payload["guild_id"])
        except ValueError as error:
            raise ProfanityTermError(
                "بنية ملف استعادة إعدادات السيرفر غير صالحة."
            ) from error
        if guild_id <= 0 or str(guild_id) != payload["guild_id"]:
            raise ProfanityTermError("بنية ملف استعادة إعدادات السيرفر غير صالحة.")
        try:
            created_at_value = payload["created_at"]
            created_at = datetime.fromisoformat(created_at_value)
            if created_at.tzinfo is None:
                raise ValueError("recovery timestamp must include a timezone")
            state = self._validate_guild_state(payload["state"])
        except (TypeError, ValueError, ProfanityTermError) as error:
            raise ProfanityTermError(
                "بيانات ملف الاستعادة غير صالحة أو لا تطابق تنسيق النسخة الاحتياطية."
            ) from error
        return guild_id, state, created_at_value

    def list_terms(self, guild_id: int, base_terms: Iterable[str]) -> list[str]:
        """Apply one guild's overrides to the existing global term list."""
        self._validate_guild_id(guild_id)
        state = self._guilds.get(str(guild_id), self._empty_state())
        removed = {ProfanityTermStore._key(term) for term in state["removed"]}
        result: list[str] = []
        seen: set[str] = set()
        for term in (*tuple(base_terms), *state["added"]):
            key = ProfanityTermStore._key(term)
            if key not in removed and key not in seen:
                result.append(term)
                seen.add(key)
        return result

    def list_overrides(
        self, guild_id: int, base_terms: Iterable[str]
    ) -> dict[str, list[str]]:
        """List the additions and exclusions that differ from the current global list."""
        self._validate_guild_id(guild_id)
        base_terms = tuple(base_terms)
        base_keys = {ProfanityTermStore._key(term) for term in base_terms}
        state = self._guilds.get(str(guild_id), self._empty_state())
        return {
            "added": [
                term
                for term in state["added"]
                if ProfanityTermStore._key(term) not in base_keys
            ],
            "removed": [
                term
                for term in state["removed"]
                if ProfanityTermStore._key(term) in base_keys
            ],
        }

    def list_override_status(
        self, guild_id: int, base_terms: Iterable[str]
    ) -> dict[str, list[str]]:
        """Classify saved overrides by whether they affect the current global list."""
        self._validate_guild_id(guild_id)
        base_keys = {
            ProfanityTermStore._key(term) for term in tuple(base_terms)
        }
        saved = self.list_saved_overrides(guild_id)
        return {
            "added_effective": [
                term
                for term in saved["added"]
                if ProfanityTermStore._key(term) not in base_keys
            ],
            "added_retained": [
                term
                for term in saved["added"]
                if ProfanityTermStore._key(term) in base_keys
            ],
            "removed_effective": [
                term
                for term in saved["removed"]
                if ProfanityTermStore._key(term) in base_keys
            ],
            "removed_retained": [
                term
                for term in saved["removed"]
                if ProfanityTermStore._key(term) not in base_keys
            ],
        }

    def list_saved_overrides(self, guild_id: int) -> dict[str, list[str]]:
        """Return every persisted addition and exclusion for one guild."""
        self._validate_guild_id(guild_id)
        state = self._guilds.get(str(guild_id), self._empty_state())
        return {
            "added": state["added"].copy(),
            "removed": state["removed"].copy(),
        }

    def list_history(self, guild_id: int) -> list[dict[str, object]]:
        """Return a copy of the change history for one guild."""
        self._validate_guild_id(guild_id)
        state = self._guilds.get(str(guild_id), self._empty_state())
        return deepcopy(state["history"])

    def list_guild_ids(self) -> list[int]:
        """Return every guild with persisted server-specific filter state."""
        return sorted(int(guild_id) for guild_id in self._guilds)

    def search_history(
        self,
        guild_id: int,
        term: str | None = None,
        action: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        restore_type: str | None = None,
        developer_id: int | None = None,
    ) -> list[dict[str, object]]:
        """Find one guild's change records with optional filters."""
        if restore_type is not None and restore_type not in {
            "added",
            "removed",
            "both",
        }:
            raise ProfanityTermError("نوع التعديلات المستعادة المحدد للبحث غير صالح.")

        history = _search_history_entries(
            self.list_history(guild_id),
            term,
            action,
            {"add", "remove", "reset", "restore"},
            start_date,
            end_date,
            developer_id=developer_id,
        )
        if restore_type is None:
            return history

        required_types = (
            {"added", "removed"} if restore_type == "both" else {restore_type}
        )
        return [
            entry
            for entry in history
            if entry.get("action") == "restore"
            and isinstance(entry.get("restore_types"), list)
            and required_types.issubset(set(entry["restore_types"]))
        ]

    def search_all_histories(
        self,
        term: str | None = None,
        action: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        restore_type: str | None = None,
        developer_id: int | None = None,
    ) -> list[tuple[int, dict[str, object]]]:
        """Search saved server histories and return each match with its guild ID."""
        if restore_type is not None and restore_type not in {
            "added",
            "removed",
            "both",
        }:
            raise ProfanityTermError("نوع التعديلات المستعادة المحدد للبحث غير صالح.")

        _search_history_entries(
            [],
            term,
            action,
            {"add", "remove", "reset", "restore"},
            start_date,
            end_date,
            developer_id=developer_id,
        )
        results = [
            (guild_id, entry)
            for guild_id in self.list_guild_ids()
            for entry in self.search_history(
                guild_id,
                term,
                action,
                start_date,
                end_date,
                restore_type,
                developer_id,
            )
        ]

        def sort_key(result: tuple[int, dict[str, object]]):
            timestamp = datetime.fromisoformat(
                str(result[1]["timestamp"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            return timestamp, result[0]

        return sorted(results, key=sort_key, reverse=True)

    def reset_guild(
        self,
        guild_id: int,
        developer_id: int,
        *,
        expected_overrides: dict[str, list[str]] | None = None,
        backup_path: Path | str | None = None,
    ) -> dict[str, object] | None:
        """Back up and clear one guild's overrides, retaining an audit record."""
        ProfanityTermStore._validate_developer_id(developer_id)
        self._validate_guild_id(guild_id)
        guild_key = str(guild_id)
        state = self._guilds.get(guild_key, self._empty_state())
        current_overrides = {
            "added": state["added"].copy(),
            "removed": state["removed"].copy(),
        }
        if (
            expected_overrides is not None
            and current_overrides != expected_overrides
        ):
            raise ProfanityTermError(
                "تغيرت تعديلات هذا السيرفر منذ المعاينة؛ لم يتغير شيء. "
                "أعد تشغيل الأمر لمعاينة الوضع الحالي."
            )
        if not current_overrides["added"] and not current_overrides["removed"]:
            return None

        previous_state = deepcopy(state)
        timestamp = datetime.now(timezone.utc).isoformat()
        if backup_path is None:
            saved_backup_path = self._write_backup(
                guild_id, previous_state, timestamp
            )
        else:
            saved_backup_path = self._verify_prepared_backup(
                Path(backup_path), guild_id, previous_state
            )
        reset_entry = {
            "developer_id": developer_id,
            "timestamp": timestamp,
            "action": "reset",
            "added": previous_state["added"].copy(),
            "removed": previous_state["removed"].copy(),
            "backup": saved_backup_path.name,
        }
        self._guilds[guild_key] = {
            "added": [],
            "removed": [],
            "history": [*previous_state["history"], reset_entry],
        }
        try:
            self._save()
        except OSError:
            self._guilds[guild_key] = previous_state
            raise
        return {
            "added": previous_state["added"].copy(),
            "removed": previous_state["removed"].copy(),
            "backup": saved_backup_path.name,
        }

    def restore_guild(
        self,
        guild_id: int,
        restored_state: dict[str, list],
        developer_id: int,
        *,
        expected_state: dict[str, list],
        restore_types: tuple[str, ...] = ("added", "removed"),
        restore_mode: str = "replace",
        backup_path: Path | str | None = None,
    ) -> dict[str, object]:
        """Back up the current state, then restore selected saved overrides."""
        ProfanityTermStore._validate_developer_id(developer_id)
        self._validate_guild_id(guild_id)
        guild_key = str(guild_id)
        previous_state = self.snapshot_guild(guild_id)
        if previous_state != expected_state:
            raise ProfanityTermError(
                "تغيرت إعدادات هذا السيرفر منذ المعاينة؛ لم يتغير شيء. "
                "أعد تشغيل الأمر لمعاينة الوضع الحالي."
            )

        if (
            not isinstance(restore_types, tuple)
            or not restore_types
            or len(set(restore_types)) != len(restore_types)
            or not set(restore_types).issubset({"added", "removed"})
        ):
            raise ProfanityTermError("يجب اختيار نوع تعديل واحد على الأقل للاستعادة.")
        if restore_mode not in {"replace", "merge"}:
            raise ProfanityTermError("اختر الاستبدال أو الدمج لاستعادة التعديلات.")

        recovery_state = self._validate_guild_state(restored_state)
        final_state = {
            "added": previous_state["added"].copy(),
            "removed": previous_state["removed"].copy(),
        }
        for override_type in restore_types:
            if restore_mode == "replace":
                final_state[override_type] = recovery_state[override_type].copy()
            else:
                final_state[override_type] = self._merge_terms(
                    previous_state[override_type],
                    recovery_state[override_type],
                )[0]

        if len(final_state["added"]) > MAX_CONFIGURED_TERMS:
            raise ProfanityTermError(
                "سيتجاوز دمج الإضافات الحد الأقصى للكلمات المحفوظة؛ "
                "لم تتغير الإعدادات."
            )
        added_keys = {ProfanityTermStore._key(term) for term in final_state["added"]}
        removed_keys = {
            ProfanityTermStore._key(term) for term in final_state["removed"]
        }
        if added_keys & removed_keys:
            raise ProfanityTermError(
                "يتعارض ملف الاستعادة مع تعديل حالي للكلمة نفسها؛ "
                "لا يمكن استعادة الإضافات والاستثناءات معاً."
            )

        timestamp = datetime.now(timezone.utc).isoformat()
        if backup_path is None:
            saved_backup_path = self._write_backup(
                guild_id, previous_state, timestamp
            )
        else:
            saved_backup_path = self._verify_prepared_backup(
                Path(backup_path), guild_id, previous_state
            )
        restore_entry = {
            "developer_id": developer_id,
            "timestamp": timestamp,
            "action": "restore",
            "added": final_state["added"].copy(),
            "removed": final_state["removed"].copy(),
            "restore_types": list(restore_types),
            "restore_mode": restore_mode,
            "backup": saved_backup_path.name,
        }
        self._guilds[guild_key] = {
            "added": final_state["added"].copy(),
            "removed": final_state["removed"].copy(),
            "history": [*previous_state["history"], restore_entry],
        }
        try:
            self._save()
        except OSError:
            self._guilds[guild_key] = previous_state
            raise
        return {
            "added": final_state["added"].copy(),
            "removed": final_state["removed"].copy(),
            "backup": saved_backup_path.name,
        }

    @staticmethod
    def _merge_terms(
        current_terms: list[str], restored_terms: list[str]
    ) -> tuple[list[str], list[str], list[str]]:
        """Merge normalized terms, preserving current spelling and order."""
        current_keys = {ProfanityTermStore._key(term) for term in current_terms}
        merged = current_terms.copy()
        added: list[str] = []
        already_present: list[str] = []
        for term in restored_terms:
            key = ProfanityTermStore._key(term)
            if key in current_keys:
                already_present.append(term)
                continue
            merged.append(term)
            added.append(term)
            current_keys.add(key)
        return merged, added, already_present

    def prepare_restore_backup(
        self,
        guild_id: int,
        *,
        expected_state: dict[str, list],
    ) -> Path:
        """Save the unchanged, previewed server state without replacing it."""
        self._validate_guild_id(guild_id)
        state = self.snapshot_guild(guild_id)
        if state != expected_state:
            raise ProfanityTermError(
                "تغيرت إعدادات هذا السيرفر منذ المعاينة؛ لم يتغير شيء. "
                "أعد تشغيل الأمر لمعاينة الوضع الحالي."
            )
        return self._write_backup(
            guild_id,
            state,
            datetime.now(timezone.utc).isoformat(),
        )

    def prepare_reset_backup(
        self,
        guild_id: int,
        *,
        expected_overrides: dict[str, list[str]] | None = None,
    ) -> Path | None:
        """Save a reset snapshot without changing the server's current settings."""
        self._validate_guild_id(guild_id)
        state = self._guilds.get(str(guild_id), self._empty_state())
        current_overrides = {
            "added": state["added"].copy(),
            "removed": state["removed"].copy(),
        }
        if (
            expected_overrides is not None
            and current_overrides != expected_overrides
        ):
            raise ProfanityTermError(
                "تغيرت تعديلات هذا السيرفر منذ المعاينة؛ لم يتغير شيء. "
                "أعد تشغيل الأمر لمعاينة الوضع الحالي."
            )
        if not current_overrides["added"] and not current_overrides["removed"]:
            return None
        return self._write_backup(
            guild_id,
            deepcopy(state),
            datetime.now(timezone.utc).isoformat(),
        )

    def _verify_prepared_backup(
        self,
        backup_path: Path,
        guild_id: int,
        state: dict[str, list],
    ) -> Path:
        """Require a prepared backup to be private, local, and for this exact state."""
        if (
            self.backup_dir.is_symlink()
            or backup_path.parent != self.backup_dir
            or backup_path.is_symlink()
            or not backup_path.is_file()
        ):
            raise OSError("prepared server filtered-term backup is unavailable")
        payload = json.loads(backup_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"guild_id", "created_at", "state"}
            or payload["guild_id"] != str(guild_id)
            or payload["state"] != state
        ):
            raise ValueError("prepared server filtered-term backup does not match")
        try:
            created_at = datetime.fromisoformat(payload["created_at"])
        except (TypeError, ValueError) as error:
            raise ValueError("prepared server filtered-term backup is invalid") from error
        if created_at.tzinfo is None:
            raise ValueError("prepared server filtered-term backup is invalid")
        return backup_path

    def _write_backup(
        self,
        guild_id: int,
        state: dict[str, list],
        timestamp: str,
    ) -> Path:
        if self.backup_dir.is_symlink():
            raise OSError(
                "server filtered-term backup directory must not be a symbolic link"
            )
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.backup_dir.is_symlink():
            raise OSError(
                "server filtered-term backup directory must not be a symbolic link"
            )
        self.backup_dir.chmod(0o700)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = (
            self.backup_dir / f"{guild_id}_{stamp}_{uuid.uuid4().hex}.json"
        )
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.backup_dir,
                prefix=".guild-filter-backup-",
                suffix=".tmp",
                delete=False,
            ) as output:
                temp_path = Path(output.name)
                os.chmod(temp_path, 0o600)
                json.dump(
                    {
                        "guild_id": str(guild_id),
                        "created_at": timestamp,
                        "state": state,
                    },
                    output,
                    ensure_ascii=False,
                    indent=2,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, backup_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return backup_path

    def add(
        self,
        guild_id: int,
        term: str,
        developer_id: int,
        base_terms: Iterable[str],
    ) -> str:
        ProfanityTermStore._validate_developer_id(developer_id)
        self._validate_guild_id(guild_id)
        cleaned = ProfanityTermStore._clean_term(term)
        key = ProfanityTermStore._key(cleaned)
        base_terms = tuple(base_terms)
        current_terms = self.list_terms(guild_id, base_terms)
        if any(ProfanityTermStore._key(item) == key for item in current_terms):
            raise ProfanityTermError("هذه الكلمة أو العبارة موجودة بالفعل.")

        state = self._guilds.get(str(guild_id), self._empty_state())
        before_state = {
            "added": state["added"].copy(),
            "removed": state["removed"].copy(),
            "history": [entry.copy() for entry in state["history"]],
        }
        base_term = next(
            (
                item
                for item in base_terms
                if ProfanityTermStore._key(item) == key
            ),
            None,
        )
        if base_term is not None:
            state["added"] = [
                item
                for item in state["added"]
                if ProfanityTermStore._key(item) != key
            ]
            state["removed"] = [
                item
                for item in state["removed"]
                if ProfanityTermStore._key(item) != key
            ]
        else:
            state["removed"] = [
                item
                for item in state["removed"]
                if ProfanityTermStore._key(item) != key
            ]
            if len(current_terms) >= MAX_CONFIGURED_TERMS:
                raise ProfanityTermError("وصلت قائمة الكلمات إلى الحد الأقصى.")
            state["added"].append(cleaned)
        state["history"].append(
            {
                "developer_id": developer_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "add",
                "term": cleaned,
            }
        )
        self._guilds[str(guild_id)] = state
        try:
            self._save()
        except OSError:
            self._guilds[str(guild_id)] = before_state
            raise
        return cleaned

    def remove(
        self,
        guild_id: int,
        term: str,
        developer_id: int,
        base_terms: Iterable[str],
    ) -> str:
        ProfanityTermStore._validate_developer_id(developer_id)
        self._validate_guild_id(guild_id)
        cleaned = ProfanityTermStore._clean_term(term)
        key = ProfanityTermStore._key(cleaned)
        base_terms = tuple(base_terms)
        existing = next(
            (
                item
                for item in self.list_terms(guild_id, base_terms)
                if ProfanityTermStore._key(item) == key
            ),
            None,
        )
        if existing is None:
            raise ProfanityTermError("هذه الكلمة أو العبارة غير موجودة في القائمة.")

        state = self._guilds.get(str(guild_id), self._empty_state())
        before_state = {
            "added": state["added"].copy(),
            "removed": state["removed"].copy(),
            "history": [entry.copy() for entry in state["history"]],
        }
        base_term = next(
            (
                item
                for item in base_terms
                if ProfanityTermStore._key(item) == key
            ),
            None,
        )
        if base_term is not None:
            state["added"] = [
                item
                for item in state["added"]
                if ProfanityTermStore._key(item) != key
            ]
            state["removed"].append(base_term)
        else:
            state["added"] = [
                item
                for item in state["added"]
                if ProfanityTermStore._key(item) != key
            ]
        state["history"].append(
            {
                "developer_id": developer_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": "remove",
                "term": existing,
            }
        )
        self._guilds[str(guild_id)] = state
        try:
            self._save()
        except OSError:
            self._guilds[str(guild_id)] = before_state
            raise
        return existing

    @staticmethod
    def _validate_guild_state(state: object) -> dict[str, list]:
        """Validate and normalize one guild's saved overrides and history."""
        if (
            not isinstance(state, dict)
            or set(state) != {"added", "removed", "history"}
            or not isinstance(state["added"], list)
            or not isinstance(state["removed"], list)
            or not isinstance(state["history"], list)
        ):
            raise ValueError("invalid server filtered-term state")

        added = [ProfanityTermStore._clean_term(term) for term in state["added"]]
        removed = [ProfanityTermStore._clean_term(term) for term in state["removed"]]
        added_keys = [ProfanityTermStore._key(term) for term in added]
        removed_keys = [ProfanityTermStore._key(term) for term in removed]
        if (
            len(set(added_keys)) != len(added_keys)
            or len(set(removed_keys)) != len(removed_keys)
            or set(added_keys) & set(removed_keys)
            or len(added) > MAX_CONFIGURED_TERMS
        ):
            raise ValueError("invalid or duplicate server filtered terms")

        history = state["history"]
        for entry in history:
            action = entry.get("action") if isinstance(entry, dict) else None
            if isinstance(action, str) and action in {"add", "remove"}:
                expected_entry_key_options = (
                    {"developer_id", "timestamp", "action", "term"},
                )
            elif action == "reset":
                expected_entry_key_options = (
                    {
                        "developer_id",
                        "timestamp",
                        "action",
                        "added",
                        "removed",
                        "backup",
                    },
                )
            elif action == "restore":
                legacy_restore_keys = {
                    "developer_id",
                    "timestamp",
                    "action",
                    "added",
                    "removed",
                    "backup",
                }
                expected_entry_key_options = (
                    legacy_restore_keys,
                    legacy_restore_keys | {"restore_types"},
                    legacy_restore_keys | {"restore_mode"},
                    legacy_restore_keys | {"restore_types", "restore_mode"},
                )
            else:
                expected_entry_key_options = (set(),)
            if (
                not isinstance(entry, dict)
                or set(entry) not in expected_entry_key_options
                or isinstance(entry["developer_id"], bool)
                or not isinstance(entry["developer_id"], int)
                or entry["developer_id"] <= 0
                or not isinstance(entry["action"], str)
                or entry["action"] not in {"add", "remove", "reset", "restore"}
            ):
                raise ValueError("invalid server filtered-term history")
            try:
                timestamp = datetime.fromisoformat(entry["timestamp"])
                if entry["action"] in {"reset", "restore"}:
                    if (
                        not isinstance(entry["added"], list)
                        or not isinstance(entry["removed"], list)
                        or not isinstance(entry["backup"], str)
                        or not entry["backup"]
                        or Path(entry["backup"]).name != entry["backup"]
                    ):
                        raise ValueError("invalid backup history entry")
                    if entry["action"] == "restore" and "restore_types" in entry:
                        restore_types = entry["restore_types"]
                        if (
                            not isinstance(restore_types, list)
                            or not restore_types
                            or any(not isinstance(kind, str) for kind in restore_types)
                            or len(set(restore_types)) != len(restore_types)
                            or not set(restore_types).issubset({"added", "removed"})
                        ):
                            raise ValueError("invalid restore history categories")
                    if (
                        entry["action"] == "restore"
                        and "restore_mode" in entry
                        and entry["restore_mode"] not in {"replace", "merge"}
                    ):
                        raise ValueError("invalid restore history mode")
                    for term in (*entry["added"], *entry["removed"]):
                        ProfanityTermStore._clean_term(term)
                else:
                    ProfanityTermStore._clean_term(entry["term"])
            except (TypeError, ValueError, ProfanityTermError) as error:
                raise ValueError("invalid server filtered-term history") from error
            if timestamp.tzinfo is None:
                raise ValueError("invalid server filtered-term history timestamp")

        return {
            "added": added,
            "removed": removed,
            "history": deepcopy(history),
        }

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (
            not isinstance(payload, dict)
            or set(payload) != {"guilds"}
            or not isinstance(payload["guilds"], dict)
        ):
            raise ValueError(f"Invalid server filtered-term data in {self.path}.")

        guilds: dict[str, dict[str, list]] = {}
        for guild_key, state in payload["guilds"].items():
            if (
                not isinstance(guild_key, str)
                or not guild_key.isdecimal()
                or str(int(guild_key)) != guild_key
                or int(guild_key) <= 0
                or not isinstance(state, dict)
                or set(state) != {"added", "removed", "history"}
                or not isinstance(state["added"], list)
                or not isinstance(state["removed"], list)
                or not isinstance(state["history"], list)
            ):
                raise ValueError(
                    f"Invalid server filtered-term data in {self.path}."
                )
            guilds[guild_key] = self._validate_guild_state(state)
        self._guilds = guilds

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temp_path = Path(output.name)
                json.dump(
                    {"guilds": self._guilds},
                    output,
                    ensure_ascii=False,
                    indent=2,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


class ServerGroupError(ValueError):
    """Raised when a saved server group is invalid or cannot be changed."""


class ServerGroupStore:
    """Persist reusable developer-managed groups of connected Discord servers."""

    def __init__(self, path: Path | str | None = None):
        self.path = (
            Path(path)
            if path
            else Path(__file__).parent / "data" / "filter_server_groups.json"
        )
        self._groups: dict[str, dict[str, object]] = {}
        self._membership_history: list[dict[str, object]] = []
        self._load()

    @staticmethod
    def _clean_name(name: str) -> str:
        if not isinstance(name, str):
            raise ServerGroupError("اكتب اسماً نصياً للمجموعة.")
        name = name.strip()
        if (
            not name
            or len(name) > MAX_SERVER_GROUP_NAME_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise ServerGroupError(
                f"يجب أن يكون اسم المجموعة بين حرف واحد و{MAX_SERVER_GROUP_NAME_LENGTH} حرفاً."
            )
        if name.casefold().startswith("group:"):
            raise ServerGroupError("لا تبدأ اسم المجموعة بالبادئة group:.")
        return name

    @staticmethod
    def _clean_guild_ids(guild_ids: Iterable[int]) -> list[int]:
        if isinstance(guild_ids, (str, bytes)):
            raise ServerGroupError("أدخل معرّفات السيرفرات كقائمة.")
        try:
            ids = list(guild_ids)
        except TypeError as error:
            raise ServerGroupError("قائمة معرّفات السيرفرات غير صالحة.") from error
        if not MIN_SERVER_GROUP_SIZE <= len(ids) <= MAX_SERVER_GROUP_SIZE:
            raise ServerGroupError(
                f"يجب أن تضم المجموعة من {MIN_SERVER_GROUP_SIZE} إلى "
                f"{MAX_SERVER_GROUP_SIZE} سيرفرات."
            )
        if any(
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            for guild_id in ids
        ):
            raise ServerGroupError("تعذر التحقق من معرّفات السيرفرات.")
        if len(set(ids)) != len(ids):
            raise ServerGroupError("اختر سيرفرات مختلفة؛ لا تكرر معرّف السيرفر.")
        return ids

    @staticmethod
    def _key(name: str) -> str:
        return name.casefold()

    def list_groups(self) -> list[dict[str, object]]:
        return [
            {"name": group["name"], "guild_ids": list(group["guild_ids"])}
            for group in self._groups.values()
        ]

    @staticmethod
    def _history_page(
        entries: list[dict[str, object]],
        limit: int | None,
        offset: int,
    ) -> list[dict[str, object]]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("History limit must be a positive integer.")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValueError("History offset must be a non-negative integer.")
        stop = None if limit is None else offset + limit
        return deepcopy(entries[offset:stop])

    def list_membership_history(
        self,
        limit: int | None = None,
        *,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        return self._history_page(
            list(reversed(self._membership_history)),
            limit,
            offset,
        )

    def list_group_history(
        self,
        name: str,
        limit: int | None = None,
        *,
        offset: int = 0,
        guild_id: int | None = None,
    ) -> list[dict[str, object]]:
        if guild_id is not None and (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
        ):
            raise ServerGroupError("تعذر التحقق من معرّف السيرفر.")

        group = self.get_group(name)
        if group is None:
            return self._history_page([], limit, offset)

        current_name = self._key(str(group["name"]))
        entries = []
        for entry in reversed(self._membership_history):
            entry_name = self._key(str(entry["group_name"]))
            if entry["action"] == "renamed":
                old_name = self._key(str(entry["old_group_name"]))
                if entry_name == current_name:
                    entries.append(entry)
                    current_name = old_name
                elif old_name == current_name:
                    break
            elif entry_name == current_name:
                if entry["action"] == "deleted":
                    break
                entries.append(entry)
                if entry["action"] == "created":
                    break

        if guild_id is not None:
            entries = [
                entry
                for entry in entries
                if guild_id in entry["before_guild_ids"]
                or guild_id in entry["after_guild_ids"]
            ]

        return self._history_page(entries, limit, offset)

    @staticmethod
    def history_entry_reference(entry: dict[str, object]) -> str:
        serialized = json.dumps(
            entry,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return uuid.uuid5(uuid.NAMESPACE_URL, serialized).hex

    def prepare_rename_reversal(
        self,
        reference: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        if not isinstance(reference, str) or not re.fullmatch(
            r"[0-9a-f]{32}",
            reference,
        ):
            raise ServerGroupError("اختيار إعادة التسمية غير صالح.")

        selected = next(
            (
                entry
                for entry in reversed(self._membership_history)
                if entry["action"] == "renamed"
                and self.history_entry_reference(entry) == reference
            ),
            None,
        )
        if selected is None:
            raise ServerGroupError(
                "لم يتم العثور على إعادة التسمية المحددة في السجل."
            )

        entry = deepcopy(selected)
        current_group = self.get_group(str(entry["group_name"]))
        if current_group is None:
            raise ServerGroupError(
                "لم تعد المجموعة موجودة؛ لم يُحفظ أي تغيير."
            )

        group_history = self.list_group_history(str(current_group["name"]))
        latest_rename = next(
            (
                history_entry
                for history_entry in group_history
                if history_entry["action"] == "renamed"
            ),
            None,
        )
        if (
            latest_rename is None
            or self.history_entry_reference(latest_rename) != reference
        ):
            raise ServerGroupError(
                "إعادة التسمية المحددة ليست الأحدث لهذه المجموعة. "
                "اعكس أحدث إعادة تسمية أولاً."
            )

        previous_name = self._clean_name(str(entry["old_group_name"]))
        previous_key = self._key(previous_name)
        current_key = self._key(str(current_group["name"]))
        if previous_key != current_key and previous_key in self._groups:
            raise ServerGroupError(
                "الاسم السابق مستخدم حالياً من مجموعة أخرى؛ "
                "لا يمكن عكس إعادة التسمية."
            )
        return entry, current_group

    def undo_rename_entry(
        self,
        reference: str,
        expected_entry: dict[str, object],
        expected_group: dict[str, object],
        *,
        developer_id: int,
    ) -> dict[str, object]:
        self._validate_developer_id(developer_id)
        entry, current_group = self.prepare_rename_reversal(reference)
        if entry != expected_entry or current_group != expected_group:
            raise ServerGroupError(
                "تغيرت المجموعة منذ عرض المعاينة؛ لم يُحفظ أي تغيير."
            )
        return self.rename(
            str(current_group["name"]),
            str(entry["old_group_name"]),
            developer_id=developer_id,
        )

    def get_group(self, name: str) -> dict[str, object] | None:
        cleaned = self._clean_name(name)
        group = self._groups.get(self._key(cleaned))
        if group is None:
            return None
        return {"name": group["name"], "guild_ids": list(group["guild_ids"])}

    @staticmethod
    def _validate_developer_id(developer_id: int) -> None:
        if (
            isinstance(developer_id, bool)
            or not isinstance(developer_id, int)
            or developer_id <= 0
        ):
            raise ServerGroupError("تعذر التحقق من هوية المطور.")

    def _save_membership_change(
        self,
        action: str,
        group_name: str,
        before_guild_ids: list[int],
        after_guild_ids: list[int],
        developer_id: int,
        *,
        old_group_name: str | None = None,
    ) -> None:
        self._validate_developer_id(developer_id)
        entry = {
            "developer_id": developer_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "group_name": group_name,
            "before_guild_ids": list(before_guild_ids),
            "after_guild_ids": list(after_guild_ids),
        }
        if old_group_name is not None:
            entry["old_group_name"] = old_group_name
        self._membership_history.append(entry)
        try:
            self._save()
        except Exception:
            self._membership_history.pop()
            raise

    def create(
        self,
        name: str,
        guild_ids: Iterable[int],
        *,
        developer_id: int,
    ) -> dict[str, object]:
        cleaned = self._clean_name(name)
        ids = self._clean_guild_ids(guild_ids)
        key = self._key(cleaned)
        if key in self._groups:
            raise ServerGroupError("يوجد بالفعل اسم مجموعة مطابق.")
        if len(self._groups) >= MAX_SERVER_GROUPS:
            raise ServerGroupError("وصلت إلى الحد الأقصى للمجموعات المحفوظة.")
        self._groups[key] = {"name": cleaned, "guild_ids": ids}
        try:
            self._save_membership_change(
                "created",
                cleaned,
                [],
                ids,
                developer_id,
            )
        except Exception:
            del self._groups[key]
            raise
        return self.get_group(cleaned)

    def add_guild(
        self,
        name: str,
        guild_id: int,
        *,
        developer_id: int,
    ) -> dict[str, object]:
        cleaned = self._clean_name(name)
        key = self._key(cleaned)
        group = self._groups.get(key)
        if group is None:
            raise ServerGroupError("لم يتم العثور على المجموعة.")
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
        ):
            raise ServerGroupError("تعذر التحقق من معرّف السيرفر.")

        current_ids = list(group["guild_ids"])
        if guild_id in current_ids:
            raise ServerGroupError("هذا السيرفر موجود بالفعل في المجموعة.")
        updated_ids = self._clean_guild_ids([*current_ids, guild_id])
        self._groups[key] = {"name": group["name"], "guild_ids": updated_ids}
        try:
            self._save_membership_change(
                "added",
                str(group["name"]),
                current_ids,
                updated_ids,
                developer_id,
            )
        except Exception:
            self._groups[key] = group
            raise
        return self.get_group(cleaned)

    def remove_guild(
        self,
        name: str,
        guild_id: int,
        *,
        developer_id: int,
    ) -> dict[str, object]:
        cleaned = self._clean_name(name)
        key = self._key(cleaned)
        group = self._groups.get(key)
        if group is None:
            raise ServerGroupError("لم يتم العثور على المجموعة.")
        if (
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
        ):
            raise ServerGroupError("تعذر التحقق من معرّف السيرفر.")

        current_ids = list(group["guild_ids"])
        if guild_id not in current_ids:
            raise ServerGroupError("هذا السيرفر غير موجود في المجموعة.")
        updated_ids = self._clean_guild_ids(
            [member_id for member_id in current_ids if member_id != guild_id]
        )
        self._groups[key] = {"name": group["name"], "guild_ids": updated_ids}
        try:
            self._save_membership_change(
                "removed",
                str(group["name"]),
                current_ids,
                updated_ids,
                developer_id,
            )
        except Exception:
            self._groups[key] = group
            raise
        return self.get_group(cleaned)

    @staticmethod
    def _clean_membership_change_ids(guild_ids: Iterable[int]) -> list[int]:
        if isinstance(guild_ids, (str, bytes)):
            raise ServerGroupError("أدخل معرّفات السيرفرات كقائمة.")
        try:
            ids = list(guild_ids)
        except TypeError as error:
            raise ServerGroupError("قائمة معرّفات السيرفرات غير صالحة.") from error
        if any(
            isinstance(guild_id, bool)
            or not isinstance(guild_id, int)
            or guild_id <= 0
            for guild_id in ids
        ):
            raise ServerGroupError("تعذر التحقق من معرّفات السيرفرات.")
        if len(set(ids)) != len(ids):
            raise ServerGroupError("لا تكرر معرّف السيرفر في التغييرات.")
        return ids

    def preview_members(
        self,
        name: str,
        add_guild_ids: Iterable[int] = (),
        remove_guild_ids: Iterable[int] = (),
    ) -> dict[str, object]:
        cleaned = self._clean_name(name)
        key = self._key(cleaned)
        group = self._groups.get(key)
        if group is None:
            raise ServerGroupError("لم يتم العثور على المجموعة.")

        additions = self._clean_membership_change_ids(add_guild_ids)
        removals = self._clean_membership_change_ids(remove_guild_ids)
        if not additions and not removals:
            raise ServerGroupError("أدخل سيرفراً واحداً على الأقل للإضافة أو الإزالة.")
        if set(additions) & set(removals):
            raise ServerGroupError(
                "لا يمكن إضافة السيرفر وإزالته في التغيير نفسه."
            )

        current_ids = list(group["guild_ids"])
        existing_additions = [
            guild_id for guild_id in additions if guild_id in current_ids
        ]
        if existing_additions:
            raise ServerGroupError(
                "هذه السيرفرات موجودة بالفعل في المجموعة: "
                + ", ".join(str(guild_id) for guild_id in existing_additions)
                + "."
            )
        missing_removals = [
            guild_id for guild_id in removals if guild_id not in current_ids
        ]
        if missing_removals:
            raise ServerGroupError(
                "هذه السيرفرات غير موجودة في المجموعة: "
                + ", ".join(str(guild_id) for guild_id in missing_removals)
                + "."
            )

        removal_ids = set(removals)
        updated_ids = [
            guild_id for guild_id in current_ids if guild_id not in removal_ids
        ] + additions
        updated_ids = self._clean_guild_ids(updated_ids)
        return {"name": group["name"], "guild_ids": updated_ids}

    def update_members(
        self,
        name: str,
        add_guild_ids: Iterable[int] = (),
        remove_guild_ids: Iterable[int] = (),
        *,
        developer_id: int,
    ) -> dict[str, object]:
        cleaned = self._clean_name(name)
        key = self._key(cleaned)
        group = self._groups.get(key)
        if group is None:
            raise ServerGroupError("لم يتم العثور على المجموعة.")

        updated = self.preview_members(name, add_guild_ids, remove_guild_ids)
        self._groups[key] = {
            "name": updated["name"],
            "guild_ids": list(updated["guild_ids"]),
        }
        try:
            self._save_membership_change(
                "updated",
                str(updated["name"]),
                list(group["guild_ids"]),
                list(updated["guild_ids"]),
                developer_id,
            )
        except Exception:
            self._groups[key] = group
            raise
        return self.get_group(cleaned)

    def rename(
        self,
        name: str,
        new_name: str,
        *,
        developer_id: int,
    ) -> dict[str, object]:
        old_name = self._clean_name(name)
        cleaned = self._clean_name(new_name)
        old_key = self._key(old_name)
        new_key = self._key(cleaned)
        group = self._groups.get(old_key)
        if group is None:
            raise ServerGroupError("لم يتم العثور على المجموعة.")
        if new_key != old_key and new_key in self._groups:
            raise ServerGroupError("يوجد بالفعل اسم مجموعة مطابق.")

        before = {"name": group["name"], "guild_ids": list(group["guild_ids"])}
        del self._groups[old_key]
        self._groups[new_key] = {"name": cleaned, "guild_ids": before["guild_ids"]}
        try:
            self._save_membership_change(
                "renamed",
                cleaned,
                list(before["guild_ids"]),
                list(before["guild_ids"]),
                developer_id,
                old_group_name=str(before["name"]),
            )
        except Exception:
            del self._groups[new_key]
            self._groups[old_key] = before
            raise
        return self.get_group(cleaned)

    def remove(
        self,
        name: str,
        *,
        developer_id: int,
    ) -> dict[str, object]:
        cleaned = self._clean_name(name)
        key = self._key(cleaned)
        group = self._groups.pop(key, None)
        if group is None:
            raise ServerGroupError("لم يتم العثور على المجموعة.")
        try:
            self._save_membership_change(
                "deleted",
                str(group["name"]),
                list(group["guild_ids"]),
                [],
                developer_id,
            )
        except Exception:
            self._groups[key] = group
            raise
        return {"name": group["name"], "guild_ids": list(group["guild_ids"])}

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (
            not isinstance(payload, dict)
            or set(payload) not in ({"groups"}, {"groups", "membership_history"})
            or not isinstance(payload["groups"], list)
            or (
                "membership_history" in payload
                and not isinstance(payload["membership_history"], list)
            )
        ):
            raise ValueError(f"Invalid saved server groups in {self.path}.")

        groups: dict[str, dict[str, object]] = {}
        for item in payload["groups"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "guild_ids"}
            ):
                raise ValueError(f"Invalid saved server groups in {self.path}.")
            try:
                name = self._clean_name(item["name"])
                guild_ids = self._clean_guild_ids(item["guild_ids"])
            except ServerGroupError as error:
                raise ValueError(
                    f"Invalid saved server groups in {self.path}."
                ) from error
            key = self._key(name)
            if key in groups:
                raise ValueError(f"Duplicate saved server groups in {self.path}.")
            groups[key] = {"name": name, "guild_ids": guild_ids}
        if len(groups) > MAX_SERVER_GROUPS:
            raise ValueError(f"Too many saved server groups in {self.path}.")
        history = payload.get("membership_history", [])
        allowed_actions = {
            "created",
            "added",
            "removed",
            "updated",
            "deleted",
            "renamed",
        }
        for entry in history:
            is_rename = isinstance(entry, dict) and entry.get("action") == "renamed"
            expected_entry_keys = {
                "developer_id",
                "timestamp",
                "action",
                "group_name",
                "before_guild_ids",
                "after_guild_ids",
            }
            if is_rename:
                expected_entry_keys.add("old_group_name")
            if (
                not isinstance(entry, dict)
                or set(entry) != expected_entry_keys
                or isinstance(entry["developer_id"], bool)
                or not isinstance(entry["developer_id"], int)
                or entry["developer_id"] <= 0
                or not isinstance(entry["action"], str)
                or entry["action"] not in allowed_actions
            ):
                raise ValueError(
                    f"Invalid saved server group history in {self.path}."
                )
            try:
                timestamp = datetime.fromisoformat(entry["timestamp"])
                name = self._clean_name(entry["group_name"])
                old_name = (
                    self._clean_name(entry["old_group_name"])
                    if is_rename
                    else None
                )
                before_ids = self._clean_membership_history_ids(
                    entry["before_guild_ids"]
                )
                after_ids = self._clean_membership_history_ids(
                    entry["after_guild_ids"]
                )
            except (TypeError, ValueError, ServerGroupError) as error:
                raise ValueError(
                    f"Invalid saved server group history in {self.path}."
                ) from error
            if (
                timestamp.tzinfo is None
                or (entry["action"] == "created" and (before_ids or not after_ids))
                or (entry["action"] == "deleted" and (not before_ids or after_ids))
                or (
                    entry["action"] in {"added", "removed", "updated"}
                    and (not before_ids or not after_ids)
                )
                or (is_rename and before_ids != after_ids)
            ):
                raise ValueError(
                    f"Invalid saved server group history in {self.path}."
                )
            entry["group_name"] = name
            if is_rename:
                entry["old_group_name"] = old_name
            entry["before_guild_ids"] = before_ids
            entry["after_guild_ids"] = after_ids
        self._groups = groups
        self._membership_history = [deepcopy(entry) for entry in history]

    @classmethod
    def _clean_membership_history_ids(cls, guild_ids: Iterable[int]) -> list[int]:
        ids = cls._clean_membership_change_ids(guild_ids)
        if ids:
            return cls._clean_guild_ids(ids)
        return ids

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temp_path = Path(output.name)
                json.dump(
                    {
                        "groups": self.list_groups(),
                        "membership_history": self._membership_history,
                    },
                    output,
                    ensure_ascii=False,
                    indent=2,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)


class ServerGroupMembershipChangeView(discord.ui.View):
    """Private, one-shot confirmation for a saved-group membership preview."""

    def __init__(
        self,
        store: ServerGroupStore,
        expected_group: dict[str, object],
        add_guild_ids: list[int],
        remove_guild_ids: list[int],
        developer_id: int,
        source_guild_id: int | None,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.store = store
        self.expected_group = {
            "name": expected_group["name"],
            "guild_ids": list(expected_group["guild_ids"]),
        }
        self.add_guild_ids = list(add_guild_ids)
        self.remove_guild_ids = list(remove_guild_ids)
        self.developer_id = developer_id
        self.source_guild_id = source_guild_id
        self.finished = False
        self.message = None

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
            )
            return False
        if getattr(interaction, "guild_id", None) != self.source_guild_id:
            await interaction.response.send_message(
                "يجب تأكيد التغيير من السيرفر نفسه الذي عُرضت فيه المعاينة.",
                ephemeral=True,
            )
            return False
        return True

    def _preview_is_current(self) -> bool:
        try:
            current_group = self.store.get_group(str(self.expected_group["name"]))
        except ServerGroupError:
            return False
        return current_group == self.expected_group

    @discord.ui.button(label="تأكيد وحفظ", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذه المعاينة.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.defer()
        try:
            if not self._preview_is_current():
                response = (
                    "تغيرت المجموعة منذ المعاينة؛ لم يُحفظ أي تغيير. "
                    "شغّل الأمر مجدداً لمعاينة جديدة."
                )
            else:
                updated = self.store.update_members(
                    str(self.expected_group["name"]),
                    self.add_guild_ids,
                    self.remove_guild_ids,
                    developer_id=self.developer_id,
                )
                response = (
                    f"حُدّثت المجموعة "
                    f"«{discord.utils.escape_markdown(str(updated['name']))}» "
                    f"({len(updated['guild_ids'])} سيرفرات)."
                )
        except ServerGroupError as error:
            response = f"{error} لم يُحفظ أي تغيير."
        except OSError:
            logger.exception("Could not update a filter server group.")
            response = "تعذر حفظ التغيير؛ لم تتغير المجموعة المحفوظة."
        await interaction.edit_original_response(
            content=response,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذه المعاينة.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت المعاينة؛ لم تتغير المجموعة المحفوظة.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة المعاينة؛ لم تتغير المجموعة المحفوظة.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not disable expired saved-group membership preview."
                )


class ServerGroupRenameReversalView(discord.ui.View):
    """Private, one-shot confirmation for reversing a saved-group rename."""

    def __init__(
        self,
        store: ServerGroupStore,
        entry_reference: str,
        expected_entry: dict[str, object],
        expected_group: dict[str, object],
        developer_id: int,
        source_guild_id: int | None,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.store = store
        self.entry_reference = entry_reference
        self.expected_entry = deepcopy(expected_entry)
        self.expected_group = {
            "name": expected_group["name"],
            "guild_ids": list(expected_group["guild_ids"]),
        }
        self.developer_id = developer_id
        self.source_guild_id = source_guild_id
        self.finished = False
        self.message = None

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if getattr(interaction, "guild_id", None) != self.source_guild_id:
            await interaction.response.send_message(
                "يجب تأكيد التغيير من السيرفر نفسه الذي عُرضت فيه المعاينة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    @discord.ui.button(label="تأكيد التراجع", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذه المعاينة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.defer()
        try:
            reversed_group = self.store.undo_rename_entry(
                self.entry_reference,
                self.expected_entry,
                self.expected_group,
                developer_id=self.developer_id,
            )
            restored_name = discord.utils.escape_markdown(
                str(reversed_group["name"])
            )
            response = (
                f"تم التراجع عن إعادة التسمية وإعادة الاسم إلى "
                f"«{restored_name}» مع الحفاظ على "
                f"{len(reversed_group['guild_ids'])} سيرفرات."
            )
        except ServerGroupError as error:
            response = f"{error} لم يُحفظ أي تغيير."
        except OSError:
            logger.exception("Could not reverse a saved server group rename.")
            response = "تعذر حفظ التراجع؛ لم تتغير المجموعة المحفوظة."
        await interaction.edit_original_response(
            content=response,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذه المعاينة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغي التراجع؛ لم تتغير المجموعة أو سجلها.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة المعاينة؛ لم يتغير الاسم أو سجل المجموعة.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not disable expired saved-group rename reversal."
                )


class ServerGroupRemovalSelect(discord.ui.Select):
    def __init__(
        self,
        options: list[discord.SelectOption],
        max_values: int,
    ):
        super().__init__(
            placeholder="اختر أعضاء المجموعة المراد إزالتهم",
            min_values=1,
            max_values=max_values,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ServerGroupRemovalSelectionView):
            await view.submit(interaction, self.values)


class ServerGroupRemovalSelectionView(discord.ui.View):
    """Private multi-select flow for choosing saved-group members to remove."""

    def __init__(
        self,
        options: list[discord.SelectOption],
        max_values: int,
        developer_id: int,
        source_guild_id: int | None,
        on_select,
        store: ServerGroupStore,
        expected_group: dict[str, object],
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.allowed_values = {option.value for option in options}
        self.max_values = max_values
        self.developer_id = developer_id
        self.source_guild_id = source_guild_id
        self.on_select = on_select
        self.store = store
        self.expected_group = {
            "name": expected_group["name"],
            "guild_ids": list(expected_group["guild_ids"]),
        }
        self.finished = False
        self.message = None
        self.add_item(ServerGroupRemovalSelect(options, max_values))

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا الاختيار متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
            )
            return False
        if getattr(interaction, "guild_id", None) != self.source_guild_id:
            await interaction.response.send_message(
                "يجب اختيار الأعضاء من السيرفر نفسه الذي بدأ الأمر.",
                ephemeral=True,
            )
            return False
        return True

    async def submit(
        self,
        interaction: discord.Interaction,
        values: list[str],
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذا الاختيار.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        if (
            not values
            or len(values) > self.max_values
            or any(
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 20
                for value in values
            )
            or len(set(values)) != len(values)
            or any(value not in self.allowed_values for value in values)
        ):
            await interaction.response.send_message(
                "اختر أعضاء مختلفين من المجموعة المحددة فقط.",
                ephemeral=True,
            )
            return

        current_group = self.store.get_group(str(self.expected_group["name"]))
        if (
            current_group is None
            or current_group["guild_ids"] != self.expected_group["guild_ids"]
        ):
            self._finish()
            await interaction.response.edit_message(
                content=(
                    "تغيرت عضوية المجموعة منذ فتح قائمة الاختيار؛ "
                    "لم تُنشأ معاينة ولم يُحفظ أي تغيير. "
                    "أعد فتح القائمة لاختيار الأعضاء الحاليين."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        self._finish()
        await self.on_select(interaction, [int(value) for value in values])

    @discord.ui.button(
        label="إلغاء",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذا الاختيار.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغي اختيار الأعضاء؛ لم تتغير المجموعة المحفوظة.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة اختيار الأعضاء؛ لم تتغير المجموعة المحفوظة.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not disable expired saved-group removal selection."
                )


class ServerGroupAdditionSelect(discord.ui.Select):
    def __init__(
        self,
        options: list[discord.SelectOption],
        max_values: int,
    ):
        super().__init__(
            placeholder="اختر السيرفرات المتصلة المراد إضافتها",
            min_values=0,
            max_values=max_values,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ServerGroupAdditionSelectionView):
            await view.update_page_selection(interaction, self.values)


class ServerGroupAdditionSelectionButton(discord.ui.Button):
    def __init__(
        self,
        label: str,
        action: str,
        *,
        disabled: bool = False,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ):
        super().__init__(label=label, style=style, disabled=disabled, row=1)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ServerGroupAdditionSelectionView):
            await view.handle_button(interaction, self.action)


class ServerGroupAdditionSelectionView(discord.ui.View):
    """Private paginated picker for connected servers to add to a saved group."""

    PAGE_SIZE = 25

    def __init__(
        self,
        options: list[discord.SelectOption],
        max_values: int,
        developer_id: int,
        source_guild_id: int | None,
        on_select,
        store: ServerGroupStore,
        expected_group: dict[str, object],
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.options = list(options)
        self.max_values = min(max_values, MAX_SERVER_GROUP_SIZE)
        self.page = 0
        self.selected_by_page: dict[int, set[str]] = {}
        self.developer_id = developer_id
        self.source_guild_id = source_guild_id
        self.on_select = on_select
        self.store = store
        self.expected_group = {
            "name": expected_group["name"],
            "guild_ids": list(expected_group["guild_ids"]),
        }
        self.finished = False
        self.message = None
        self._refresh_components()

    @property
    def page_count(self) -> int:
        return (len(self.options) + self.PAGE_SIZE - 1) // self.PAGE_SIZE

    def _page_options(self) -> list[discord.SelectOption]:
        start = self.page * self.PAGE_SIZE
        return self.options[start : start + self.PAGE_SIZE]

    def _selected_ids(self) -> list[int]:
        selected = {
            value
            for page_values in self.selected_by_page.values()
            for value in page_values
        }
        return [
            int(option.value)
            for option in self.options
            if option.value in selected
        ]

    def _prompt(self, notice: str | None = None) -> str:
        selected_count = len(self._selected_ids())
        text = (
            f"اختر حتى {self.max_values} سيرفراً متصلاً لإضافتها. "
            f"المحدد: {selected_count}/{self.max_values}. "
            f"الصفحة {self.page + 1} من {self.page_count}. "
            "استخدم زر المعاينة عند الانتهاء."
        )
        return f"{notice}\n{text}" if notice else text

    def _refresh_components(self) -> None:
        self.clear_items()
        page_options = self._page_options()
        selected_on_page = self.selected_by_page.get(self.page, set())
        options = [
            discord.SelectOption(
                label=option.label,
                value=option.value,
                description=option.description,
                default=option.value in selected_on_page,
            )
            for option in page_options
        ]
        self.add_item(
            ServerGroupAdditionSelect(
                options,
                min(self.max_values, len(options)),
            )
        )
        self.add_item(
            ServerGroupAdditionSelectionButton(
                "السابق",
                "previous",
                disabled=self.page == 0,
            )
        )
        self.add_item(
            ServerGroupAdditionSelectionButton(
                "التالي",
                "next",
                disabled=self.page >= self.page_count - 1,
            )
        )
        self.add_item(
            ServerGroupAdditionSelectionButton(
                "معاينة الإضافات",
                "preview",
                style=discord.ButtonStyle.primary,
            )
        )
        self.add_item(
            ServerGroupAdditionSelectionButton(
                "إلغاء",
                "cancel",
            )
        )

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا الاختيار متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if getattr(interaction, "guild_id", None) != self.source_guild_id:
            await interaction.response.send_message(
                "يجب اختيار السيرفرات من السيرفر نفسه الذي بدأ الأمر.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    def _selection_is_current(self) -> bool:
        try:
            current_group = self.store.get_group(str(self.expected_group["name"]))
        except ServerGroupError:
            return False
        return current_group == self.expected_group

    async def _reject_stale_selection(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if self._selection_is_current():
            return False
        self._finish()
        await interaction.response.edit_message(
            content=(
                "تغيرت عضوية المجموعة منذ فتح قائمة الاختيار؛ "
                "لم تُنشأ معاينة ولم يُحفظ أي تغيير. "
                "أعد فتح القائمة لاختيار الأعضاء الحاليين."
            ),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    async def update_page_selection(
        self,
        interaction: discord.Interaction,
        values: list[str],
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت مهلة هذا الاختيار.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._authorize(interaction):
            return
        if await self._reject_stale_selection(interaction):
            return

        allowed_values = {option.value for option in self._page_options()}
        if (
            len(values) > self.max_values
            or len(set(values)) != len(values)
            or any(
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 20
                or value not in allowed_values
                for value in values
            )
        ):
            await interaction.response.send_message(
                "اختر سيرفرات مختلفة من الصفحة الحالية فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        selected_elsewhere = {
            value
            for page_number, page_values in self.selected_by_page.items()
            if page_number != self.page
            for value in page_values
        }
        if len(selected_elsewhere) + len(values) > self.max_values:
            await interaction.response.send_message(
                "تجاوز الاختيار الحد المتاح للإضافات. أزل بعض الاختيارات من "
                "الصفحات السابقة أو اعرض المعاينة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        self.selected_by_page[self.page] = set(values)
        self._refresh_components()
        await interaction.response.edit_message(
            content=self._prompt(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_button(
        self,
        interaction: discord.Interaction,
        action: str,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت مهلة هذا الاختيار.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not await self._authorize(interaction):
            return
        if await self._reject_stale_selection(interaction):
            return

        if action == "cancel":
            self._finish()
            await interaction.response.edit_message(
                content="أُلغي اختيار السيرفرات؛ لم تتغير المجموعة المحفوظة.",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "previous":
            self.page = max(0, self.page - 1)
            self._refresh_components()
            await interaction.response.edit_message(
                content=self._prompt(),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "next":
            self.page = min(self.page_count - 1, self.page + 1)
            self._refresh_components()
            await interaction.response.edit_message(
                content=self._prompt(),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action != "preview":
            return

        selected_ids = self._selected_ids()
        if not selected_ids:
            await interaction.response.edit_message(
                content=self._prompt("اختر سيرفراً واحداً على الأقل أولاً."),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self._finish()
        await self.on_select(interaction, selected_ids)

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة اختيار السيرفرات؛ لم تتغير المجموعة المحفوظة.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not disable expired saved-group addition selection."
                )


def contains_bad_word(
    text: str | None,
    terms: Iterable[str] = BAD_WORDS,
) -> str | None:
    """Return the matched configured term, using whole words and phrases."""
    if not text:
        return None

    words = _normalize(text).split()
    if not words:
        return None

    for configured_term in terms:
        term_words = _normalize(configured_term).split()
        width = len(term_words)
        for index in range(len(words) - width + 1):
            candidate = words[index : index + width]
            if candidate == term_words:
                return configured_term

            # Match Arabic definite-article forms (for example, "الغبي")
            # without matching the configured term inside unrelated words.
            if width == 1 and candidate[0].startswith("ال"):
                if candidate[0][2:] == term_words[0]:
                    return configured_term
    return None


class GlobalFilterChangeView(discord.ui.View):
    """Private, one-shot confirmation for a previewed global filter change."""

    def __init__(
        self,
        store: ProfanityTermStore,
        guild_store: GuildProfanityTermStore | None,
        bot,
        action: str,
        term: str,
        developer_id: int,
        source_guild_id: int | None,
        expected_global_terms: list[str],
        expected_overrides: dict[int, dict[str, list[str]]],
        expected_guild_ids: set[int],
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.store = store
        self.guild_store = guild_store
        self.bot = bot
        self.action = action
        self.term = term
        self.developer_id = developer_id
        self.source_guild_id = source_guild_id
        self.expected_global_terms = expected_global_terms.copy()
        self.expected_overrides = deepcopy(expected_overrides)
        self.expected_guild_ids = set(expected_guild_ids)
        self.finished = False
        self.expired = False
        self.message = None

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
            )
            return False
        if getattr(interaction, "guild_id", None) != self.source_guild_id:
            await interaction.response.send_message(
                "يجب تأكيد التغيير من السيرفر نفسه الذي عُرضت فيه المعاينة.",
                ephemeral=True,
            )
            return False
        return True

    def _preview_is_current(self) -> bool:
        guilds = list(getattr(self.bot, "guilds", ()))
        if {guild.id for guild in guilds} != self.expected_guild_ids:
            return False
        if self.store.list_terms() != self.expected_global_terms:
            return False
        if self.guild_store is not None:
            for guild in guilds:
                if (
                    self.guild_store.list_saved_overrides(guild.id)
                    != self.expected_overrides.get(guild.id)
                ):
                    return False
        return True

    @discord.ui.button(label="تأكيد وحفظ", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                (
                    "انتهت مهلة التأكيد؛ لم تتغير القائمة العامة أو إعدادات السيرفرات."
                    if self.expired
                    else "انتهى استخدام هذا التأكيد."
                ),
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.defer()
        try:
            if not self._preview_is_current():
                response = (
                    "تغيرت القائمة العامة أو تعديلات السيرفرات أو السيرفرات "
                    "المتصلة منذ المعاينة؛ لم يُحفظ أي تغيير. شغّل الأمر مجدداً "
                    "لمعاينة جديدة."
                )
            elif self.action == "add":
                added = self.store.add(self.term, self.developer_id)
                response = (
                    f"أُضيفت «{discord.utils.escape_markdown(added)}» "
                    "إلى قائمة المرشح."
                )
            else:
                removed = self.store.remove(self.term, self.developer_id)
                response = (
                    f"أُزيلت «{discord.utils.escape_markdown(removed)}» "
                    "من قائمة المرشح."
                )
        except ProfanityTermError as error:
            response = f"{error} لم يُحفظ أي تغيير."
        except OSError:
            logger.exception("Could not save the filtered-term list.")
            response = "تعذر حفظ القائمة؛ لم يتغير إعداد المرشح."
        await interaction.edit_original_response(
            content=response,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                (
                    "انتهت مهلة التأكيد؛ لم تتغير القائمة العامة أو إعدادات السيرفرات."
                    if self.expired
                    else "انتهى استخدام هذا التأكيد."
                ),
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت المعاينة؛ لم تتغير القائمة العامة أو إعدادات السيرفرات.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.expired = True
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة التأكيد؛ لم تتغير الإعدادات.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning("Could not disable expired global filter preview.")


class GlobalFilterBatchChangeView(discord.ui.View):
    """Private, one-shot confirmation for a batch of global filter changes."""

    def __init__(
        self,
        store: ProfanityTermStore,
        guild_store: GuildProfanityTermStore | None,
        bot,
        add_terms: list[str],
        remove_terms: list[str],
        developer_id: int,
        source_guild_id: int | None,
        expected_global_terms: list[str],
        expected_overrides: dict[int, dict[str, list[str]]],
        expected_guild_ids: set[int],
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.store = store
        self.guild_store = guild_store
        self.bot = bot
        self.add_terms = list(add_terms)
        self.remove_terms = list(remove_terms)
        self.developer_id = developer_id
        self.source_guild_id = source_guild_id
        self.expected_global_terms = expected_global_terms.copy()
        self.expected_overrides = deepcopy(expected_overrides)
        self.expected_guild_ids = set(expected_guild_ids)
        self.finished = False
        self.expired = False
        self.message = None

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
            )
            return False
        if getattr(interaction, "guild_id", None) != self.source_guild_id:
            await interaction.response.send_message(
                "يجب تأكيد التغيير من السيرفر نفسه الذي عُرضت فيه المعاينة.",
                ephemeral=True,
            )
            return False
        return True

    def _preview_is_current(self) -> bool:
        guilds = list(getattr(self.bot, "guilds", ()))
        if {guild.id for guild in guilds} != self.expected_guild_ids:
            return False
        if self.store.list_terms() != self.expected_global_terms:
            return False
        if self.guild_store is not None:
            for guild in guilds:
                if (
                    self.guild_store.list_saved_overrides(guild.id)
                    != self.expected_overrides.get(guild.id)
                ):
                    return False
        return True

    @discord.ui.button(label="تأكيد وحفظ", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                (
                    "انتهت مهلة المعاينة؛ لم تتغير القائمة العامة أو إعدادات السيرفرات."
                    if self.expired
                    else "انتهى استخدام هذا التأكيد."
                ),
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.defer()
        try:
            if not self._preview_is_current():
                response = (
                    "تغيرت القائمة العامة أو تعديلات السيرفرات أو السيرفرات "
                    "المتصلة منذ المعاينة؛ لم يُحفظ أي تغيير. شغّل الأمر مجدداً "
                    "لمعاينة جديدة."
                )
            else:
                changes = self.store.apply_changes(
                    self.add_terms,
                    self.remove_terms,
                    self.developer_id,
                )
                added = "، ".join(
                    f"«{discord.utils.escape_markdown(term)}»"
                    for term in changes["added"]
                )
                removed = "، ".join(
                    f"«{discord.utils.escape_markdown(term)}»"
                    for term in changes["removed"]
                )
                details = []
                if added:
                    details.append(f"أُضيفت: {added}")
                if removed:
                    details.append(f"أُزيلت: {removed}")
                response = "حُفظت التغييرات دفعة واحدة. " + "؛ ".join(details)
        except ProfanityTermError as error:
            response = f"{error} لم يُحفظ أي تغيير."
        except OSError:
            logger.exception("Could not save the batch filtered-term changes.")
            response = "تعذر حفظ القائمة؛ لم يتغير إعداد المرشح."
        await interaction.edit_original_response(
            content=response,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                (
                    "انتهت مهلة المعاينة؛ لم تتغير القائمة العامة أو إعدادات السيرفرات."
                    if self.expired
                    else "انتهى استخدام هذا التأكيد."
                ),
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت المعاينة؛ لم تتغير القائمة العامة أو إعدادات السيرفرات.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.expired = True
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة المعاينة؛ لم تتغير الإعدادات.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning(
                    "Could not disable expired global filter batch preview."
                )


class GuildFilterResetView(discord.ui.View):
    """One-shot developer confirmation for resetting one guild's overrides."""

    def __init__(
        self,
        store: GuildProfanityTermStore,
        guild_id: int,
        developer_id: int,
        expected_overrides: dict[str, list[str]] | None = None,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.store = store
        self.guild_id = guild_id
        self.developer_id = developer_id
        self.expected_overrides = (
            deepcopy(expected_overrides) if expected_overrides is not None else None
        )
        self.finished = False
        self.message = None

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
            )
            return False
        if getattr(interaction, "guild_id", None) != self.guild_id:
            await interaction.response.send_message(
                "يجب تأكيد إعادة الضبط من السيرفر نفسه.",
                ephemeral=True,
            )
            return False
        return True

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="تأكيد إعادة الضبط", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذا التأكيد.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.defer()
        try:
            backup_path = self.store.prepare_reset_backup(
                self.guild_id,
                expected_overrides=self.expected_overrides,
            )
        except ProfanityTermError as error:
            content = str(error)
        except (OSError, ValueError):
            logger.exception(
                "Could not back up/reset profanity overrides for guild %s.",
                self.guild_id,
            )
            content = "تعذر حفظ النسخة الاحتياطية أو الإعدادات؛ لم يتغير المرشح."
        else:
            if backup_path is None:
                content = "لم تعد هناك إضافات أو استثناءات محفوظة؛ لم يتغير شيء."
            else:
                backup_file = None
                try:
                    backup_file = discord.File(
                        str(backup_path), filename=backup_path.name
                    )
                    await interaction.user.send(
                        content=(
                            "نسخة استعادة خاصة لتعديلات مرشح اللغة "
                            f"للسيرفر ID {self.guild_id}. احتفظ بهذه الرسالة "
                            "أو نزّل الملف لاستعادته بعد استبدال الخادم."
                        ),
                        file=backup_file,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.HTTPException, OSError):
                    logger.exception(
                        "Could not deliver private profanity-filter recovery "
                        "copy for guild %s.",
                        self.guild_id,
                    )
                    content = (
                        "تعذر إرسال نسخة الاستعادة إلى رسائلك الخاصة؛ "
                        "لم يتغير المرشح."
                    )
                else:
                    try:
                        result = self.store.reset_guild(
                            self.guild_id,
                            self.developer_id,
                            expected_overrides=self.expected_overrides,
                            backup_path=backup_path,
                        )
                    except ProfanityTermError as error:
                        content = (
                            f"{error} أُرسلت نسخة الاستعادة إلى رسائلك الخاصة."
                        )
                    except (OSError, ValueError):
                        logger.exception(
                            "Recovery copy delivered but filter reset could not "
                            "be saved for guild %s.",
                            self.guild_id,
                        )
                        content = (
                            "أُرسلت نسخة الاستعادة إلى رسائلك الخاصة، لكن تعذر "
                            "حفظ الإعدادات؛ لم يتغير المرشح."
                        )
                    else:
                        if result is None:
                            content = (
                                "لم تعد هناك إضافات أو استثناءات محفوظة؛ "
                                "لم يتغير شيء."
                            )
                        else:
                            content = (
                                "أُعيد مرشح هذا السيرفر إلى القائمة العامة. "
                                "أُرسلت نسخة الاستعادة الخاصة إلى رسائلك، "
                                f"({len(result['added'])} إضافة، "
                                f"{len(result['removed'])} استثناء)."
                            )
                finally:
                    if backup_file is not None:
                        backup_file.close()
        await interaction.edit_original_response(
            content=content,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذا التأكيد.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت إعادة الضبط؛ لم تتغير الإعدادات.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة التأكيد؛ لم تتغير الإعدادات.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning("Could not disable expired filter reset confirmation.")


class GuildFilterRestoreView(discord.ui.View):
    """One-shot developer confirmation for restoring one guild's overrides."""

    def __init__(
        self,
        store: GuildProfanityTermStore,
        guild_id: int,
        developer_id: int,
        restored_state: dict[str, list],
        expected_state: dict[str, list],
        *,
        restore_types: tuple[str, ...] = ("added", "removed"),
        restore_mode: str = "replace",
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.store = store
        self.guild_id = guild_id
        self.developer_id = developer_id
        self.restored_state = deepcopy(restored_state)
        self.expected_state = deepcopy(expected_state)
        self.restore_types = restore_types
        self.restore_mode = restore_mode
        type_labels = {
            "added": "الإضافات",
            "removed": "الاستثناءات",
        }
        selected_labels = " و".join(type_labels[kind] for kind in restore_types)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "تأكيد الاستعادة":
                if restore_mode == "merge":
                    child.label = f"تأكيد دمج الاستعادة: {selected_labels}"
                else:
                    child.label = f"تأكيد الاستعادة: {selected_labels}"
        self.finished = False
        self.message = None

    async def _authorize(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت الذي بدأ الأمر فقط.",
                ephemeral=True,
            )
            return False
        if getattr(interaction, "guild_id", None) != self.guild_id:
            await interaction.response.send_message(
                "يجب تأكيد الاستعادة من السيرفر نفسه.",
                ephemeral=True,
            )
            return False
        return True

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="تأكيد الاستعادة", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت مهلة التأكيد؛ لم تتغير الإعدادات.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.defer()
        try:
            backup_path = self.store.prepare_restore_backup(
                self.guild_id,
                expected_state=self.expected_state,
            )
        except ProfanityTermError as error:
            content = str(error)
        except (OSError, ValueError):
            logger.exception(
                "Could not prepare a profanity-filter restore backup for guild %s.",
                self.guild_id,
            )
            content = "تعذر حفظ النسخة الاحتياطية؛ لم يتغير المرشح."
        else:
            backup_file = None
            try:
                backup_file = discord.File(
                    str(backup_path), filename=backup_path.name
                )
                await interaction.user.send(
                    content=(
                        "نسخة استعادة خاصة للحالة السابقة لمرشح اللغة "
                        f"للسيرفر ID {self.guild_id}. احتفظ بهذه الرسالة "
                        "أو نزّل الملف لاستعادة الحالة بعد استبدال الخادم."
                    ),
                    file=backup_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.HTTPException, OSError):
                logger.exception(
                    "Could not deliver private profanity-filter recovery "
                    "copy before restore for guild %s.",
                    self.guild_id,
                )
                content = (
                    "تعذر إرسال نسخة الاستعادة إلى رسائلك الخاصة؛ "
                    "لم يتغير المرشح."
                )
            else:
                try:
                    result = self.store.restore_guild(
                        self.guild_id,
                        self.restored_state,
                        self.developer_id,
                        expected_state=self.expected_state,
                        restore_types=self.restore_types,
                        restore_mode=self.restore_mode,
                        backup_path=backup_path,
                    )
                except ProfanityTermError as error:
                    content = (
                        f"{error} أُرسلت نسخة الحالة السابقة إلى رسائلك الخاصة."
                    )
                except (OSError, ValueError):
                    logger.exception(
                        "Recovery copy delivered but profanity-filter restore "
                        "could not be saved for guild %s.",
                        self.guild_id,
                    )
                    content = (
                        "أُرسلت نسخة الحالة السابقة إلى رسائلك الخاصة، لكن "
                        "تعذر حفظ الإعدادات؛ لم يتغير المرشح."
                    )
                else:
                    type_labels = {
                        "added": "الإضافات",
                        "removed": "الاستثناءات",
                    }
                    selected_labels = " و".join(
                        type_labels[kind] for kind in self.restore_types
                    )
                    restore_action = (
                        "دُمجت" if self.restore_mode == "merge" else "استُعيدت"
                    )
                    content = (
                        f"{restore_action} {selected_labels} مرشح هذا السيرفر فقط. "
                        f"({len(result['added'])} إضافة، "
                        f"{len(result['removed'])} استثناء). "
                        "أُرسلت الحالة السابقة إلى رسائلك الخاصة، وحُفظت "
                        "أيضاً في نسخة احتياطية محلية."
                    )
            finally:
                if backup_file is not None:
                    backup_file.close()
        await interaction.edit_original_response(
            content=content,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذا التأكيد.",
                ephemeral=True,
            )
            return
        if not await self._authorize(interaction):
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت الاستعادة؛ لم تتغير الإعدادات.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة التأكيد؛ لم تتغير الإعدادات.",
                    view=self,
                )
            except discord.HTTPException:
                logger.warning("Could not disable expired filter restore confirmation.")


def setup_profanity_commands(
    bot,
    store: ProfanityTermStore,
    developer_id: int,
    guild_store: GuildProfanityTermStore | None = None,
    server_group_store: ServerGroupStore | None = None,
):
    """Register private, developer-only commands for managing filtered terms."""
    existing = getattr(bot, "_profanity_filter_command_group", None)
    if existing is not None:
        return existing

    group = app_commands.Group(
        name="filter",
        description="إدارة الكلمات والعبارات التي يحذفها مرشح اللغة",
    )

    async def reject_non_developer(interaction: discord.Interaction) -> bool:
        if interaction.user.id == developer_id:
            return False
        await interaction.response.send_message(
            "هذا الأمر خاص بمطور البوت فقط.",
            ephemeral=True,
        )
        return True

    async def send_private_chunks(
        interaction,
        text: str,
        empty_message: str = "لا توجد كلمات أو عبارات في القائمة.",
        view: discord.ui.View | None = None,
        *,
        edit_existing_message: bool = False,
    ) -> None:
        chunks = []
        current = ""
        for line in text.splitlines():
            if current and len(current) + len(line) + 1 > 1750:
                chunks.append(current)
                current = ""
            current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        if not chunks:
            chunks = [empty_message]
        if edit_existing_message:
            await interaction.response.edit_message(
                content=chunks[0],
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                chunks[0],
                ephemeral=True,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if view is not None:
            if edit_existing_message:
                view.message = getattr(interaction, "message", None)
            elif hasattr(interaction, "original_response"):
                view.message = await interaction.original_response()
        for chunk in chunks[1:]:
            await interaction.followup.send(
                chunk,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def preview_global_change(
        interaction: discord.Interaction,
        action: str,
        term: str,
    ) -> None:
        if await reject_non_developer(interaction):
            return
        try:
            cleaned, global_before, global_after = store.preview_change(action, term)
        except ProfanityTermError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        connected_guilds = sorted(
            getattr(bot, "guilds", ()),
            key=lambda guild: (str(getattr(guild, "name", "")).casefold(), guild.id),
        )
        expected_overrides: dict[int, dict[str, list[str]]] = {}
        gaining: list[str] = []
        losing: list[str] = []
        key = ProfanityTermStore._key(cleaned)
        for guild in connected_guilds:
            before_terms = (
                guild_store.list_terms(guild.id, global_before)
                if guild_store is not None
                else global_before
            )
            after_terms = (
                guild_store.list_terms(guild.id, global_after)
                if guild_store is not None
                else global_after
            )
            before_has_term = any(
                ProfanityTermStore._key(item) == key for item in before_terms
            )
            after_has_term = any(
                ProfanityTermStore._key(item) == key for item in after_terms
            )
            if before_has_term == after_has_term:
                continue
            server_name = discord.utils.escape_markdown(
                str(getattr(guild, "name", f"Server {guild.id}")).replace("\n", " ")
            )
            entry = f"• {server_name} (ID: {guild.id})"
            (gaining if after_has_term else losing).append(entry)

        if guild_store is not None:
            expected_overrides = {
                guild.id: guild_store.list_saved_overrides(guild.id)
                for guild in connected_guilds
            }
        view = GlobalFilterChangeView(
            store,
            guild_store,
            bot,
            action,
            cleaned,
            developer_id,
            getattr(interaction, "guild_id", None),
            global_before,
            expected_overrides,
            {guild.id for guild in connected_guilds},
        )
        operation_summary = (
            f"إضافة «{discord.utils.escape_markdown(cleaned)}» إلى القائمة العامة."
            if action == "add"
            else f"إزالة «{discord.utils.escape_markdown(cleaned)}» من القائمة العامة."
        )
        preview_lines = [
            f"معاينة خاصة — {operation_summary}",
            "لن تُحفظ أي تغييرات قبل التأكيد الصريح.",
            f"السيرفرات التي ستفعّل الكلمة ({len(gaining)}):",
            *(gaining or ["• لا يوجد"]),
            f"السيرفرات التي ستوقف الكلمة ({len(losing)}):",
            *(losing or ["• لا يوجد"]),
        ]
        if not connected_guilds:
            preview_lines.append("البوت غير متصل حالياً بأي سيرفر.")
        preview_lines.append("اختر «تأكيد وحفظ» للحفظ أو «إلغاء» دون تغيير الإعدادات.")
        await send_private_chunks(
            interaction,
            "\n".join(preview_lines),
            view=view,
        )

    async def preview_global_batch(
        interaction: discord.Interaction,
        add_terms_text: str | None,
        remove_terms_text: str | None,
    ) -> None:
        if await reject_non_developer(interaction):
            return

        def parse_terms(value: str | None) -> list[str]:
            if value is None or not value.strip():
                return []
            if len(value) > 4000:
                raise ProfanityTermError("لا يمكن أن يتجاوز كل حقل 4000 حرف.")
            terms = [part.strip() for part in value.split(",")]
            if any(not term for term in terms):
                raise ProfanityTermError(
                    "افصل الكلمات بفواصل، ولا تترك عنصراً فارغاً بين الفواصل."
                )
            return terms

        try:
            requested_additions = parse_terms(add_terms_text)
            requested_removals = parse_terms(remove_terms_text)
            (
                add_terms,
                remove_terms,
                global_before,
                global_after,
            ) = store.preview_changes(requested_additions, requested_removals)
        except ProfanityTermError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        connected_guilds = sorted(
            getattr(bot, "guilds", ()),
            key=lambda guild: (
                str(getattr(guild, "name", "")).casefold(),
                guild.id,
            ),
        )
        expected_overrides = (
            {
                guild.id: guild_store.list_saved_overrides(guild.id)
                for guild in connected_guilds
            }
            if guild_store is not None
            else {}
        )
        preview_lines = [
            "معاينة خاصة لدفعة تغييرات على القائمة العامة.",
            "لن تُحفظ أي تغييرات قبل التأكيد الصريح.",
        ]
        for action, terms in (("add", add_terms), ("remove", remove_terms)):
            for term in terms:
                key = ProfanityTermStore._key(term)
                gaining = []
                losing = []
                for guild in connected_guilds:
                    before_terms = (
                        guild_store.list_terms(guild.id, global_before)
                        if guild_store is not None
                        else global_before
                    )
                    after_terms = (
                        guild_store.list_terms(guild.id, global_after)
                        if guild_store is not None
                        else global_after
                    )
                    before_has_term = any(
                        ProfanityTermStore._key(item) == key
                        for item in before_terms
                    )
                    after_has_term = any(
                        ProfanityTermStore._key(item) == key
                        for item in after_terms
                    )
                    if before_has_term == after_has_term:
                        continue
                    server_name = discord.utils.escape_markdown(
                        str(
                            getattr(guild, "name", f"Server {guild.id}")
                        ).replace("\n", " ")
                    )
                    entry = f"• {server_name} (ID: {guild.id})"
                    (gaining if after_has_term else losing).append(entry)
                escaped_term = discord.utils.escape_markdown(term)
                if action == "add":
                    preview_lines.extend(
                        [
                            f"إضافة «{escaped_term}» — ستبدأ في "
                            f"{len(gaining)} سيرفرات:",
                            *(gaining or ["• لا يوجد"]),
                        ]
                    )
                else:
                    preview_lines.extend(
                        [
                            f"إزالة «{escaped_term}» — ستتوقف في "
                            f"{len(losing)} سيرفرات:",
                            *(losing or ["• لا يوجد"]),
                        ]
                    )
        if not connected_guilds:
            preview_lines.append("البوت غير متصل حالياً بأي سيرفر.")
        preview_lines.append(
            "اختر «تأكيد وحفظ» لحفظ الدفعة أو «إلغاء» دون تغيير الإعدادات."
        )
        view = GlobalFilterBatchChangeView(
            store,
            guild_store,
            bot,
            add_terms,
            remove_terms,
            developer_id,
            getattr(interaction, "guild_id", None),
            global_before,
            expected_overrides,
            {guild.id for guild in connected_guilds},
        )
        await send_private_chunks(
            interaction,
            "\n".join(preview_lines),
            view=view,
        )

    @group.command(name="add", description="إضافة كلمة أو عبارة إلى المرشح")
    @app_commands.describe(term="كلمة أو عبارة من خمس كلمات كحد أقصى")
    @app_commands.guild_only()
    async def add_term(interaction: discord.Interaction, term: str) -> None:
        await preview_global_change(interaction, "add", term)

    @group.command(name="remove", description="إزالة كلمة أو عبارة من المرشح")
    @app_commands.describe(term="الكلمة أو العبارة المطلوب إزالتها")
    @app_commands.guild_only()
    async def remove_term(interaction: discord.Interaction, term: str) -> None:
        await preview_global_change(interaction, "remove", term)

    @group.command(
        name="batch",
        description="معاينة عدة إضافات وإزالات عامة ثم حفظها معاً",
    )
    @app_commands.describe(
        add_terms="كلمات وعبارات للإضافة، افصل بينها بفواصل",
        remove_terms="كلمات وعبارات للإزالة، افصل بينها بفواصل",
    )
    @app_commands.guild_only()
    async def batch_terms(
        interaction: discord.Interaction,
        add_terms: str | None = None,
        remove_terms: str | None = None,
    ) -> None:
        await preview_global_batch(interaction, add_terms, remove_terms)

    @group.command(
        name="undo",
        description="التراجع عن آخر إضافة أو إزالة، أو اختيار سجل أقدم",
    )
    @app_commands.describe(
        entry_number="رقم التغيير الظاهر في سجل history (يبدأ من 1)",
    )
    @app_commands.guild_only()
    async def undo_term_change(
        interaction: discord.Interaction,
        entry_number: int | None = None,
    ) -> None:
        if await reject_non_developer(interaction):
            return
        try:
            if entry_number is not None and entry_number < 1:
                raise ProfanityTermError(
                    "رقم سجل التغيير يبدأ من 1 كما يظهر في أمر history."
                )
            entry = store.undo(
                interaction.user.id,
                None if entry_number is None else entry_number - 1,
            )
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception("Could not save blocked-term undo.")
            response = "تعذر حفظ التراجع؛ لم تتغير القائمة أو سجل التغييرات."
        else:
            action_labels = {"add": "إضافة", "remove": "إزالة"}
            response = (
                f"تم التراجع عن {action_labels[entry['undone_action']]} "
                f"الكلمة أو العبارة «{discord.utils.escape_markdown(str(entry['term']))}» "
                f"من سجل رقم {int(entry['undone_history_index']) + 1}."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(
        name="undo-batch",
        description="التراجع عن جميع تغييرات دفعة واحدة",
    )
    @app_commands.describe(
        entry_number="رقم أي تغيير من الدفعة الظاهر في سجل history (يبدأ من 1)",
    )
    @app_commands.guild_only()
    async def undo_batch_change(
        interaction: discord.Interaction,
        entry_number: int,
    ) -> None:
        if await reject_non_developer(interaction):
            return
        try:
            if entry_number < 1:
                raise ProfanityTermError(
                    "رقم سجل التغيير يبدأ من 1 كما يظهر في أمر history."
                )
            result = store.undo_batch(
                interaction.user.id,
                entry_number - 1,
            )
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception("Could not save blocked-term batch undo.")
            response = "تعذر حفظ التراجع عن الدفعة؛ لم تتغير القائمة أو سجل التغييرات."
        else:
            response = (
                f"تم التراجع عن الدفعة رقم {result['batch_number']} كاملةً "
                f"في خطوة واحدة (عدد التغييرات: {len(result['changes'])})."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="redo", description="إعادة آخر تغيير تم التراجع عنه")
    @app_commands.guild_only()
    async def redo_term_change(interaction: discord.Interaction) -> None:
        if await reject_non_developer(interaction):
            return
        try:
            entry = store.redo(interaction.user.id)
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception("Could not save blocked-term redo.")
            response = "تعذر حفظ الإعادة؛ لم تتغير القائمة أو سجل التغييرات."
        else:
            action_labels = {"add": "إضافة", "remove": "إزالة"}
            response = (
                f"تمت إعادة تطبيق {action_labels[entry['redone_action']]} "
                f"الكلمة أو العبارة «{discord.utils.escape_markdown(str(entry['term']))}»."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="list", description="عرض الكلمات والعبارات في المرشح")
    @app_commands.guild_only()
    async def list_terms(interaction: discord.Interaction) -> None:
        if await reject_non_developer(interaction):
            return
        terms = store.list_terms()
        lines = [f"• {discord.utils.escape_markdown(term)}" for term in terms]
        await send_private_chunks(
            interaction,
            f"قائمة الكلمات والعبارات المحظورة ({len(terms)}):\n"
            + ("\n".join(lines) if lines else "لا توجد كلمات أو عبارات في القائمة."),
        )

    @group.command(name="history", description="البحث في سجل تغييرات قائمة المرشح")
    @app_commands.describe(
        term="كلمة أو جزء من كلمة للبحث عنها",
        action="تصفية حسب نوع التغيير",
        start_date="بداية الفترة بالتنسيق YYYY-MM-DD (شامل)",
        end_date="نهاية الفترة بالتنسيق YYYY-MM-DD (شامل)",
        export_file="إرسال سجل التغييرات العامة كملف JSON خاص بدلاً من عرضه",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="إضافة", value="add"),
            app_commands.Choice(name="إزالة", value="remove"),
            app_commands.Choice(name="تراجع", value="undo"),
            app_commands.Choice(name="إعادة", value="redo"),
        ]
    )
    @app_commands.guild_only()
    async def list_changes(
        interaction: discord.Interaction,
        term: str | None = None,
        action: app_commands.Choice[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        export_file: bool = False,
    ) -> None:
        if await reject_non_developer(interaction):
            return
        action_value = (
            action.value if isinstance(action, app_commands.Choice) else action
        )
        action_labels = {
            "add": "إضافة",
            "remove": "إزالة",
            "undo": "تراجع",
            "redo": "إعادة",
        }
        try:
            history = store.search_history(
                term,
                action_value,
                start_date,
                end_date,
                include_index=True,
            )
        except ProfanityTermError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        filters = []
        if term is not None:
            filters.append(f"الكلمة: «{discord.utils.escape_markdown(term.strip())}»")
        if action_value is not None:
            filters.append(
                f"الإجراء: {action_labels.get(action_value, action_value)}"
            )
        if start_date is not None:
            filters.append(f"من: {start_date.strip()}")
        if end_date is not None:
            filters.append(f"إلى: {end_date.strip()}")

        if export_file:
            applied_filters = {
                "term": term.strip() if term is not None else None,
                "action": action_value,
                "start_date": start_date.strip() if start_date is not None else None,
                "end_date": end_date.strip() if end_date is not None else None,
            }
            attachment = None
            try:
                export = {
                    "title": "Global filter history",
                    "schema_version": 1,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "filters": applied_filters,
                    "entry_count": len(history),
                    "history": [entry.copy() for entry in history],
                }
                attachment = discord.File(
                    io.BytesIO(
                        json.dumps(
                            export,
                            ensure_ascii=False,
                            indent=2,
                        ).encode("utf-8")
                    ),
                    filename="filter-global-history.json",
                )
                await interaction.response.send_message(
                    "تم تجهيز تصدير سجل التغييرات العامة المطابق للفلاتر "
                    f"({len(history)} سجل).",
                    file=attachment,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                logger.exception("Could not export global blocked-term history.")
                if attachment is not None:
                    try:
                        attachment.close()
                    except Exception:
                        logger.exception(
                            "Could not close the failed global history attachment."
                        )
                error_message = (
                    "تعذر إعداد ملف سجل التغييرات العامة أو إرساله. "
                    "حاول مرة أخرى لاحقاً."
                )
                try:
                    response_is_done = getattr(interaction.response, "is_done", None)
                    if callable(response_is_done) and response_is_done():
                        await interaction.followup.send(
                            error_message,
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        await interaction.response.send_message(
                            error_message,
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except Exception:
                    logger.exception(
                        "Could not report global blocked-term history export failure."
                    )
            return

        lines = []
        shown_batch_counts: dict[int, int] = {}
        batch_sizes: dict[int, int] = {}
        for entry in history:
            history_index = int(entry["history_index"])
            batch_number = store.batch_number_for_history_index(history_index)
            if batch_number is not None:
                shown_batch_counts[batch_number] = (
                    shown_batch_counts.get(batch_number, 0) + 1
                )
                batch_sizes.setdefault(
                    batch_number,
                    len(store.batch_history_indexes(history_index)),
                )

        rendered_batches: set[int] = set()
        for entry in history:
            history_index = int(entry["history_index"])
            batch_number = store.batch_number_for_history_index(history_index)
            term_text = (
                f"«{discord.utils.escape_markdown(str(entry['term']))}»"
            )
            if entry["action"] == "undo":
                description = (
                    f"تراجع عن {action_labels[entry['undone_action']]} "
                    f"{term_text} (undo — عن سجل رقم "
                    f"{int(entry['undone_history_index']) + 1})"
                )
            elif entry["action"] == "redo":
                description = (
                    f"إعادة تطبيق {action_labels[entry['redone_action']]} "
                    f"{term_text} (redo)"
                )
            else:
                description = (
                    f"{action_labels[entry['action']]} ({entry['action']}) "
                    f"{term_text}"
                )

            if batch_number is not None:
                if batch_number not in rendered_batches:
                    shown_count = shown_batch_counts[batch_number]
                    batch_size = batch_sizes[batch_number]
                    count_text = (
                        f"{batch_size} تغييرات حُفظت معاً"
                        if shown_count == batch_size
                        else (
                            f"{shown_count} من {batch_size} تغييرات "
                            "مطابقة ضمن الدفعة"
                        )
                    )
                    lines.append(
                        f"دفعة رقم {batch_number} — {entry['timestamp']} — "
                        f"المطور: {entry['developer_id']} — {count_text}:"
                    )
                    rendered_batches.add(batch_number)
                lines.append(f"  • سجل رقم {history_index + 1} — {description}")
            else:
                lines.append(
                    f"• سجل رقم {history_index + 1} — {entry['timestamp']} — "
                    f"{description} (المطور: {entry['developer_id']})"
                )
        await send_private_chunks(
            interaction,
            (
                f"نتائج سجل تغييرات قائمة المرشح ({len(history)})"
                + (f" — {'؛ '.join(filters)}" if filters else "")
                + ":\n"
                + "\n".join(lines)
                if history
                else ""
            ),
            empty_message=(
                "لا توجد تغييرات تطابق معايير البحث."
                if filters
                else "لا توجد تغييرات مسجلة على القائمة."
            ),
        )

    if guild_store is not None:
        server_group_store = server_group_store or ServerGroupStore()

        async def get_server_id(interaction) -> int | None:
            if await reject_non_developer(interaction):
                return None
            guild_id = getattr(interaction, "guild_id", None)
            if (
                isinstance(guild_id, bool)
                or not isinstance(guild_id, int)
                or guild_id <= 0
            ):
                await interaction.response.send_message(
                    "هذا الأمر متاح داخل السيرفر فقط.",
                    ephemeral=True,
                )
                return None
            return guild_id

        @group.command(name="server-add", description="إضافة كلمة لهذا السيرفر فقط")
        @app_commands.describe(term="كلمة أو عبارة من خمس كلمات كحد أقصى")
        @app_commands.guild_only()
        async def add_server_term(interaction: discord.Interaction, term: str) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            try:
                added = guild_store.add(
                    guild_id,
                    term,
                    interaction.user.id,
                    store.list_terms(),
                )
            except ProfanityTermError as error:
                response = str(error)
            except OSError:
                logger.exception(
                    "Could not save filtered terms for guild %s.", guild_id
                )
                response = "تعذر حفظ القائمة؛ لم يتغير إعداد المرشح."
            else:
                response = (
                    f"أُضيفت «{discord.utils.escape_markdown(added)}» "
                    "إلى قائمة هذا السيرفر فقط."
                )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(
            name="server-remove", description="إزالة كلمة من قائمة هذا السيرفر فقط"
        )
        @app_commands.describe(term="الكلمة أو العبارة المطلوب إزالتها")
        @app_commands.guild_only()
        async def remove_server_term(
            interaction: discord.Interaction, term: str
        ) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            try:
                removed = guild_store.remove(
                    guild_id,
                    term,
                    interaction.user.id,
                    store.list_terms(),
                )
            except ProfanityTermError as error:
                response = str(error)
            except OSError:
                logger.exception(
                    "Could not save filtered terms for guild %s.", guild_id
                )
                response = "تعذر حفظ القائمة؛ لم يتغير إعداد المرشح."
            else:
                response = (
                    f"أُزيلت «{discord.utils.escape_markdown(removed)}» "
                    "من قائمة هذا السيرفر فقط."
                )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(name="server-list", description="عرض قائمة المرشح لهذا السيرفر")
        @app_commands.guild_only()
        async def list_server_terms(interaction: discord.Interaction) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            terms = guild_store.list_terms(guild_id, store.list_terms())
            lines = [f"• {discord.utils.escape_markdown(term)}" for term in terms]
            await send_private_chunks(
                interaction,
                f"قائمة هذا السيرفر ({len(terms)}):\n" + "\n".join(lines),
            )

        @group.command(
            name="server-overrides",
            description="عرض الإضافات والاستثناءات الخاصة بهذا السيرفر",
        )
        @app_commands.guild_only()
        async def list_server_overrides(interaction: discord.Interaction) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            statuses = guild_store.list_override_status(
                guild_id, store.list_terms()
            )

            def format_terms(terms: list[str], empty_message: str) -> str:
                return "\n".join(
                    f"• {discord.utils.escape_markdown(term)}" for term in terms
                ) or empty_message

            await send_private_chunks(
                interaction,
                "حالة تعديلات مرشح هذا السيرفر — عرض خاص، دون تغيير أي إعداد.\n"
                f"إضافات محلية فعالة الآن ({len(statuses['added_effective'])}):\n"
                + format_terms(
                    statuses["added_effective"], "لا توجد إضافات فعالة."
                )
                + f"\nإضافات محفوظة موجودة أيضاً في القائمة العامة "
                f"({len(statuses['added_retained'])}) — لا تغيّر القائمة الفعالة:\n"
                + format_terms(
                    statuses["added_retained"], "لا توجد إضافات متداخلة."
                )
                + f"\nاستثناءات فعالة من القائمة العامة "
                f"({len(statuses['removed_effective'])}):\n"
                + format_terms(
                    statuses["removed_effective"], "لا توجد استثناءات فعالة."
                )
                + f"\nاستثناءات محفوظة بلا تطابق حالي في القائمة العامة "
                f"({len(statuses['removed_retained'])}) — لا تغيّر القائمة الفعالة:\n"
                + format_terms(
                    statuses["removed_retained"], "لا توجد استثناءات محتفظ بها."
                ),
            )

        def parse_group_guild_ids(value: str) -> list[int]:
            if not isinstance(value, str):
                raise ServerGroupError("أدخل معرّفات السيرفرات مفصولة بفواصل.")
            raw_ids = [part.strip() for part in value.split(",")]
            if not raw_ids or any(
                not part.isascii() or not part.isdecimal() for part in raw_ids
            ):
                raise ServerGroupError(
                    "أدخل معرّفات صحيحة مفصولة بفواصل، دون عناصر فارغة."
                )
            ids = [int(part) for part in raw_ids]
            ids = ServerGroupStore._clean_guild_ids(ids)
            unavailable = [
                str(guild_id)
                for guild_id in ids
                if bot.get_guild(guild_id) is None
            ]
            if unavailable:
                raise ServerGroupError(
                    "يمكن حفظ السيرفرات المتصلة بالبوت فقط؛ تعذر العثور على "
                    f"(ID: {', '.join(unavailable)})."
                )
            return ids

        def parse_connected_guild_id(value: str) -> int:
            if (
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 20
            ):
                raise ServerGroupError("أدخل معرّف سيرفر صحيحاً.")
            guild_id = int(value)
            if guild_id <= 0:
                raise ServerGroupError("تعذر التحقق من هوية السيرفر.")
            if bot.get_guild(guild_id) is None:
                raise ServerGroupError(
                    "يمكن إضافة السيرفرات المتصلة بالبوت فقط؛ "
                    f"تعذر العثور على (ID: {guild_id})."
                )
            return guild_id

        def parse_group_member_ids(
            value: str,
            *,
            require_connected: bool,
        ) -> list[int]:
            if not isinstance(value, str):
                raise ServerGroupError("أدخل معرّفات السيرفرات مفصولة بفواصل.")
            raw_ids = [part.strip() for part in value.split(",")]
            if any(
                not part
                or not part.isascii()
                or not part.isdecimal()
                or len(part) > 20
                for part in raw_ids
            ):
                raise ServerGroupError(
                    "أدخل معرّفات صحيحة مفصولة بفواصل، دون عناصر فارغة."
                )
            ids = ServerGroupStore._clean_membership_change_ids(
                int(part) for part in raw_ids
            )
            if require_connected:
                unavailable = [
                    str(guild_id)
                    for guild_id in ids
                    if bot.get_guild(guild_id) is None
                ]
                if unavailable:
                    raise ServerGroupError(
                        "يمكن إضافة السيرفرات المتصلة بالبوت فقط؛ تعذر العثور على "
                        f"(ID: {', '.join(unavailable)})."
                    )
            return ids

        async def connected_server_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            query = (current or "").strip().casefold()
            guilds = sorted(
                bot.guilds,
                key=lambda guild: (guild.name.casefold(), guild.id),
            )
            choices = []
            for guild in guilds:
                if query and (
                    query not in guild.name.casefold()
                    and query not in str(guild.id)
                ):
                    continue
                suffix = f" ({guild.id})"
                choices.append(
                    app_commands.Choice(
                        name=f"{guild.name[:100 - len(suffix)]}{suffix}",
                        value=str(guild.id),
                    )
                )
            return choices[:25]

        async def server_group_name_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            query = (current or "").strip().casefold()
            saved_groups = sorted(
                server_group_store.list_groups(),
                key=lambda saved_group: str(saved_group["name"]).casefold(),
            )
            return [
                app_commands.Choice(
                    name=str(saved_group["name"]),
                    value=str(saved_group["name"]),
                )
                for saved_group in saved_groups
                if not query
                or query in str(saved_group["name"]).casefold()
            ][:25]

        async def server_group_rename_reversal_choices(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []

            namespace = getattr(interaction, "namespace", None)
            group_name = getattr(namespace, "group_name", None)
            page = getattr(namespace, "page", 1)
            if page is None:
                page = 1
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or not 1 <= page <= MAX_SERVER_GROUP_HISTORY_PAGE
            ):
                return []
            offset = (page - 1) * SERVER_GROUP_HISTORY_PAGE_SIZE
            if group_name is None:
                history = server_group_store.list_membership_history(
                    limit=SERVER_GROUP_HISTORY_PAGE_SIZE,
                    offset=offset,
                )
            else:
                try:
                    selected_group = server_group_store.get_group(group_name)
                except ServerGroupError:
                    return []
                if selected_group is None:
                    return []
                history = server_group_store.list_group_history(
                    str(selected_group["name"]),
                    limit=SERVER_GROUP_HISTORY_PAGE_SIZE,
                    offset=offset,
                )

            query = (current or "").strip().casefold()
            choices = []
            for entry in history:
                if entry["action"] != "renamed":
                    continue
                reference = server_group_store.history_entry_reference(entry)
                try:
                    server_group_store.prepare_rename_reversal(reference)
                except ServerGroupError:
                    continue

                old_name = str(entry["old_group_name"]).replace("\r", " ").replace(
                    "\n", " "
                )
                new_name = str(entry["group_name"]).replace("\r", " ").replace(
                    "\n", " "
                )
                developer_text = str(entry["developer_id"])
                try:
                    timestamp = datetime.fromisoformat(
                        str(entry["timestamp"])
                    ).astimezone(timezone.utc)
                    time_text = timestamp.strftime("%Y-%m-%d %H:%MZ")
                except ValueError:
                    time_text = str(entry["timestamp"])[:16]
                searchable = (
                    f"{old_name} {new_name} {time_text} {developer_text}"
                ).casefold()
                if query and query not in searchable:
                    continue

                label = (
                    f"{old_name[:25]} → {new_name[:25]} · "
                    f"{time_text} · {developer_text}"
                )
                choices.append(
                    app_commands.Choice(
                        name=label[:100],
                        value=reference,
                    )
                )
            return choices[:25]

        async def server_group_member_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            namespace = getattr(interaction, "namespace", None)
            group_name = getattr(namespace, "name", None)
            if not isinstance(group_name, str):
                return []
            saved_group = server_group_store.get_group(group_name)
            if saved_group is None:
                return []

            query = (current or "").strip().casefold()
            choices = []
            for guild_id in saved_group["guild_ids"]:
                guild = bot.get_guild(guild_id)
                guild_name = guild.name if guild is not None else None
                if query and query not in str(guild_id) and (
                    guild_name is None or query not in guild_name.casefold()
                ):
                    continue

                if guild_name is None:
                    label = f"غير متصل (ID: {guild_id})"
                else:
                    suffix = f" ({guild_id})"
                    label = f"{guild_name[:100 - len(suffix)]}{suffix}"
                choices.append(
                    app_commands.Choice(name=label, value=str(guild_id))
                )
            return choices[:25]

        async def server_group_removal_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            namespace = getattr(interaction, "namespace", None)
            group_name = getattr(namespace, "name", None)
            if not isinstance(group_name, str):
                return []
            saved_group = server_group_store.get_group(group_name)
            if saved_group is None:
                return []

            member_ids = set(saved_group["guild_ids"])
            text = (current or "").strip()
            parts = text.split(",") if text else []
            has_trailing_comma = bool(text) and text.endswith(",")
            if has_trailing_comma:
                selected_parts = parts[:-1]
                query = ""
            else:
                selected_parts = parts[:-1]
                query = parts[-1].strip() if parts else ""

            selected_ids = []
            for part in selected_parts:
                token = part.strip()
                if (
                    not token
                    or not token.isascii()
                    or not token.isdecimal()
                    or len(token) > 20
                ):
                    return []
                guild_id = int(token)
                if guild_id not in member_ids:
                    return []
                if guild_id not in selected_ids:
                    selected_ids.append(guild_id)

            if (
                query
                and query.isascii()
                and query.isdecimal()
                and len(query) <= 20
                and int(query) in member_ids
            ):
                guild_id = int(query)
                if guild_id not in selected_ids:
                    selected_ids.append(guild_id)
                query = ""

            query = query.casefold()
            choices = []
            for guild_id in saved_group["guild_ids"]:
                if guild_id in selected_ids:
                    continue
                guild = bot.get_guild(guild_id)
                guild_name = guild.name if guild is not None else None
                if query and query not in str(guild_id) and (
                    guild_name is None or query not in guild_name.casefold()
                ):
                    continue

                if guild_name is None:
                    label = f"غير متصل (ID: {guild_id})"
                else:
                    suffix = f" ({guild_id})"
                    label = f"{guild_name[:100 - len(suffix)]}{suffix}"
                choice_value = ",".join(
                    str(value) for value in [*selected_ids, guild_id]
                )
                if len(choice_value) > 100:
                    continue
                choices.append(
                    app_commands.Choice(name=label, value=choice_value)
                )
            return choices[:25]

        async def server_group_addition_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            namespace = getattr(interaction, "namespace", None)
            group_name = getattr(namespace, "name", None)
            if not isinstance(group_name, str):
                return []
            saved_group = server_group_store.get_group(group_name)
            if saved_group is None:
                return []

            existing_ids = set(saved_group["guild_ids"])
            raw_removals = getattr(namespace, "remove_server_ids", None)
            removal_ids = set()
            if isinstance(raw_removals, str) and raw_removals.strip():
                try:
                    parsed_removals = parse_group_member_ids(
                        raw_removals,
                        require_connected=False,
                    )
                except ServerGroupError:
                    parsed_removals = []
                if set(parsed_removals).issubset(existing_ids):
                    removal_ids = set(parsed_removals)

            connected_guilds = sorted(
                bot.guilds,
                key=lambda guild: (guild.name.casefold(), guild.id),
            )
            connected_by_id = {guild.id: guild for guild in connected_guilds}

            text = (current or "").strip()
            parts = text.split(",") if text else []
            has_trailing_comma = bool(text) and text.endswith(",")
            if has_trailing_comma:
                selected_parts = parts[:-1]
                query = ""
            else:
                selected_parts = parts[:-1]
                query = parts[-1].strip() if parts else ""

            selected_ids = []
            for part in selected_parts:
                token = part.strip()
                if (
                    not token
                    or not token.isascii()
                    or not token.isdecimal()
                    or len(token) > 20
                ):
                    return []
                guild_id = int(token)
                if (
                    guild_id <= 0
                    or guild_id not in connected_by_id
                    or guild_id in existing_ids
                ):
                    return []
                if guild_id not in selected_ids:
                    selected_ids.append(guild_id)

            if (
                query
                and query.isascii()
                and query.isdecimal()
                and len(query) <= 20
            ):
                guild_id = int(query)
                if guild_id in connected_by_id and guild_id not in existing_ids:
                    if guild_id not in selected_ids:
                        selected_ids.append(guild_id)
                    query = ""

            remaining_group_size = (
                len(existing_ids - removal_ids) + len(selected_ids)
            )
            if remaining_group_size >= MAX_SERVER_GROUP_SIZE:
                return []

            query = query.casefold()
            choices = []
            for guild in connected_guilds:
                guild_id = guild.id
                if guild_id in existing_ids or guild_id in selected_ids:
                    continue
                guild_name = str(guild.name)
                if query and query not in str(guild_id) and (
                    query not in guild_name.casefold()
                ):
                    continue

                suffix = f" ({guild_id})"
                label = f"{guild_name[:100 - len(suffix)]}{suffix}"
                choice_value = ",".join(
                    str(value) for value in [*selected_ids, guild_id]
                )
                if len(choice_value) > 100:
                    continue
                choices.append(
                    app_commands.Choice(name=label, value=choice_value)
                )
            return choices[:25]

        def disconnected_group_guild_ids(group_data: dict) -> list[int]:
            return [
                guild_id
                for guild_id in group_data["guild_ids"]
                if bot.get_guild(guild_id) is None
            ]

        def server_group_member_label(guild_id: int) -> str:
            guild = bot.get_guild(guild_id)
            if guild is None:
                return f"غير متصل (ID: {guild_id})"
            name = str(guild.name).replace("\r", " ").replace("\n", " ")
            name = discord.utils.escape_markdown(name)
            return f"{name} (ID: {guild_id})"

        async def server_group_history_server_choices(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            namespace = getattr(interaction, "namespace", None)
            group_name = getattr(namespace, "group_name", None)
            if not isinstance(group_name, str):
                return []
            try:
                selected_group = server_group_store.get_group(group_name)
            except ServerGroupError:
                return []
            if selected_group is None:
                return []

            query = (current or "").strip().casefold()
            seen_ids: set[int] = set()
            choices = []
            for entry in server_group_store.list_group_history(
                str(selected_group["name"])
            ):
                for guild_id in (
                    *entry["before_guild_ids"],
                    *entry["after_guild_ids"],
                ):
                    if guild_id in seen_ids:
                        continue
                    seen_ids.add(guild_id)
                    guild = bot.get_guild(guild_id)
                    guild_name = (
                        str(guild.name).replace("\r", " ").replace("\n", " ")
                        if guild is not None
                        else "غير متصل"
                    )
                    if query and query not in str(guild_id) and (
                        query not in guild_name.casefold()
                    ):
                        continue
                    suffix = f" (ID: {guild_id})"
                    if guild is None:
                        label = f"غير متصل{suffix}"
                    else:
                        label = (
                            f"{discord.utils.escape_markdown(guild_name)[:100 - len(suffix)]}"
                            f"{suffix}"
                        )
                    choices.append(
                        app_commands.Choice(
                            name=label[:100],
                            value=str(guild_id),
                        )
                    )
            return choices[:25]

        async def preview_server_group_membership_change(
            interaction: discord.Interaction,
            name: str,
            add_guild_ids: list[int],
            remove_guild_ids: list[int],
            *,
            edit_existing_message: bool = False,
        ) -> None:
            try:
                expected_group = server_group_store.get_group(name)
                if expected_group is None:
                    raise ServerGroupError("لم يتم العثور على المجموعة.")
                proposed_group = server_group_store.preview_members(
                    name,
                    add_guild_ids,
                    remove_guild_ids,
                )
            except ServerGroupError as error:
                if edit_existing_message:
                    await interaction.response.edit_message(
                        content=str(error),
                        view=None,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await interaction.response.send_message(
                        str(error),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                return

            group_name = discord.utils.escape_markdown(
                str(proposed_group["name"])
            )
            view = ServerGroupMembershipChangeView(
                server_group_store,
                expected_group,
                add_guild_ids,
                remove_guild_ids,
                interaction.user.id,
                getattr(interaction, "guild_id", None),
            )

            def member_lines(guild_ids: list[int]) -> str:
                return "\n".join(
                    f"• {server_group_member_label(guild_id)}"
                    for guild_id in guild_ids
                ) or "لا يوجد"

            preview = "\n".join(
                (
                    f"معاينة خاصة لتعديل أعضاء المجموعة «{group_name}».",
                    "الإضافات المقترحة:",
                    member_lines(add_guild_ids),
                    "الإزالات المقترحة:",
                    member_lines(remove_guild_ids),
                    f"العضوية الناتجة ({len(proposed_group['guild_ids'])} سيرفرات):",
                    member_lines(proposed_group["guild_ids"]),
                    "لن تُحفظ التغييرات قبل التأكيد الصريح. تنتهي المعاينة بعد دقيقة.",
                )
            )
            await send_private_chunks(
                interaction,
                preview,
                view=view,
                edit_existing_message=edit_existing_message,
            )

        def server_group_choice_name(group_data: dict) -> str:
            name = str(group_data["name"])
            label = f"مجموعة: {name}"
            disconnected_ids = disconnected_group_guild_ids(group_data)
            if not disconnected_ids:
                return label[:100]

            warning = " ⚠ غير متصل: "
            for shown_count in range(min(len(disconnected_ids), 3), 0, -1):
                shown_ids = ", ".join(
                    str(guild_id) for guild_id in disconnected_ids[:shown_count]
                )
                remaining_count = len(disconnected_ids) - shown_count
                if remaining_count:
                    shown_ids += f" … +{remaining_count}"
                suffix = f"{warning}{shown_ids}"
                if len(suffix) < 100:
                    return f"{label[:100 - len(suffix)]}{suffix}"
            return f"{label[:100 - len(warning) - 1]}{warning}{disconnected_ids[0]}"

        @group.command(
            name="server-group-create",
            description="حفظ مجموعة من السيرفرات المتصلة للمقارنة لاحقاً",
        )
        @app_commands.describe(
            name="اسم المجموعة",
            server_ids="معرّفات 3 إلى 25 سيرفراً متصلاً، مفصولة بفواصل",
        )
        @app_commands.guild_only()
        async def create_server_group(
            interaction: discord.Interaction,
            name: str,
            server_ids: str,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            try:
                ids = parse_group_guild_ids(server_ids)
                created = server_group_store.create(
                    name,
                    ids,
                    developer_id=interaction.user.id,
                )
            except ServerGroupError as error:
                response = str(error)
            except OSError:
                logger.exception("Could not save a filter server group.")
                response = "تعذر حفظ المجموعة؛ لم يتم تغيير المجموعات المحفوظة."
            else:
                response = (
                    f"حُفظت المجموعة «"
                    f"{discord.utils.escape_markdown(str(created['name']))}» "
                    f"وتضم {len(created['guild_ids'])} سيرفرات."
                )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(
            name="server-group-add-server",
            description="إضافة سيرفر متصل إلى مجموعة محفوظة",
        )
        @app_commands.describe(name="اسم المجموعة", server_id="معرّف السيرفر المتصل")
        @app_commands.autocomplete(
            name=server_group_name_choices,
            server_id=connected_server_choices,
        )
        @app_commands.guild_only()
        async def add_server_group_guild(
            interaction: discord.Interaction,
            name: str,
            server_id: str,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            try:
                guild_id = parse_connected_guild_id(server_id)
            except ServerGroupError as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await preview_server_group_membership_change(
                interaction,
                name,
                [guild_id],
                [],
            )

        @group.command(
            name="server-group-update-members",
            description="إضافة وإزالة عدة سيرفرات، مع قائمة اختيار للإزالات",
        )
        @app_commands.describe(
            name="اسم المجموعة",
            add_server_ids=(
                "معرّفات متصلة مفصولة بفواصل؛ اختر الاقتراحات أو أدخل قوائم طويلة يدويًا"
            ),
            remove_server_ids="معرّفات السيرفرات المراد إزالتها، مفصولة بفواصل",
            select_additions=(
                "افتح قائمة خاصة لاختيار السيرفرات المتصلة المراد إضافتها"
            ),
            select_removals=(
                "افتح قائمة لاختيار أعضاء المجموعة بدلاً من كتابة المعرّفات"
            ),
        )
        @app_commands.autocomplete(
            name=server_group_name_choices,
            add_server_ids=server_group_addition_choices,
            remove_server_ids=server_group_removal_choices,
        )
        @app_commands.guild_only()
        async def update_server_group_members(
            interaction: discord.Interaction,
            name: str,
            add_server_ids: str | None = None,
            remove_server_ids: str | None = None,
            select_additions: bool = False,
            select_removals: bool = False,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            proposed_group = None
            try:
                if select_additions and add_server_ids is not None:
                    raise ServerGroupError(
                        "استخدم قائمة اختيار الإضافات أو أدخل معرّفاتها، وليس كليهما."
                    )
                if select_additions and select_removals:
                    raise ServerGroupError(
                        "افتح قائمة اختيار واحدة في كل مرة."
                    )
                additions = (
                    []
                    if select_additions
                    else (
                        parse_group_member_ids(
                            add_server_ids,
                            require_connected=True,
                        )
                        if add_server_ids is not None
                        else []
                    )
                )
                if select_removals:
                    if remove_server_ids is not None:
                        raise ServerGroupError(
                            "استخدم قائمة الاختيار أو أدخل معرّفات الإزالة، وليس كليهما."
                        )
                    removals = []
                else:
                    removals = (
                        parse_group_member_ids(
                            remove_server_ids,
                            require_connected=False,
                        )
                        if remove_server_ids is not None
                        else []
                    )
                expected_group = server_group_store.get_group(name)
                if expected_group is None:
                    raise ServerGroupError("لم يتم العثور على المجموعة.")
                if not select_removals and not select_additions:
                    proposed_group = server_group_store.preview_members(
                        name,
                        additions,
                        removals,
                    )
            except ServerGroupError as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if select_removals:
                member_ids = list(expected_group["guild_ids"])
                max_removals = min(
                    len(member_ids),
                    len(member_ids) + len(additions) - MIN_SERVER_GROUP_SIZE,
                )
                if max_removals <= 0:
                    await interaction.response.send_message(
                        "لا يمكن إزالة أعضاء إضافيين؛ يجب أن تضم المجموعة "
                        f"{MIN_SERVER_GROUP_SIZE} سيرفرات على الأقل.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                options = []
                for guild_id in member_ids:
                    guild = bot.get_guild(guild_id)
                    suffix = f" ({guild_id})"
                    if guild is None:
                        label = f"غير متصل (ID: {guild_id})"
                    else:
                        guild_name = (
                            str(guild.name)
                            .replace("\r", " ")
                            .replace("\n", " ")
                        )
                        label = f"{guild_name[:100 - len(suffix)]}{suffix}"
                    options.append(
                        discord.SelectOption(
                            label=label,
                            value=str(guild_id),
                        )
                    )

                async def preview_selected_removals(
                    selection_interaction: discord.Interaction,
                    selected_ids: list[int],
                ) -> None:
                    await preview_server_group_membership_change(
                        selection_interaction,
                        str(expected_group["name"]),
                        additions,
                        selected_ids,
                        edit_existing_message=True,
                    )

                selection_view = ServerGroupRemovalSelectionView(
                    options,
                    max_removals,
                    interaction.user.id,
                    getattr(interaction, "guild_id", None),
                    preview_selected_removals,
                    server_group_store,
                    expected_group,
                )
                group_name = discord.utils.escape_markdown(
                    str(expected_group["name"])
                )
                prompt = (
                    f"اختر من 1 إلى {max_removals} من أعضاء المجموعة "
                    f"«{group_name}» المراد إزالتهم. تشمل القائمة السيرفرات "
                    "غير المتصلة. ستظهر معاينة خاصة قبل حفظ أي تغيير."
                )
                await send_private_chunks(
                    interaction,
                    prompt,
                    view=selection_view,
                )
                return

            if select_additions:
                try:
                    remaining_members = (
                        server_group_store.preview_members(
                            str(expected_group["name"]),
                            [],
                            removals,
                        )["guild_ids"]
                        if removals
                        else expected_group["guild_ids"]
                    )
                except ServerGroupError as error:
                    await interaction.response.send_message(
                        str(error),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                max_additions = (
                    MAX_SERVER_GROUP_SIZE - len(remaining_members)
                )
                if max_additions <= 0:
                    await interaction.response.send_message(
                        "لا تتسع المجموعة لإضافات أخرى بعد الإزالات المحددة.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                existing_ids = set(expected_group["guild_ids"])
                connected_guilds = sorted(
                    (
                        guild
                        for guild in bot.guilds
                        if guild.id not in existing_ids
                    ),
                    key=lambda guild: (str(guild.name).casefold(), guild.id),
                )
                if not connected_guilds:
                    await interaction.response.send_message(
                        "لا توجد سيرفرات متصلة جديدة لإضافتها إلى هذه المجموعة.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                options = []
                for guild in connected_guilds:
                    guild_name = (
                        str(guild.name).replace("\r", " ").replace("\n", " ")
                    )
                    suffix = f" ({guild.id})"
                    options.append(
                        discord.SelectOption(
                            label=f"{guild_name[:100 - len(suffix)]}{suffix}",
                            value=str(guild.id),
                        )
                    )

                async def preview_selected_additions(
                    selection_interaction: discord.Interaction,
                    selected_ids: list[int],
                ) -> None:
                    try:
                        validated_ids = parse_group_member_ids(
                            ",".join(str(guild_id) for guild_id in selected_ids),
                            require_connected=True,
                        )
                    except ServerGroupError as error:
                        await selection_interaction.response.edit_message(
                            content=str(error),
                            view=None,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    await preview_server_group_membership_change(
                        selection_interaction,
                        str(expected_group["name"]),
                        validated_ids,
                        removals,
                        edit_existing_message=True,
                    )

                selection_view = ServerGroupAdditionSelectionView(
                    options,
                    max_additions,
                    interaction.user.id,
                    getattr(interaction, "guild_id", None),
                    preview_selected_additions,
                    server_group_store,
                    expected_group,
                )
                group_name = discord.utils.escape_markdown(
                    str(expected_group["name"])
                )
                prompt = (
                    f"اختر السيرفرات المتصلة الجديدة لإضافتها إلى المجموعة "
                    f"«{group_name}». يمكنك التنقل بين الصفحات والاختيار "
                    f"حتى {max_additions} سيرفراً. ستظهر معاينة خاصة قبل الحفظ."
                )
                await send_private_chunks(
                    interaction,
                    prompt + "\n" + selection_view._prompt(),
                    view=selection_view,
                )
                return

            assert proposed_group is not None
            group_name = discord.utils.escape_markdown(
                str(proposed_group["name"])
            )
            view = ServerGroupMembershipChangeView(
                server_group_store,
                expected_group,
                additions,
                removals,
                developer_id,
                getattr(interaction, "guild_id", None),
            )
            def member_lines(guild_ids: list[int]) -> str:
                return "\n".join(
                    f"• {server_group_member_label(guild_id)}"
                    for guild_id in guild_ids
                ) or "لا يوجد"

            preview = "\n".join(
                (
                    f"معاينة خاصة لتعديل أعضاء المجموعة «{group_name}».",
                    "الإضافات المقترحة:",
                    member_lines(additions),
                    "الإزالات المقترحة:",
                    member_lines(removals),
                    f"العضوية الناتجة ({len(proposed_group['guild_ids'])} سيرفرات):",
                    member_lines(proposed_group["guild_ids"]),
                    "لن تُحفظ التغييرات قبل التأكيد الصريح. تنتهي المعاينة بعد دقيقة.",
                )
            )
            await send_private_chunks(
                interaction,
                preview,
                view=view,
            )

        @group.command(
            name="server-group-remove-server",
            description="إزالة سيرفر من مجموعة محفوظة",
        )
        @app_commands.describe(name="اسم المجموعة", server_id="معرّف السيرفر في المجموعة")
        @app_commands.autocomplete(
            name=server_group_name_choices,
            server_id=server_group_member_choices,
        )
        @app_commands.guild_only()
        async def remove_server_group_guild(
            interaction: discord.Interaction,
            name: str,
            server_id: str,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            if (
                not isinstance(server_id, str)
                or not server_id.isascii()
                or not server_id.isdecimal()
                or len(server_id) > 20
            ):
                await interaction.response.send_message(
                    "أدخل معرّف سيرفر صحيحاً.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await preview_server_group_membership_change(
                interaction,
                name,
                [],
                [int(server_id)],
            )

        @group.command(
            name="server-group-list",
            description="عرض مجموعات السيرفرات المحفوظة",
        )
        @app_commands.guild_only()
        async def list_server_groups(interaction: discord.Interaction) -> None:
            if await reject_non_developer(interaction):
                return
            groups = server_group_store.list_groups()
            lines = []
            for item in groups:
                name = str(item["name"]).replace("\r", " ").replace("\n", " ")
                name = discord.utils.escape_markdown(name)
                lines.append(f"• {name}")
                lines.extend(
                    f"  - {server_group_member_label(guild_id)}"
                    for guild_id in item["guild_ids"]
                )
            await send_private_chunks(
                interaction,
                "مجموعات السيرفرات المحفوظة:\n" + "\n".join(lines),
                empty_message="لا توجد مجموعات سيرفرات محفوظة.",
            )

        @group.command(
            name="server-group-history",
            description="عرض تغييرات المجموعات المحفوظة",
        )
        @app_commands.describe(
            group_name="المجموعة المحفوظة المراد عرض سجلها",
            page="رقم الصفحة؛ كل صفحة تعرض 20 تغييراً",
            reverse_rename="إعادة تسمية من هذه الصفحة لعكسها بعد التأكيد",
            server_id="تصفية سجل المجموعة حسب معرّف سيرفر ظهر في تغييراتها",
        )
        @app_commands.autocomplete(
            group_name=server_group_name_choices,
            reverse_rename=server_group_rename_reversal_choices,
            server_id=server_group_history_server_choices,
        )
        @app_commands.guild_only()
        async def list_server_group_history(
            interaction: discord.Interaction,
            group_name: str | None = None,
            page: app_commands.Range[int, 1, MAX_SERVER_GROUP_HISTORY_PAGE] = 1,
            reverse_rename: str | None = None,
            server_id: str | None = None,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or not 1 <= page <= MAX_SERVER_GROUP_HISTORY_PAGE
            ):
                await interaction.response.send_message(
                    "رقم الصفحة يجب أن يكون بين 1 و"
                    f"{MAX_SERVER_GROUP_HISTORY_PAGE}.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if reverse_rename is not None:
                try:
                    entry, expected_group = (
                        server_group_store.prepare_rename_reversal(reverse_rename)
                    )
                except ServerGroupError as error:
                    await interaction.response.send_message(
                        f"{error} لم يُحفظ أي تغيير.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                previous_name = discord.utils.escape_markdown(
                    str(entry["old_group_name"])
                )
                current_name = discord.utils.escape_markdown(
                    str(entry["group_name"])
                )
                try:
                    timestamp = datetime.fromisoformat(
                        str(entry["timestamp"])
                    ).astimezone(timezone.utc)
                    time_text = timestamp.strftime("%Y-%m-%d %H:%M:%SZ")
                except ValueError:
                    time_text = str(entry["timestamp"])
                current_ids = ", ".join(
                    str(guild_id) for guild_id in expected_group["guild_ids"]
                )
                view = ServerGroupRenameReversalView(
                    server_group_store,
                    reverse_rename,
                    entry,
                    expected_group,
                    interaction.user.id,
                    getattr(interaction, "guild_id", None),
                )
                preview = (
                    "معاينة خاصة للتراجع عن إعادة تسمية:\n"
                    f"• من «{previous_name}» إلى «{current_name}» "
                    f"— سُجلت في {time_text} بواسطة المطور "
                    f"{entry['developer_id']}\n"
                    f"• السيرفرات الحالية التي ستبقى كما هي "
                    f"({len(expected_group['guild_ids'])}): {current_ids}\n"
                    "لن يتغير شيء قبل التأكيد. تنتهي المعاينة بعد دقيقة."
                )
                await send_private_chunks(
                    interaction,
                    preview,
                    view=view,
                )
                return

            history_server_id = None
            if server_id is not None:
                if group_name is None:
                    await interaction.response.send_message(
                        "اختر مجموعة محفوظة عند تصفية سجلها حسب معرّف السيرفر.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                if not isinstance(server_id, str):
                    await interaction.response.send_message(
                        "أدخل معرّف سيرفر صحيحاً باستخدام الأرقام فقط.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                normalized_server_id = server_id.strip()
                if (
                    not normalized_server_id.isascii()
                    or not normalized_server_id.isdecimal()
                    or len(normalized_server_id) > 20
                ):
                    await interaction.response.send_message(
                        "أدخل معرّف سيرفر صحيحاً باستخدام الأرقام فقط.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                history_server_id = int(normalized_server_id)
                if (
                    history_server_id <= 0
                    or history_server_id > (1 << 64) - 1
                ):
                    await interaction.response.send_message(
                        "أدخل معرّف سيرفر صحيحاً ضمن نطاق معرّفات Discord.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

            offset = (page - 1) * SERVER_GROUP_HISTORY_PAGE_SIZE
            selected_group_name = None
            if group_name is None:
                history = server_group_store.list_membership_history(
                    limit=SERVER_GROUP_HISTORY_PAGE_SIZE + 1,
                    offset=offset,
                )
            else:
                try:
                    selected_group = server_group_store.get_group(group_name)
                except ServerGroupError:
                    selected_group = None
                if selected_group is None:
                    requested_name = discord.utils.escape_markdown(group_name)
                    await interaction.response.send_message(
                        f"لم يتم العثور على المجموعة المحفوظة «{requested_name}». "
                        "ربما أُعيدت تسميتها أو حُذفت؛ اختر اسماً محفوظاً حالياً.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                selected_group_name = str(selected_group["name"])
                history = server_group_store.list_group_history(
                    selected_group_name,
                    limit=SERVER_GROUP_HISTORY_PAGE_SIZE + 1,
                    offset=offset,
                    guild_id=history_server_id,
                )

            has_older_page = len(history) > SERVER_GROUP_HISTORY_PAGE_SIZE
            history = history[:SERVER_GROUP_HISTORY_PAGE_SIZE]
            if not history:
                if selected_group_name is None:
                    empty_message = (
                        f"لا توجد تغييرات مسجلة في الصفحة {page}. اختر صفحة أصغر."
                    )
                else:
                    safe_group_name = discord.utils.escape_markdown(
                        selected_group_name
                    )
                    if history_server_id is None:
                        empty_message = (
                            f"لا توجد تغييرات مسجلة للمجموعة «{safe_group_name}» "
                            f"في الصفحة {page}. اختر صفحة أصغر."
                        )
                    else:
                        empty_message = (
                            f"لا توجد تغييرات للمجموعة «{safe_group_name}» "
                            f"تشمل السيرفر (ID: {history_server_id}) في الصفحة "
                            f"{page}. تحقق من المعرّف أو اختر صفحة أصغر."
                        )
                await send_private_chunks(
                    interaction,
                    "",
                    empty_message=empty_message,
                )
                return

            action_labels = {
                "created": "إنشاء مجموعة",
                "added": "إضافة سيرفر",
                "removed": "إزالة سيرفر",
                "updated": "تحديث العضوية",
                "deleted": "حذف مجموعة",
                "renamed": "إعادة تسمية مجموعة",
            }
            lines = []
            for entry in history:
                timestamp = datetime.fromisoformat(
                    str(entry["timestamp"])
                ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
                name = discord.utils.escape_markdown(str(entry["group_name"]))
                before_ids = ", ".join(
                    str(guild_id) for guild_id in entry["before_guild_ids"]
                ) or "—"
                after_ids = ", ".join(
                    str(guild_id) for guild_id in entry["after_guild_ids"]
                ) or "—"
                if entry["action"] == "renamed":
                    old_name = discord.utils.escape_markdown(
                        str(entry["old_group_name"])
                    )
                    group_label = f"من «{old_name}» إلى «{name}»"
                else:
                    group_label = f"«{name}»"
                lines.extend(
                    (
                        f"• {timestamp} — {action_labels[entry['action']]} "
                        f"{group_label} — المطور: {entry['developer_id']}",
                        f"  قبل: {before_ids}",
                        f"  بعد: {after_ids}",
                    )
                )
                if history_server_id is not None:
                    was_member = history_server_id in entry["before_guild_ids"]
                    is_member = history_server_id in entry["after_guild_ids"]
                    if not was_member and is_member:
                        membership_change = "أُضيف إلى المجموعة"
                    elif was_member and not is_member:
                        membership_change = "أُزيل من المجموعة"
                    else:
                        membership_change = "كان ضمن المجموعة وبقي فيها"
                    lines.append(
                        f"  السيرفر (ID: {history_server_id}): "
                        f"{membership_change}."
                    )
            if selected_group_name is None:
                heading = (
                    f"أحدث تغييرات المجموعات ({len(history)}) "
                    f"— الصفحة {page}:\n"
                )
            else:
                safe_group_name = discord.utils.escape_markdown(
                    selected_group_name
                )
                server_filter_label = (
                    f" للسيرفر (ID: {history_server_id})"
                    if history_server_id is not None
                    else ""
                )
                heading = (
                    f"أحدث تغييرات المجموعة «{safe_group_name}»"
                    f"{server_filter_label} ({len(history)}) — الصفحة {page}:\n"
                )
            lines.append(
                (
                    f"للتغييرات الأقدم، اطلب الصفحة {page + 1}."
                    if has_older_page
                    else "هذه أقدم صفحة متاحة."
                )
            )
            await send_private_chunks(
                interaction,
                heading + "\n".join(lines),
            )

        @group.command(
            name="server-group-rename",
            description="تغيير اسم مجموعة سيرفرات محفوظة",
        )
        @app_commands.describe(name="الاسم الحالي", new_name="الاسم الجديد")
        @app_commands.guild_only()
        async def rename_server_group(
            interaction: discord.Interaction,
            name: str,
            new_name: str,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            try:
                renamed = server_group_store.rename(
                    name,
                    new_name,
                    developer_id=interaction.user.id,
                )
            except ServerGroupError as error:
                response = str(error)
            except OSError:
                logger.exception("Could not rename a filter server group.")
                response = "تعذر حفظ الاسم الجديد؛ لم تتغير المجموعات المحفوظة."
            else:
                response = (
                    f"تم تغيير اسم المجموعة إلى "
                    f"«{discord.utils.escape_markdown(str(renamed['name']))}»."
                )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @group.command(
            name="server-group-remove",
            description="حذف مجموعة سيرفرات محفوظة",
        )
        @app_commands.describe(name="اسم المجموعة المطلوب حذفها")
        @app_commands.guild_only()
        async def remove_server_group(
            interaction: discord.Interaction,
            name: str,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            try:
                removed = server_group_store.remove(
                    name,
                    developer_id=interaction.user.id,
                )
            except ServerGroupError as error:
                response = str(error)
            except OSError:
                logger.exception("Could not remove a filter server group.")
                response = "تعذر حذف المجموعة؛ لم تتغير المجموعات المحفوظة."
            else:
                response = (
                    f"حُذفت المجموعة "
                    f"«{discord.utils.escape_markdown(str(removed['name']))}»."
                )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        async def available_server_choices(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[str]]:
            if interaction.user.id != developer_id:
                return []
            current = current or ""
            if "," in current:
                prefix, query = current.rsplit(",", maxsplit=1)
                prefix = f"{prefix.strip()}, "
                selected_ids = {
                    int(part.strip())
                    for part in current.rsplit(",", maxsplit=1)[0].split(",")
                    if part.strip().isdigit()
                }
            else:
                prefix = ""
                query = current
                selected_ids = set()
            query = query.strip().casefold()
            guilds = sorted(
                bot.guilds,
                key=lambda guild: (guild.name.casefold(), guild.id),
            )
            choices = []
            for guild in guilds:
                if guild.id in selected_ids:
                    continue
                if query and (
                    query not in guild.name.casefold()
                    and query not in str(guild.id)
                ):
                    continue
                value = f"{prefix}{guild.id}"
                if len(value) > 100:
                    continue
                guild_suffix = f" ({guild.id})"
                choices.append(
                    app_commands.Choice(
                        name=f"{guild.name[:100 - len(guild_suffix)]}{guild_suffix}",
                        value=value,
                    )
                )
            if "," not in current:
                group_query = query.removeprefix("group:")
                for saved_group in server_group_store.list_groups():
                    name = str(saved_group["name"])
                    if group_query and group_query not in name.casefold():
                        continue
                    value = f"group:{name}"
                    choices.append(
                        app_commands.Choice(
                            name=server_group_choice_name(saved_group),
                            value=value,
                        )
                    )
            return choices[:25]

        @group.command(
            name="server-compare",
            description="مقارنة السيرفرات بالمعرّفات أو بمجموعة محفوظة",
        )
        @app_commands.describe(
            server_ids="معرّفات مفصولة بفواصل أو مجموعة محفوظة",
            report_format="تنزيل تقرير المقارنة كملف",
        )
        @app_commands.choices(
            report_format=[
                app_commands.Choice(name="نص (.txt)", value="text"),
                app_commands.Choice(name="JSON (.json)", value="json"),
                app_commands.Choice(name="CSV (.csv)", value="csv"),
            ]
        )
        @app_commands.autocomplete(
            server_ids=available_server_choices,
        )
        async def compare_server_overrides(
            interaction: discord.Interaction,
            server_ids: str,
            report_format: str | None = None,
        ) -> None:
            if await reject_non_developer(interaction):
                return

            if report_format not in (None, "text", "json", "csv"):
                await interaction.response.send_message(
                    "اختر تقريراً نصياً أو JSON أو CSV.",
                    ephemeral=True,
                )
                return

            if not isinstance(server_ids, str):
                await interaction.response.send_message(
                    "أدخل معرّفات السيرفرات مفصولة بفواصل.",
                    ephemeral=True,
                )
                return

            saved_group_name = None
            if server_ids.casefold().startswith("group:"):
                saved_group_name = server_ids[len("group:"):].strip()
                try:
                    saved_group = server_group_store.get_group(saved_group_name)
                except ServerGroupError:
                    saved_group = None
                if saved_group is None:
                    await interaction.response.send_message(
                        "لم يتم العثور على مجموعة السيرفرات المحفوظة.",
                        ephemeral=True,
                    )
                    return
                selected_ids = list(saved_group["guild_ids"])
                saved_group_name = str(saved_group["name"])
            else:
                raw_ids = [part.strip() for part in server_ids.split(",")]
                if not raw_ids or any(
                    not part.isascii() or not part.isdecimal() for part in raw_ids
                ):
                    await interaction.response.send_message(
                        "أدخل معرّفات صحيحة مفصولة بفواصل، دون عناصر فارغة.",
                        ephemeral=True,
                    )
                    return
                selected_ids = [int(part) for part in raw_ids]
            if any(guild_id <= 0 for guild_id in selected_ids):
                await interaction.response.send_message(
                    "تعذر التحقق من هوية السيرفر.",
                    ephemeral=True,
                )
                return
            if len(selected_ids) < 3:
                await interaction.response.send_message(
                    "اختر ثلاثة سيرفرات مختلفة على الأقل للمقارنة.",
                    ephemeral=True,
                )
                return
            if len(selected_ids) > 25:
                await interaction.response.send_message(
                    "يمكن مقارنة 25 سيرفراً كحد أقصى في المرة الواحدة.",
                    ephemeral=True,
                )
                return
            if len(set(selected_ids)) != len(selected_ids):
                await interaction.response.send_message(
                    "اختر سيرفرات مختلفة؛ لا تكرر معرّف السيرفر.",
                    ephemeral=True,
                )
                return

            selected_guilds = [
                (guild_id, bot.get_guild(guild_id)) for guild_id in selected_ids
            ]
            unavailable = [
                str(guild_id)
                for guild_id, guild in selected_guilds
                if guild is None
            ]
            if unavailable:
                await interaction.response.send_message(
                    "تعذر العثور على السيرفر المحدد ضمن السيرفرات المتاحة للبوت "
                    f"(ID: {', '.join(unavailable)}).",
                    ephemeral=True,
                )
                return

            base_terms = store.list_terms()
            effective_terms = {
                guild_id: guild_store.list_terms(guild_id, base_terms)
                for guild_id, _ in selected_guilds
            }
            effective_keys = {
                guild_id: {
                    _normalize(term) for term in effective_terms[guild_id]
                }
                for guild_id, _ in selected_guilds
            }
            shared_keys = set.intersection(
                *(effective_keys[guild_id] for guild_id, _ in selected_guilds)
            )
            first_guild_id = selected_guilds[0][0]
            first_terms = effective_terms[first_guild_id]
            shared_terms = [
                term for term in first_terms if _normalize(term) in shared_keys
            ]

            def format_terms(terms: list[str], empty_message: str) -> str:
                return "\n".join(
                    f"• {discord.utils.escape_markdown(term)}" for term in terms
                ) or empty_message

            def guild_label(guild_id: int, guild: discord.Guild) -> str:
                name = guild.name.replace("\r", " ").replace("\n", " ")
                return (
                    f"{discord.utils.escape_markdown(name)} (ID: {guild_id})"
                )

            report_data = {
                "title": "Server filter comparison",
                "saved_group": saved_group_name,
                "global_terms": base_terms.copy(),
                "shared_effective_terms": shared_terms,
                "servers": [],
            }
            sections = [
                "مقارنة تعديلات مرشح اللغة — عرض خاص، دون تغيير أي إعداد.",
                "",
                *(
                    [f"المجموعة المحفوظة: {discord.utils.escape_markdown(saved_group_name)}", ""]
                    if saved_group_name is not None
                    else []
                ),
                f"السيرفرات المختارة ({len(selected_guilds)}): "
                + "، ".join(
                    guild_label(guild_id, guild)
                    for guild_id, guild in selected_guilds
                ),
                "",
                *(
                    [
                        "القائمة العامة الأساسية المستخدمة في المقارنة "
                        f"({len(base_terms)}):",
                        format_terms(
                            base_terms,
                            "لا توجد ألفاظ في القائمة العامة.",
                        ),
                        "",
                    ]
                    if report_format is not None
                    else []
                ),
                f"ألفاظ فعالة في جميع السيرفرات المختارة ({len(shared_terms)}):",
                format_terms(shared_terms, "لا توجد ألفاظ مشتركة بين الجميع."),
            ]
            for guild_id, guild in selected_guilds:
                guild_terms = effective_terms[guild_id]
                guild_only_terms = [
                    term
                    for term in guild_terms
                    if _normalize(term) not in shared_keys
                ]
                label = guild_label(guild_id, guild)
                sections.extend(
                    [
                        "",
                        f"ألفاظ فعالة في {label} وغير مشتركة بين الجميع "
                        f"({len(guild_only_terms)}):",
                        format_terms(
                            guild_only_terms,
                            "لا توجد ألفاظ مختلفة عن القائمة المشتركة.",
                        ),
                    ]
                )

                statuses = guild_store.list_override_status(guild_id, base_terms)
                report_data["servers"].append(
                    {
                        "id": guild_id,
                        "name": guild.name,
                        "effective_terms": guild_terms,
                        "overrides": {
                            "effective": {
                                "additions": statuses["added_effective"],
                                "exclusions": statuses["removed_effective"],
                            },
                            "retained": {
                                "additions": statuses["added_retained"],
                                "exclusions": statuses["removed_retained"],
                            },
                        },
                    }
                )
                effective_count = len(effective_terms[guild_id])
                sections.extend(
                    [
                        "",
                        f"**{label}**",
                        f"إجمالي الكلمات والعبارات الفعالة: {effective_count}",
                        f"إضافات محلية فعالة ({len(statuses['added_effective'])}):",
                        format_terms(
                            statuses["added_effective"], "لا توجد إضافات فعالة."
                        ),
                        f"استثناءات فعالة من العامة "
                        f"({len(statuses['removed_effective'])}):",
                        format_terms(
                            statuses["removed_effective"],
                            "لا توجد استثناءات فعالة.",
                        ),
                        f"إضافات محفوظة غير مؤثرة حالياً "
                        f"({len(statuses['added_retained'])}):",
                        format_terms(
                            statuses["added_retained"],
                            "لا توجد إضافات محفوظة غير مؤثرة.",
                        ),
                        f"استثناءات محفوظة غير مؤثرة حالياً "
                        f"({len(statuses['removed_retained'])}):",
                        format_terms(
                            statuses["removed_retained"],
                            "لا توجد استثناءات محفوظة غير مؤثرة.",
                        ),
                    ]
                )
            if report_format is None:
                await send_private_chunks(interaction, "\n".join(sections))
                return

            generated_at_utc = (
                datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
            report_data["generated_at_utc"] = generated_at_utc
            sections.insert(1, f"Generated at (UTC): {generated_at_utc}")

            if report_format == "csv":
                csv_header = (
                    "record_type",
                    "saved_group_name",
                    "server_id",
                    "server_name",
                    "term",
                    "generated_at_utc",
                )
                csv_saved_group_name = (
                    _spreadsheet_safe_text(saved_group_name)
                    if saved_group_name is not None
                    else ""
                )

                def term_rows(
                    record_type: str,
                    terms: Iterable[str],
                    *,
                    server_id: int | str = "",
                    server_name: str = "",
                ) -> Iterable[tuple[object, ...]]:
                    for term in terms:
                        yield (
                            record_type,
                            csv_saved_group_name,
                            server_id,
                            server_name,
                            _spreadsheet_safe_text(term),
                            generated_at_utc,
                        )

                def comparison_rows() -> Iterable[tuple[object, ...]]:
                    yield from term_rows("global_term", base_terms)
                    yield from term_rows("shared_effective_term", shared_terms)
                    for server in report_data["servers"]:
                        server_id = server["id"]
                        server_name = _spreadsheet_safe_text(server["name"])
                        yield (
                            "server",
                            csv_saved_group_name,
                            server_id,
                            server_name,
                            "",
                            generated_at_utc,
                        )
                        yield from term_rows(
                            "effective_term",
                            server["effective_terms"],
                            server_id=server_id,
                            server_name=server_name,
                        )
                        for record_type, terms in (
                            (
                                "effective_addition",
                                server["overrides"]["effective"]["additions"],
                            ),
                            (
                                "effective_exclusion",
                                server["overrides"]["effective"]["exclusions"],
                            ),
                            (
                                "retained_addition",
                                server["overrides"]["retained"]["additions"],
                            ),
                            (
                                "retained_exclusion",
                                server["overrides"]["retained"]["exclusions"],
                            ),
                        ):
                            yield from term_rows(
                                record_type,
                                terms,
                                server_id=server_id,
                                server_name=server_name,
                            )

                try:
                    csv_parts = _split_comparison_csv_rows(
                        csv_header,
                        comparison_rows(),
                        max_part_bytes=MAX_COMPARISON_CSV_PART_BYTES,
                    )
                except ComparisonCsvRowTooLarge:
                    await interaction.response.send_message(
                        "تعذر تقسيم تقرير CSV كاملاً لأن صفاً واحداً يتجاوز الحد "
                        "الآمن لحجم الملف. لم تُرسل أي أجزاء. قلّل عدد السيرفرات "
                        "أو جرّب تنزيل التقرير بصيغة النص أو JSON.",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                part_count = len(csv_parts)
                part_digest: str | None = None
                if part_count > 1:
                    repeated_header = b"\xef\xbb\xbf" + _encode_comparison_csv_row(
                        csv_header
                    )
                    complete_csv = csv_parts[0] + b"".join(
                        part[len(repeated_header) :] for part in csv_parts[1:]
                    )
                    part_digest = hashlib.sha256(complete_csv).hexdigest()
                delivery_parts = []
                for part_number, part_contents in enumerate(csv_parts, start=1):
                    if part_count == 1:
                        single_file_digest = hashlib.sha256(
                            part_contents
                        ).hexdigest()
                        filename = (
                            "filter-server-comparison-sha256-"
                            f"{single_file_digest}.csv"
                        )
                        content = "أُرفق تقرير المقارنة الخاص."
                    else:
                        member_digest = hashlib.sha256(part_contents).hexdigest()
                        filename = (
                            "filter-server-comparison-part-"
                            f"{part_number:03d}-of-{part_count:03d}"
                            f"-sha256-{part_digest}"
                            f"-part-sha256-{member_digest}.csv"
                        )
                        content = (
                            "أُرفق الجزء "
                            f"{part_number} من {part_count} من تقرير المقارنة الخاص."
                        )
                    delivery_parts.append(
                        (part_number, filename, content, part_contents)
                    )

                if part_count > 1:
                    archive_contents = _build_comparison_csv_archive(
                        (filename, part_contents)
                        for _, filename, _, part_contents in delivery_parts
                    )
                    if archive_contents is not None:
                        archive_file = discord.File(
                            io.BytesIO(archive_contents),
                            filename="filter-server-comparison-parts.zip",
                        )
                        try:
                            await interaction.response.send_message(
                                "أُرفق تقرير المقارنة الكامل داخل ملف ZIP الخاص.",
                                file=archive_file,
                                ephemeral=True,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except discord.HTTPException as error:
                            archive_file.close()
                            if error.status != 413 and error.code != 40005:
                                await interaction.response.send_message(
                                    "تعذر إرسال تقرير CSV بسبب مشكلة أثناء الإرسال "
                                    "إلى Discord. لم تُرسل أي ملفات ولم يتغير أي "
                                    "إعداد. حاول مرة أخرى لاحقاً.",
                                    ephemeral=True,
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )
                                return
                            # Discord may enforce a lower per-file limit than the
                            # conservative local limit. Fall back to the CSV parts.
                        else:
                            return

                for delivery_index, (
                    part_number,
                    filename,
                    content,
                    part_contents,
                ) in enumerate(delivery_parts):
                    report_file = discord.File(
                        io.BytesIO(part_contents),
                        filename=filename,
                    )
                    try:
                        if part_number == 1:
                            await interaction.response.send_message(
                                content,
                                file=report_file,
                                ephemeral=True,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        else:
                            await interaction.followup.send(
                                content,
                                file=report_file,
                                ephemeral=True,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                    except discord.HTTPException as error:
                        is_size_error = error.status == 413 or error.code == 40005
                        if is_size_error and part_number == 1:
                            notice = (
                                "رفض Discord إرفاق أول جزء من تقرير CSV بسبب حد "
                                "حجم الملفات. لم تُرسل أي أجزاء ولم يتغير أي إعداد. "
                                "قلّل عدد السيرفرات أو جرّب النص أو JSON."
                            )
                        elif is_size_error:
                            notice = (
                                f"تعذر إرفاق الجزء {part_number} من {part_count} "
                                "من تقرير CSV بسبب حد حجم الملفات. أُرسلت الأجزاء "
                                "السابقة فقط، والتقرير غير مكتمل. لم يتغير أي إعداد. "
                                "قلّل عدد السيرفرات أو جرّب النص أو JSON."
                            )
                        elif part_number == 1:
                            notice = (
                                "تعذر إرسال تقرير CSV بسبب مشكلة أثناء الإرسال إلى "
                                "Discord. لم تُرسل أي أجزاء ولم يتغير أي إعداد. "
                                "حاول مرة أخرى لاحقاً."
                            )
                        else:
                            notice = (
                                f"تعذر إرفاق الجزء {part_number} من {part_count} "
                                "من تقرير CSV بسبب مشكلة أثناء الإرسال إلى Discord. "
                                "أُرسلت الأجزاء السابقة فقط، والتقرير غير مكتمل. "
                                "لم يتغير أي إعداد. حاول مرة أخرى لاحقاً."
                            )
                        notice += (
                            " يمكنك إعادة إرسال الجزء المتعثر والأجزاء التي بعده "
                            "خلال 10 دقائق ما دام البوت يعمل، باستخدام الزر أدناه؛ "
                            "لن تُرسل الأجزاء السابقة مرة أخرى."
                        )
                        retry_view = ComparisonCsvRetryView(
                            [
                                (number, part_filename, contents)
                                for number, part_filename, _, contents
                                in delivery_parts[delivery_index:]
                            ],
                            part_count,
                            interaction.user.id,
                        )
                        if part_number == 1:
                            await interaction.response.send_message(
                                notice,
                                ephemeral=True,
                                view=retry_view,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                            retry_view.message = await interaction.original_response()
                        else:
                            retry_view.message = await interaction.followup.send(
                                notice,
                                ephemeral=True,
                                view=retry_view,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        return
                return

            if report_format == "json":
                attachment_contents = json.dumps(
                    report_data,
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
                filename = "filter-server-comparison.json"
            else:
                attachment_contents = "\n".join(sections).encode("utf-8")
                filename = "filter-server-comparison.txt"
            report_file = discord.File(
                io.BytesIO(attachment_contents),
                filename=filename,
            )
            try:
                await interaction.response.send_message(
                    "أُرفق تقرير المقارنة الخاص.",
                    file=report_file,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as error:
                if error.status == 413 or error.code == 40005:
                    notice = (
                        "رفض Discord إرفاق تقرير المقارنة لأنه يتجاوز حد رفع "
                        "الملفات. لم يتغير أي إعداد. جرّب مقارنة عدد أقل من "
                        "السيرفرات، أو اختر صيغة أخرى مثل النص أو CSV."
                    )
                else:
                    notice = (
                        "تعذر إرسال تقرير المقارنة بسبب مشكلة أثناء الإرسال إلى "
                        "Discord. لم يتغير أي إعداد. حاول مرة أخرى لاحقاً."
                    )
                await interaction.response.send_message(
                    notice,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        @group.command(
            name="server-diff",
            description="مراجعة تقرير محفوظ أو مقارنة تقريرين محفوظين",
        )
        @app_commands.describe(
            report="تقرير JSON/CSV سابق أو ZIP يحتوي كل أجزاء CSV",
            compare_with="ملف JSON أو CSV لاحق لمقارنته بالتقرير الأول",
        )
        async def diff_server_comparison(
            interaction: discord.Interaction,
            report: discord.Attachment,
            compare_with: discord.Attachment | None = None,
        ) -> None:
            if await reject_non_developer(interaction):
                return
            if compare_with is not None:
                async def load_archived_report(
                    attachment: discord.Attachment,
                    report_label: str,
                    *,
                    allow_csv: bool,
                ) -> dict[str, object] | None:
                    filename = str(getattr(attachment, "filename", ""))
                    is_csv = filename.casefold().endswith(".csv")
                    is_zip = filename.casefold().endswith(".zip")
                    if (is_csv or is_zip) and not allow_csv:
                        await interaction.response.send_message(
                            f"{report_label} يجب أن يكون تقرير JSON؛ لم تُقرأ "
                            "إعدادات حية ولم يتغير أي إعداد.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return None
                    size_limit = (
                        MAX_COMPARISON_ARCHIVE_BYTES
                        if is_zip
                        else MAX_RECOVERY_FILE_BYTES
                    )
                    if attachment.size > size_limit:
                        await interaction.response.send_message(
                            f"{report_label} أكبر من الحد المسموح؛ لم تُقرأ "
                            "إعدادات حية ولم يتغير أي إعداد.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return None
                    try:
                        report_bytes = await attachment.read()
                    except (discord.HTTPException, OSError):
                        await interaction.response.send_message(
                            f"تعذر قراءة {report_label}؛ لم تُقرأ إعدادات حية "
                            "ولم يتغير أي إعداد.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return None
                    if len(report_bytes) > size_limit:
                        await interaction.response.send_message(
                            f"{report_label} أكبر من الحد المسموح؛ لم تُقرأ "
                            "إعدادات حية ولم يتغير أي إعداد.",
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return None
                    try:
                        if is_zip:
                            return _parse_server_comparison_csv_archive(
                                report_bytes
                            )
                        if is_csv:
                            return _parse_server_comparison_csv_file(
                                report_bytes,
                                filename,
                            )
                        payload = json.loads(report_bytes.decode("utf-8"))
                        return _validate_server_comparison_report(payload)
                    except UnicodeDecodeError:
                        detail = "تعذر فك ترميز التقرير بترميز UTF-8."
                    except json.JSONDecodeError:
                        detail = "تعذر قراءة صيغة JSON."
                    except csv.Error:
                        detail = "تعذر قراءة صيغة CSV أو ترميزها."
                    except ValueError as error:
                        detail = str(error)
                    recovery_hint = (
                        " أنشئ تصديراً جديداً بالأمر /filter server-compare "
                        "بصيغة CSV، ثم استبدل بهذا الملف المرفق في خيار "
                        "report أو compare_with."
                        if is_zip
                        else ""
                    )
                    await interaction.response.send_message(
                        f"{report_label} غير صالح: {detail} لم تُقرأ إعدادات "
                        f"حية ولم يتغير أي إعداد.{recovery_hint}",
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return None

                first_archived_report = await load_archived_report(
                    report,
                    "التقرير الأول",
                    allow_csv=True,
                )
                if first_archived_report is None:
                    return
                second_archived_report = await load_archived_report(
                    compare_with,
                    "التقرير الثاني",
                    allow_csv=True,
                )
                if second_archived_report is None:
                    return
                await send_private_chunks(
                    interaction,
                    _diff_archived_server_comparison_reports(
                        first_archived_report,
                        second_archived_report,
                    ),
                )
                return

            if str(getattr(report, "filename", "")).casefold().endswith(".zip"):
                await interaction.response.send_message(
                    "ملف ZIP يحتوي أجزاء CSV؛ أرفقه كتقرير أول وحدد تقرير JSON "
                    "لاحقاً في خيار compare_with. لم تُقرأ إعدادات حية ولم "
                    "يتغير أي إعداد.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if report.size > MAX_RECOVERY_FILE_BYTES:
                await interaction.response.send_message(
                    "ملف المقارنة أكبر من الحد المسموح؛ لم يتغير أي إعداد.",
                    ephemeral=True,
                )
                return
            try:
                report_bytes = await report.read()
            except (discord.HTTPException, OSError):
                await interaction.response.send_message(
                    "تعذر قراءة ملف المقارنة؛ لم يتغير أي إعداد.",
                    ephemeral=True,
                )
                return
            if len(report_bytes) > MAX_RECOVERY_FILE_BYTES:
                await interaction.response.send_message(
                    "ملف المقارنة أكبر من الحد المسموح؛ لم يتغير أي إعداد.",
                    ephemeral=True,
                )
                return
            try:
                if str(getattr(report, "filename", "")).casefold().endswith(
                    ".csv"
                ):
                    previous_report = _parse_server_comparison_csv_file(
                        report_bytes,
                        str(getattr(report, "filename", "")),
                    )
                else:
                    payload = json.loads(report_bytes.decode("utf-8"))
                    previous_report = _validate_server_comparison_report(payload)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                csv.Error,
                ValueError,
            ) as error:
                if isinstance(error, ValueError):
                    detail = str(error)
                elif str(getattr(report, "filename", "")).casefold().endswith(
                    ".csv"
                ):
                    detail = "تعذر قراءة صيغة CSV أو ترميزها."
                else:
                    detail = "تعذر فك ترميز JSON."
                await interaction.response.send_message(
                    f"ملف المقارنة غير صالح: {detail} لم يتغير أي إعداد.",
                    ephemeral=True,
                )
                return

            def diff_terms(
                previous: list[str],
                current: list[str],
            ) -> tuple[list[str], list[str]]:
                previous_keys = {_normalize(term) for term in previous}
                current_keys = {_normalize(term) for term in current}
                added = [
                    term
                    for term in current
                    if _normalize(term) not in previous_keys
                ]
                removed = [
                    term
                    for term in previous
                    if _normalize(term) not in current_keys
                ]
                return added, removed

            def format_terms(terms: list[str]) -> str:
                return ", ".join(
                    discord.utils.escape_markdown(term) for term in terms
                ) or "لا شيء"

            def append_change(
                lines: list[str],
                label: str,
                previous: list[str],
                current: list[str],
            ) -> None:
                added, removed = diff_terms(previous, current)
                lines.append(
                    f"{label}: أُضيفت ({len(added)}): {format_terms(added)}؛ "
                    f"أُزيلت ({len(removed)}): {format_terms(removed)}"
                )

            current_global_terms = store.list_terms()
            generated_at_utc = _format_comparison_generated_at_utc(
                previous_report.get("generated_at_utc")
            )
            timestamp_label = (
                generated_at_utc
                if generated_at_utc is not None
                else "غير متاح (مفقود أو غير صالح)"
            )
            lines = [
                "مراجعة مقارنة محفوظة — عرض خاص للقراءة فقط؛ "
                "لم تُغيّر إعدادات المرشح.",
                f"وقت إنشاء التقرير (UTC): {timestamp_label}",
                *(
                    [saved_group_label]
                    if (
                        saved_group_label := _format_archived_saved_group_label(
                            previous_report
                        )
                    )
                    is not None
                    else []
                ),
                "",
            ]
            append_change(
                lines,
                "القائمة العامة",
                previous_report["global_terms"],
                current_global_terms,
            )
            for previous_server in previous_report["servers"]:
                server_id = previous_server["id"]
                old_name = discord.utils.escape_markdown(previous_server["name"])
                guild = bot.get_guild(server_id)
                if guild is None:
                    lines.extend(
                        [
                            "",
                            f"**{old_name} (ID: {server_id})**",
                            "لم يعد هذا السيرفر متصلاً بالبوت؛ "
                            "تعذر فحص إعداداته الحالية.",
                        ]
                    )
                    continue

                current_name = discord.utils.escape_markdown(guild.name)
                label = f"{current_name} (ID: {server_id})"
                if current_name != old_name:
                    label += f" — كان اسمه {old_name}"
                current_effective_terms = guild_store.list_terms(
                    server_id,
                    current_global_terms,
                )
                current_statuses = guild_store.list_override_status(
                    server_id,
                    current_global_terms,
                )
                current_overrides = {
                    "effective": {
                        "additions": current_statuses["added_effective"],
                        "exclusions": current_statuses["removed_effective"],
                    },
                    "retained": {
                        "additions": current_statuses["added_retained"],
                        "exclusions": current_statuses["removed_retained"],
                    },
                }
                previous_overrides = previous_server["overrides"]
                lines.extend(["", f"**{label}**"])
                append_change(
                    lines,
                    "الكلمات الفعالة",
                    previous_server["effective_terms"],
                    current_effective_terms,
                )
                for status, status_label in (
                    ("effective", "التعديلات الفعالة"),
                    ("retained", "التعديلات المحفوظة غير المؤثرة"),
                ):
                    for override_type, override_label in (
                        ("additions", "الإضافات"),
                        ("exclusions", "الاستثناءات"),
                    ):
                        append_change(
                            lines,
                            f"{status_label} — {override_label}",
                            previous_overrides[status][override_type],
                            current_overrides[status][override_type],
                        )

            await send_private_chunks(interaction, "\n".join(lines))

        @group.command(
            name="server-reset",
            description="إعادة مرشح هذا السيرفر إلى القائمة العامة بعد التأكيد",
        )
        @app_commands.guild_only()
        async def reset_server_terms(interaction: discord.Interaction) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            overrides = guild_store.list_saved_overrides(guild_id)
            additions = overrides["added"]
            exclusions = overrides["removed"]
            if not additions and not exclusions:
                await interaction.response.send_message(
                    "لا توجد إضافات أو استثناءات محفوظة لهذا السيرفر؛ لم يتغير شيء.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            def format_terms(terms: list[str]) -> str:
                return "\n".join(
                    f"• {discord.utils.escape_markdown(term)}" for term in terms
                ) or "لا توجد."

            preview = (
                f"**معاينة إعادة ضبط مرشح هذا السيرفر فقط**\n"
                f"ستُحذف الإضافات المحلية ({len(additions)}):\n"
                f"{format_terms(additions)}\n"
                f"وستُلغى الاستثناءات من القائمة العامة ({len(exclusions)}):\n"
                f"{format_terms(exclusions)}\n"
                "لن تتغير القائمة العامة أو إعدادات السيرفرات الأخرى. "
                "سيُحفظ الوضع الحالي في نسخة احتياطية قبل الحفظ."
            )
            view = GuildFilterResetView(
                guild_store,
                guild_id,
                interaction.user.id,
                expected_overrides=overrides,
            )
            await send_private_chunks(interaction, preview, view=view)
            view.message = await interaction.original_response()

        @group.command(
            name="server-restore",
            description="استعادة تعديلات مرشح هذا السيرفر من ملف احتياطي",
        )
        @app_commands.describe(
            backup="ملف JSON المرسل إليك بعد إعادة الضبط",
            restore_type="أنواع التعديلات التي ستُستعاد من الملف",
            restore_mode="استبدال التعديلات المحددة أو دمجها مع الحالية",
        )
        @app_commands.choices(
            restore_type=[
                app_commands.Choice(name="الإضافات فقط", value="added"),
                app_commands.Choice(name="الاستثناءات فقط", value="removed"),
                app_commands.Choice(
                    name="الإضافات والاستثناءات", value="both"
                ),
            ],
            restore_mode=[
                app_commands.Choice(name="استبدال المحدد", value="replace"),
                app_commands.Choice(name="دمج مع الموجود", value="merge"),
            ],
        )
        @app_commands.guild_only()
        async def restore_server_terms(
            interaction: discord.Interaction,
            backup: discord.Attachment,
            restore_type: str = "both",
            restore_mode: str = "replace",
        ) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            restore_type_map = {
                "added": ("added",),
                "removed": ("removed",),
                "both": ("added", "removed"),
            }
            if restore_type not in restore_type_map:
                await interaction.response.send_message(
                    "اختر الإضافات أو الاستثناءات أو كليهما للاستعادة.",
                    ephemeral=True,
                )
                return
            restore_types = restore_type_map[restore_type]
            if restore_mode not in {"replace", "merge"}:
                await interaction.response.send_message(
                    "اختر استبدال التعديلات المحددة أو دمجها مع الحالية.",
                    ephemeral=True,
                )
                return
            if not backup.filename.lower().endswith(".json"):
                await interaction.response.send_message(
                    "ارفع ملف استعادة بامتداد JSON.",
                    ephemeral=True,
                )
                return
            if backup.size > MAX_RECOVERY_FILE_BYTES:
                await interaction.response.send_message(
                    "حجم ملف الاستعادة أكبر من الحد المسموح به.",
                    ephemeral=True,
                )
                return
            try:
                contents = await backup.read()
            except (discord.HTTPException, OSError):
                await interaction.response.send_message(
                    "تعذر قراءة ملف الاستعادة؛ لم تتغير الإعدادات.",
                    ephemeral=True,
                )
                return
            if len(contents) > MAX_RECOVERY_FILE_BYTES:
                await interaction.response.send_message(
                    "حجم ملف الاستعادة أكبر من الحد المسموح به.",
                    ephemeral=True,
                )
                return
            try:
                recovery_guild_id, recovered_state, created_at = (
                    guild_store.parse_recovery_file(contents)
                )
            except ProfanityTermError as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if recovery_guild_id != guild_id:
                await interaction.response.send_message(
                    "معرّف السيرفر في الملف لا يطابق السيرفر الحالي؛ "
                    "لم تتغير الإعدادات.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            expected_state = guild_store.snapshot_guild(guild_id)

            def format_terms(terms: list[str]) -> str:
                return "\n".join(
                    f"• {discord.utils.escape_markdown(term)}" for term in terms
                ) or "لا توجد."

            def compare_terms(
                current_terms: list[str], restored_terms: list[str]
            ) -> tuple[list[str], list[str], list[str]]:
                current_keys = {
                    ProfanityTermStore._key(term) for term in current_terms
                }
                restored_keys = {
                    ProfanityTermStore._key(term) for term in restored_terms
                }
                return (
                    [
                        term
                        for term in current_terms
                        if ProfanityTermStore._key(term) in restored_keys
                    ],
                    [
                        term
                        for term in restored_terms
                        if ProfanityTermStore._key(term) not in current_keys
                    ],
                    [
                        term
                        for term in current_terms
                        if ProfanityTermStore._key(term) not in restored_keys
                    ],
                )

            current_added = expected_state["added"]
            current_removed = expected_state["removed"]
            added_diff = compare_terms(
                current_added, recovered_state["added"]
            )
            removed_diff = compare_terms(
                current_removed, recovered_state["removed"]
            )
            merged_added, merge_new_added, merge_existing_added = (
                GuildProfanityTermStore._merge_terms(
                    current_added, recovered_state["added"]
                )
            )
            merged_removed, merge_new_removed, merge_existing_removed = (
                GuildProfanityTermStore._merge_terms(
                    current_removed, recovered_state["removed"]
                )
            )
            final_added = (
                merged_added
                if restore_mode == "merge"
                else recovered_state["added"]
            ) if "added" in restore_types else current_added
            final_removed = (
                merged_removed
                if restore_mode == "merge"
                else recovered_state["removed"]
            ) if "removed" in restore_types else current_removed
            if len(final_added) > MAX_CONFIGURED_TERMS:
                await interaction.response.send_message(
                    "سيتجاوز دمج الإضافات الحد الأقصى للكلمات المحفوظة؛ "
                    "لم تتغير الإعدادات.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if {
                ProfanityTermStore._key(term) for term in final_added
            } & {
                ProfanityTermStore._key(term) for term in final_removed
            }:
                await interaction.response.send_message(
                    "يتعارض ملف الاستعادة مع تعديل حالي للكلمة نفسها؛ "
                    "لا يمكن استعادة الإضافات والاستثناءات معاً.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            def format_diff_section(
                title: str,
                added_terms: list[str],
                excluded_terms: list[str],
            ) -> str:
                return (
                    f"{title}\n"
                    f"الإضافات ({len(added_terms)}):\n"
                    f"{format_terms(added_terms)}\n"
                    f"الاستثناءات ({len(excluded_terms)}):\n"
                    f"{format_terms(excluded_terms)}"
                )

            def format_merge_section(
                title: str,
                current_terms: list[str],
                new_terms: list[str],
                existing_terms: list[str],
            ) -> str:
                return (
                    f"{title}\n"
                    f"الحالية التي ستبقى ({len(current_terms)}):\n"
                    f"{format_terms(current_terms)}\n"
                    f"ستُضاف من الملف ({len(new_terms)}):\n"
                    f"{format_terms(new_terms)}\n"
                    f"موجودة بالفعل ولن تتكرر ({len(existing_terms)}):\n"
                    f"{format_terms(existing_terms)}"
                )

            type_labels = {
                "added": "الإضافات",
                "removed": "الاستثناءات",
            }
            selected_labels = " و".join(
                type_labels[kind] for kind in restore_types
            )
            unchanged_types = [
                type_labels[kind]
                for kind in ("added", "removed")
                if kind not in restore_types
            ]
            unchanged_notice = (
                f"ستبقى {' و'.join(unchanged_types)} الحالية دون تغيير.\n"
                if unchanged_types
                else ""
            )
            latest_filter_change = max(
                (
                    datetime.fromisoformat(entry["timestamp"])
                    for entry in expected_state["history"]
                ),
                default=None,
            )
            recovery_created_at = datetime.fromisoformat(created_at)
            recovery_age_notice = ""
            if latest_filter_change is None:
                recovery_age_notice = (
                    "ملاحظة: لا يتوفر سجل تغييرات لهذا السيرفر للتحقق من "
                    "حداثة ملف الاستعادة.\n"
                )
            else:
                recovery_age_notice = (
                    f"أحدث تغيير مسجل: {latest_filter_change.isoformat()}\n"
                )
                if recovery_created_at < latest_filter_change:
                    recovery_age_notice = (
                        "**تحذير: ملف الاستعادة أقدم من أحدث تغيير مسجل في مرشح "
                        "هذا السيرفر.**\n"
                        f"{recovery_age_notice}"
                        "قد يؤدي تأكيد الاستعادة إلى إزالة تعديلات أحدث.\n"
                    )
            restore_mode_label = (
                "دمج التعديلات المحددة مع الحالية"
                if restore_mode == "merge"
                else "استبدال التعديلات المحددة"
            )
            if restore_mode == "merge":
                merge_sections = []
                if "added" in restore_types:
                    merge_sections.append(
                        format_merge_section(
                            "فئة الإضافات:",
                            current_added,
                            merge_new_added,
                            merge_existing_added,
                        )
                    )
                if "removed" in restore_types:
                    merge_sections.append(
                        format_merge_section(
                            "فئة الاستثناءات:",
                            current_removed,
                            merge_new_removed,
                            merge_existing_removed,
                        )
                    )
                changes_preview = "\n".join(merge_sections)
            else:
                selected_added_diff = (
                    added_diff
                    if "added" in restore_types
                    else compare_terms(current_added, current_added)
                )
                selected_removed_diff = (
                    removed_diff
                    if "removed" in restore_types
                    else compare_terms(current_removed, current_removed)
                )
                changes_preview = (
                    f"{format_diff_section('التعديلات الحالية التي ستبقى:', selected_added_diff[0], selected_removed_diff[0])}\n"
                    f"{format_diff_section('تعديلات جديدة من ملف الاستعادة:', selected_added_diff[1], selected_removed_diff[1])}\n"
                    f"{format_diff_section('تعديلات حالية ستُزال بالاستعادة:', selected_added_diff[2], selected_removed_diff[2])}"
                )
            preview = (
                f"**معاينة استعادة مرشح السيرفر ID {guild_id} فقط**\n"
                f"الطريقة: {restore_mode_label}.\n"
                f"أنواع التعديلات المطلوب استعادتها: {selected_labels}.\n"
                f"{unchanged_notice}"
                f"تاريخ ووقت إنشاء ملف الاستعادة: {created_at}\n"
                f"{recovery_age_notice}"
                f"{changes_preview}\n"
                "ستُحفظ الحالة الحالية كاملة في نسخة احتياطية قبل الحفظ. "
                "لن تتغير القائمة العامة أو إعدادات السيرفرات الأخرى."
            )
            view = GuildFilterRestoreView(
                guild_store,
                guild_id,
                interaction.user.id,
                recovered_state,
                expected_state,
                restore_types=restore_types,
                restore_mode=restore_mode,
            )
            await send_private_chunks(interaction, preview, view=view)
            view.message = await interaction.original_response()

        @group.command(
            name="server-history",
            description="البحث في سجل تغييرات هذا السيرفر أو جميع السيرفرات",
        )
        @app_commands.describe(
            term="كلمة أو جزء من كلمة للبحث عنها",
            action="تصفية حسب نوع التغيير",
            start_date="بداية الفترة بالتنسيق YYYY-MM-DD (شامل)",
            end_date="نهاية الفترة بالتنسيق YYYY-MM-DD (شامل)",
            restore_type="تصفية حسب أنواع التعديلات التي استُعيدت",
            export_file="إرسال النتائج المطابقة للفلاتر كملف JSON خاص بدلاً من عرضها",
            all_servers="البحث في سجلات جميع السيرفرات بدلاً من السيرفر الحالي",
            developer_id="تصفية حسب معرّف المطور الذي أجرى التغيير",
        )
        @app_commands.choices(
            action=[
                app_commands.Choice(name="إضافة", value="add"),
                app_commands.Choice(name="إزالة", value="remove"),
                app_commands.Choice(name="إعادة ضبط", value="reset"),
                app_commands.Choice(name="استعادة", value="restore"),
            ],
            restore_type=[
                app_commands.Choice(name="الإضافات", value="added"),
                app_commands.Choice(name="الاستثناءات", value="removed"),
                app_commands.Choice(name="كلا النوعين", value="both"),
            ],
        )
        @app_commands.guild_only()
        async def list_server_changes(
            interaction: discord.Interaction,
            term: str | None = None,
            action: app_commands.Choice[str] | None = None,
            start_date: str | None = None,
            end_date: str | None = None,
            restore_type: app_commands.Choice[str] | None = None,
            export_file: bool = False,
            all_servers: bool = False,
            developer_id: int | None = None,
        ) -> None:
            guild_id = await get_server_id(interaction)
            if guild_id is None:
                return
            if all_servers and export_file:
                await interaction.response.send_message(
                    "تعذر تصدير سجلات جميع السيرفرات بهذا الخيار؛ "
                    "أوقف التصدير لعرض نتائج البحث الخاصة.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            action_value = (
                action.value if isinstance(action, app_commands.Choice) else action
            )
            restore_type_value = (
                restore_type.value
                if isinstance(restore_type, app_commands.Choice)
                else restore_type
            )
            action_labels = {
                "add": "إضافة",
                "remove": "إزالة",
                "reset": "إعادة ضبط",
                "restore": "استعادة",
            }
            restore_type_labels = {
                "added": "الإضافات",
                "removed": "الاستثناءات",
            }
            restore_filter_labels = {
                "added": "الإضافات",
                "removed": "الاستثناءات",
                "both": "كلا النوعين",
            }
            try:
                if all_servers:
                    unfiltered_history = guild_store.search_all_histories(
                        term,
                        action_value,
                        start_date,
                        end_date,
                        developer_id=developer_id,
                    )
                    legacy_restore_count = sum(
                        entry.get("action") == "restore"
                        and not isinstance(entry.get("restore_types"), list)
                        for _, entry in unfiltered_history
                    )
                    history_with_guilds = (
                        guild_store.search_all_histories(
                            term,
                            action_value,
                            start_date,
                            end_date,
                            restore_type_value,
                            developer_id,
                        )
                        if restore_type_value is not None
                        else unfiltered_history
                    )
                    history = [entry for _, entry in history_with_guilds]
                else:
                    history = guild_store.search_history(
                        guild_id,
                        term,
                        action_value,
                        start_date,
                        end_date,
                        developer_id=developer_id,
                    )
                    legacy_restore_count = 0
                    if restore_type_value is not None:
                        legacy_restore_count = sum(
                            entry.get("action") == "restore"
                            and not isinstance(entry.get("restore_types"), list)
                            for entry in history
                        )
                        history = guild_store.search_history(
                            guild_id,
                            term,
                            action_value,
                            start_date,
                            end_date,
                            restore_type=restore_type_value,
                            developer_id=developer_id,
                        )
                    history_with_guilds = [
                        (guild_id, entry) for entry in history
                    ]
            except ProfanityTermError as error:
                await interaction.response.send_message(
                    str(error),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            applied_filters = {
                "term": term.strip() if term is not None else None,
                "action": action_value,
                "start_date": start_date.strip() if start_date is not None else None,
                "end_date": end_date.strip() if end_date is not None else None,
                "restore_type": restore_type_value,
                "developer_id": developer_id,
            }
            if export_file:
                exported_history = []
                for entry in history:
                    exported_entry = dict(entry)
                    if entry.get("action") == "restore":
                        restore_types = entry.get("restore_types")
                        if isinstance(restore_types, list):
                            exported_entry["restore_categories"] = (
                                restore_types.copy()
                            )
                            exported_entry["restore_categories_status"] = (
                                "recorded"
                            )
                        else:
                            exported_entry["restore_categories"] = None
                            exported_entry["restore_categories_status"] = (
                                "legacy_unknown"
                            )
                        restore_mode = entry.get("restore_mode")
                        if isinstance(restore_mode, str) and restore_mode in {
                            "replace",
                            "merge",
                        }:
                            exported_entry["restore_mode_status"] = "recorded"
                        else:
                            exported_entry["restore_mode"] = None
                            exported_entry["restore_mode_status"] = (
                                "legacy_unknown"
                            )

                        legacy_details = []
                        if not isinstance(restore_types, list):
                            exported_entry["legacy"] = True
                            legacy_details.append(
                                "restored categories were not recorded."
                            )
                        if not isinstance(restore_mode, str) or restore_mode not in {
                            "replace",
                            "merge",
                        }:
                            exported_entry["legacy"] = True
                            legacy_details.append(
                                "restore mode was not recorded."
                            )
                        if legacy_details:
                            exported_entry["legacy_note"] = (
                                "Legacy restore entry; "
                                + " ".join(legacy_details)
                            )

                    exported_history.append(exported_entry)

                guild = getattr(interaction, "guild", None)
                guild_name = getattr(guild, "name", None)
                if not isinstance(guild_name, str):
                    guild_name = None
                export = {
                    "title": "Server filter history",
                    "schema_version": 1,
                    "guild_id": str(guild_id),
                    "guild_name": guild_name,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "filters": applied_filters,
                    "excluded_legacy_restore_count": legacy_restore_count,
                    "entry_count": len(exported_history),
                    "history": exported_history,
                }
                export_file_attachment = discord.File(
                    io.BytesIO(
                        json.dumps(
                            export,
                            ensure_ascii=False,
                            indent=2,
                        ).encode("utf-8")
                    ),
                    filename=f"filter-server-history-{guild_id}.json",
                )
                await interaction.response.send_message(
                    f"تم تجهيز تصدير السجلات المطابقة لهذا السيرفر "
                    f"({len(exported_history)} سجل).",
                    file=export_file_attachment,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            filters = []
            if term is not None:
                filters.append(
                    f"الكلمة: «{discord.utils.escape_markdown(term.strip())}»"
                )
            if action_value is not None:
                filters.append(
                    f"الإجراء: {action_labels.get(action_value, action_value)}"
                )
            if start_date is not None:
                filters.append(f"من: {start_date.strip()}")
            if end_date is not None:
                filters.append(f"إلى: {end_date.strip()}")
            if restore_type_value is not None:
                filters.append(
                    f"نوع الاستعادة: "
                    f"{restore_filter_labels.get(restore_type_value, restore_type_value)}"
                )
            if developer_id is not None:
                filters.append(f"معرّف المطور: {developer_id}")

            legacy_restore_note = (
                f"ملاحظة: استُبعدت {legacy_restore_count} سجلات استعادة قديمة "
                "لأن نوع التعديلات غير مسجل."
                if restore_type_value is not None and legacy_restore_count
                else ""
            )

            def format_recorded_terms(entry: dict[str, object]) -> list[str]:
                if isinstance(entry.get("term"), str):
                    return [
                        f"الكلمة: «{discord.utils.escape_markdown(entry['term'])}»"
                    ]

                def format_terms(label: str, values: object) -> list[str]:
                    if not isinstance(values, list):
                        return [f"  {label}: لا توجد"]
                    formatted = [
                        f"  {label}: «{discord.utils.escape_markdown(value)}»"
                        for value in values
                        if isinstance(value, str)
                    ]
                    return formatted or [f"  {label}: لا توجد"]

                return [
                    *format_terms(
                        "الإضافات",
                        entry.get("matched_added", entry.get("added")),
                    ),
                    *format_terms(
                        "الاستثناءات",
                        entry.get("matched_removed", entry.get("removed")),
                    ),
                ]

            def format_restore_types(entry: dict[str, object]) -> str:
                restore_types = entry.get("restore_types")
                if not isinstance(restore_types, list):
                    return "غير محددة (سجل قديم)"
                return " و".join(
                    restore_type_labels[restore_type]
                    for restore_type in restore_types
                )

            def format_restore_mode(entry: dict[str, object]) -> str:
                restore_mode = entry.get("restore_mode")
                if restore_mode == "replace":
                    return "استبدال"
                if restore_mode == "merge":
                    return "دمج"
                return "غير محددة (سجل قديم)"

            lines = []
            guild_labels: dict[int, str] = {}
            for result_guild_id, entry in history_with_guilds:
                server_context = ""
                if all_servers:
                    if result_guild_id not in guild_labels:
                        client = getattr(interaction, "client", None)
                        get_guild = getattr(client, "get_guild", None)
                        guild = (
                            get_guild(result_guild_id)
                            if callable(get_guild)
                            else None
                        )
                        guild_name = getattr(guild, "name", None)
                        if isinstance(guild_name, str) and guild_name.strip():
                            guild_labels[result_guild_id] = (
                                f"«{discord.utils.escape_markdown(guild_name)}» "
                                f"(ID: {result_guild_id})"
                            )
                        else:
                            guild_labels[result_guild_id] = (
                                f"ID: {result_guild_id}"
                            )
                    server_context = (
                        f" — السيرفر: {guild_labels[result_guild_id]}"
                    )
                header = (
                    f"• {entry['timestamp']}{server_context} — "
                    f"{action_labels[entry['action']]} ({entry['action']}) "
                    f"(المطور: {entry['developer_id']})"
                    + (
                        f"; الأنواع المستعادة: {format_restore_types(entry)}"
                        if entry["action"] == "restore"
                        else ""
                    )
                    + (
                        f"; طريقة الاستعادة: {format_restore_mode(entry)}"
                        if entry["action"] == "restore"
                        else ""
                    )
                    + (
                        f"; النسخة الاحتياطية: "
                        f"{discord.utils.escape_markdown(str(entry['backup']))}"
                        if entry["action"] in {"reset", "restore"}
                        else ""
                    )
                )
                lines.append(header)
                lines.extend(format_recorded_terms(entry))
            await send_private_chunks(
                interaction,
                (
                    f"نتائج سجل تغييرات "
                    f"{'جميع السيرفرات' if all_servers else 'هذا السيرفر'} "
                    f"({len(history)})"
                    + (f" — {'؛ '.join(filters)}" if filters else "")
                    + ":\n"
                    + "\n".join(lines)
                    + (f"\n\n{legacy_restore_note}" if legacy_restore_note else "")
                    if history
                    else ""
                ),
                empty_message=(
                    (
                        "لا توجد تغييرات تطابق معايير البحث في سجلات جميع السيرفرات."
                        if all_servers
                        else "لا توجد تغييرات تطابق معايير البحث لهذا السيرفر."
                    )
                    + (f"\n{legacy_restore_note}" if legacy_restore_note else "")
                    if filters
                    else (
                        "لا توجد تغييرات مسجلة في سجلات السيرفرات."
                        if all_servers
                        else "لا توجد تغييرات مسجلة لهذا السيرفر."
                    )
                ),
            )

    bot.tree.add_command(group)
    bot._profanity_filter_command_group = group
    return group


async def handle_profanity(
    message: discord.Message,
    developer_id: int,
    terms: Iterable[str] = BAD_WORDS,
) -> bool:
    """Delete and warn for a prohibited term; return whether moderation handled it."""
    if message.guild is None or message.author.bot:
        return False

    permissions = getattr(message.author, "guild_permissions", None)
    if (
        message.author.id == developer_id
        or getattr(permissions, "administrator", False)
    ):
        return False

    if contains_bad_word(message.content, terms) is None:
        return False

    try:
        await message.delete()
    except discord.NotFound:
        # The message is already gone; still warn the member about the rule.
        pass
    except discord.Forbidden:
        logger.warning(
            "Could not delete a prohibited-language message in guild %s, channel %s; "
            "the bot needs Manage Messages.",
            message.guild.id,
            message.channel.id,
        )
        return True
    except discord.HTTPException:
        logger.exception(
            "Discord rejected deletion of a prohibited-language message in guild %s, channel %s.",
            message.guild.id,
            message.channel.id,
        )
        return True

    try:
        await message.channel.send(
            f"🚫 {message.author.mention} يرجى استخدام لغة محترمة. "
            "حُذفت رسالتك؛ سيختفي هذا التنبيه بعد 5 ثوانٍ.",
            delete_after=WARNING_SECONDS,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        logger.warning(
            "Could not send a prohibited-language warning in guild %s, channel %s.",
            message.guild.id,
            message.channel.id,
        )
    except discord.HTTPException:
        logger.exception(
            "Discord rejected a prohibited-language warning in guild %s, channel %s.",
            message.guild.id,
            message.channel.id,
        )

    logger.info(
        "Removed a prohibited-language message from member %s in guild %s, channel %s.",
        message.author.id,
        message.guild.id,
        message.channel.id,
    )
    return True