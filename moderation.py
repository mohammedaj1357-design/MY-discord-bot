"""Per-guild moderation settings, keyword shortcuts, and join automation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import discord

from co_owners import CoOwnerStore, is_owner_or_co_owner


logger = logging.getLogger(__name__)

KEYWORD_ACTIONS = {
    "مالديف": "ban",
    "انقلع": "ban",
    "لابووو": "ban",
    "بنعالي": "ban",
    "ارقص": "ban",
    "تشمس": "ban",
    "بالشبشب": "ban",
    "طير": "ban",
    "برا": "ban",
    "احظر": "ban",
    "اطرد": "kick",
    "اخرج": "kick",
    "طلع": "kick",
    "اسكت": "mute",
    "اخرس": "mute",
    "صمت": "mute",
    "سكوت": "mute",
    "امسك": "mute",
    "سكت": "mute",
    "هدي": "mute",
    "كتم": "mute",
    "تكلم": "unmute",
    "اتكلم": "unmute",
    "افتح": "unmute",
    "حل": "unmute",
    "افك": "unmute",
    "فك": "unmute",
    "فتح": "unmute",
    "حرر": "unmute",
    "حذر": "warn",
    "حذار": "warn",
    "انذر": "warn",
    "عاقب": "warn",
    "ملف": "info",
    "معلومات": "info",
    "مين": "info",
    "منو": "info",
    "بيانات": "info",
    "حساب": "info",
    "امسح": "purge",
    "نظف": "purge",
    "شيل": "purge",
    "احذف": "purge",
    "مسح": "purge",
    "بطيء": "slowmode",
    "هدوء": "slowmode",
    "بطء": "slowmode",
}

DEFAULT_KEYWORD_SETTINGS = {
    "keyword_roles": {},
    "allowed_users": [],
    "whitelist_channels": [],
    "blacklist_channels": [],
}
DEFAULT_AUTOROLE_SETTINGS = {
    "enabled": False,
    "roles": [],
    "bots_roles": [],
    "welcome_channel": None,
    "welcome_message": None,
}
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60
MAX_TEMP_BAN_SECONDS = 365 * 24 * 60 * 60
MAX_PURGE_COUNT = 100
MAX_SLOWMODE_SECONDS = 6 * 60 * 60
MAX_WEEKLY_REPORT_ATTEMPTS = 3
WEEKLY_REPORT_RETRY_DELAY = timedelta(days=1)
MODERATION_STATS_ACTIONS = frozenset(
    {"ban", "kick", "mute", "unmute", "warn", "info", "purge", "slowmode"}
)
MODERATION_NOTICE_ACTIONS = frozenset(
    {"ban", "kick", "mute", "unmute", "warn", "purge", "slowmode"}
)
MODERATION_STATS_RETENTION_DAYS = 30

SHORTCUT_HELP = {
    "ban": {
        "name": "الحظر",
        "emoji": "⛔",
        "color": 0xED4245,
        "usage": "`مالديف @عضو [1h] [السبب]`\n`/ban @عضو [المدة] [السبب]`",
        "examples": [
            "`مالديف @عضو` — حظر دائم",
            "`مالديف @عضو 1h تكرار المخالفة` — حظر لمدة ساعة",
            "`/ban @عضو 7d السبب` — حظر لمدة أسبوع",
        ],
    },
    "kick": {
        "name": "الطرد",
        "emoji": "👢",
        "color": 0xE67E22,
        "usage": "`اطرد @عضو [السبب]`\n`/kick @عضو [السبب]`",
        "examples": [
            "`اطرد @عضو`",
            "`اطرد @عضو إزعاج متكرر`",
            "`/kick @عضو السبب`",
        ],
    },
    "mute": {
        "name": "الإسكات",
        "emoji": "🔇",
        "color": 0x9B59B6,
        "usage": "`اسكت @عضو 10m [السبب]`\n`/timeout @عضو 10m [السبب]`",
        "examples": [
            "`اسكت @عضو 10m`",
            "`اسكت @عضو 1h إزعاج`",
            "`/timeout @عضو 30m السبب`",
        ],
    },
    "unmute": {
        "name": "فك الإسكات",
        "emoji": "🔊",
        "color": 0x2ECC71,
        "usage": "`تكلم @عضو [السبب]`\n`/unmute @عضو [السبب]`",
        "examples": [
            "`تكلم @عضو`",
            "`فك @عضو`",
            "`/unmute @عضو انتهاء المهلة`",
        ],
    },
    "warn": {
        "name": "التحذير",
        "emoji": "⚠️",
        "color": 0xF1C40F,
        "usage": "`حذر @عضو [السبب]`\n`/warn @عضو [السبب]`",
        "examples": [
            "`حذر @عضو`",
            "`انذر @عضو مخالفة القواعد`",
            "`/warn @عضو السبب`",
        ],
    },
    "info": {
        "name": "معلومات العضو",
        "emoji": "🪪",
        "color": 0x3498DB,
        "usage": "`ملف [@عضو]`\n`/memberinfo [@عضو]`",
        "examples": [
            "`ملف @عضو`",
            "`معلومات @عضو`",
            "`/memberinfo @عضو`",
        ],
    },
    "purge": {
        "name": "حذف الرسائل",
        "emoji": "🧹",
        "color": 0x3498DB,
        "usage": "`امسح العدد [@عضو]`\n`/purge العدد [@عضو]`",
        "examples": [
            "`امسح 10`",
            "`نظف 20 @عضو`",
            "`/purge 15 @عضو`",
        ],
    },
    "slowmode": {
        "name": "الوضع البطيء",
        "emoji": "🐢",
        "color": 0x5865F2,
        "usage": "`بطيء 5s` للتفعيل، أو `بطيء 0` للإيقاف\n`/slowmode الثواني`",
        "examples": [
            "`بطيء 5s`",
            "`هدوء 1m`",
            "`بطيء 0` — إيقاف الوضع البطيء",
        ],
    },
}


def shortcut_help_embed(
    action: str | None = None,
    *,
    keyword: str | None = None,
    error: str | None = None,
) -> discord.Embed:
    """Create a polished full shortcut list or focused usage card."""
    if action is None:
        embed = discord.Embed(
            title="دليل اختصارات الإشراف",
            description=(
                "اكتب الاختصار مباشرة في قناة مسموحة. أضف منشن العضو عند الحاجة، "
                "وتختلف الصلاحيات حسب إعدادات السيرفر."
            ),
            color=0x5865F2,
        )
        for action_name, details in SHORTCUT_HELP.items():
            aliases = " · ".join(
                f"`{word}`"
                for word, mapped_action in KEYWORD_ACTIONS.items()
                if mapped_action == action_name
            )
            embed.add_field(
                name=f"{details['emoji']} {details['name']}",
                value=(
                    f"{aliases}\n"
                    f"**الاستخدام:** {details['usage']}\n"
                    f"**مثال:** {details['examples'][0]}"
                ),
                inline=False,
            )
        embed.set_footer(text="استخدم /keyword لمعرفة أو ضبط صلاحيات الاختصارات.")
        return embed

    details = SHORTCUT_HELP.get(action)
    if details is None:
        raise ValueError(f"Unknown shortcut action: {action}")
    title = f"{details['emoji']} اختصارات {details['name']}"
    embed = discord.Embed(
        title=title,
        description=(
            f"الكلمة `{keyword}` تنفّذ إجراء **{details['name']}**."
            if keyword
            else f"اختصارات تنفيذ إجراء **{details['name']}**."
        ),
        color=details["color"],
    )
    aliases = " · ".join(
        f"`{word}`"
        for word, mapped_action in KEYWORD_ACTIONS.items()
        if mapped_action == action
    )
    embed.add_field(name="الاختصارات", value=aliases, inline=False)
    embed.add_field(name="طريقة الاستخدام", value=details["usage"], inline=False)
    embed.add_field(
        name="أمثلة",
        value="\n".join(details["examples"]),
        inline=False,
    )
    if error:
        embed.add_field(name="راجع هذا الإدخال", value=error[:1024], inline=False)
    embed.set_footer(
        text="تُنفّذ الأوامر فقط إذا كانت القناة وصلاحياتك تسمحان بذلك."
    )
    return embed


_DURATION_PATTERN = re.compile(
    r"\s*(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)",
    re.IGNORECASE,
)
_DURATION_UNITS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 60 * 60,
    "hr": 60 * 60,
    "hrs": 60 * 60,
    "hour": 60 * 60,
    "hours": 60 * 60,
    "d": 24 * 60 * 60,
    "day": 24 * 60 * 60,
    "days": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "weeks": 7 * 24 * 60 * 60,
}


class ModerationError(ValueError):
    """A user-correctable moderation request or permission error."""


def parse_duration(value: str | None) -> timedelta | None:
    """Parse compact durations such as ``1h30m`` or ``2 days``."""
    if not value:
        return None

    source = value.strip().lower()
    if not source:
        return None

    seconds = 0
    position = 0
    matches = 0
    while position < len(source):
        match = _DURATION_PATTERN.match(source, position)
        if match is None:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        seconds += amount * _DURATION_UNITS[unit]
        if seconds > MAX_TEMP_BAN_SECONDS:
            raise ModerationError("الحد الأقصى للمدة هو 365 يوماً.")
        position = match.end()
        matches += 1

    if matches == 0 or seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def format_duration(duration: timedelta) -> str:
    seconds = max(0, int(duration.total_seconds()))
    parts = []
    for unit_seconds, singular, plural in (
        (7 * 24 * 60 * 60, "أسبوع", "أسابيع"),
        (24 * 60 * 60, "يوم", "أيام"),
        (60 * 60, "ساعة", "ساعات"),
        (60, "دقيقة", "دقائق"),
        (1, "ثانية", "ثوانٍ"),
    ):
        count, seconds = divmod(seconds, unit_seconds)
        if count:
            parts.append(f"{count} {singular if count == 1 else plural}")
    return " و".join(parts) if parts else "أقل من ثانية"


def _positive_id(value: int, label: str = "المعرّف") -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} يجب أن يكون رقماً موجباً.")
    return value


def _validate_id_list(value, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or any(type(item) is not int or item <= 0 for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"قائمة {label} غير صالحة.")
    return list(value)


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class _GuildSettingsStore:
    """Shared atomic persistence for independent per-guild JSON settings."""

    def __init__(self, path: str | Path, defaults: dict, validator):
        self.path = Path(path)
        self._defaults = deepcopy(defaults)
        self._validator = validator
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as source:
                payload = json.load(source)
            self._guilds = self._validate_payload(payload)

    def _validate_payload(self, payload) -> dict[str, dict]:
        if not isinstance(payload, dict) or set(payload) != {"guilds"}:
            raise ValueError(f"Invalid settings file: {self.path}")
        guilds = payload["guilds"]
        if not isinstance(guilds, dict):
            raise ValueError(f"Invalid guild settings in {self.path}")
        validated = {}
        for guild_id, settings in guilds.items():
            if not isinstance(guild_id, str) or not guild_id.isdecimal():
                raise ValueError(f"Invalid guild ID in {self.path}")
            validated[guild_id] = self._validator(settings)
        return validated

    def get(self, guild_id: int) -> dict:
        key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            return deepcopy(self._guilds.get(key, self._defaults))

    def update(self, guild_id: int, change):
        key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            updated = deepcopy(self._guilds)
            settings = updated.setdefault(key, deepcopy(self._defaults))
            result = change(settings)
            settings = self._validator(settings)
            updated[key] = settings
            if updated != self._guilds:
                _atomic_json_write(self.path, {"guilds": updated})
                self._guilds = updated
            return result, deepcopy(settings)

    def reset(self, guild_id: int) -> bool:
        key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            if key not in self._guilds:
                return False
            updated = deepcopy(self._guilds)
            del updated[key]
            _atomic_json_write(self.path, {"guilds": updated})
            self._guilds = updated
            return True


def _validate_keyword_settings(settings) -> dict:
    if not isinstance(settings, dict) or set(settings) != set(DEFAULT_KEYWORD_SETTINGS):
        raise ValueError("Invalid keyword permission settings.")
    roles = settings["keyword_roles"]
    if not isinstance(roles, dict):
        raise ValueError("Invalid keyword role map.")
    checked_roles = {}
    for keyword, role_ids in roles.items():
        if keyword not in KEYWORD_ACTIONS:
            raise ValueError(f"Unknown moderation keyword: {keyword!r}.")
        checked_roles[keyword] = _validate_id_list(role_ids, "رتب الكلمة")
    return {
        "keyword_roles": checked_roles,
        "allowed_users": _validate_id_list(settings["allowed_users"], "الأعضاء المسموحين"),
        "whitelist_channels": _validate_id_list(
            settings["whitelist_channels"], "القنوات المسموحة"
        ),
        "blacklist_channels": _validate_id_list(
            settings["blacklist_channels"], "القنوات الممنوعة"
        ),
    }


class KeywordPermissionStore(_GuildSettingsStore):
    """Persistent role, user, and channel permissions for message shortcuts."""

    def __init__(self, path: str | Path):
        super().__init__(path, DEFAULT_KEYWORD_SETTINGS, _validate_keyword_settings)

    def _add_to_list(self, guild_id: int, path: str, value: int) -> bool:
        value = _positive_id(value)

        def change(settings):
            values = settings[path]
            if value in values:
                return False
            values.append(value)
            return True

        result, _ = self.update(guild_id, change)
        return result

    def _remove_from_list(self, guild_id: int, path: str, value: int) -> bool:
        value = _positive_id(value)

        def change(settings):
            values = settings[path]
            if value not in values:
                return False
            values.remove(value)
            return True

        result, _ = self.update(guild_id, change)
        return result

    def add_keyword_role(self, guild_id: int, keyword: str, role_id: int) -> bool:
        if keyword not in KEYWORD_ACTIONS:
            raise ValueError("الكلمة المفتاحية غير موجودة.")
        role_id = _positive_id(role_id, "معرّف الرتبة")

        def change(settings):
            roles = settings["keyword_roles"].setdefault(keyword, [])
            if role_id in roles:
                return False
            roles.append(role_id)
            return True

        result, _ = self.update(guild_id, change)
        return result

    def remove_keyword_role(self, guild_id: int, keyword: str, role_id: int) -> bool:
        role_id = _positive_id(role_id, "معرّف الرتبة")

        def change(settings):
            roles = settings["keyword_roles"].get(keyword)
            if not roles or role_id not in roles:
                return False
            roles.remove(role_id)
            if not roles:
                settings["keyword_roles"].pop(keyword, None)
            return True

        result, _ = self.update(guild_id, change)
        return result

    def clear_keyword_roles(self, guild_id: int, keyword: str) -> bool:
        def change(settings):
            return settings["keyword_roles"].pop(keyword, None) is not None

        result, _ = self.update(guild_id, change)
        return result

    def add_user(self, guild_id: int, user_id: int) -> bool:
        return self._add_to_list(guild_id, "allowed_users", user_id)

    def remove_user(self, guild_id: int, user_id: int) -> bool:
        return self._remove_from_list(guild_id, "allowed_users", user_id)

    def add_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        return self._add_to_list(guild_id, "whitelist_channels", channel_id)

    def remove_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        return self._remove_from_list(guild_id, "whitelist_channels", channel_id)

    def block_channel(self, guild_id: int, channel_id: int) -> bool:
        return self._add_to_list(guild_id, "blacklist_channels", channel_id)

    def unblock_channel(self, guild_id: int, channel_id: int) -> bool:
        return self._remove_from_list(guild_id, "blacklist_channels", channel_id)

    def can_use(self, member, keyword: str, developer_id: int) -> bool:
        guild = getattr(member, "guild", None)
        if guild is None or keyword not in KEYWORD_ACTIONS:
            return False
        if member.id == developer_id or member.id == guild.owner_id:
            return True
        permissions = getattr(member, "guild_permissions", None)
        if getattr(permissions, "administrator", False):
            return True
        settings = self.get(guild.id)
        if member.id in settings["allowed_users"]:
            return True
        member_role_ids = {role.id for role in getattr(member, "roles", ())}
        return any(
            role_id in member_role_ids
            for role_id in settings["keyword_roles"].get(keyword, ())
        )

    def channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        settings = self.get(guild_id)
        if channel_id in settings["blacklist_channels"]:
            return False
        allowed = settings["whitelist_channels"]
        return not allowed or channel_id in allowed


def _validate_autorole_settings(settings) -> dict:
    if not isinstance(settings, dict) or set(settings) != set(DEFAULT_AUTOROLE_SETTINGS):
        raise ValueError("Invalid autorole settings.")
    if type(settings["enabled"]) is not bool:
        raise ValueError("Invalid autorole enabled flag.")
    channel_id = settings["welcome_channel"]
    if channel_id is not None:
        channel_id = _positive_id(channel_id, "معرّف قناة الترحيب")
    welcome_message = settings["welcome_message"]
    if welcome_message is not None and (
        not isinstance(welcome_message, str) or len(welcome_message) > 1800
    ):
        raise ValueError("Invalid autorole welcome message.")
    return {
        "enabled": settings["enabled"],
        "roles": _validate_id_list(settings["roles"], "رتب الأعضاء"),
        "bots_roles": _validate_id_list(settings["bots_roles"], "رتب البوتات"),
        "welcome_channel": channel_id,
        "welcome_message": welcome_message,
    }


class AutoRoleStore(_GuildSettingsStore):
    """Persistent per-server join roles and welcome message settings."""

    def __init__(self, path: str | Path):
        super().__init__(path, DEFAULT_AUTOROLE_SETTINGS, _validate_autorole_settings)

    def _set_role(self, guild_id: int, role_id: int, key: str, add: bool) -> bool:
        role_id = _positive_id(role_id, "معرّف الرتبة")

        def change(settings):
            roles = settings[key]
            if add:
                if role_id in roles:
                    return False
                roles.append(role_id)
                return True
            if role_id not in roles:
                return False
            roles.remove(role_id)
            return True

        result, _ = self.update(guild_id, change)
        return result

    def add_role(self, guild_id: int, role_id: int) -> bool:
        return self._set_role(guild_id, role_id, "roles", True)

    def remove_role(self, guild_id: int, role_id: int) -> bool:
        return self._set_role(guild_id, role_id, "roles", False)

    def add_bot_role(self, guild_id: int, role_id: int) -> bool:
        return self._set_role(guild_id, role_id, "bots_roles", True)

    def remove_bot_role(self, guild_id: int, role_id: int) -> bool:
        return self._set_role(guild_id, role_id, "bots_roles", False)

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean.")
        self.update(
            guild_id,
            lambda settings: settings.__setitem__("enabled", enabled),
        )

    def set_welcome(self, guild_id: int, channel_id: int, message: str) -> None:
        channel_id = _positive_id(channel_id, "معرّف قناة الترحيب")
        if not isinstance(message, str) or not message.strip() or len(message) > 1800:
            raise ValueError("رسالة الترحيب يجب أن تكون بين حرف واحد و1800 حرف.")

        def change(settings):
            settings["welcome_channel"] = channel_id
            settings["welcome_message"] = message.strip()

        self.update(guild_id, change)

    def clear_welcome(self, guild_id: int) -> None:
        def change(settings):
            settings["welcome_channel"] = None
            settings["welcome_message"] = None

        self.update(guild_id, change)


def _validate_temporary_bans(payload) -> dict[str, dict[str, dict]]:
    if not isinstance(payload, dict) or set(payload) != {"guilds"}:
        raise ValueError("Invalid temporary-ban file.")
    guilds = payload["guilds"]
    if not isinstance(guilds, dict):
        raise ValueError("Invalid temporary-ban guild map.")
    validated = {}
    for guild_id, bans in guilds.items():
        if not isinstance(guild_id, str) or not guild_id.isdecimal():
            raise ValueError("Invalid guild ID in temporary-ban file.")
        if not isinstance(bans, dict):
            raise ValueError("Invalid temporary-ban list.")
        checked_bans = {}
        for user_id, record in bans.items():
            if not isinstance(user_id, str) or not user_id.isdecimal():
                raise ValueError("Invalid user ID in temporary-ban file.")
            if not isinstance(record, dict) or set(record) != {
                "expires_at",
                "reason",
                "moderator_id",
            }:
                raise ValueError("Invalid temporary-ban record.")
            expiry = datetime.fromisoformat(record["expires_at"])
            if expiry.tzinfo is None:
                raise ValueError("Temporary-ban expiry must include a timezone.")
            reason = record["reason"]
            if not isinstance(reason, str) or len(reason) > 500:
                raise ValueError("Invalid temporary-ban reason.")
            moderator_id = _positive_id(record["moderator_id"], "معرّف الإداري")
            checked_bans[user_id] = {
                "expires_at": expiry.astimezone(UTC).isoformat(),
                "reason": reason,
                "moderator_id": moderator_id,
            }
        validated[guild_id] = checked_bans
    return validated


class TemporaryBanStore:
    """Durable expiry schedule for timed bans, including across bot restarts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as source:
                self._guilds = _validate_temporary_bans(json.load(source))

    def _save(self, guilds: dict[str, dict[str, dict]]) -> None:
        _atomic_json_write(self.path, {"guilds": guilds})

    def schedule(
        self,
        guild_id: int,
        user_id: int,
        expires_at: datetime,
        reason: str,
        moderator_id: int,
    ) -> None:
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        user_key = str(_positive_id(user_id, "معرّف العضو"))
        moderator_id = _positive_id(moderator_id, "معرّف الإداري")
        if expires_at.tzinfo is None:
            raise ValueError("انتهاء الحظر يجب أن يتضمن منطقة زمنية.")
        if not isinstance(reason, str) or len(reason) > 500:
            raise ValueError("سبب الحظر غير صالح.")
        with self._lock:
            updated = deepcopy(self._guilds)
            updated.setdefault(guild_key, {})[user_key] = {
                "expires_at": expires_at.astimezone(UTC).isoformat(),
                "reason": reason,
                "moderator_id": moderator_id,
            }
            self._save(updated)
            self._guilds = updated

    def remove(self, guild_id: int, user_id: int) -> bool:
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        user_key = str(_positive_id(user_id, "معرّف العضو"))
        with self._lock:
            if user_key not in self._guilds.get(guild_key, {}):
                return False
            updated = deepcopy(self._guilds)
            del updated[guild_key][user_key]
            if not updated[guild_key]:
                del updated[guild_key]
            self._save(updated)
            self._guilds = updated
            return True

    def list_due(self, now: datetime | None = None) -> list[tuple[int, int, dict]]:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must include a timezone.")
        due = []
        with self._lock:
            for guild_id, bans in self._guilds.items():
                for user_id, record in bans.items():
                    expires_at = datetime.fromisoformat(record["expires_at"])
                    if expires_at <= now.astimezone(UTC):
                        due.append(
                            (int(guild_id), int(user_id), deepcopy(record))
                        )
        return due


