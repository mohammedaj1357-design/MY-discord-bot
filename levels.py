"""Persistent, guild-scoped Discord XP and levels.

The level data deliberately lives under one top-level ``levels`` key in the
existing game store.  It is therefore committed atomically with other game
state, while never sharing the economy's ``users`` records.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import random
import time
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from co_owners import (
    CoOwnerStore,
    can_manage_server_settings,
    is_owner_or_co_owner,
)

logger = logging.getLogger(__name__)

XP_PER_MESSAGE = (15, 25)
XP_PER_VOICE_MINUTE = 10
VOICE_HISTORY_RETENTION_DAYS = 60
VOICE_MILESTONES = (
    (60, "ساعة", 100),
    (360, "6 ساعات", 500),
    (1_440, "يوم", 1_000),
    (10_080, "أسبوع", 5_000),
    (43_200, "30 يومًا", 20_000),
)
MESSAGE_COOLDOWN = 60.0
MAX_MANUAL_XP_GRANT = 1_000_000
MAX_MANUAL_XP_GRANT_HISTORY = 100
MAX_MANUAL_XP_GRANT_HISTORY_RESULTS = 10
DEFAULT_XP_EXPORT_FILE_SIZE_LIMIT = 8 * 1024 * 1024
MANUAL_XP_EXPORT_COLUMNS = (
    "grantor_id",
    "recipient_id",
    "amount",
    "old_level",
    "new_level",
    "guild_id",
    "timestamp",
)
LEVEL_UP_MESSAGE_LIMIT = 500
LEVEL_NOTICE_FAILURE_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_LEVEL_NOTICE_FAILURES = 50
LEVEL_NOTICE_FAILURE_ROUTES = frozenset({"dm", "channel"})
LEVEL_NOTICE_FAILURE_CAUSE_CODES = {
    "dm": frozenset(
        {"private_unavailable", "destination_unavailable", "discord_error"}
    ),
    "channel": frozenset(
        {
            "missing_channel_permissions",
            "destination_unavailable",
            "discord_error",
        }
    ),
}
LEVEL_NOTICE_FAILURE_CAUSE_LABELS = {
    "private_unavailable": "غير متاح للعضو",
    "missing_channel_permissions": "صلاحيات البوت في الروم غير كافية",
    "destination_unavailable": "وجهة الإرسال غير متاحة",
    "discord_error": "تعذر الإرسال بسبب خطأ من Discord",
}


def format_voice_time(total_minutes: int) -> str:
    """Format whole voice minutes as a readable Arabic duration."""
    if type(total_minutes) is not int or total_minutes < 0:
        raise ValueError("Voice time must be a non-negative whole number of minutes.")
    days, remainder = divmod(total_minutes, 1_440)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    for count, singular, dual, plural in (
        (days, "يوم", "يومان", "أيام"),
        (hours, "ساعة", "ساعتان", "ساعات"),
        (minutes, "دقيقة", "دقيقتان", "دقائق"),
    ):
        if not count:
            continue
        unit = singular if count == 1 or count > 10 else dual if count == 2 else plural
        parts.append(f"{count} {unit}")
    return " و ".join(parts) if parts else "0 دقيقة"


def format_voice_time_short(total_minutes: int) -> str:
    """Format whole voice minutes using compact d/h/m units."""
    if type(total_minutes) is not int or total_minutes < 0:
        raise ValueError("Voice time must be a non-negative whole number of minutes.")
    days, remainder = divmod(total_minutes, 1_440)
    hours, minutes = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"


def _voice_day(now: date | datetime | None = None) -> date:
    if now is None:
        return datetime.now(timezone.utc).date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc).date()
    if isinstance(now, date):
        return now
    raise ValueError("Voice statistics date must be a date or datetime.")


def _voice_milestone_bonus(old_minutes: int, new_minutes: int) -> int:
    """Return bonuses for milestone thresholds crossed by this award."""
    return sum(
        bonus
        for threshold, _label, bonus in VOICE_MILESTONES
        if old_minutes < threshold <= new_minutes
    )


def _manual_xp_export_csv_header() -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output).writerow(MANUAL_XP_EXPORT_COLUMNS)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def _manual_xp_export_csv_row(record: Mapping[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output).writerow(
        (
            record["actor_id"],
            record["user_id"],
            record["amount"],
            record["old_level"],
            record["new_level"],
            record["guild_id"],
            record["timestamp"],
        )
    )
    return output.getvalue().encode("utf-8")


def _split_manual_xp_export(
    records: list[Mapping[str, Any]],
    max_file_bytes: int,
) -> list[bytes]:
    """Serialize complete, independently readable CSV parts under the upload cap."""
    if type(max_file_bytes) is not int or max_file_bytes <= 0:
        raise ValueError("The XP export file-size limit must be a positive integer.")

    header = _manual_xp_export_csv_header()
    if len(header) > max_file_bytes:
        raise ValueError("The XP export file-size limit is too small for its CSV header.")

    parts: list[bytes] = []
    current = bytearray(header)
    for record in records:
        row = _manual_xp_export_csv_row(record)
        if len(header) + len(row) > max_file_bytes:
            raise ValueError("A retained XP grant cannot fit in one CSV attachment.")
        if len(current) + len(row) > max_file_bytes:
            parts.append(bytes(current))
            current = bytearray(header)
        current.extend(row)

    parts.append(bytes(current))
    return parts
LEVEL_ANNOUNCEMENT_MODES = frozenset({"same", "channel", "dm", "off"})
DEFAULT_LEVEL_ANNOUNCEMENT_SETTINGS = {
    "mode": "same",
    "channel_id": None,
    "mention_user": True,
    "custom_message": None,
}
LEVEL_TITLES = (
    (100, "إمبراطور"),
    (75, "نجم"),
    (50, "أسطورة"),
    (30, "خبير"),
    (20, "محترف"),
    (10, "نشيط"),
    (5, "مبتدئ"),
)


def title_for_level(level: int) -> str:
    """Return the highest milestone title reached by a member."""
    for threshold, title in LEVEL_TITLES:
        if level >= threshold:
            return title
    return "عضو جديد"


def xp_for_level(level: int) -> int:
    """Return the total XP required to reach ``level``."""
    if level < 0:
        raise ValueError("level must be non-negative")
    return 5 * level * level + 50 * level + 100


def _level_for_xp(xp: int) -> int:
    level = 0
    while xp >= xp_for_level(level + 1):
        level += 1
    return level


def _timestamp(value: float | int | None) -> float:
    return time.time() if value is None else float(value)


def _is_recent_failure(
    failure: Mapping[str, Any],
    cutoff: float,
    current: float,
) -> bool:
    """Reject malformed and expired persisted entries before they reach admins."""
    try:
        failed_at = float(failure["failed_at"])
        user_id = int(failure["user_id"])
        level = int(failure["level"])
        routes = failure["routes"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if not (
        cutoff <= failed_at <= current
        and user_id > 0
        and level > 0
        and isinstance(routes, list)
        and bool(routes)
        and all(
            isinstance(route, str) and route in LEVEL_NOTICE_FAILURE_ROUTES
            for route in routes
        )
    ):
        return False
    causes = failure.get("causes", {})
    if not isinstance(causes, Mapping):
        return False
    return all(
        route in routes
        and isinstance(cause, str)
        and cause in LEVEL_NOTICE_FAILURE_CAUSE_CODES[route]
        for route, cause in causes.items()
    )


def _notice_delivery_failure_cause(route: str, error: Exception) -> str:
    """Map Discord failures to safe categories without retaining error text."""
    status = getattr(error, "status", None)
    if route == "channel" and status == 403:
        return "missing_channel_permissions"
    if route == "dm" and status == 403:
        return "private_unavailable"
    if status == 404:
        return "destination_unavailable"
    return "discord_error"


class LevelSystem:
    """Award XP and query profiles using a GameStore-compatible object."""

    def __init__(
        self,
        store: Any,
        rng: Any = random,
    ) -> None:
        self.store = store
        self.rng = rng

    @staticmethod
    def _profile_template(guild_id: int, user_id: int) -> dict[str, Any]:
        return {
            "guild_id": int(guild_id),
            "user_id": int(user_id),
            "xp": 0,
            "level": 0,
            "total_messages": 0,
            "voice_minutes": 0,
            "voice_bonus_xp": 0,
            "voice_minutes_by_day": {},
            "last_message_at": None,
        }

    def _guild_profiles(self, data: dict[str, Any], guild_id: int) -> dict[str, Any]:
        levels = data.setdefault("levels", {})
        return levels.setdefault(str(guild_id), {})

    @staticmethod
    def _guild_role_rewards(
        data: dict[str, Any], guild_id: int
    ) -> dict[str, Any]:
        rewards = data.setdefault("level_role_rewards", {})
        return rewards.setdefault(str(guild_id), {})

    def _get_or_create(
        self, data: dict[str, Any], guild_id: int, user_id: int
    ) -> dict[str, Any]:
        profiles = self._guild_profiles(data, guild_id)
        key = str(user_id)
        profile = profiles.setdefault(key, self._profile_template(guild_id, user_id))
        # Make old/partially-created records forward compatible.
        for field, default in self._profile_template(guild_id, user_id).items():
            profile.setdefault(field, default)
        return profile

    def profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """Return a detached profile; querying does not create or persist it."""
        snapshot = self.store.snapshot()
        profile = (
            snapshot.get("levels", {})
            .get(str(guild_id), {})
            .get(str(user_id))
        )
        return deepcopy(
            profile
            if isinstance(profile, Mapping)
            else self._profile_template(guild_id, user_id)
        )

    def add_admin_xp(
        self,
        guild_id: int,
        user_id: int,
        amount: int,
    ) -> tuple[int, int, dict[str, Any]]:
        """Grant bounded XP without changing message or voice activity totals."""
        if type(amount) is not int or not 1 <= amount <= MAX_MANUAL_XP_GRANT:
            raise ValueError(
                f"XP must be a whole number from 1 to {MAX_MANUAL_XP_GRANT:,}."
            )
        with self.store.transaction() as data:
            profile = self._get_or_create(data, guild_id, user_id)
            old_level = int(profile["level"])
            profile["xp"] = int(profile["xp"]) + amount
            profile["level"] = _level_for_xp(int(profile["xp"]))
            return old_level, int(profile["level"]), deepcopy(profile)

    def add_manual_xp(
        self,
        guild_id: int,
        actor_id: int,
        user_id: int,
        amount: int,
    ) -> tuple[int, int, dict[str, Any]]:
        """Grant XP and persist its bounded audit record in the same transaction."""
        if type(amount) is not int or not 1 <= amount <= MAX_MANUAL_XP_GRANT:
            raise ValueError(
                f"XP must be a whole number from 1 to {MAX_MANUAL_XP_GRANT:,}."
            )
        if any(
            type(value) is not int or value <= 0
            for value in (guild_id, actor_id, user_id)
        ):
            raise ValueError("Guild, actor, and member IDs must be positive integers.")

        guild_key = str(guild_id)
        with self.store.transaction() as data:
            profile = self._get_or_create(data, guild_id, user_id)
            old_level = int(profile["level"])
            profile["xp"] = int(profile["xp"]) + amount
            profile["level"] = _level_for_xp(int(profile["xp"]))
            new_level = int(profile["level"])

            histories = data.setdefault("manual_xp_grants", {})
            if not isinstance(histories, dict):
                raise ValueError("Manual XP grant history is invalid; no grant was saved.")
            history = histories.setdefault(guild_key, [])
            if not isinstance(history, list):
                raise ValueError("Manual XP grant history is invalid; no grant was saved.")
            history.append(
                {
                    "guild_id": guild_id,
                    "actor_id": actor_id,
                    "user_id": user_id,
                    "amount": amount,
                    "old_level": old_level,
                    "new_level": new_level,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            del history[:-MAX_MANUAL_XP_GRANT_HISTORY]
            return old_level, new_level, deepcopy(profile)

    def recent_manual_xp_grants(
        self,
        guild_id: int | None = None,
        limit: int | None = MAX_MANUAL_XP_GRANT_HISTORY_RESULTS,
        grantor_id: int | None = None,
        recipient_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return valid grant records, optionally limiting the newest results."""
        if limit is not None and (
            type(limit) is not int or not 1 <= limit <= MAX_MANUAL_XP_GRANT_HISTORY
        ):
            raise ValueError(
                f"History limit must be from 1 to {MAX_MANUAL_XP_GRANT_HISTORY}."
            )
        for label, value in (
            ("Guild", guild_id),
            ("Grantor", grantor_id),
            ("Recipient", recipient_id),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{label} ID must be a positive integer.")

        histories = self.store.snapshot().get("manual_xp_grants", {})
        if not isinstance(histories, Mapping):
            return []
        selected = (
            {str(guild_id): histories.get(str(guild_id), [])}
            if guild_id is not None
            else histories
        )
        entries: list[tuple[datetime, dict[str, Any]]] = []
        for guild_key, history in selected.items():
            if not isinstance(history, list):
                continue
            try:
                stored_guild_id = int(guild_key)
            except (TypeError, ValueError):
                continue
            for record in history:
                if not isinstance(record, Mapping):
                    continue
                try:
                    timestamp = datetime.fromisoformat(record["timestamp"])
                    values = {
                        key: record[key]
                        for key in (
                            "guild_id",
                            "actor_id",
                            "user_id",
                            "amount",
                            "old_level",
                            "new_level",
                        )
                    }
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if timestamp.tzinfo is None:
                    continue
                if (
                    type(values["guild_id"]) is not int
                    or values["guild_id"] != stored_guild_id
                    or any(
                        type(values[key]) is not int or values[key] <= 0
                        for key in ("actor_id", "user_id", "amount")
                    )
                    or any(
                        type(values[key]) is not int or values[key] < 0
                        for key in ("old_level", "new_level")
                    )
                ):
                    continue
                if (
                    (grantor_id is not None and values["actor_id"] != grantor_id)
                    or (
                        recipient_id is not None
                        and values["user_id"] != recipient_id
                    )
                ):
                    continue
                entries.append(
                    (timestamp, {**values, "timestamp": record["timestamp"]})
                )
        entries.sort(key=lambda item: item[0], reverse=True)
        records = [record for _, record in entries]
        return records if limit is None else records[:limit]

    def award_message(
        self, guild_id: int, user_id: int, now: float | int | None = None
    ) -> tuple[bool, int, int]:
        """Award one random message amount unless the persisted cooldown applies."""
        old_level, new_level, amount = self.award_message_transition(
            guild_id, user_id, now
        )
        return new_level > old_level, new_level, amount

    def award_message_transition(
        self, guild_id: int, user_id: int, now: float | int | None = None
    ) -> tuple[int, int, int]:
        """Award message XP and return the exact old/new levels and XP amount."""
        current = _timestamp(now)
        with self.store.transaction() as data:
            profile = self._get_or_create(data, guild_id, user_id)
            previous = profile["last_message_at"]
            if previous is not None and current - float(previous) < MESSAGE_COOLDOWN:
                level = int(profile["level"])
                return level, level, 0
            amount = int(self.rng.randint(*XP_PER_MESSAGE))
            old_level = int(profile["level"])
            profile["xp"] = int(profile["xp"]) + amount
            profile["level"] = _level_for_xp(int(profile["xp"]))
            profile["total_messages"] = int(profile["total_messages"]) + 1
            profile["last_message_at"] = current
            return old_level, int(profile["level"]), amount

    def award_voice_minutes(
        self,
        guild_id: int,
        user_id: int,
        minutes: int = 1,
        *,
        now: date | datetime | None = None,
    ) -> tuple[bool, int, int]:
        """Award ten XP for each eligible whole voice minute supplied by caller."""
        old_level, new_level, amount = self.award_voice_minutes_transition(
            guild_id, user_id, minutes, now=now
        )
        return new_level > old_level, new_level, amount

    def award_voice_minutes_transition(
        self,
        guild_id: int,
        user_id: int,
        minutes: int = 1,
        *,
        now: date | datetime | None = None,
    ) -> tuple[int, int, int]:
        """Award voice XP, daily activity and one-time threshold bonuses."""
        minutes = int(minutes)
        if minutes <= 0:
            level = self.profile(guild_id, user_id)["level"]
            return int(level), int(level), 0
        day = _voice_day(now)
        with self.store.transaction() as data:
            profile = self._get_or_create(data, guild_id, user_id)
            old_level = int(profile["level"])
            old_minutes = int(profile["voice_minutes"])
            new_minutes = old_minutes + minutes
            bonus_xp = _voice_milestone_bonus(old_minutes, new_minutes)
            daily = profile.get("voice_minutes_by_day")
            if not isinstance(daily, dict):
                raise ValueError("Voice daily history must be an object.")
            day_key = day.isoformat()
            previous_daily = daily.get(day_key, 0)
            if type(previous_daily) is not int or previous_daily < 0:
                raise ValueError("Voice daily history contains an invalid minute total.")
            daily[day_key] = previous_daily + minutes
            cutoff = day - timedelta(days=VOICE_HISTORY_RETENTION_DAYS - 1)
            for stored_day in tuple(daily):
                try:
                    parsed_day = date.fromisoformat(stored_day)
                except (TypeError, ValueError):
                    raise ValueError("Voice daily history contains an invalid date.") from None
                stored_minutes = daily[stored_day]
                if type(stored_minutes) is not int or stored_minutes < 0:
                    raise ValueError("Voice daily history contains an invalid minute total.")
                if parsed_day < cutoff:
                    del daily[stored_day]
            amount = XP_PER_VOICE_MINUTE * minutes + bonus_xp
            profile["xp"] = int(profile["xp"]) + amount
            profile["level"] = _level_for_xp(int(profile["xp"]))
            profile["voice_minutes"] = new_minutes
            profile["voice_bonus_xp"] = (
                int(profile.get("voice_bonus_xp", 0)) + bonus_xp
            )
            return old_level, int(profile["level"]), amount

    def voice_stats(
        self,
        guild_id: int,
        user_id: int,
        *,
        now: date | datetime | None = None,
    ) -> dict[str, Any]:
        """Return total, current-period and seven-day voice activity."""
        today = _voice_day(now)
        profile = self.profile(guild_id, user_id)
        raw_daily = profile.get("voice_minutes_by_day", {})
        if not isinstance(raw_daily, Mapping):
            raise ValueError("Voice daily history must be an object.")
        daily: dict[date, int] = {}
        for key, value in raw_daily.items():
            try:
                stored_day = date.fromisoformat(key)
            except (TypeError, ValueError):
                raise ValueError("Voice daily history contains an invalid date.") from None
            if type(value) is not int or value < 0:
                raise ValueError("Voice daily history contains an invalid minute total.")
            daily[stored_day] = value

        week_start = today - timedelta(days=6)
        month_start = today.replace(day=1)
        chart = []
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            chart.append(
                {
                    "date": day.isoformat(),
                    "minutes": daily.get(day, 0),
                }
            )
        total_minutes = int(profile.get("voice_minutes", 0))
        bonus_xp = int(profile.get("voice_bonus_xp", 0))
        return {
            "today": daily.get(today, 0),
            "week": sum(value for day, value in daily.items() if week_start <= day <= today),
            "month": sum(
                value
                for day, value in daily.items()
                if month_start <= day <= today
            ),
            "total": total_minutes,
            "voice_xp": total_minutes * XP_PER_VOICE_MINUTE + bonus_xp,
            "bonus_xp": bonus_xp,
            "daily_chart": chart,
        }

    def set_role_reward(self, guild_id: int, level: int, role_id: int) -> None:
        """Persist one guild's role reward for a positive level milestone."""
        level = int(level)
        role_id = int(role_id)
        if level <= 0:
            raise ValueError("level must be positive")
        if role_id <= 0:
            raise ValueError("role_id must be positive")
        with self.store.transaction() as data:
            self._guild_role_rewards(data, guild_id)[str(level)] = role_id
            self._clear_role_reward_block_alert(data, guild_id, level)

    def remove_role_reward(self, guild_id: int, level: int) -> bool:
        """Remove one guild's role reward; return whether it existed."""
        level = int(level)
        with self.store.transaction() as data:
            rewards = data.setdefault("level_role_rewards", {}).get(str(guild_id), {})
            removed = rewards.pop(str(level), None) is not None
            self._clear_role_reward_block_alert(data, guild_id, level)
            return removed

    @staticmethod
    def _clear_role_reward_block_alert(
        data: dict[str, Any], guild_id: int, level: int
    ) -> None:
        """Forget alert state when an administrator edits a reward."""
        alerts_by_guild = data.get("level_role_reward_block_alerts")
        if not isinstance(alerts_by_guild, dict):
            return
        guild_alerts = alerts_by_guild.get(str(int(guild_id)))
        if not isinstance(guild_alerts, dict):
            return
        guild_alerts.pop(str(int(level)), None)
        if not guild_alerts:
            alerts_by_guild.pop(str(int(guild_id)), None)

    def unreported_role_reward_blocks(
        self,
        guild_id: int,
        blocked_rewards: Mapping[int, tuple[int, str]],
    ) -> list[tuple[int, int, str]]:
        """Return blocked configured rewards not yet reported to this guild's owner."""
        all_alerts = self.store.snapshot().get("level_role_reward_block_alerts", {})
        alerts = (
            all_alerts.get(str(int(guild_id)), {})
            if isinstance(all_alerts, Mapping)
            else {}
        )
        if not isinstance(alerts, Mapping):
            alerts = {}

        pending: list[tuple[int, int, str]] = []
        for raw_level, (raw_role_id, reason) in blocked_rewards.items():
            try:
                level = int(raw_level)
                role_id = int(raw_role_id)
            except (TypeError, ValueError, OverflowError):
                continue
            previous = alerts.get(str(level))
            if isinstance(previous, Mapping):
                try:
                    if (
                        int(previous.get("role_id", 0)) == role_id
                        and previous.get("reason") == reason
                    ):
                        continue
                except (TypeError, ValueError, OverflowError):
                    pass
            pending.append((level, role_id, str(reason)))
        return sorted(pending)

    def record_role_reward_block_check(
        self,
        guild_id: int,
        blocked_rewards: Mapping[int, tuple[int, str]],
        delivered_levels: set[int],
    ) -> None:
        """Persist delivered blockers and clear recovered or removed rewards."""
        guild_key = str(int(guild_id))
        delivered = {int(level) for level in delivered_levels}
        with self.store.transaction() as data:
            alerts_by_guild = data.get("level_role_reward_block_alerts")
            if not isinstance(alerts_by_guild, dict):
                alerts_by_guild = {}
                data["level_role_reward_block_alerts"] = alerts_by_guild
            stored = alerts_by_guild.get(guild_key, {})
            if not isinstance(stored, Mapping):
                stored = {}

            updated: dict[str, dict[str, Any]] = {}
            for raw_level, (raw_role_id, reason) in blocked_rewards.items():
                try:
                    level = int(raw_level)
                    role_id = int(raw_role_id)
                except (TypeError, ValueError, OverflowError):
                    continue
                key = str(level)
                previous = stored.get(key)
                if level in delivered:
                    updated[key] = {"role_id": role_id, "reason": str(reason)}
                    continue
                if isinstance(previous, Mapping):
                    try:
                        if (
                            int(previous.get("role_id", 0)) == role_id
                            and previous.get("reason") == reason
                        ):
                            updated[key] = {
                                "role_id": role_id,
                                "reason": str(reason),
                            }
                    except (TypeError, ValueError, OverflowError):
                        pass

            if updated:
                alerts_by_guild[guild_key] = updated
            else:
                alerts_by_guild.pop(guild_key, None)

    def role_rewards(self, guild_id: int) -> dict[int, int]:
        """Return a detached mapping of milestone levels to Discord role IDs."""
        rewards = self.store.snapshot().get("level_role_rewards", {}).get(
            str(guild_id), {}
        )
        return {
            int(level): int(role_id)
            for level, role_id in rewards.items()
        }

    def earned_role_rewards(self, guild_id: int) -> list[tuple[int, int, int]]:
        """Return unique (user ID, milestone, role ID) rewards already earned."""
        snapshot = self.store.snapshot()
        raw_rewards = (
            snapshot.get("level_role_rewards", {})
            .get(str(int(guild_id)), {})
        )
        profiles = (
            snapshot.get("levels", {})
            .get(str(int(guild_id)), {})
        )
        if not isinstance(profiles, Mapping) or not isinstance(raw_rewards, Mapping):
            return []

        try:
            rewards = {
                int(level): int(role_id)
                for level, role_id in raw_rewards.items()
            }
        except (TypeError, ValueError, OverflowError):
            return []

        earned: dict[tuple[int, int], int] = {}
        for raw_user_id, profile in profiles.items():
            if not isinstance(profile, Mapping):
                continue
            try:
                user_id = int(raw_user_id)
                current_level = int(profile.get("level", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if user_id <= 0 or current_level <= 0:
                continue
            for milestone, role_id in rewards.items():
                if current_level < milestone:
                    continue
                assignment = (user_id, role_id)
                earned[assignment] = min(
                    milestone,
                    earned.get(assignment, milestone),
                )
        return sorted(
            (
                user_id,
                milestone,
                role_id,
            )
            for (user_id, role_id), milestone in earned.items()
        )

    def announcement_settings(self, guild_id: int) -> dict[str, Any]:
        """Return detached per-server announcement settings with safe defaults."""
        snapshot = self.store.snapshot()
        raw = (
            snapshot.get("level_announcement_settings", {})
            .get(str(guild_id), {})
        )
        settings = deepcopy(DEFAULT_LEVEL_ANNOUNCEMENT_SETTINGS)
        if isinstance(raw, Mapping):
            settings.update(
                {
                    key: deepcopy(raw[key])
                    for key in DEFAULT_LEVEL_ANNOUNCEMENT_SETTINGS
                    if key in raw
                }
            )
        return settings

    def set_announcement_settings(
        self, guild_id: int, updates: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Persist validated per-server announcement settings atomically."""
        unknown = set(updates) - set(DEFAULT_LEVEL_ANNOUNCEMENT_SETTINGS)
        if unknown:
            raise ValueError(f"unknown announcement settings: {sorted(unknown)}")

        guild_key = str(int(guild_id))
        with self.store.transaction() as data:
            settings_by_guild = data.setdefault("level_announcement_settings", {})
            stored = settings_by_guild.get(guild_key, {})
            settings = deepcopy(DEFAULT_LEVEL_ANNOUNCEMENT_SETTINGS)
            if isinstance(stored, Mapping):
                settings.update(stored)
            settings.update(deepcopy(dict(updates)))

            if (
                not isinstance(settings["mode"], str)
                or settings["mode"] not in LEVEL_ANNOUNCEMENT_MODES
            ):
                raise ValueError("invalid announcement mode")
            if settings["mode"] == "channel":
                channel_id = settings["channel_id"]
                if (
                    isinstance(channel_id, bool)
                    or not isinstance(channel_id, int)
                    or channel_id <= 0
                ):
                    raise ValueError("channel mode requires a positive channel ID")
            elif settings["channel_id"] is not None:
                raise ValueError("only channel mode may have a channel ID")
            if not isinstance(settings["mention_user"], bool):
                raise ValueError("mention_user must be boolean")
            custom_message = settings["custom_message"]
            if custom_message is not None and (
                not isinstance(custom_message, str)
                or not custom_message.strip()
                or len(custom_message) > LEVEL_UP_MESSAGE_LIMIT
            ):
                raise ValueError(
                    f"custom_message must be non-empty and at most "
                    f"{LEVEL_UP_MESSAGE_LIMIT} characters"
                )

            settings_by_guild[guild_key] = settings
            return deepcopy(settings)

    def reset_announcement_settings(self, guild_id: int) -> bool:
        """Remove one server's announcement overrides and return whether present."""
        with self.store.transaction() as data:
            settings_by_guild = data.setdefault("level_announcement_settings", {})
            return settings_by_guild.pop(str(int(guild_id)), None) is not None

    def record_notice_delivery_failure(
        self,
        guild_id: int,
        user_id: int,
        level: int,
        failed_routes: tuple[str, ...] | list[str],
        now: float | int | None = None,
        *,
        failure_causes: Mapping[str, str] | None = None,
    ) -> None:
        """Save a privacy-safe, bounded record of a failed level-up notice."""
        routes = list(dict.fromkeys(failed_routes))
        if not routes or any(
            not isinstance(route, str) or route not in LEVEL_NOTICE_FAILURE_ROUTES
            for route in routes
        ):
            raise ValueError("failed_routes contains an unsupported destination")
        if failure_causes is not None and not isinstance(
            failure_causes,
            Mapping,
        ):
            raise ValueError("failure_causes must map routes to safe cause codes")
        causes: dict[str, str] = {}
        for route, cause in (failure_causes or {}).items():
            if (
                route not in routes
                or route not in LEVEL_NOTICE_FAILURE_CAUSE_CODES
                or not isinstance(cause, str)
                or cause not in LEVEL_NOTICE_FAILURE_CAUSE_CODES[route]
            ):
                raise ValueError("failure_causes contains an unsupported category")
            causes[route] = cause

        current = _timestamp(now)
        cutoff = current - LEVEL_NOTICE_FAILURE_RETENTION_SECONDS
        guild_key = str(int(guild_id))
        event = {
            "user_id": int(user_id),
            "level": int(level),
            "failed_at": current,
            "routes": routes,
            "causes": causes,
        }
        with self.store.transaction() as data:
            failures_by_guild = data.setdefault("level_notice_delivery_failures", {})
            stored = failures_by_guild.get(guild_key, [])
            if not isinstance(stored, list):
                stored = []
            recent = [
                deepcopy(failure)
                for failure in stored
                if isinstance(failure, Mapping)
                and _is_recent_failure(failure, cutoff, current)
            ]
            recent.append(event)
            failures_by_guild[guild_key] = recent[-MAX_LEVEL_NOTICE_FAILURES:]

    def recent_notice_delivery_failures(
        self,
        guild_id: int,
        limit: int = MAX_LEVEL_NOTICE_FAILURES,
        now: float | int | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent privacy-safe failures for this guild only."""
        current = _timestamp(now)
        cutoff = current - LEVEL_NOTICE_FAILURE_RETENTION_SECONDS
        guild_key = str(int(guild_id))
        bounded_limit = max(0, min(int(limit), MAX_LEVEL_NOTICE_FAILURES))
        with self.store.transaction() as data:
            failures_by_guild = data.setdefault("level_notice_delivery_failures", {})
            stored = failures_by_guild.get(guild_key, [])
            if not isinstance(stored, list):
                stored = []
            recent = [
                deepcopy(failure)
                for failure in stored
                if isinstance(failure, Mapping)
                and _is_recent_failure(failure, cutoff, current)
            ][-MAX_LEVEL_NOTICE_FAILURES:]
            if recent:
                failures_by_guild[guild_key] = recent
            else:
                failures_by_guild.pop(guild_key, None)
        if bounded_limit == 0:
            return []
        return list(reversed(recent[-bounded_limit:]))

    def clear_notice_delivery_failures(self, guild_id: int) -> int:
        """Remove this guild's stored notice delivery failures and return their count."""
        guild_key = str(int(guild_id))
        with self.store.transaction() as data:
            failures_by_guild = data.setdefault("level_notice_delivery_failures", {})
            stored = failures_by_guild.pop(guild_key, [])
        return len(stored) if isinstance(stored, list) else 0

    def role_rewards_crossed(
        self, guild_id: int, old_level: int, new_level: int
    ) -> list[tuple[int, int]]:
        """Return configured (level, role ID) rewards crossed by this XP gain."""
        if new_level <= old_level:
            return []
        return [
            (level, role_id)
            for level, role_id in sorted(self.role_rewards(guild_id).items())
            if old_level < level <= new_level
        ]

    def leaderboard(
        self,
        guild_id: int,
        limit: int = 10,
        *,
        metric: str = "xp",
    ) -> list[dict[str, Any]]:
        """Return active profiles sorted by XP, counted messages, or voice time."""
        if limit <= 0:
            return []
        fields = {
            "xp": "xp",
            "messages": "total_messages",
            "voice": "voice_minutes",
        }
        if metric not in fields:
            raise ValueError("Leaderboard metric must be xp, messages, or voice.")
        field = fields[metric]
        snapshot = self.store.snapshot()
        profiles = snapshot.get("levels", {}).get(str(guild_id), {})
        values = [
            deepcopy(profile)
            for profile in profiles.values()
            if isinstance(profile, Mapping) and int(profile.get(field, 0)) > 0
        ]
        values.sort(
            key=lambda profile: (
                -int(profile.get(field, 0)),
                int(profile.get("user_id", 0)),
            )
        )
        return values[:limit]

    def rank(self, guild_id: int, user_id: int) -> tuple[int | None, int]:
        """Return ``(one_based_rank, guild_population)``."""
        entries = self.leaderboard(guild_id, limit=10**9)
        total = len(entries)
        for position, entry in enumerate(entries, 1):
            if int(entry.get("user_id", 0)) == int(user_id):
                return position, total
        return None, total

    def guild_stats(self, guild_id: int) -> dict[str, int]:
        """Return aggregate, non-identifying level statistics for one server."""
        profiles = (
            self.store.snapshot()
            .get("levels", {})
            .get(str(guild_id), {})
        )
        valid_profiles = [
            profile for profile in profiles.values() if isinstance(profile, Mapping)
        ]
        return {
            "members": sum(int(profile.get("xp", 0)) > 0 for profile in valid_profiles),
            "total_xp": sum(int(profile.get("xp", 0)) for profile in valid_profiles),
            "total_messages": sum(
                int(profile.get("total_messages", 0)) for profile in valid_profiles
            ),
            "voice_minutes": sum(
                int(profile.get("voice_minutes", 0)) for profile in valid_profiles
            ),
            "highest_level": max(
                (int(profile.get("level", 0)) for profile in valid_profiles),
                default=0,
            ),
        }


def _safe_name(member: discord.Member) -> str:
    return discord.utils.escape_markdown(
        getattr(member, "display_name", getattr(member, "name", str(member)))
    )[:100]


def _render_level_notice_custom_message(
    template: str,
    *,
    user_label: str,
    level: int,
    guild_name: str,
) -> str:
    replacements = {
        "{user}": user_label,
        "{level}": str(level),
        "{title}": title_for_level(level),
        "{guild}": guild_name,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


async def _leaderboard_member_label(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return f"👤 عضو غادر (`{user_id}`)"
        except discord.HTTPException:
            logger.exception(
                "Could not fetch leaderboard member %s in guild %s.",
                user_id,
                guild.id,
            )
            return f"<@{user_id}>"
    return member.mention


async def send_level_up_notice(
    system: LevelSystem,
    guild: discord.Guild,
    member: discord.Member,
    channel,
    level: int,
) -> None:
    """Send a level-up notice using the guild's saved destination and template."""
    settings = system.announcement_settings(guild.id)
    mode = settings["mode"]
    if mode == "off":
        return

    mention_user = settings["mention_user"]
    safe_name = _safe_name(member)
    user_label = member.mention if mention_user else safe_name
    custom_message = settings["custom_message"]
    if custom_message:
        description = _render_level_notice_custom_message(
            custom_message,
            user_label=user_label,
            level=level,
            guild_name=discord.utils.escape_markdown(guild.name)[:100],
        )
    else:
        description = (
            f"وصل **{safe_name}** إلى المستوى **{level}**.\n"
            f"الرتبة: **{title_for_level(level)}**"
        )

    embed = discord.Embed(
        title="🎉 مستوى جديد!",
        description=description,
        color=0xFFD700,
    )
    avatar = getattr(member, "display_avatar", None) or getattr(member, "avatar", None)
    avatar_url = getattr(avatar, "url", None)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    allowed_mentions = discord.AllowedMentions(
        users=[member] if mention_user else False,
        roles=False,
        everyone=False,
        replied_user=False,
    )
    send_options = {
        "content": member.mention if mention_user else None,
        "embed": embed,
        "allowed_mentions": allowed_mentions,
    }

    failed_routes: list[str] = []
    failure_causes: dict[str, str] = {}
    if mode == "dm":
        try:
            await member.send(**send_options)
            return
        except discord.HTTPException as error:
            failed_routes.append("dm")
            failure_causes["dm"] = _notice_delivery_failure_cause(
                "dm",
                error,
            )
            logger.info(
                "Could not DM level-up notice in guild %s; falling back to channel.",
                guild.id,
            )

    target_channel = channel
    if mode == "channel":
        configured = guild.get_channel(settings["channel_id"])
        if configured is not None:
            target_channel = configured
        else:
            logger.warning(
                "Configured level-up channel is unavailable in guild %s; "
                "falling back to the triggering channel.",
                guild.id,
            )

    if target_channel is None:
        logger.info("No channel is available for a level-up notice in guild %s.", guild.id)
        return
    try:
        await target_channel.send(**send_options)
    except discord.HTTPException as error:
        failed_routes.append("channel")
        failure_causes["channel"] = _notice_delivery_failure_cause(
            "channel",
            error,
        )
        logger.warning(
            "Could not send level-up notice in guild %s (%s).",
            guild.id,
            failure_causes["channel"],
        )
        try:
            system.record_notice_delivery_failure(
                guild.id,
                member.id,
                level,
                failed_routes,
                failure_causes=failure_causes,
            )
        except Exception:
            logger.exception(
                "Could not record level-up notice delivery failure in guild %s.",
                guild.id,
            )


def role_reward_block_reason(role: Any, bot_member: Any) -> str | None:
    """Explain why the bot cannot assign a configured role, if it cannot."""
    if role is None:
        return "the configured role no longer exists"
    if bot_member is None:
        return "the bot's server member is unavailable"
    if not getattr(getattr(bot_member, "guild_permissions", None), "manage_roles", False):
        return "the bot is missing Manage Roles permission"
    if getattr(role, "managed", False):
        return "the role is managed by an integration and cannot be assigned"
    is_default = getattr(role, "is_default", None)
    if callable(is_default) and is_default():
        return "the server's @everyone role cannot be assigned as a reward"
    top_role = getattr(bot_member, "top_role", None)
    if top_role is None or role >= top_role:
        return "the role is not below the bot's highest role"
    return None


def _role_reward_block_reason_message(reason: str) -> str:
    """Translate role-assignment blockers for level configuration replies."""
    messages = {
        "the configured role no longer exists": "الرتبة المحددة لم تعد موجودة",
        "the bot's server member is unavailable": "تعذر التحقق من رتبة البوت وصلاحياته",
        "the bot is missing Manage Roles permission": (
            "البوت لا يملك صلاحية إدارة الرتب (Manage Roles)"
        ),
        "the role is managed by an integration and cannot be assigned": (
            "يدير هذه الرتبة تكامل خارجي ولا يستطيع البوت منحها"
        ),
        "the server's @everyone role cannot be assigned as a reward": (
            "لا يمكن استخدام رتبة @everyone كمكافأة"
        ),
        "the role is not below the bot's highest role": (
            "الرتبة ليست أدنى من أعلى رتبة للبوت؛ ارفع رتبة البوت فوقها"
        ),
    }
    return messages.get(reason, reason)


def _guild_bot_member(guild: Any, bot: commands.Bot) -> Any:
    bot_member = getattr(guild, "me", None)
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    return bot_member


async def notify_unassignable_role_rewards(
    system: LevelSystem,
    guild: discord.Guild,
    bot_member: discord.Member | None = None,
) -> int:
    """Privately notify a guild owner about newly blocked saved role rewards."""
    rewards = system.role_rewards(guild.id)
    if bot_member is None:
        bot_member = getattr(guild, "me", None)
    if bot_member is None:
        # Do not treat a temporary inability to inspect the bot as a reward failure.
        return 0

    blocked: dict[int, tuple[int, str]] = {}
    role_by_id: dict[int, Any] = {}
    for level, role_id in rewards.items():
        role = guild.get_role(role_id)
        role_by_id[role_id] = role
        reason = role_reward_block_reason(role, bot_member)
        if reason is not None:
            blocked[level] = (role_id, reason)

    pending = system.unreported_role_reward_blocks(guild.id, blocked)
    if not pending:
        system.record_role_reward_block_check(guild.id, blocked, set())
        return 0

    owner = getattr(guild, "owner", None)
    if owner is None:
        owner_id = getattr(guild, "owner_id", None)
        fetch_member = getattr(guild, "fetch_member", None)
        if owner_id is not None and callable(fetch_member):
            try:
                owner = await fetch_member(owner_id)
            except discord.HTTPException:
                logger.warning(
                    "Could not find the owner to report blocked level rewards in guild %s.",
                    guild.id,
                    exc_info=True,
                )
                system.record_role_reward_block_check(guild.id, blocked, set())
                return 0
    if owner is None or not callable(getattr(owner, "send", None)):
        logger.warning(
            "Could not find the owner to report blocked level rewards in guild %s.",
            guild.id,
        )
        system.record_role_reward_block_check(guild.id, blocked, set())
        return 0

    grouped: dict[tuple[int, str], list[int]] = {}
    for level, role_id, reason in pending:
        grouped.setdefault((role_id, reason), []).append(level)

    guild_name = discord.utils.escape_markdown(
        str(getattr(guild, "name", "هذا السيرفر")).replace("\n", " ")
    )
    lines = []
    for (role_id, reason), levels in sorted(grouped.items()):
        role = role_by_id.get(role_id)
        role_name = getattr(role, "name", None)
        if role_name is not None:
            safe_role_name = discord.utils.escape_markdown(
                str(role_name).replace("\n", " ")
            )
            role_label = f"**{safe_role_name}**"
        else:
            role_label = f"رتبة محذوفة (ID `{role_id}`)"
        level_label = " و ".join(f"`{level}`" for level in sorted(levels))
        lines.append(
            f"- {role_label} (المستويات {level_label}): "
            f"{_role_reward_block_reason_message(reason)}."
        )

    message = (
        f"⚠️ تنبيه لإدارة السيرفر **{guild_name}**: "
        "لم تعد مكافآت الرتب التالية قابلة للمنح:\n"
        + "\n".join(lines)
        + "\nلم يغيّر البوت XP أو إعدادات المكافآت. "
        "أصلح صلاحية إدارة الرتب أو ترتيبها، ثم تحقق باستخدام `!level_roles`."
    )
    try:
        await owner.send(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.warning(
            "Could not DM the owner about blocked level rewards in guild %s.",
            guild.id,
            exc_info=True,
        )
        system.record_role_reward_block_check(guild.id, blocked, set())
        return 0

    system.record_role_reward_block_check(
        guild.id,
        blocked,
        {level for level, _, _ in pending},
    )
    return len(grouped)


def _level_notice_channel_missing_permissions(
    channel: Any,
    bot_member: Any,
) -> tuple[str, ...]:
    """Return missing effective permissions required to post level-up embeds."""
    if bot_member is None:
        return ("تعذر التحقق من صلاحيات البوت في الروم المحدد",)
    try:
        permissions = channel.permissions_for(bot_member)
    except Exception:
        logger.exception(
            "Could not inspect level notice channel permissions for guild %s.",
            getattr(getattr(channel, "guild", None), "id", "unknown"),
        )
        return ("تعذر التحقق من صلاحيات البوت في الروم المحدد",)

    required = (
        ("view_channel", "عرض الروم (View Channel)"),
        ("send_messages", "إرسال الرسائل (Send Messages)"),
        ("embed_links", "إرسال الروابط المضمّنة (Embed Links)"),
    )
    return tuple(
        label
        for attribute, label in required
        if not getattr(permissions, attribute, False)
    )


async def reconcile_role_rewards(
    system: LevelSystem,
    guild: discord.Guild,
    bot_member: discord.Member | None = None,
) -> dict[str, int]:
    """Grant currently earned rewards to members of this guild without changing XP."""
    candidates = system.earned_role_rewards(guild.id)
    summary = {
        "eligible_members": len({user_id for user_id, _, _ in candidates}),
        "granted": 0,
        "already_present": 0,
        "blocked": 0,
        "members_not_found": 0,
        "member_lookup_failed": 0,
        "assignment_failed": 0,
    }
    if not candidates:
        return summary

    if bot_member is None:
        bot_member = getattr(guild, "me", None)

    role_status: dict[int, tuple[Any, str | None]] = {}
    for _, _, role_id in candidates:
        if role_id in role_status:
            continue
        role = guild.get_role(role_id)
        role_status[role_id] = (
            role,
            role_reward_block_reason(role, bot_member),
        )

    candidates_by_member: dict[int, list[tuple[int, Any]]] = {}
    for user_id, milestone, role_id in candidates:
        role, reason = role_status[role_id]
        if reason is not None or role is None:
            summary["blocked"] += 1
            continue
        candidates_by_member.setdefault(user_id, []).append((milestone, role))

    for user_id, rewards in candidates_by_member.items():
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                summary["members_not_found"] += 1
                continue
            except (discord.Forbidden, discord.HTTPException):
                summary["member_lookup_failed"] += 1
                continue

        for milestone, role in rewards:
            if any(existing_role.id == role.id for existing_role in member.roles):
                summary["already_present"] += 1
                continue
            try:
                await member.add_roles(
                    role,
                    reason=f"Reconciled earned level {milestone} reward",
                )
                summary["granted"] += 1
            except (discord.Forbidden, discord.HTTPException):
                summary["assignment_failed"] += 1

    return summary


class _RoleRewardReconcileView(discord.ui.View):
    """Require the bot developer to confirm a guild-scoped bulk role grant."""

    def __init__(
        self,
        system: LevelSystem,
        guild: discord.Guild,
        developer_id: int,
        bot: commands.Bot,
    ) -> None:
        super().__init__(timeout=60)
        self.system = system
        self.guild = guild
        self.developer_id = int(developer_id)
        self.bot = bot
        self.finished = False
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            getattr(interaction, "guild_id", None) != self.guild.id
            or getattr(getattr(interaction, "user", None), "id", None)
            != self.developer_id
        ):
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت ومن داخل السيرفر الذي بدأ المراجعة فقط.",
                ephemeral=True,
            )
            return False
        return True

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="تأكيد المراجعة", style=discord.ButtonStyle.danger)
    async def confirm_reconcile(
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
        self._finish()
        await interaction.response.defer()
        try:
            bot_member = getattr(self.guild, "me", None)
            if bot_member is None and self.bot.user is not None:
                bot_member = self.guild.get_member(self.bot.user.id)
            summary = await reconcile_role_rewards(
                self.system,
                self.guild,
                bot_member,
            )
        except Exception:
            logger.exception(
                "Could not reconcile level role rewards in guild %s.",
                self.guild.id,
            )
            await interaction.edit_original_response(
                content=(
                    "تعذرت مراجعة مكافآت الرتب. قد تكون بعض الرتب أُضيفت؛ "
                    "تحقق من الأعضاء ثم أعد المحاولة بأمان."
                ),
                view=self,
            )
            return

        guild_name = discord.utils.escape_markdown(self.guild.name)
        await interaction.edit_original_response(
            content=(
                f"انتهت مراجعة مكافآت الرتب في **{guild_name}**: "
                f"أُضيفت `{summary['granted']}`، كانت موجودة مسبقًا "
                f"`{summary['already_present']}`، تعذر منح "
                f"`{summary['blocked'] + summary['assignment_failed']}`، "
                f"وغاب أو تعذر جلب "
                f"`{summary['members_not_found'] + summary['member_lookup_failed']}` "
                "عضو. لم يتغير XP."
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            view=self,
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel_reconcile(
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
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت مراجعة مكافآت الرتب؛ لم تُضف أي رتبة.",
            view=self,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self._finish()
        if self.message is not None:
            try:
                await self.message.edit(
                    content="انتهت مهلة التأكيد؛ لم تُضف أي رتبة.",
                    view=self,
                )
            except discord.HTTPException:
                pass


class _LevelSettingsResetView(discord.ui.View):
    """Require the invoking administrator to confirm this guild-only reset."""

    _TIMEOUT_SECONDS = 60
    _EXPIRED_MESSAGE = (
        "انتهت مهلة تأكيد إعادة الضبط؛ لم تتغير الإعدادات. "
        "ابدأ من جديد باستخدام الأمر `/levelsetup`."
    )

    def __init__(
        self,
        system: LevelSystem,
        guild_id: int,
        user_id: int,
        interaction: discord.Interaction | None = None,
    ) -> None:
        super().__init__(timeout=self._TIMEOUT_SECONDS)
        self.system = system
        self.guild_id = guild_id
        self.user_id = user_id
        self.interaction = interaction
        self._expires_at = time.monotonic() + self._TIMEOUT_SECONDS
        self.finished = False
        self.expired = False

    def _is_expired(self) -> bool:
        return self.expired or time.monotonic() >= self._expires_at

    def _finish(self) -> None:
        self.finished = True
        for child in self.children:
            child.disabled = True
        self.stop()

    async def _handle_expired_interaction(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if not self._is_expired():
            return False
        self.expired = True
        self._finish()
        await interaction.response.edit_message(
            content=self._EXPIRED_MESSAGE,
            view=self,
        )
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمن بدأ إعادة الضبط فقط.",
                ephemeral=True,
            )
            return False
        permissions = getattr(interaction.user, "guild_permissions", None)
        if permissions is None or not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "يجب أن تكون إداريًا في السيرفر لتأكيد إعادة الضبط.",
                ephemeral=True,
            )
            return False
        if await self._handle_expired_interaction(interaction):
            return False
        if self.finished:
            await interaction.response.send_message(
                "هذا التأكيد لم يعد نشطًا.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="تأكيد إعادة الضبط",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_reset(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._handle_expired_interaction(interaction):
            return
        if self.finished:
            await interaction.response.send_message(
                "هذا التأكيد لم يعد نشطًا.",
                ephemeral=True,
            )
            return
        self._finish()
        try:
            self.system.reset_announcement_settings(self.guild_id)
        except Exception:
            logger.exception(
                "Could not reset level announcement settings for guild %s.",
                self.guild_id,
            )
            await interaction.response.edit_message(
                content="تعذر حفظ إعادة الضبط؛ بقيت الإعدادات كما هي.",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content="أُعيدت إعدادات إشعارات المستويات لهذا السيرفر إلى الافتراضي.",
                view=None,
            )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel_reset(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if await self._handle_expired_interaction(interaction):
            return
        if self.finished:
            await interaction.response.send_message(
                "هذا التأكيد لم يعد نشطًا.",
                ephemeral=True,
            )
            return
        self._finish()
        await interaction.response.edit_message(
            content="أُلغيت إعادة الضبط؛ لم تتغير الإعدادات.",
            view=None,
        )

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.expired = True
        self._finish()
        if self.interaction is None:
            return
        try:
            await self.interaction.edit_original_response(
                content=self._EXPIRED_MESSAGE,
                allowed_mentions=discord.AllowedMentions.none(),
                view=self,
            )
        except discord.HTTPException:
            logger.warning(
                "Could not update expired level settings reset prompt for guild %s.",
                self.guild_id,
                exc_info=True,
            )


def _parse_level_delivery_dates(
    from_date: str | None,
    to_date: str | None,
) -> tuple[date | None, date | None]:
    """Parse optional inclusive UTC calendar-date bounds."""
    def parse(value: str | None, label: str) -> date | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(
                f"تاريخ {label} غير صالح. استخدم الصيغة YYYY-MM-DD."
            )
        normalized = value.strip()
        try:
            parsed = date.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError(
                f"تاريخ {label} غير صالح. استخدم الصيغة YYYY-MM-DD."
            ) from error
        if parsed.isoformat() != normalized:
            raise ValueError(
                f"تاريخ {label} غير صالح. استخدم الصيغة YYYY-MM-DD."
            )
        return parsed

    start = parse(from_date, "البداية")
    end = parse(to_date, "النهاية")
    if start is not None and end is not None and start > end:
        raise ValueError("تاريخ البداية يجب ألا يكون بعد تاريخ النهاية.")
    return start, end


def _level_notice_failures_export_payload(
    guild_id: int,
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project only privacy-safe retained fields into the downloadable report."""
    exported_failures = []
    for failure in failures:
        routes = list(failure["routes"])
        raw_causes = failure.get("causes", {})
        causes: dict[str, str] = {}
        if isinstance(raw_causes, Mapping):
            for route in routes:
                cause = raw_causes.get(route)
                if (
                    isinstance(route, str)
                    and isinstance(cause, str)
                    and cause in LEVEL_NOTICE_FAILURE_CAUSE_CODES.get(route, ())
                ):
                    causes[route] = cause
        exported_failures.append(
            {
                "user_id": int(failure["user_id"]),
                "level": int(failure["level"]),
                "failed_at_unix": int(float(failure["failed_at"])),
                "routes": routes,
                "causes": causes,
            }
        )
    return {
        "title": "Level notice delivery failures",
        "schema_version": 1,
        "guild_id": int(guild_id),
        "exported_at_unix": int(time.time()),
        "failures": exported_failures,
    }


class LevelResetApprovalView(discord.ui.View):
    """Require a developer's explicit approval before backing up and resetting XP."""

    def __init__(self, developer_id: int, action, *, timeout: float = 30 * 60):
        super().__init__(timeout=timeout)
        self.developer_id = int(developer_id)
        self.action = action
        self.deadline = time.monotonic() + timeout
        self.finished = False
        self.message = None

    def _disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _check_developer(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا الطلب خاص بمطور البوت.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if self.finished or time.monotonic() >= self.deadline:
            self.finished = True
            self._disable_buttons()
            if self.message is not None:
                try:
                    await self.message.edit(
                        content="انتهت مهلة التأكيد؛ لم يتم تصفير XP.",
                        embed=None,
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    logger.info("Could not update a late XP reset approval.")
            await interaction.response.send_message(
                "انتهت صلاحية التأكيد؛ لم يتم تصفير XP.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        self._disable_buttons()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="انتهت مهلة التأكيد؛ لم يتم تصفير XP.",
                embed=None,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.info("Could not update an expired XP reset approval.")

    @discord.ui.button(label="تأكيد التصفير", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if not await self._check_developer(interaction):
            return
        self.finished = True
        self._disable_buttons()
        await interaction.response.defer()
        try:
            result = await self.action()
        except Exception:
            logger.exception("Approved XP reset failed before completion.")
            result = "تعذر إكمال التصفير؛ لم يُجرَ أي حذف دون النسخة الاحتياطية."
        if self.message is not None:
            try:
                await self.message.edit(
                    content=result,
                    embed=None,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.info("Could not update the completed XP reset approval.")

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if not await self._check_developer(interaction):
            return
        self.finished = True
        self._disable_buttons()
        await interaction.response.edit_message(
            content="أُلغي الطلب؛ لم يتم تصفير XP.",
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ManualXPAuditView(discord.ui.View):
    """Keep a developer-only, filtered snapshot of manual XP grants pageable."""

    def __init__(
        self,
        developer_id: int,
        records: list[dict[str, Any]],
        *,
        guild_id: int | None,
        grantor_id: int | None,
        recipient_id: int | None,
    ) -> None:
        super().__init__(timeout=300)
        self.developer_id = int(developer_id)
        self.records = tuple(records)
        self.guild_id = guild_id
        self.grantor_id = grantor_id
        self.recipient_id = recipient_id
        self.page_index = 0
        self.page_count = max(
            1,
            (len(self.records) + MAX_MANUAL_XP_GRANT_HISTORY_RESULTS - 1)
            // MAX_MANUAL_XP_GRANT_HISTORY_RESULTS,
        )
        self._update_navigation()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(getattr(interaction, "user", None), "id", None) != self.developer_id:
            await interaction.response.send_message(
                "تصفح سجل منح XP متاح لمطور البوت فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    def render_page(self) -> str:
        if not self.records:
            return "لا توجد منح XP يدوية محفوظة لهذا الاختيار."

        start = self.page_index * MAX_MANUAL_XP_GRANT_HISTORY_RESULTS
        page_records = self.records[
            start : start + MAX_MANUAL_XP_GRANT_HISTORY_RESULTS
        ]
        filters = []
        if self.guild_id is not None:
            filters.append(f"السيرفر `{self.guild_id}`")
        if self.grantor_id is not None:
            filters.append(f"المانح <@{self.grantor_id}>")
        if self.recipient_id is not None:
            filters.append(f"المتلقي <@{self.recipient_id}>")
        filter_line = f"\nالفلاتر: {' · '.join(filters)}" if filters else ""
        lines = [
            (
                f"السيرفر `{entry['guild_id']}` · "
                f"<@{entry['actor_id']}> منح <@{entry['user_id']}> "
                f"`{entry['amount']:,}` XP · "
                f"المستوى `{entry['old_level']}` → `{entry['new_level']}` · "
                f"`{entry['timestamp']}`"
            )
            for entry in page_records
        ]
        header = (
            f"سجل منح XP اليدوية · صفحة {self.page_index + 1}/{self.page_count} "
            f"· {len(self.records)} نتيجة"
        )
        return "\n".join((header + filter_line, *lines))

    def _update_navigation(self) -> None:
        for child in self.children:
            if child.custom_id == "xpaudit:previous":
                child.disabled = self.page_index <= 0
            elif child.custom_id == "xpaudit:next":
                child.disabled = self.page_index >= self.page_count - 1

    async def _move_page(
        self,
        interaction: discord.Interaction,
        offset: int,
    ) -> None:
        next_page = self.page_index + offset
        if not 0 <= next_page < self.page_count:
            await interaction.response.send_message(
                "لا توجد صفحة أخرى في هذا الاتجاه.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.page_index = next_page
        self._update_navigation()
        await interaction.response.edit_message(
            content=self.render_page(),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="السابق",
        style=discord.ButtonStyle.secondary,
        custom_id="xpaudit:previous",
    )
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._move_page(interaction, -1)

    @discord.ui.button(
        label="التالي",
        style=discord.ButtonStyle.secondary,
        custom_id="xpaudit:next",
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._move_page(interaction, 1)


def setup_commands(
    bot: commands.Bot,
    system: LevelSystem,
    developer_id: int | None = None,
    *,
    co_owner_store: CoOwnerStore | None = None,
    admin_engine=None,
) -> commands.Bot:
    """Register level display, administration, and announcement commands once."""
    if getattr(bot, "_levels_commands_registered", False):
        return bot

    resolved_developer_id = int(developer_id) if developer_id is not None else 0

    def can_manage_xp(user, guild) -> bool:
        return is_owner_or_co_owner(
            user,
            guild,
            resolved_developer_id,
            co_owner_store,
        )

    def can_manage_settings(user, guild) -> bool:
        return can_manage_server_settings(
            user,
            guild,
            resolved_developer_id,
            co_owner_store,
        )

    def can_manage_level_settings(ctx) -> bool:
        return can_manage_settings(ctx.author, ctx.guild)

    def admin_xp_embed(guild, member, amount, old_level, new_level, profile):
        current_xp = int(profile["xp"])
        embed = discord.Embed(
            title="تمت إضافة XP",
            description=(
                f"العضو: {member.mention}\n"
                f"السيرفر: **{discord.utils.escape_markdown(guild.name)}**"
            ),
            color=0x9B59B6,
        )
        embed.add_field(name="XP المضاف", value=f"`{amount:,}`", inline=True)
        embed.add_field(name="المستوى الحالي", value=f"`{new_level:,}`", inline=True)
        embed.add_field(name="إجمالي XP", value=f"`{current_xp:,}`", inline=True)
        embed.add_field(
            name="المتبقي للمستوى التالي",
            value=f"`{max(0, xp_for_level(new_level + 1) - current_xp):,}`",
            inline=True,
        )
        if new_level > old_level:
            embed.add_field(
                name="ترقية",
                value=f"ارتقى العضو من المستوى `{old_level}` إلى `{new_level}`.",
                inline=False,
            )
        embed.set_thumbnail(url=member.display_avatar.url)
        return embed

    async def send_manual_xp_dm(member, embed) -> None:
        try:
            await member.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.info("Could not DM the member after a manual XP grant.")

    @bot.command(name="addxp")
    @commands.guild_only()
    async def add_xp_prefix(
        ctx: commands.Context,
        member: discord.Member,
        amount: int,
    ) -> None:
        if not can_manage_xp(ctx.author, ctx.guild):
            await ctx.reply(
                "إضافة XP متاحة لمطور البوت أو صاحب السيرفر أو الأونر المساعد.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            old_level, new_level, profile = system.add_manual_xp(
                ctx.guild.id,
                ctx.author.id,
                member.id,
                amount,
            )
        except ValueError as error:
            await ctx.reply(
                str(error),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        embed = admin_xp_embed(
            ctx.guild,
            member,
            amount,
            old_level,
            new_level,
            profile,
        )
        await ctx.reply(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await send_manual_xp_dm(member, embed)

    @bot.tree.command(name="addxp", description="إضافة XP لعضو في هذا السيرفر")
    @app_commands.guild_only()
    @app_commands.describe(
        member="العضو الذي سيحصل على XP",
        amount="كمية XP من 1 إلى 1,000,000",
    )
    async def add_xp_slash(
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر يعمل داخل السيرفر فقط.",
                ephemeral=True,
            )
            return
        if not can_manage_xp(interaction.user, guild):
            await interaction.response.send_message(
                "إضافة XP متاحة لمطور البوت أو صاحب السيرفر أو الأونر المساعد.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            old_level, new_level, profile = system.add_manual_xp(
                guild.id,
                interaction.user.id,
                member.id,
                amount,
            )
        except ValueError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        embed = admin_xp_embed(
            guild,
            member,
            amount,
            old_level,
            new_level,
            profile,
        )
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await send_manual_xp_dm(member, embed)

    @bot.tree.command(
        name="xpaudit",
        description="البحث في سجل منح XP اليدوية لمطور البوت فقط",
    )
    @app_commands.describe(
        guild_id="معرّف السيرفر الرقمي (اختياري)",
        grantor="اختر حساب المانح من هذا السياق (اختياري)",
        grantor_id="معرّف من منح XP الرقمي (اختياري)",
        recipient="اختر حساب المتلقي من هذا السياق (اختياري)",
        recipient_id="معرّف العضو الذي تلقى XP الرقمي (اختياري)",
    )
    async def manual_xp_audit(
        interaction: discord.Interaction,
        guild_id: str | None = None,
        grantor: discord.User | None = None,
        grantor_id: str | None = None,
        recipient: discord.User | None = None,
        recipient_id: str | None = None,
    ) -> None:
        if interaction.user.id != resolved_developer_id:
            await interaction.response.send_message(
                "سجل منح XP متاح لمطور البوت فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        def parse_id(value: str | None) -> int | None:
            if value is None:
                return None
            if (
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 20
            ):
                raise ValueError
            identifier = int(value)
            if not 0 < identifier <= (1 << 64) - 1:
                raise ValueError
            return identifier

        def resolve_user_filter(
            selected_user: discord.User | None,
            numeric_id: str | None,
        ) -> int | None:
            if selected_user is not None and numeric_id is not None:
                raise ValueError
            if selected_user is None:
                return parse_id(numeric_id)
            identifier = getattr(selected_user, "id", None)
            if type(identifier) is not int or not 0 < identifier <= (1 << 64) - 1:
                raise ValueError
            return identifier

        try:
            selected_guild_id = parse_id(guild_id)
            selected_grantor_id = resolve_user_filter(grantor, grantor_id)
            selected_recipient_id = resolve_user_filter(recipient, recipient_id)
            records = system.recent_manual_xp_grants(
                selected_guild_id,
                limit=None,
                grantor_id=selected_grantor_id,
                recipient_id=selected_recipient_id,
            )
        except ValueError:
            await interaction.response.send_message(
                "أدخل معرّفات رقمية موجبة وصالحة، واختر حسابًا أو أدخل معرّفه الرقمي لكل فلتر وليس كليهما.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = None
        if not records:
            message = "لا توجد منح XP يدوية محفوظة لهذا الاختيار."
        else:
            view = ManualXPAuditView(
                resolved_developer_id,
                records,
                guild_id=selected_guild_id,
                grantor_id=selected_grantor_id,
                recipient_id=selected_recipient_id,
            )
            message = view.render_page()
        await interaction.response.send_message(
            message,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="xpexport",
        description="تنزيل سجل منح XP اليدوية لمطور البوت فقط",
    )
    @app_commands.describe(
        guild_id="معرّف السيرفر الرقمي (اختياري)",
        grantor_id="معرّف من منح XP الرقمي (اختياري)",
        recipient_id="معرّف العضو الذي تلقى XP الرقمي (اختياري)",
    )
    async def manual_xp_export(
        interaction: discord.Interaction,
        guild_id: str | None = None,
        grantor_id: str | None = None,
        recipient_id: str | None = None,
    ) -> None:
        if interaction.user.id != resolved_developer_id:
            await interaction.response.send_message(
                "تنزيل سجل منح XP متاح لمطور البوت فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        def parse_id(value: str | None) -> int | None:
            if value is None:
                return None
            if (
                not isinstance(value, str)
                or not value.isascii()
                or not value.isdecimal()
                or len(value) > 20
            ):
                raise ValueError
            identifier = int(value)
            if not 0 < identifier <= (1 << 64) - 1:
                raise ValueError
            return identifier

        try:
            selected_guild_id = parse_id(guild_id)
            selected_grantor_id = parse_id(grantor_id)
            selected_recipient_id = parse_id(recipient_id)
            records = system.recent_manual_xp_grants(
                selected_guild_id,
                limit=None,
                grantor_id=selected_grantor_id,
                recipient_id=selected_recipient_id,
            )
        except ValueError:
            await interaction.response.send_message(
                "أدخل معرّفات رقمية موجبة وصالحة للسيرفر والمانح والمتلقي.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        selected_filters = []
        if selected_guild_id is not None:
            selected_filters.append(f"server {selected_guild_id}")
        if selected_grantor_id is not None:
            selected_filters.append(f"grantor {selected_grantor_id}")
        if selected_recipient_id is not None:
            selected_filters.append(f"recipient {selected_recipient_id}")
        selected_label = f" for {', '.join(selected_filters)}" if selected_filters else ""
        max_file_bytes = getattr(interaction, "filesize_limit", None)
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            max_file_bytes = DEFAULT_XP_EXPORT_FILE_SIZE_LIMIT
        try:
            export_parts = _split_manual_xp_export(records, max_file_bytes)
        except (KeyError, TypeError, ValueError):
            logger.exception("Could not prepare the manual XP grant export.")
            await interaction.response.send_message(
                "تعذر تجهيز تنزيل سجل منح XP؛ لم يُرسل أي ملف. حاول مرة أخرى لاحقًا.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        part_count = len(export_parts)
        sent_parts = 0
        for part_index, part_bytes in enumerate(export_parts, start=1):
            filename = (
                "manual-xp-grants.csv"
                if part_count == 1
                else f"manual-xp-grants-part-{part_index:03}.csv"
            )
            if part_count == 1:
                message = (
                    f"CSV export of {len(records)} retained manual XP grants"
                    f"{selected_label}."
                )
            else:
                message = (
                    f"XP export part {part_index}/{part_count} "
                    f"({len(records)} retained grants total){selected_label}."
                )

            try:
                export_file = discord.File(
                    io.BytesIO(part_bytes),
                    filename=filename,
                )
                if part_index == 1:
                    await interaction.response.send_message(
                        message,
                        file=export_file,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await interaction.followup.send(
                        message,
                        file=export_file,
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.HTTPException, OSError):
                logger.exception(
                    "Could not send manual XP grant export part %s/%s.",
                    part_index,
                    part_count,
                )
                failure_notice = (
                    f"تعذر على Discord قبول تنزيل سجل XP. "
                    f"أُرسل {sent_parts} من {part_count} أجزاء؛ "
                    "لم تُرسل الأجزاء المتبقية. حاول تنزيل السجل مرة أخرى."
                )
                try:
                    response_is_done = getattr(interaction.response, "is_done", None)
                    if callable(response_is_done) and response_is_done():
                        await interaction.followup.send(
                            failure_notice,
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    else:
                        await interaction.response.send_message(
                            failure_notice,
                            ephemeral=True,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                except (discord.HTTPException, OSError):
                    logger.exception(
                        "Could not send the private manual XP export failure notice."
                    )
                return
            sent_parts += 1

    @bot.command(name="resetxp")
    @commands.guild_only()
    async def reset_xp_prefix(
        ctx: commands.Context,
        member: discord.Member,
    ) -> None:
        if not can_manage_xp(ctx.author, ctx.guild):
            await ctx.reply(
                "تصفير XP متاح لمطور البوت أو صاحب السيرفر أو الأونر المساعد.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if developer_id is None or admin_engine is None:
            await ctx.reply(
                "التصفير متوقف لأن تأكيد المطور أو النسخ الاحتياطي غير متاح.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            developer = bot.get_user(resolved_developer_id)
            if developer is None:
                developer = await bot.fetch_user(resolved_developer_id)
        except discord.HTTPException:
            logger.exception("Could not fetch the developer for XP reset approval.")
            await ctx.reply(
                "تعذر الوصول إلى المطور لطلب التأكيد؛ لم يتم تصفير XP.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        safe_guild_name = discord.utils.escape_markdown(ctx.guild.name)
        safe_member_name = discord.utils.escape_markdown(member.display_name)

        async def perform_reset() -> str:
            if not can_manage_xp(ctx.author, ctx.guild):
                return "رُفض الطلب لأن صلاحية مقدّمه لم تعد سارية؛ لم يُصفّر XP."
            if ctx.guild.get_member(member.id) is None:
                return "غادر العضو السيرفر؛ لم يتم تصفير XP."
            try:
                removed = admin_engine.reset_level_profile(
                    ctx.guild.id,
                    member.id,
                )
            except Exception:
                logger.exception("XP profile reset or its full-state backup failed.")
                return "فشلت النسخة الاحتياطية أو عملية الحفظ؛ لم يتم تصفير XP."
            if removed:
                result = (
                    f"تم تصفير سجل المستوى لـ {member.mention} في "
                    f"**{safe_guild_name}** بعد نسخة احتياطية كاملة. "
                    "يشمل السجل XP والمستوى وعدّادات الرسائل والصوت."
                )
            else:
                result = "لا يوجد سجل مستوى محفوظ لهذا العضو؛ لم يتغير شيء."
            try:
                await ctx.author.send(
                    result,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.info("Could not DM the requester after the XP reset.")
            return result

        view = LevelResetApprovalView(resolved_developer_id, perform_reset)
        approval = discord.Embed(
            title="طلب تصفير سجل مستوى",
            description=(
                f"السيرفر: **{safe_guild_name}**\n"
                f"العضو: {member.mention} (`{safe_member_name}`)\n"
                f"مقدّم الطلب: {ctx.author.mention}\n\n"
                "التأكيد يحذف سجل العضو كاملاً: XP والمستوى وعدّادات الرسائل "
                "والصوت. تُحفظ نسخة كاملة قبل الحذف، ولن يحدث تغيير إذا فشل "
                "النسخ الاحتياطي."
            ),
            color=0xE74C3C,
        )
        try:
            view.message = await developer.send(
                embed=approval,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not deliver the XP reset approval request.")
            await ctx.reply(
                "تعذر إرسال طلب التأكيد إلى المطور؛ لم يتم تصفير XP.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.reply(
            "أُرسل طلب التصفير إلى مطور البوت للموافقة. لم يُحذف أي سجل بعد.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="مستواي", aliases=["level", "rank", "لفلي"])
    @commands.guild_only()
    async def my_level(ctx: commands.Context, member: discord.Member | None = None):
        member = member or ctx.author
        data = system.profile(ctx.guild.id, member.id)
        rank, total = system.rank(ctx.guild.id, member.id)
        current = int(data["level"])
        previous = xp_for_level(current) if current else 0
        needed = max(1, xp_for_level(current + 1) - previous)
        progress = max(0, int(data["xp"]) - previous)
        percentage = min(100.0, progress * 100 / needed)
        filled = min(20, int(20 * percentage / 100))
        embed = discord.Embed(title=f"📊 مستوى {_safe_name(member)}", color=0x9B59B6)
        embed.add_field(name="🎯 المستوى", value=f"`{current}`", inline=True)
        embed.add_field(name="🏆 الترتيب", value=f"`#{rank or '-'} / {total}`", inline=True)
        embed.add_field(name="✨ XP", value=f"`{int(data['xp']):,}`", inline=True)
        embed.add_field(name="🏅 الرتبة", value=title_for_level(current), inline=True)
        embed.add_field(
            name="📈 التقدم",
            value=f"`{'█' * filled}{'░' * (20 - filled)}`\n"
            f"`{progress:,}` / `{needed:,}` ({percentage:.1f}%)",
            inline=False,
        )
        embed.add_field(name="💬 الرسائل", value=f"`{int(data['total_messages']):,}`")
        voice_minutes = int(data["voice_minutes"])
        embed.add_field(
            name="🎤 وقت الصوت",
            value=(
                f"**{format_voice_time(voice_minutes)}**\n"
                f"`{format_voice_time_short(voice_minutes)}`"
            ),
        )
        embed.add_field(name="🆔 المعرّف", value=f"`{member.id}`")
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bot.command(
        name="voicetime",
        aliases=["voice_time", "وقت_الصوت", "إحصائيات_الصوت"],
    )
    @commands.guild_only()
    async def voice_time_stats(
        ctx: commands.Context,
        member: discord.Member | None = None,
    ):
        member = member or ctx.author
        stats = system.voice_stats(ctx.guild.id, member.id)
        embed = discord.Embed(
            title=f"🎤 وقت الصوت — {_safe_name(member)}",
            color=0x3498DB,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(
            name="اليوم",
            value=f"**{format_voice_time(stats['today'])}**",
            inline=True,
        )
        embed.add_field(
            name="آخر 7 أيام",
            value=f"**{format_voice_time(stats['week'])}**",
            inline=True,
        )
        embed.add_field(
            name="هذا الشهر",
            value=f"**{format_voice_time(stats['month'])}**",
            inline=True,
        )
        embed.add_field(
            name="الإجمالي",
            value=(
                f"**{format_voice_time(stats['total'])}**\n"
                f"`{format_voice_time_short(stats['total'])}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="XP من الصوت",
            value=f"`{stats['voice_xp']:,}` (منها `{stats['bonus_xp']:,}` مكافأة إنجاز)",
            inline=True,
        )
        chart_rows = []
        chart_max = max(
            (entry["minutes"] for entry in stats["daily_chart"]),
            default=0,
        )
        for entry in stats["daily_chart"]:
            minutes = int(entry["minutes"])
            bar_length = (
                min(12, max(1, round(12 * minutes / chart_max)))
                if minutes and chart_max
                else 0
            )
            day_label = date.fromisoformat(entry["date"]).strftime("%m/%d")
            chart_rows.append(
                f"`{day_label}` {'█' * bar_length}{'░' * (12 - bar_length)} "
                f"`{minutes}د`"
            )
        chart_description = (
            "\n".join(chart_rows)
            if any(entry["minutes"] for entry in stats["daily_chart"])
            else "لا يوجد وقت صوت مسجل خلال آخر 7 أيام."
        )
        embed.add_field(
            name="النشاط اليومي — آخر 7 أيام",
            value=chart_description,
            inline=False,
        )
        embed.set_footer(text="تُحسب الفترات حسب أيام UTC.")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def send_metric_leaderboard(
        ctx: commands.Context,
        *,
        metric: str,
        title: str,
        color: int,
    ) -> None:
        entries = system.leaderboard(ctx.guild.id, limit=10, metric=metric)
        embed = discord.Embed(
            title=title,
            description=f"في سيرفر **{discord.utils.escape_markdown(ctx.guild.name)}**",
            color=color,
        )
        if not entries:
            embed.description += "\nلا توجد بيانات بعد."
        else:
            labels = await asyncio.gather(
                *(
                    _leaderboard_member_label(ctx.guild, int(entry["user_id"]))
                    for entry in entries
                )
            )
            lines = []
            for index, (entry, member_label) in enumerate(zip(entries, labels), 1):
                if metric == "messages":
                    detail = f"`{int(entry['total_messages']):,}` رسالة محتسبة"
                elif metric == "voice":
                    minutes = int(entry["voice_minutes"])
                    detail = (
                        f"**{format_voice_time(minutes)}** "
                        f"(`{format_voice_time_short(minutes)}`)"
                    )
                else:
                    detail = f"مستوى `{entry['level']}` • `{int(entry['xp']):,}` XP"
                lines.append(f"**{index}.** {member_label} — {detail}")
            embed.description += "\n" + "\n".join(lines)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bot.command(name="توب_الرسائل", aliases=["top_messages", "ترتيب_الرسائل"])
    @commands.guild_only()
    async def message_leaderboard(ctx: commands.Context):
        await send_metric_leaderboard(
            ctx,
            metric="messages",
            title="💬 أكثر الأعضاء نشاطًا بالرسائل",
            color=0x2ECC71,
        )

    @bot.command(name="توب_الصوت", aliases=["top_voice", "ترتيب_الصوت"])
    @commands.guild_only()
    async def voice_leaderboard(ctx: commands.Context):
        await send_metric_leaderboard(
            ctx,
            metric="voice",
            title="🎤 أطول وقت صوت",
            color=0x3498DB,
        )

    @bot.command(name="توب_XP", aliases=["top_xp", "ترتيب_xp"])
    @commands.guild_only()
    async def xp_leaderboard(ctx: commands.Context):
        await send_metric_leaderboard(
            ctx,
            metric="xp",
            title="✨ أعلى XP",
            color=0xF1C40F,
        )

    @bot.command(name="توب", aliases=["top_levels", "ترتيب"])
    @commands.guild_only()
    async def level_leaderboard(ctx: commands.Context):
        stats = system.guild_stats(ctx.guild.id)
        embed = discord.Embed(
            title="🏆 ملخص مستويات السيرفر",
            description=f"في سيرفر **{discord.utils.escape_markdown(ctx.guild.name)}**",
            color=0xFFD700,
        )
        embed.add_field(name="أعضاء لديهم XP", value=f"`{stats['members']:,}`")
        embed.add_field(name="إجمالي XP", value=f"`{stats['total_xp']:,}`")
        embed.add_field(name="الرسائل المحتسبة", value=f"`{stats['total_messages']:,}`")
        embed.add_field(
            name="وقت الصوت المسجل",
            value=f"**{format_voice_time(stats['voice_minutes'])}**",
            inline=False,
        )
        top_specs = (
            ("xp", "✨ الأعلى في XP", "xp"),
            ("messages", "💬 الأكثر رسائل", "total_messages"),
            ("voice", "🎤 الأعلى في وقت الصوت", "voice_minutes"),
        )
        member_labels: dict[int, str] = {}
        for metric, title, field in top_specs:
            top = system.leaderboard(ctx.guild.id, limit=1, metric=metric)
            if not top:
                value = "لا يوجد نشاط مسجل."
            else:
                entry = top[0]
                user_id = int(entry["user_id"])
                if user_id not in member_labels:
                    member_labels[user_id] = await _leaderboard_member_label(
                        ctx.guild,
                        user_id,
                    )
                label = member_labels[user_id]
                if metric == "xp":
                    embed.description += f"\n\nالأعلى في XP: {label}"
                if metric == "voice":
                    value = (
                        f"{label} — **{format_voice_time(int(entry[field]))}**"
                    )
                elif metric == "messages":
                    value = f"{label} — `{int(entry[field]):,}` رسالة محتسبة"
                else:
                    value = f"{label} — مستوى `{entry['level']}` • `{int(entry[field]):,}` XP"
            embed.add_field(name=title, value=value, inline=False)
        embed.set_footer(
            text="التفاصيل: !توب_XP • !توب_الرسائل • !توب_الصوت • !voicetime"
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @bot.command(name="xp_help", aliases=["مستويات_help"])
    @commands.guild_only()
    async def levels_help(ctx: commands.Context):
        await ctx.send(
            "🎮 اكسب `15-25` XP من رسالة كل 60 ثانية، و`10` XP لكل دقيقة صوت.\n"
            "تُمنح مكافآت XP إضافية عند بلوغ إنجازات وقت الصوت. "
            "استخدم `!مستواي` لعرض تقدمك، و`!voicetime` لعرض إحصاءات الصوت. "
            "يعرض `!توب` ملخص السيرفر؛ وتعرض `!توب_XP` و`!توب_الرسائل` "
            "و`!توب_الصوت` قوائم أفضل 10 أعضاء حسب كل فئة. "
            "يمكن للإداريين ضبط مكافآت الرتب باستخدام "
            "`!level_role_set <level> @role` وعرضها باستخدام `!level_roles`، "
            "وطلب مراجعة المكافآت المستحقة باستخدام `!level_role_reconcile`، "
            "وضبط إشعارات الارتقاء باستخدام `/levelsetup`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="level_role_set")
    @commands.guild_only()
    @commands.check(can_manage_level_settings)
    async def set_level_role(
        ctx: commands.Context, level: int, role: discord.Role
    ) -> None:
        if level <= 0:
            await ctx.reply("يجب أن يكون المستوى رقمًا موجبًا.")
            return
        reason = role_reward_block_reason(
            role,
            _guild_bot_member(ctx.guild, bot),
        )
        if reason is not None:
            safe_role_name = discord.utils.escape_markdown(
                getattr(role, "name", f"ID {getattr(role, 'id', 'unknown')}")
            )
            previously_configured = level in system.role_rewards(ctx.guild.id)
            previous_status = (
                " بقي الإعداد السابق دون تغيير."
                if previously_configured
                else ""
            )
            await ctx.reply(
                f"لم تُحفظ مكافأة المستوى `{level}` على رتبة "
                f"**{safe_role_name}**: "
                f"{_role_reward_block_reason_message(reason)}."
                f"{previous_status}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        system.set_role_reward(ctx.guild.id, level, role.id)
        safe_role_name = discord.utils.escape_markdown(role.name)
        await ctx.reply(
            f"تم حفظ مكافأة المستوى `{level}` على رتبة **{safe_role_name}**؛ "
            "يستطيع البوت منحها حاليًا.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="level_role_remove")
    @commands.guild_only()
    @commands.check(can_manage_level_settings)
    async def remove_level_role(ctx: commands.Context, level: int) -> None:
        if level <= 0:
            await ctx.reply("يجب أن يكون المستوى رقمًا موجبًا.")
            return
        if system.remove_role_reward(ctx.guild.id, level):
            await ctx.reply(f"تم حذف مكافأة المستوى `{level}`.")
        else:
            await ctx.reply(f"لا توجد مكافأة مضبوطة للمستوى `{level}`.")

    @bot.command(name="level_roles")
    @commands.guild_only()
    async def list_level_roles(ctx: commands.Context) -> None:
        rewards = system.role_rewards(ctx.guild.id)
        if not rewards:
            await ctx.reply("لم تُضبط مكافآت رتب للمستويات في هذا السيرفر.")
            return
        lines = []
        bot_member = _guild_bot_member(ctx.guild, bot)
        for level, role_id in sorted(rewards.items()):
            role = ctx.guild.get_role(role_id)
            reason = role_reward_block_reason(role, bot_member)
            role_label = (
                f"**{discord.utils.escape_markdown(role.name)}**"
                if role is not None
                else f"رتبة مفقودة (ID `{role_id}`)"
            )
            status = (
                f" — ⚠️ غير قابلة للمنح: "
                f"{_role_reward_block_reason_message(reason)}"
                if reason is not None
                else ""
            )
            lines.append(f"المستوى `{level}` ← {role_label}{status}")
        await ctx.reply(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(
        name="level_role_reconcile",
        aliases=["level_roles_reconcile", "level_role_sync"],
    )
    @commands.guild_only()
    @commands.check(can_manage_level_settings)
    async def reconcile_level_roles_command(ctx: commands.Context) -> None:
        """طلب مراجعة مكافآت الرتب المستحقة، مع تأكيد مطور البوت."""
        candidates = system.earned_role_rewards(ctx.guild.id)
        if not candidates:
            await ctx.reply("لا توجد مكافآت رتب مستحقة للمراجعة في هذا السيرفر.")
            return
        if developer_id is None:
            await ctx.reply(
                "تعذر بدء المراجعة؛ تأكيد مطور البوت غير مضبوط.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        eligible_members = len({user_id for user_id, _, _ in candidates})
        view = _RoleRewardReconcileView(
            system,
            ctx.guild,
            developer_id,
            bot,
        )
        view.message = await ctx.send(
            f"طلب **{discord.utils.escape_markdown(ctx.author.display_name)}** "
            f"مراجعة مكافآت الرتب المستحقة لـ `{eligible_members}` عضوًا "
            f"في هذا السيرفر فقط. سيُعاد فحص المستويات والصلاحيات عند التأكيد؛ "
            "التأكيد متاح لمطور البوت فقط.",
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="levelsetup",
        description="إعداد إشعارات المستويات لهذا السيرفر",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        action="اختر إعداد إشعارات الارتقاء",
        channel="روم نصي للإشعارات",
        message="نص اختياري؛ يدعم {user} و{level} و{title} و{guild}",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="تحديد روم مخصص", value="set_channel"),
            app_commands.Choice(name="نفس روم كسب المستوى", value="same_channel"),
            app_commands.Choice(name="إرسال في الخاص", value="dm"),
            app_commands.Choice(name="إيقاف الإشعارات", value="off"),
            app_commands.Choice(name="عرض الإعدادات", value="show"),
            app_commands.Choice(name="إعادة الافتراضي", value="reset"),
            app_commands.Choice(name="تحديد رسالة مخصصة", value="set_message"),
            app_commands.Choice(name="معاينة رسالة مخصصة", value="preview_message"),
            app_commands.Choice(name="مسح الرسالة المخصصة", value="clear_message"),
            app_commands.Choice(name="منشن العضو: تشغيل", value="mention_on"),
            app_commands.Choice(name="منشن العضو: إيقاف", value="mention_off"),
        ]
    )
    async def level_setup(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
        message: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر متاح داخل السيرفر فقط.",
                ephemeral=True,
            )
            return
        if not can_manage_settings(interaction.user, guild):
            await interaction.response.send_message(
                "إعداد إشعارات المستويات متاح لإداريي السيرفر أو مالكه أو "
                "مطور البوت أو الأونرز المساعدين.",
                ephemeral=True,
            )
            return

        if action.value == "show":
            settings = system.announcement_settings(guild.id)
            mode_labels = {
                "same": "نفس روم كسب المستوى",
                "channel": "روم مخصص",
                "dm": "الخاص",
                "off": "متوقف",
            }
            embed = discord.Embed(
                title="إعدادات إشعارات المستويات",
                description=f"في سيرفر **{discord.utils.escape_markdown(guild.name)}**",
                color=0x4A90E2,
            )
            embed.add_field(
                name="وجهة الإشعار",
                value=mode_labels[settings["mode"]],
                inline=True,
            )
            if settings["mode"] == "channel":
                configured = guild.get_channel(settings["channel_id"])
                embed.add_field(
                    name="الروم",
                    value=configured.mention if configured else "الروم المحفوظ غير متاح",
                    inline=True,
                )
                if configured is not None:
                    missing_permissions = (
                        _level_notice_channel_missing_permissions(
                            configured,
                            _guild_bot_member(guild, bot),
                        )
                    )
                    if missing_permissions:
                        if (
                            "تعذر التحقق من صلاحيات البوت في الروم المحدد"
                            in missing_permissions
                        ):
                            warning = (
                                "تعذر التحقق من صلاحيات البوت الحالية في هذا "
                                "الروم."
                            )
                        else:
                            permission_list = "\n".join(
                                f"• {permission}"
                                for permission in missing_permissions
                            )
                            warning = (
                                "لا يستطيع البوت نشر إشعارات الارتقاء دون "
                                "الصلاحيات الناقصة التالية:\n"
                                f"{permission_list}"
                            )
                        embed.add_field(
                            name="تنبيه صلاحيات النشر",
                            value=warning,
                            inline=False,
                        )
            embed.add_field(
                name="منشن العضو",
                value="مفعّل" if settings["mention_user"] else "متوقف",
                inline=True,
            )
            custom_message = settings["custom_message"]
            embed.add_field(
                name="الرسالة المخصصة",
                value=custom_message or "الرسالة الافتراضية",
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if action.value == "reset":
            view = _LevelSettingsResetView(
                system,
                guild.id,
                interaction.user.id,
                interaction=interaction,
            )
            await interaction.response.send_message(
                "سيُعاد ضبط إعدادات إشعارات المستويات لهذا السيرفر فقط. "
                "هل تريد المتابعة؟",
                view=view,
                ephemeral=True,
            )
            return

        updates: dict[str, Any]
        if action.value == "set_channel":
            if channel is None:
                await interaction.response.send_message(
                    "حدد رومًا نصيًا باستخدام خيار `channel`.",
                    ephemeral=True,
                )
                return
            if channel.guild.id != guild.id:
                await interaction.response.send_message(
                    "اختر رومًا من هذا السيرفر.",
                    ephemeral=True,
                )
                return
            missing_permissions = _level_notice_channel_missing_permissions(
                channel,
                _guild_bot_member(guild, bot),
            )
            if missing_permissions:
                details = "\n".join(
                    f"• {permission}" for permission in missing_permissions
                )
                await interaction.response.send_message(
                    "لم يتم حفظ الروم. يجب أن يتمكن البوت من عرض الروم وإرسال "
                    "الرسائل والروابط المضمّنة فيه. الصلاحيات الناقصة أو "
                    "التي تعذر التحقق منها:\n"
                    f"{details}",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            updates = {"mode": "channel", "channel_id": channel.id}
        elif action.value == "same_channel":
            updates = {"mode": "same", "channel_id": None}
        elif action.value == "dm":
            updates = {"mode": "dm", "channel_id": None}
        elif action.value == "off":
            updates = {"mode": "off", "channel_id": None}
        elif action.value in {"set_message", "preview_message"}:
            if message is None or not message.strip():
                await interaction.response.send_message(
                    "اكتب الرسالة في خيار `message`.",
                    ephemeral=True,
                )
                return
            if len(message) > LEVEL_UP_MESSAGE_LIMIT:
                await interaction.response.send_message(
                    f"يجب ألا تتجاوز الرسالة {LEVEL_UP_MESSAGE_LIMIT} حرفًا.",
                    ephemeral=True,
                )
                return
            normalized_message = message.strip()
            if action.value == "preview_message":
                settings = system.announcement_settings(guild.id)
                sample_user = "عضو تجريبي"
                mention_user = settings["mention_user"]
                user_label = (
                    f"@{sample_user}" if mention_user else sample_user
                )
                preview_level = 10
                description = _render_level_notice_custom_message(
                    normalized_message,
                    user_label=user_label,
                    level=preview_level,
                    guild_name=discord.utils.escape_markdown(guild.name)[:100],
                )
                embed = discord.Embed(
                    title="🎉 مستوى جديد!",
                    description=description,
                    color=0xFFD700,
                )
                embed.set_footer(
                    text=(
                        "معاينة خاصة وفق إعداد المنشن الحالي؛ "
                        "لم تُحفظ ولم تُرسل للأعضاء."
                    )
                )
                await interaction.response.send_message(
                    content=user_label if mention_user else None,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                    ephemeral=True,
                )
                return
            updates = {"custom_message": normalized_message}
        elif action.value == "clear_message":
            updates = {"custom_message": None}
        elif action.value == "mention_on":
            updates = {"mention_user": True}
        elif action.value == "mention_off":
            updates = {"mention_user": False}
        else:
            await interaction.response.send_message(
                "الإعداد المطلوب غير معروف.",
                ephemeral=True,
            )
            return

        try:
            system.set_announcement_settings(guild.id, updates)
        except Exception:
            logger.exception(
                "Could not save level announcement settings for guild %s.",
                guild.id,
            )
            await interaction.response.send_message(
                "تعذر حفظ الإعدادات؛ لم تتغير.",
                ephemeral=True,
            )
            return

        messages = {
            "same_channel": "ستظهر الإشعارات في روم كسب المستوى.",
            "dm": "ستُرسل الإشعارات للعضو في الخاص، مع الرجوع للروم عند تعذر الإرسال.",
            "off": "أُوقفت إشعارات الارتقاء؛ مكافآت الرتب تستمر كالمعتاد.",
            "set_message": "حُفظت رسالة الارتقاء المخصصة.",
            "clear_message": "عادت رسالة الارتقاء إلى النص الافتراضي.",
            "mention_on": "فُعّل منشن العضو في إشعار الارتقاء.",
            "mention_off": "أُوقف منشن العضو في إشعار الارتقاء.",
        }
        await interaction.response.send_message(
            (
                f"ستظهر إشعارات الارتقاء في {channel.mention}."
                if action.value == "set_channel"
                else messages[action.value]
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @bot.tree.command(
        name="leveldelivery",
        description="عرض حالات تعذر تسليم إشعارات المستويات",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        route="اختياري: فلترة حسب وجهة الإرسال",
        from_date="اختياري: بداية الفترة شاملة، بتوقيت UTC وبصيغة YYYY-MM-DD",
        to_date="اختياري: نهاية الفترة شاملة، بتوقيت UTC وبصيغة YYYY-MM-DD",
    )
    @app_commands.choices(
        route=[
            app_commands.Choice(name="الخاص", value="dm"),
            app_commands.Choice(name="الروم", value="channel"),
        ]
    )
    async def level_delivery_report(
        interaction: discord.Interaction,
        route: app_commands.Choice[str] | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر متاح داخل السيرفر فقط.",
                ephemeral=True,
            )
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "تقرير تسليم إشعارات المستويات متاح لإداريي السيرفر فقط.",
                ephemeral=True,
            )
            return

        route_value = getattr(route, "value", route)
        if route_value is not None and (
            not isinstance(route_value, str)
            or route_value not in LEVEL_NOTICE_FAILURE_ROUTES
        ):
            await interaction.response.send_message(
                "وجهة الإرسال المحددة غير صالحة. اختر الخاص أو الروم.",
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
            return
        try:
            start_day, end_day = _parse_level_delivery_dates(from_date, to_date)
        except ValueError as error:
            await interaction.response.send_message(
                str(error),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
            return

        retained_failures = system.recent_notice_delivery_failures(
            guild.id,
            limit=MAX_LEVEL_NOTICE_FAILURES,
        )
        has_filters = (
            route_value is not None
            or start_day is not None
            or end_day is not None
        )
        failures = list(retained_failures)
        if route_value is not None:
            failures = [
                failure
                for failure in failures
                if route_value in failure["routes"]
            ]
        if start_day is not None or end_day is not None:
            dated_failures = []
            for failure in failures:
                failed_day = datetime.fromtimestamp(
                    float(failure["failed_at"]),
                    timezone.utc,
                ).date()
                if start_day is not None and failed_day < start_day:
                    continue
                if end_day is not None and failed_day > end_day:
                    continue
                dated_failures.append(failure)
            failures = dated_failures

        if has_filters and not failures:
            await interaction.response.send_message(
                "لا توجد حالات تعذر تسليم تطابق هذه المرشحات في سجلات هذا السيرفر.",
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
            return

        if not failures:
            description = "لم تُسجل حالات تعذر تسليم خلال آخر 30 يومًا."
        else:
            if has_filters:
                description = (
                    f"تطابق {len(failures)} حالة من أصل "
                    f"{len(retained_failures)} حالة محفوظة."
                )
                active_filters = []
                if route_value is not None:
                    route_filter_label = {
                        "dm": "الخاص",
                        "channel": "الروم",
                    }[route_value]
                    active_filters.append(f"الوجهة: {route_filter_label}")
                if start_day is not None:
                    active_filters.append(f"من: {start_day.isoformat()}")
                if end_day is not None:
                    active_filters.append(f"إلى: {end_day.isoformat()}")
                description += (
                    "\nالمرشحات: "
                    + "؛ ".join(active_filters)
                    + " (التواريخ بتوقيت UTC)."
                )
            else:
                description = (
                    f"سُجلت {len(failures)} حالة خلال آخر 30 يومًا "
                    "(الحد الأقصى 50 حالة محفوظة)."
                )
            route_labels = {
                ("dm",): "الخاص",
                ("channel",): "الروم",
                ("dm", "channel"): "الخاص والروم",
                ("channel", "dm"): "الخاص والروم",
            }
            route_name_labels = {
                "dm": "الخاص",
                "channel": "الروم",
            }
            rows = []
            for failure in failures[:10]:
                routes = tuple(failure["routes"])
                route_label = route_labels.get(routes, "وجهات الإشعار")
                causes = failure.get("causes", {})
                cause_details = []
                if isinstance(causes, Mapping):
                    for route in routes:
                        cause_label = LEVEL_NOTICE_FAILURE_CAUSE_LABELS.get(
                            causes.get(route)
                        )
                        if cause_label is not None:
                            cause_details.append(
                                f"{route_name_labels[route]}: {cause_label}"
                            )
                cause_summary = (
                    f" — السبب: {'؛ '.join(cause_details)}"
                    if cause_details
                    else ""
                )
                rows.append(
                    f"• <@{failure['user_id']}> — المستوى `{failure['level']}`"
                    f" — {route_label}{cause_summary} — "
                    f"<t:{int(failure['failed_at'])}:R>"
                )
            description += "\n\n" + "\n".join(rows)
            if len(failures) > len(rows):
                description += f"\n\nيُعرض أحدث {len(rows)} إخفاقات فقط."

        embed = discord.Embed(
            title="تقرير تسليم إشعارات المستويات",
            description=description,
            color=0xE67E22,
        )
        embed.set_footer(text="لا يتضمن التقرير نص الإشعار أو تفاصيل الرسائل الخاصة.")
        await interaction.response.send_message(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @bot.tree.command(
        name="leveldeliveryexport",
        description="تنزيل حالات تعذر تسليم إشعارات المستويات لهذا السيرفر",
    )
    @app_commands.guild_only()
    async def export_level_delivery_report(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر متاح داخل السيرفر فقط.",
                ephemeral=True,
            )
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "تنزيل تقرير تسليم إشعارات المستويات متاح لإداريي السيرفر فقط.",
                ephemeral=True,
            )
            return

        failures = system.recent_notice_delivery_failures(
            guild.id,
            limit=MAX_LEVEL_NOTICE_FAILURES,
        )
        if not failures:
            await interaction.response.send_message(
                "لا توجد حالات محفوظة لتنزيلها لهذا السيرفر.",
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
            return

        payload = _level_notice_failures_export_payload(guild.id, failures)
        export_file = discord.File(
            io.BytesIO(
                json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            ),
            filename="level-notice-failures.json",
        )
        await interaction.response.send_message(
            "أُرفقت نسخة من سجل هذا السيرفر فقط؛ لم يُمسح أي سجل.",
            file=export_file,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    @bot.tree.command(
        name="leveldeliveryclear",
        description="مسح حالات تعذر تسليم إشعارات المستويات لهذا السيرفر",
    )
    @app_commands.guild_only()
    async def clear_level_delivery_report(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر متاح داخل السيرفر فقط.",
                ephemeral=True,
            )
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        if not getattr(permissions, "administrator", False):
            await interaction.response.send_message(
                "مسح تقرير تسليم إشعارات المستويات متاح لإداريي السيرفر فقط.",
                ephemeral=True,
            )
            return

        removed = system.clear_notice_delivery_failures(guild.id)
        if removed:
            message = (
                "تم مسح تقرير إخفاقات إشعارات المستويات لهذا السيرفر فقط؛ "
                f"أُزيلت {removed} حالة."
            )
        else:
            message = "لا توجد حالات محفوظة؛ لم يُحذف شيء من تقرير هذا السيرفر."
        await interaction.response.send_message(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    if developer_id is not None or co_owner_store is not None:
        @bot.command(name="level_stats")
        @commands.guild_only()
        async def level_stats(ctx: commands.Context) -> None:
            if not can_manage_xp(ctx.author, ctx.guild):
                await ctx.reply(
                    "إحصائيات المستويات متاحة لمطور البوت أو صاحب السيرفر "
                    "أو الأونر المساعد.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            stats = system.guild_stats(ctx.guild.id)
            embed = discord.Embed(
                title="إحصائيات المستويات",
                description=f"في سيرفر **{discord.utils.escape_markdown(ctx.guild.name)}**",
                color=0x9B59B6,
            )
            embed.add_field(name="أعضاء لديهم XP", value=f"`{stats['members']:,}`")
            embed.add_field(name="إجمالي XP", value=f"`{stats['total_xp']:,}`")
            embed.add_field(
                name="أعلى مستوى",
                value=f"`{stats['highest_level']:,}`",
            )
            embed.add_field(
                name="الرسائل المحتسبة",
                value=f"`{stats['total_messages']:,}`",
            )
            embed.add_field(
                name="دقائق الصوت",
                value=f"`{stats['voice_minutes']:,}`",
            )
            await ctx.reply(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    from level_medals import setup_medal_commands

    setup_medal_commands(
        bot,
        system,
        resolved_developer_id,
        admin_engine=admin_engine,
    )
    bot._levels_commands_registered = True
    return bot