def _empty_moderation_stats() -> dict:
    return {
        "total": 0,
        "next_weekly_report_at": None,
        "weekly_report_retry": None,
        "days": {},
    }


def _validate_stat_counts(value, label: str, *, allowed=None, numeric_keys=False) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label} counts.")
    checked = {}
    for key, count in value.items():
        if not isinstance(key, str):
            raise ValueError(f"Invalid {label} key.")
        if numeric_keys and (not key.isdecimal() or int(key) <= 0):
            raise ValueError(f"Invalid {label} ID.")
        if allowed is not None and key not in allowed:
            raise ValueError(f"Unknown {label} value: {key!r}.")
        if type(count) is not int or count < 0:
            raise ValueError(f"Invalid {label} count.")
        checked[key] = count
    return checked


def _validate_moderation_stats(payload) -> dict[str, dict]:
    if not isinstance(payload, dict) or set(payload) != {"guilds"}:
        raise ValueError("Invalid moderation-stats file.")
    guilds = payload["guilds"]
    if not isinstance(guilds, dict):
        raise ValueError("Invalid moderation-stats guild map.")
    validated = {}
    for guild_id, record in guilds.items():
        if not isinstance(guild_id, str) or not guild_id.isdecimal():
            raise ValueError("Invalid guild ID in moderation-stats file.")
        old_record_keys = {"total", "next_weekly_report_at", "days"}
        new_record_keys = old_record_keys | {"weekly_report_retry"}
        if not isinstance(record, dict) or frozenset(record) not in {
            frozenset(old_record_keys),
            frozenset(new_record_keys),
        }:
            raise ValueError("Invalid moderation-stats record.")
        total = record["total"]
        if type(total) is not int or total < 0:
            raise ValueError("Invalid total moderation count.")
        next_report = record["next_weekly_report_at"]
        if next_report is not None:
            if not isinstance(next_report, str):
                raise ValueError("Invalid weekly report schedule.")
            next_report_dt = datetime.fromisoformat(next_report)
            if next_report_dt.tzinfo is None:
                raise ValueError("Weekly report schedule must include a timezone.")
            next_report = next_report_dt.astimezone(UTC).isoformat()
        retry = record.get("weekly_report_retry")
        if retry is not None:
            if not isinstance(retry, dict) or set(retry) != {
                "scheduled_at",
                "attempt_count",
                "delivered_recipient_ids",
                "retry_at",
            }:
                raise ValueError("Invalid weekly report retry state.")
            checked_retry = {}
            for timestamp_key in ("scheduled_at", "retry_at"):
                timestamp = retry[timestamp_key]
                if not isinstance(timestamp, str):
                    raise ValueError("Invalid weekly report retry timestamp.")
                parsed_timestamp = datetime.fromisoformat(timestamp)
                if parsed_timestamp.tzinfo is None:
                    raise ValueError(
                        "Weekly report retry timestamps must include a timezone."
                    )
                checked_retry[timestamp_key] = (
                    parsed_timestamp.astimezone(UTC).isoformat()
                )
            attempts = retry["attempt_count"]
            if (
                type(attempts) is not int
                or not 0 <= attempts <= MAX_WEEKLY_REPORT_ATTEMPTS
            ):
                raise ValueError("Invalid weekly report retry attempt count.")
            delivered_ids = retry["delivered_recipient_ids"]
            if (
                not isinstance(delivered_ids, list)
                or any(
                    type(user_id) is not int or user_id <= 0
                    for user_id in delivered_ids
                )
                or len(delivered_ids) != len(set(delivered_ids))
            ):
                raise ValueError("Invalid delivered weekly report recipient IDs.")
            checked_retry.update(
                {
                    "attempt_count": attempts,
                    "delivered_recipient_ids": list(delivered_ids),
                }
            )
            retry = checked_retry
        days = record["days"]
        if not isinstance(days, dict):
            raise ValueError("Invalid moderation-stats day map.")
        checked_days = {}
        for day_text, day_record in days.items():
            if not isinstance(day_text, str) or date.fromisoformat(day_text).isoformat() != day_text:
                raise ValueError("Invalid moderation-stats date.")
            if not isinstance(day_record, dict) or set(day_record) != {
                "count",
                "actions",
                "keywords",
                "actors",
                "targets",
            }:
                raise ValueError("Invalid moderation-stats day.")
            count = day_record["count"]
            if type(count) is not int or count < 0:
                raise ValueError("Invalid daily moderation count.")
            checked_days[day_text] = {
                "count": count,
                "actions": _validate_stat_counts(
                    day_record["actions"],
                    "action",
                    allowed=MODERATION_STATS_ACTIONS,
                ),
                "keywords": _validate_stat_counts(
                    day_record["keywords"],
                    "keyword",
                    allowed=KEYWORD_ACTIONS,
                ),
                "actors": _validate_stat_counts(
                    day_record["actors"],
                    "actor",
                    numeric_keys=True,
                ),
                "targets": _validate_stat_counts(
                    day_record["targets"],
                    "target",
                    numeric_keys=True,
                ),
            }
        validated[guild_id] = {
            "total": total,
            "next_weekly_report_at": next_report,
            "weekly_report_retry": retry,
            "days": checked_days,
        }
    return validated


class ModerationStatsStore:
    """Persist privacy-minimal moderation counts and weekly report schedules."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as source:
                self._guilds = _validate_moderation_stats(json.load(source))

    def _save(self, guilds: dict[str, dict]) -> None:
        _atomic_json_write(self.path, {"guilds": guilds})

    def _guild_record(self, guilds: dict[str, dict], guild_id: int) -> dict:
        return guilds.setdefault(
            str(_positive_id(guild_id, "معرّف السيرفر")),
            _empty_moderation_stats(),
        )

    def record_action(
        self,
        guild_id: int,
        action: str,
        actor_id: int,
        target_id: int | None = None,
        keyword: str | None = None,
        *,
        at: datetime | None = None,
    ) -> None:
        if action not in MODERATION_STATS_ACTIONS:
            raise ValueError("Unknown moderation-stat action.")
        actor_id = _positive_id(actor_id, "معرّف الإداري")
        if target_id is not None:
            target_id = _positive_id(target_id, "معرّف العضو")
        if keyword is not None and (
            keyword not in KEYWORD_ACTIONS or KEYWORD_ACTIONS[keyword] != action
        ):
            raise ValueError("The keyword does not match the moderation action.")
        at = at or datetime.now(UTC)
        if at.tzinfo is None:
            raise ValueError("Moderation-stat timestamp must include a timezone.")
        day = at.astimezone(UTC).date().isoformat()

        with self._lock:
            updated = deepcopy(self._guilds)
            record = self._guild_record(updated, guild_id)
            record["total"] += 1
            bucket = record["days"].setdefault(
                day,
                {
                    "count": 0,
                    "actions": {},
                    "keywords": {},
                    "actors": {},
                    "targets": {},
                },
            )
            bucket["count"] += 1
            for category, value in (
                ("actions", action),
                ("keywords", keyword),
                ("actors", str(actor_id)),
                ("targets", str(target_id) if target_id is not None else None),
            ):
                if value is not None:
                    counts = bucket[category]
                    counts[value] = counts.get(value, 0) + 1

            cutoff = (at.astimezone(UTC).date() - timedelta(days=MODERATION_STATS_RETENTION_DAYS)).isoformat()
            record["days"] = {
                key: value for key, value in record["days"].items() if key >= cutoff
            }
            self._save(updated)
            self._guilds = updated

    def ensure_report_schedule(
        self,
        guild_id: int,
        *,
        now: datetime | None = None,
    ) -> datetime:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("Weekly report time must include a timezone.")
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            updated = deepcopy(self._guilds)
            record = self._guild_record(updated, int(guild_key))
            scheduled = record["next_weekly_report_at"]
            if scheduled is None:
                scheduled = (now.astimezone(UTC) + timedelta(days=7)).isoformat()
                record["next_weekly_report_at"] = scheduled
                self._save(updated)
                self._guilds = updated
            return datetime.fromisoformat(scheduled).astimezone(UTC)

    def claim_due_report(
        self,
        guild_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("Weekly report time must include a timezone.")
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            updated = deepcopy(self._guilds)
            record = self._guild_record(updated, int(guild_key))
            scheduled_text = record["next_weekly_report_at"]
            if scheduled_text is None:
                record["next_weekly_report_at"] = (
                    now.astimezone(UTC) + timedelta(days=7)
                ).isoformat()
                self._save(updated)
                self._guilds = updated
                return False
            scheduled = datetime.fromisoformat(scheduled_text).astimezone(UTC)
            if scheduled > now.astimezone(UTC):
                return False
            record["next_weekly_report_at"] = (
                now.astimezone(UTC) + timedelta(days=7)
            ).isoformat()
            self._save(updated)
            self._guilds = updated
            return True

    def claim_due_report_attempt(
        self,
        guild_id: int,
        *,
        now: datetime | None = None,
    ) -> dict | None:
        """Begin one persisted delivery attempt when a report or retry is due."""
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("Weekly report time must include a timezone.")
        now_utc = now.astimezone(UTC)
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            updated = deepcopy(self._guilds)
            record = self._guild_record(updated, int(guild_key))
            retry = record["weekly_report_retry"]
            if retry is None:
                scheduled_text = record["next_weekly_report_at"]
                if scheduled_text is None:
                    record["next_weekly_report_at"] = (
                        now_utc + timedelta(days=7)
                    ).isoformat()
                    self._save(updated)
                    self._guilds = updated
                    return None
                scheduled = datetime.fromisoformat(scheduled_text).astimezone(UTC)
                if scheduled > now_utc:
                    return None
                retry = {
                    "scheduled_at": scheduled.isoformat(),
                    "attempt_count": 0,
                    "delivered_recipient_ids": [],
                    "retry_at": scheduled.isoformat(),
                }
                record["weekly_report_retry"] = retry
            elif retry["attempt_count"] >= MAX_WEEKLY_REPORT_ATTEMPTS:
                record["next_weekly_report_at"] = (
                    now_utc + timedelta(days=7)
                ).isoformat()
                record["weekly_report_retry"] = None
                self._save(updated)
                self._guilds = updated
                return None

            retry_at = datetime.fromisoformat(retry["retry_at"]).astimezone(UTC)
            if retry_at > now_utc:
                return None
            retry["attempt_count"] += 1
            retry["retry_at"] = (
                now_utc + WEEKLY_REPORT_RETRY_DELAY
            ).isoformat()
            self._save(updated)
            self._guilds = updated
            return deepcopy(retry)

    def mark_weekly_report_recipient_delivered(
        self,
        guild_id: int,
        recipient_id: int,
    ) -> bool:
        recipient_id = _positive_id(recipient_id, "معرّف مستلم التقرير")
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        with self._lock:
            current = self._guilds.get(guild_key)
            retry = current.get("weekly_report_retry") if current else None
            if retry is None or recipient_id in retry["delivered_recipient_ids"]:
                return False
            updated = deepcopy(self._guilds)
            updated[guild_key]["weekly_report_retry"][
                "delivered_recipient_ids"
            ].append(recipient_id)
            self._save(updated)
            self._guilds = updated
            return True

    def finish_weekly_report_attempt(
        self,
        guild_id: int,
        *,
        now: datetime | None = None,
        delivered: bool,
    ) -> bool:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("Weekly report time must include a timezone.")
        guild_key = str(_positive_id(guild_id, "معرّف السيرفر"))
        now_utc = now.astimezone(UTC)
        with self._lock:
            current = self._guilds.get(guild_key)
            retry = current.get("weekly_report_retry") if current else None
            if retry is None:
                return False
            if not delivered and retry["attempt_count"] < MAX_WEEKLY_REPORT_ATTEMPTS:
                return False
            updated = deepcopy(self._guilds)
            record = updated[guild_key]
            record["next_weekly_report_at"] = (
                now_utc + timedelta(days=7)
            ).isoformat()
            record["weekly_report_retry"] = None
            self._save(updated)
            self._guilds = updated
            return True

    def weekly_summary(
        self,
        guild_id: int,
        *,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("Weekly summary time must include a timezone.")
        now_day = now.astimezone(UTC).date()
        first_day = now_day - timedelta(days=6)
        empty = {
            "total": 0,
            "week_total": 0,
            "actions": {},
            "keywords": {},
            "actors": {},
            "targets": {},
            "daily": {},
        }
        with self._lock:
            record = deepcopy(
                self._guilds.get(
                    str(_positive_id(guild_id, "معرّف السيرفر")),
                    _empty_moderation_stats(),
                )
            )
        summary = deepcopy(empty)
        summary["total"] = record["total"]
        for offset in range(7):
            day = (first_day + timedelta(days=offset)).isoformat()
            bucket = record["days"].get(day)
            summary["daily"][day] = bucket["count"] if bucket else 0
            if bucket is None:
                continue
            summary["week_total"] += bucket["count"]
            for category in ("actions", "keywords", "actors", "targets"):
                for key, count in bucket[category].items():
                    summary[category][key] = summary[category].get(key, 0) + count
        return summary


@dataclass(frozen=True)
class KeywordInvocation:
    keyword: str
    action: str
    target: object | None = None
    duration: timedelta | None = None
    reason: str = "بدون سبب"
    count: int | None = None
    slowmode_seconds: int | None = None
    error: str | None = None


def parse_keyword_invocation(message) -> KeywordInvocation | None:
    """Parse a supplied text shortcut without mutating Discord message objects."""
    content = message.content.strip()
    if not content or content.startswith("!"):
        return None
    parts = content.split(maxsplit=1)
    keyword = parts[0]
    action = KEYWORD_ACTIONS.get(keyword)
    if action is None:
        return None

    remainder = parts[1] if len(parts) > 1 else ""
    mentions = list(getattr(message, "mentions", ()))
    target = mentions[0] if mentions else None
    if target is not None:
        remainder = re.sub(rf"<@!?{target.id}>", " ", remainder, count=1)
    arguments = remainder.split()

    if action in {"ban", "kick", "mute", "unmute", "warn"} and target is None:
        return KeywordInvocation(
            keyword=keyword,
            action=action,
            error=f"استخدم الكلمة مع منشن العضو. مثال: `{keyword} @عضو`",
        )

    if action == "info" and target is None:
        target = message.author

    if action in {"ban", "mute"}:
        duration = parse_duration(arguments[0]) if arguments else None
        if action == "mute" and duration is None:
            return KeywordInvocation(
                keyword=keyword,
                action=action,
                target=target,
                error=f"حدد مدة للكتم، مثل: `{keyword} @عضو 10m سبب`.",
            )
        if duration is not None:
            arguments = arguments[1:]
        if action == "mute" and duration and duration.total_seconds() > MAX_TIMEOUT_SECONDS:
            return KeywordInvocation(
                keyword=keyword,
                action=action,
                target=target,
                error="الحد الأقصى للكتم هو 28 يوماً.",
            )
        reason = " ".join(arguments).strip() or "بدون سبب"
        return KeywordInvocation(keyword, action, target, duration, reason)

    if action in {"kick", "unmute", "warn"}:
        return KeywordInvocation(
            keyword=keyword,
            action=action,
            target=target,
            reason=" ".join(arguments).strip() or "بدون سبب",
        )

    if action == "info":
        return KeywordInvocation(keyword, action, target)

    if action == "purge":
        if not arguments or not arguments[0].isdecimal():
            return KeywordInvocation(
                keyword,
                action,
                target,
                error=f"حدد عدد الرسائل من 1 إلى {MAX_PURGE_COUNT}، مثل: `{keyword} 10`.",
            )
        count = int(arguments[0])
        if not 1 <= count <= MAX_PURGE_COUNT:
            return KeywordInvocation(
                keyword,
                action,
                target,
                error=f"عدد الرسائل يجب أن يكون من 1 إلى {MAX_PURGE_COUNT}.",
            )
        return KeywordInvocation(keyword, action, target, count=count)

    if action == "slowmode":
        if arguments and arguments[0].lower() in {"0", "0s", "0sec", "0secs", "0second", "0seconds"}:
            return KeywordInvocation(keyword, action, slowmode_seconds=0)
        duration = parse_duration(arguments[0]) if arguments else None
        if duration is None:
            return KeywordInvocation(
                keyword,
                action,
                error=f"حدد مدة مثل `{keyword} 5s` أو `{keyword} 1m`.",
            )
        seconds = int(duration.total_seconds())
        if seconds > MAX_SLOWMODE_SECONDS:
            return KeywordInvocation(
                keyword, action, error="الحد الأقصى للوضع البطيء هو 6 ساعات."
            )
        return KeywordInvocation(keyword, action, slowmode_seconds=seconds)

    return KeywordInvocation(keyword, action)


class ModerationService:
    """Execute moderation requests and coordinate join/temporary-ban events."""

    _ACTOR_PERMISSIONS = {
        "ban": "ban_members",
        "kick": "kick_members",
        "mute": "moderate_members",
        "unmute": "moderate_members",
        "warn": "moderate_members",
        "purge": "manage_messages",
        "slowmode": "manage_channels",
        "info": None,
    }
    _BOT_PERMISSIONS = {
        "ban": "ban_members",
        "kick": "kick_members",
        "mute": "moderate_members",
        "unmute": "moderate_members",
        "warn": None,
        "purge": "manage_messages",
        "slowmode": "manage_channels",
        "info": None,
    }

    def __init__(
        self,
        bot,
        keyword_store: KeywordPermissionStore,
        autorole_store: AutoRoleStore,
        temporary_ban_store: TemporaryBanStore,
        developer_id: int,
        guild_allowed=None,
        stats_store: ModerationStatsStore | None = None,
        co_owner_store: CoOwnerStore | None = None,
    ):
        self.bot = bot
        self.keyword_store = keyword_store
        self.autorole_store = autorole_store
        self.temporary_ban_store = temporary_ban_store
        self.developer_id = developer_id
        self.guild_allowed = guild_allowed or (lambda _guild_id: True)
        self.stats_store = stats_store
        self.co_owner_store = co_owner_store
        self._expiry_task: asyncio.Task | None = None
        self._weekly_stats_task: asyncio.Task | None = None

    def is_developer_or_owner(self, user, guild) -> bool:
        return bool(
            guild is not None
            and (user.id == self.developer_id or user.id == guild.owner_id)
        )

    def is_config_manager(self, user, guild) -> bool:
        """Allow assistant owners to manage settings without bypassing moderation checks."""
        return is_owner_or_co_owner(
            user,
            guild,
            self.developer_id,
            self.co_owner_store,
        )

    def actor_can_use(self, action: str, actor, guild) -> bool:
        if self.is_developer_or_owner(actor, guild):
            return True
        permissions = getattr(actor, "guild_permissions", None)
        if getattr(permissions, "administrator", False):
            return True
        needed = self._ACTOR_PERMISSIONS.get(action)
        return needed is None or bool(getattr(permissions, needed, False))

    def _bot_member(self, guild):
        if getattr(guild, "me", None) is not None:
            return guild.me
        if self.bot.user is not None:
            return guild.get_member(self.bot.user.id)
        return None

    def _check_bot_permission(self, guild, action: str) -> None:
        needed = self._BOT_PERMISSIONS[action]
        if needed is None:
            return
        bot_member = self._bot_member(guild)
        permissions = getattr(bot_member, "guild_permissions", None)
        if not getattr(permissions, needed, False):
            raise ModerationError(
                f"البوت يحتاج صلاحية Discord: `{needed}`."
            )

    def _check_target_hierarchy(self, guild, actor, target) -> None:
        if target.id == actor.id:
            raise ModerationError("لا يمكنك تطبيق هذا الإجراء على نفسك.")
        if self.bot.user is not None and target.id == self.bot.user.id:
            raise ModerationError("لا يمكن تطبيق هذا الإجراء على البوت.")
        if target.id == guild.owner_id:
            raise ModerationError("لا يمكن تطبيق هذا الإجراء على صاحب السيرفر.")

        bot_member = self._bot_member(guild)
        if bot_member is None or bot_member.top_role <= target.top_role:
            raise ModerationError(
                "رتبة البوت يجب أن تكون أعلى من رتبة العضو المستهدف."
            )
        if actor.id != guild.owner_id and actor.top_role <= target.top_role:
            raise ModerationError(
                "لا يمكنك تطبيق هذا الإجراء على عضو رتبته مساوية لرتبتك أو أعلى."
            )

    async def _guild_owner(self, guild):
        owner = getattr(guild, "owner", None)
        if owner is not None:
            return owner
        fetch_member = getattr(guild, "fetch_member", None)
        if fetch_member is None:
            return None
        try:
            return await fetch_member(guild.owner_id)
        except Exception:
            logger.warning(
                "Could not resolve owner for guild %s.",
                guild.id,
                exc_info=True,
            )
            return None

    async def _developer_user(self, *, known_users=()):
        for user in known_users:
            if user is not None and getattr(user, "id", None) == self.developer_id:
                return user

        get_user = getattr(self.bot, "get_user", None)
        try:
            developer = get_user(self.developer_id) if get_user is not None else None
        except Exception:
            logger.warning(
                "Could not resolve developer %s from the user cache.",
                self.developer_id,
                exc_info=True,
            )
            developer = None
        if developer is not None:
            return developer

        fetch_user = getattr(self.bot, "fetch_user", None)
        if fetch_user is None:
            return None
        try:
            return await fetch_user(self.developer_id)
        except Exception:
            logger.warning(
                "Could not resolve developer %s for moderation notices.",
                self.developer_id,
                exc_info=True,
            )
            return None

    def _action_embed(
        self,
        action: str,
        guild,
        actor,
        target,
        duration: timedelta | None,
        reason: str,
        *,
        member_notice: bool = False,
        details: str | None = None,
    ) -> discord.Embed:
        names = {
            "ban": ("حظر", 0xE74C3C),
            "kick": ("طرد", 0xE67E22),
            "mute": ("إسكات", 0x9B59B6),
            "unmute": ("فك الإسكات", 0x2ECC71),
            "warn": ("تحذير", 0xF1C40F),
            "purge": ("حذف رسائل", 0x3498DB),
            "slowmode": ("تغيير الوضع البطيء", 0x3498DB),
        }
        action_name, color = names[action]
        if member_notice:
            embed = discord.Embed(
                title=f"إشعار: {action_name}",
                description=f"تم اتخاذ إجراء إداري بحقك في **{guild.name}**.",
                color=color,
                timestamp=datetime.now(UTC),
            )
            embed.add_field(name="الإداري", value=str(actor), inline=True)
        else:
            embed = discord.Embed(
                title=f"إشعار إداري: {action_name}",
                description=f"تم تنفيذ الإجراء في **{guild.name}**.",
                color=color,
                timestamp=datetime.now(UTC),
            )
            if target is not None:
                embed.add_field(name="العضو", value=str(target), inline=True)
            embed.add_field(name="منفذ الإجراء", value=str(actor), inline=True)
        if duration is not None:
            embed.add_field(
                name="المدة",
                value=format_duration(duration),
                inline=True,
            )
        if details:
            embed.add_field(name="التفاصيل", value=details[:1024], inline=False)
        if reason and reason != "بدون سبب":
            embed.add_field(name="السبب", value=reason[:1024], inline=False)
        if getattr(guild, "icon", None):
            embed.set_footer(text=guild.name, icon_url=guild.icon.url)
        else:
            embed.set_footer(text=guild.name)
        return embed

    async def _send_notice(self, recipient, embed, *, recipient_type: str, guild_id: int) -> bool:
        try:
            await recipient.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.warning(
                "Moderation notice delivery failed for %s %s in guild %s.",
                recipient_type,
                getattr(recipient, "id", "unknown"),
                guild_id,
                exc_info=True,
            )
            return False
        return True

    async def _notify_action(
        self,
        action: str,
        guild,
        actor,
        target,
        duration: timedelta | None,
        reason: str,
        *,
        member_already_notified: bool = False,
        details: str | None = None,
    ) -> None:
        owner = await self._guild_owner(guild)
        owner_id = getattr(owner, "id", guild.owner_id)
        developer = await self._developer_user(known_users=(owner, actor, target))

        recipients = []
        if (
            target is not None
            and not member_already_notified
            and target.id not in {actor.id, owner_id, self.developer_id}
        ):
            recipients.append(
                (
                    target,
                    "member",
                    self._action_embed(
                        action,
                        guild,
                        actor,
                        target,
                        duration,
                        reason,
                        member_notice=True,
                        details=details,
                    ),
                ),
            )

        if owner is None:
            logger.warning(
                "Could not resolve guild owner for moderation notice in guild %s.",
                guild.id,
            )
        else:
            recipients.append(
                (
                    owner,
                    "owner",
                    self._action_embed(
                        action, guild, actor, target, duration, reason, details=details
                    ),
                ),
            )

        if actor.id != owner_id:
            recipients.append(
                (
                    actor,
                    "moderator",
                    self._action_embed(
                        action, guild, actor, target, duration, reason, details=details
                    ),
                ),
            )

        if developer is None:
            logger.warning(
                "Could not resolve developer %s for moderation notice in guild %s.",
                self.developer_id,
                guild.id,
            )
        else:
            recipients.append(
                (
                    developer,
                    "developer",
                    self._action_embed(
                        action, guild, actor, target, duration, reason, details=details
                    ),
                )
            )

        delivered_ids = set()
        for recipient, recipient_type, embed in recipients:
            recipient_id = getattr(recipient, "id", None)
            if recipient_id in delivered_ids:
                continue
            delivered_ids.add(recipient_id)
            await self._send_notice(
                recipient,
                embed,
                recipient_type=recipient_type,
                guild_id=guild.id,
            )

    async def _complete_action(
        self,
        action: str,
        guild,
        actor,
        target,
        duration: timedelta | None,
        reason: str,
        text: str,
        embed: discord.Embed | None = None,
        *,
        keyword: str | None = None,
        member_already_notified: bool = False,
        details: str | None = None,
    ) -> tuple[str, discord.Embed | None]:
        if self.stats_store is not None:
            try:
                self.stats_store.record_action(
                    guild.id,
                    action,
                    actor.id,
                    getattr(target, "id", None),
                    keyword,
                )
            except (OSError, ValueError):
                logger.exception(
                    "Could not record moderation statistics for action %s in guild %s.",
                    action,
                    guild.id,
                )
        if action in MODERATION_NOTICE_ACTIONS:
            try:
                await self._notify_action(
                    action,
                    guild,
                    actor,
                    target,
                    duration,
                    reason,
                    member_already_notified=member_already_notified,
                    details=details,
                )
            except Exception:
                logger.exception(
                    "Could not send moderation notices for action %s in guild %s.",
                    action,
                    guild.id,
                )
        return text, embed

    async def execute(
        self,
        action: str,
        guild,
        actor,
        *,
        target=None,
        duration: timedelta | None = None,
        reason: str = "بدون سبب",
        channel=None,
        count: int | None = None,
        before_message=None,
        keyword: str | None = None,
        slowmode_seconds: int | None = None,
    ) -> tuple[str, discord.Embed | None]:
        if keyword is not None:
            if not self.keyword_store.can_use(actor, keyword, self.developer_id):
                raise ModerationError(f"ليس لديك صلاحية استخدام `{keyword}`.")
        elif not self.actor_can_use(action, actor, guild):
            needed = self._ACTOR_PERMISSIONS.get(action)
            permission_label = needed or "صلاحية الإدارة"
            raise ModerationError(
                f"تحتاج صلاحية `{permission_label}` لاستخدام هذا الأمر."
            )

        self._check_bot_permission(guild, action)
        reason = (reason or "بدون سبب").strip()[:400]

        if action in {"ban", "kick", "mute", "unmute", "warn"}:
            if target is None:
                raise ModerationError("حدد عضواً في السيرفر.")
            self._check_target_hierarchy(guild, actor, target)

        if action == "ban":
            if duration is not None:
                seconds = int(duration.total_seconds())
                if not 1 <= seconds <= MAX_TEMP_BAN_SECONDS:
                    raise ModerationError("مدة الحظر المؤقت يجب أن تكون بين ثانية و365 يوماً.")
                expiry = datetime.now(UTC) + duration
                self.temporary_ban_store.schedule(
                    guild.id, target.id, expiry, reason, actor.id
                )
            try:
                await guild.ban(target, reason=f"{reason} — بواسطة {actor}")
            except Exception:
                if duration is not None:
                    self.temporary_ban_store.remove(guild.id, target.id)
                raise
            if duration is None:
                text = f"تم حظر {target} نهائياً. السبب: {reason}"
            else:
                text = (
                    f"تم حظر {target} لمدة {format_duration(duration)}. "
                    f"السبب: {reason}"
                )
            return await self._complete_action(
                action, guild, actor, target, duration, reason, text, keyword=keyword
            )

        if action == "kick":
            await target.kick(reason=f"{reason} — بواسطة {actor}")
            return await self._complete_action(
                action,
                guild,
                actor,
                target,
                None,
                reason,
                f"تم طرد {target}. السبب: {reason}",
                keyword=keyword,
            )

        if action == "mute":
            if duration is None:
                raise ModerationError("حدد مدة للمهلة، مثل `10m` أو `1h`.")
            if not 1 <= duration.total_seconds() <= MAX_TIMEOUT_SECONDS:
                raise ModerationError("مدة المهلة يجب أن تكون بين ثانية و28 يوماً.")
            await target.timeout(duration, reason=f"{reason} — بواسطة {actor}")
            return await self._complete_action(
                action,
                guild,
                actor,
                target,
                duration,
                reason,
                f"تم إسكات {target} لمدة {format_duration(duration)}. السبب: {reason}",
                keyword=keyword,
            )

        if action == "unmute":
            await target.timeout(None, reason=f"{reason} — بواسطة {actor}")
            return await self._complete_action(
                action,
                guild,
                actor,
                target,
                None,
                reason,
                f"تم فك الإسكات عن {target}.",
                keyword=keyword,
            )

        if action == "warn":
            embed = discord.Embed(
                title="⚠️ تحذير من إدارة السيرفر",
                description=f"تلقيت تحذيراً في **{guild.name}**.",
                color=0xF1C40F,
                timestamp=datetime.now(UTC),
            )
            embed.add_field(name="بواسطة", value=str(actor), inline=True)
            embed.add_field(name="السبب", value=reason[:1024], inline=False)
            try:
                await target.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return await self._complete_action(
                    action,
                    guild,
                    actor,
                    target,
                    None,
                    reason,
                    f"تم إرسال تحذير إلى {target}.",
                    keyword=keyword,
                    member_already_notified=True,
                )
            except (discord.Forbidden, discord.HTTPException):
                return (
                    f"تعذر إرسال التحذير إلى الخاص لـ {target}؛ لم يتغير شيء في السيرفر.",
                    None,
                )

        if action == "info":
            if target is None:
                raise ModerationError("حدد عضواً.")
            roles = [
                role.mention
                for role in sorted(
                    getattr(target, "roles", ()),
                    key=lambda role: role.position,
                    reverse=True,
                )
                if not role.is_default()
            ]
            embed = discord.Embed(
                title=f"معلومات {target}",
                color=getattr(target, "color", discord.Color.blurple()),
                timestamp=datetime.now(UTC),
            )
            embed.set_thumbnail(url=target.display_avatar.url)
            embed.add_field(name="المعرّف", value=f"`{target.id}`", inline=True)
            embed.add_field(
                name="انضم للسيرفر",
                value=(
                    discord.utils.format_dt(target.joined_at, style="R")
                    if target.joined_at
                    else "غير متاح"
                ),
                inline=True,
            )
            embed.add_field(
                name="أنشأ الحساب",
                value=discord.utils.format_dt(target.created_at, style="R"),
                inline=True,
            )
            embed.add_field(
                name="الرتب",
                value=" ".join(roles[:20]) if roles else "لا توجد رتب إضافية",
                inline=False,
            )
            if getattr(target, "timed_out_until", None):
                embed.add_field(
                    name="المهلة",
                    value=discord.utils.format_dt(target.timed_out_until, style="R"),
                    inline=True,
                )
            return await self._complete_action(
                action, guild, actor, target, None, reason, "", embed, keyword=keyword
            )

        if action == "purge":
            if channel is None or count is None or not 1 <= count <= MAX_PURGE_COUNT:
                raise ModerationError(f"عدد الرسائل يجب أن يكون من 1 إلى {MAX_PURGE_COUNT}.")
            check = None
            if target is not None:
                check = lambda message: message.author.id == target.id
            messages = await channel.purge(
                limit=count,
                before=before_message,
                check=check,
                reason=f"مسح بواسطة {actor}",
            )
            details = f"عدد الرسائل المحذوفة: {len(messages)}"
            return await self._complete_action(
                action,
                guild,
                actor,
                target,
                None,
                reason,
                f"تم حذف {len(messages)} رسالة.",
                keyword=keyword,
                details=details,
            )

        if action == "slowmode":
            if channel is None or slowmode_seconds is None:
                raise ModerationError("حدد مدة الوضع البطيء.")
            if not 0 <= slowmode_seconds <= MAX_SLOWMODE_SECONDS:
                raise ModerationError("الحد الأقصى للوضع البطيء هو 6 ساعات.")
            await channel.edit(
                slowmode_delay=slowmode_seconds,
                reason=f"تغيير الوضع البطيء بواسطة {actor}",
            )
            if slowmode_seconds == 0:
                text = "تم إيقاف الوضع البطيء."
                details = "تم إيقاف الوضع البطيء."
            else:
                text = (
                    "تم ضبط الوضع البطيء على "
                    f"{format_duration(timedelta(seconds=slowmode_seconds))}."
                )
                details = text
            return await self._complete_action(
                action,
                guild,
                actor,
                None,
                None,
                reason,
                text,
                keyword=keyword,
                details=details,
            )

        raise ModerationError("هذا الإجراء غير معروف.")

    async def handle_keyword_message(self, message) -> bool:
        """Handle one mapped no-prefix keyword; return whether it was consumed."""
        if message.guild is None or message.content.lstrip().startswith("!"):
            return False
        invocation = parse_keyword_invocation(message)
        if invocation is None:
            return False

        async def reply(text=None, *, embed=None):
            await message.reply(
                text or None,
                embed=embed,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if not self.keyword_store.channel_allowed(
            message.guild.id, message.channel.id
        ):
            await reply("اختصارات الإدارة غير مفعّلة في هذه القناة.")
            return True
        if not self.keyword_store.can_use(
            message.author, invocation.keyword, self.developer_id
        ):
            await reply(f"ليس لديك صلاحية استخدام `{invocation.keyword}`.")
            return True
        if invocation.error:
            await reply(
                embed=shortcut_help_embed(
                    invocation.action,
                    keyword=invocation.keyword,
                    error=invocation.error,
                )
            )
            return True

        try:
            result, embed = await self.execute(
                invocation.action,
                message.guild,
                message.author,
                target=invocation.target,
                duration=invocation.duration,
                reason=invocation.reason,
                channel=message.channel,
                count=invocation.count,
                before_message=message if invocation.action == "purge" else None,
                keyword=invocation.keyword,
                slowmode_seconds=invocation.slowmode_seconds,
            )
        except ModerationError as error:
            await reply(str(error))
        except discord.Forbidden:
            logger.info(
                "Discord denied keyword action %s in guild %s.",
                invocation.action,
                message.guild.id,
            )
            await reply("رفض Discord هذا الإجراء. تحقق من صلاحيات البوت وترتيب الرتب.")
        except discord.HTTPException:
            logger.exception(
                "Keyword moderation action %s failed in guild %s.",
                invocation.action,
                message.guild.id,
            )
            await reply("تعذر تنفيذ الإجراء بسبب خطأ مؤقت من Discord.")
        except OSError:
            logger.exception("Could not persist moderation settings or timed ban.")
            await reply("تعذر حفظ الإعداد أو الحظر المؤقت؛ لم يكتمل الإجراء.")
        else:
            await reply(result, embed=embed)
        return True

    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if not self.guild_allowed(guild.id):
            return
        settings = self.autorole_store.get(guild.id)
        if not settings["enabled"]:
            return

        role_ids = settings["bots_roles"] if member.bot else settings["roles"]
        bot_member = self._bot_member(guild)
        if bot_member is None:
            logger.warning("AutoRole cannot find the bot member in guild %s.", guild.id)
        elif not bot_member.guild_permissions.manage_roles:
            logger.warning("AutoRole needs Manage Roles in guild %s.", guild.id)
        else:
            for role_id in role_ids:
                role = guild.get_role(role_id)
                if role is None:
                    logger.warning(
                        "Configured AutoRole %s no longer exists in guild %s.",
                        role_id,
                        guild.id,
                    )
                    continue
                if role.is_default() or role.managed or bot_member.top_role <= role:
                    logger.warning(
                        "AutoRole skipped unassignable role %s in guild %s.",
                        role_id,
                        guild.id,
                    )
                    continue
                try:
                    await member.add_roles(role, reason="AutoRole: عضو جديد")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Could not assign AutoRole %s to member %s in guild %s.",
                        role_id,
                        member.id,
                        guild.id,
                    )

        welcome_message = settings["welcome_message"]
        welcome_channel_id = settings["welcome_channel"]
        if welcome_message is None or welcome_channel_id is None:
            return
        channel = guild.get_channel(welcome_channel_id)
        if channel is None:
            logger.warning(
                "Configured AutoRole welcome channel %s is missing in guild %s.",
                welcome_channel_id,
                guild.id,
            )
            return
        rendered = (
            welcome_message.replace("{user}", member.mention)
            .replace("{name}", member.display_name)
            .replace("{server}", guild.name)
            .replace("{count}", str(guild.member_count or "غير متاح"))
        )
        try:
            await channel.send(
                rendered,
                allowed_mentions=discord.AllowedMentions(
                    users=[member],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not send AutoRole welcome message in guild %s.",
                guild.id,
            )

    def _weekly_stats_embed(self, guild, *, now: datetime | None = None):
        if self.stats_store is None:
            return None
        now = now or datetime.now(UTC)
        summary = self.stats_store.weekly_summary(guild.id, now=now)
        if summary["week_total"] == 0:
            return None

        action_names = {
            "ban": "حظر",
            "kick": "طرد",
            "mute": "إسكات",
            "unmute": "فك الإسكات",
            "warn": "تحذير",
            "info": "معلومات عضو",
            "purge": "حذف رسائل",
            "slowmode": "وضع بطيء",
        }
        embed = discord.Embed(
            title=f"ملخص الإشراف الأسبوعي — {guild.name}",
            description=(
                f"إجمالي الإجراءات المسجلة: **{summary['total']}**\n"
                f"خلال آخر 7 أيام: **{summary['week_total']}**"
            ),
            color=0x5865F2,
            timestamp=now,
        )
        if getattr(guild, "icon", None):
            embed.set_thumbnail(url=guild.icon.url)

        def top_entry(counts):
            return max(counts.items(), key=lambda item: (item[1], item[0])) if counts else None

        most_action = top_entry(summary["actions"])
        if most_action:
            action, count = most_action
            embed.add_field(
                name="أكثر إجراء",
                value=f"{action_names.get(action, action)} ({count})",
                inline=True,
            )

        most_keyword = top_entry(summary["keywords"])
        if most_keyword:
            keyword, count = most_keyword
            embed.add_field(
                name="أكثر اختصار استخداماً",
                value=f"`{keyword}` ({count})",
                inline=True,
            )

        most_active = top_entry(summary["actors"])
        if most_active:
            user_id, count = most_active
            embed.add_field(
                name="أنشط إداري",
                value=f"<@{user_id}> — {count}",
                inline=True,
            )

        most_targeted = top_entry(summary["targets"])
        if most_targeted:
            user_id, count = most_targeted
            embed.add_field(
                name="أكثر عضو استُهدف",
                value=f"<@{user_id}> — {count}",
                inline=True,
            )

        max_daily = max(summary["daily"].values(), default=0)
        daily_lines = []
        for day_text, count in summary["daily"].items():
            day = date.fromisoformat(day_text)
            bar = "▰" * min(10, round(count / max_daily * 10)) if max_daily else "—"
            daily_lines.append(f"`{day.strftime('%d %b')}` {bar} {count}")
        embed.add_field(
            name="النشاط اليومي",
            value="\n".join(daily_lines),
            inline=False,
        )
        embed.set_footer(text="إحصاءات مجمّعة؛ لا تتضمن محتوى الرسائل أو أسباب العقوبات.")
        return embed

    async def send_weekly_stats(
        self,
        guild,
        *,
        now: datetime | None = None,
        delivered_recipient_ids=(),
        on_recipient_delivered=None,
    ) -> str:
        if self.stats_store is None:
            return "unavailable"
        if not self.guild_allowed(guild.id):
            return "guild_not_allowed"
        embed = self._weekly_stats_embed(guild, now=now)
        if embed is None:
            return "no_activity"
        owner = await self._guild_owner(guild)
        developer = await self._developer_user(known_users=(owner,))
        recipients = []
        if owner is not None:
            recipients.append((owner, "owner"))
        else:
            logger.warning(
                "Could not resolve guild owner for weekly moderation report in guild %s.",
                guild.id,
            )
        if developer is not None:
            recipients.append((developer, "developer"))
        else:
            logger.warning(
                "Could not resolve developer %s for weekly moderation report in guild %s.",
                self.developer_id,
                guild.id,
            )

        delivered_ids = set(delivered_recipient_ids)
        delivered = bool(delivered_ids)
        delivery_failed = owner is None or developer is None
        for recipient, recipient_type in recipients:
            recipient_id = getattr(recipient, "id", None)
            if recipient_id in delivered_ids:
                continue
            recipient_delivered = await self._send_notice(
                recipient,
                embed,
                recipient_type=f"weekly-report-{recipient_type}",
                guild_id=guild.id,
            )
            delivered = recipient_delivered or delivered
            delivery_failed = not recipient_delivered or delivery_failed
            if recipient_delivered:
                delivered_ids.add(recipient_id)
                if (
                    recipient_id is not None
                    and on_recipient_delivered is not None
                ):
                    on_recipient_delivered(recipient_id)
        if not delivered:
            return "delivery_failed" if recipients else "recipient_unavailable"
        if delivery_failed:
            return "partial_delivery"
        return "sent"

    async def _weekly_stats_cycle(self, *, now: datetime | None = None) -> None:
        if self.stats_store is None:
            return
        now = now or datetime.now(UTC)
        for guild in list(getattr(self.bot, "guilds", ())):
            if not self.guild_allowed(guild.id):
                continue
            try:
                self.stats_store.ensure_report_schedule(guild.id, now=now)
                attempt = self.stats_store.claim_due_report_attempt(
                    guild.id,
                    now=now,
                )
                if attempt is not None:
                    report_at = datetime.fromisoformat(attempt["scheduled_at"])
                    status = await self.send_weekly_stats(
                        guild,
                        now=report_at,
                        delivered_recipient_ids=attempt[
                            "delivered_recipient_ids"
                        ],
                        on_recipient_delivered=lambda recipient_id: (
                            self.stats_store.mark_weekly_report_recipient_delivered(
                                guild.id,
                                recipient_id,
                            )
                        ),
                    )
                    self.stats_store.finish_weekly_report_attempt(
                        guild.id,
                        now=now,
                        delivered=status in {"sent", "no_activity"},
                    )
                    log = logger.warning if status in {
                        "delivery_failed",
                        "recipient_unavailable",
                        "partial_delivery",
                    } else logger.info
                    log("Weekly moderation report for guild %s: %s.", guild.id, status)
            except (OSError, ValueError):
                logger.exception(
                    "Could not process weekly moderation report for guild %s.",
                    guild.id,
                )

    async def _weekly_stats_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await self._weekly_stats_cycle()
            await asyncio.sleep(60)

    async def on_ready(self) -> None:
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(
                self._expire_temporary_bans(),
                name="temporary-ban-expiry",
            )
        if self.stats_store is not None and (
            self._weekly_stats_task is None or self._weekly_stats_task.done()
        ):
            self._weekly_stats_task = asyncio.create_task(
                self._weekly_stats_loop(),
                name="weekly-moderation-stats",
            )

    async def _expire_temporary_bans(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            for guild_id, user_id, record in self.temporary_ban_store.list_due():
                guild = self.bot.get_guild(guild_id)
                if guild is None or not self.guild_allowed(guild_id):
                    continue
                try:
                    await guild.unban(
                        discord.Object(id=user_id),
                        reason="انتهاء مدة الحظر المؤقت",
                    )
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception(
                        "Could not expire temporary ban for user %s in guild %s.",
                        user_id,
                        guild_id,
                    )
                    continue
                try:
                    self.temporary_ban_store.remove(guild_id, user_id)
                except OSError:
                    logger.exception(
                        "Could not remove expired temporary ban record for user %s "
                        "in guild %s.",
                        user_id,
                        guild_id,
                    )
            await asyncio.sleep(30)

    async def close(self) -> None:
        tasks = (self._expiry_task, self._weekly_stats_task)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
