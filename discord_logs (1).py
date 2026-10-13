"""Per-server Discord audit logs with premium UI design."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands


logger = logging.getLogger(__name__)

# ═══════════════════════════════════════
# 🎨 نظام الألوان الاحترافي
# ═══════════════════════════════════════
class LogColors:
    """ألوان متناسقة لكل نوع حدث."""
    # الإيجابي
    SUCCESS = 0x2ECC71       # أخضر
    JOIN = 0x57F287          # أخضر ديسكورد
    UNBAN = 0x3BA55D         # أخضر داكن
    
    # السلبي
    DANGER = 0xED4245        # أحمر ديسكورد
    BAN = 0xE74C3C           # أحمر
    KICK = 0xE67E22          # برتقالي
    DELETE = 0xFF6B6B        # أحمر فاتح
    
    # المعلوماتي
    INFO = 0x5865F2          # أزرق ديسكورد
    UPDATE = 0x3498DB        # أزرق
    WARNING = 0xFEE75C       # أصفر
    TIMEOUT = 0xFFA500       # برتقالي
    
    # المحايد
    NEUTRAL = 0x95A5A6       # رمادي
    VOICE = 0x9B59B6         # بنفسجي
    INVITE = 0x1ABC9C        # تركوازي
    ROLE = 0x7289DA          # أزرق فاتح


# ═══════════════════════════════════════
# 😀 إيموجي لكل حدث
# ═══════════════════════════════════════
LOG_EVENT_EMOJIS = {
    "عضو جديد": "🎉",
    "عضو غادر السيرفر": "👋",
    "طرد عضو": "👢",
    "حظر عضو": "🔨",
    "إلغاء حظر عضو": "🔓",
    "تغيير اسم العضو": "✏️",
    "تغيير رتب العضو": "🎭",
    "تغيير المهلة": "⏳",
    "تعديل رسالة": "📝",
    "حذف رسالة": "🗑️",
    "حذف جماعي للرسائل": "🧹",
    "تغيير حالة صوتية": "🎤",
    "إنشاء قناة": "📢",
    "حذف قناة": "🗑️",
    "تعديل قناة": "⚙️",
    "إنشاء موضوع": "💬",
    "حذف موضوع": "🗑️",
    "إنشاء رتبة": "🎨",
    "حذف رتبة": "🗑️",
    "تعديل رتبة": "⚙️",
    "إنشاء دعوة": "🔗",
    "حذف دعوة": "🔗",
}

# ═══════════════════════════════════════
# 🎨 ألوان لكل حدث
# ═══════════════════════════════════════
LOG_EVENT_COLORS = {
    "عضو جديد": LogColors.JOIN,
    "عضو غادر السيرفر": LogColors.DANGER,
    "طرد عضو": LogColors.KICK,
    "حظر عضو": LogColors.BAN,
    "إلغاء حظر عضو": LogColors.UNBAN,
    "تغيير اسم العضو": LogColors.UPDATE,
    "تغيير رتب العضو": LogColors.ROLE,
    "تغيير المهلة": LogColors.TIMEOUT,
    "تعديل رسالة": LogColors.WARNING,
    "حذف رسالة": LogColors.DELETE,
    "حذف جماعي للرسائل": LogColors.DELETE,
    "تغيير حالة صوتية": LogColors.VOICE,
    "إنشاء قناة": LogColors.SUCCESS,
    "حذف قناة": LogColors.DANGER,
    "تعديل قناة": LogColors.UPDATE,
    "إنشاء موضوع": LogColors.SUCCESS,
    "حذف موضوع": LogColors.DANGER,
    "إنشاء رتبة": LogColors.SUCCESS,
    "حذف رتبة": LogColors.DANGER,
    "تعديل رتبة": LogColors.UPDATE,
    "إنشاء دعوة": LogColors.INVITE,
    "حذف دعوة": LogColors.DANGER,
}


# ═══════════════════════════════════════
# 💾 الإعدادات الافتراضية
# ═══════════════════════════════════════
DEFAULT_SETTINGS = {
    "enabled": False,
    "spam_enabled": False,
    "log_channel_id": None,
    "spam_channel_id": None,
    "ship_member_id": None,
    "spam_exempt_role_ids": [],
    "spam_exempt_channel_ids": [],
}
_ID_KEYS = ("log_channel_id", "spam_channel_id", "ship_member_id")
_SPAM_EXEMPTION_KEYS = ("spam_exempt_role_ids", "spam_exempt_channel_ids")
_LEGACY_SETTINGS_KEYS = set(DEFAULT_SETTINGS) - set(_SPAM_EXEMPTION_KEYS)

MENTION_SPAM_THRESHOLD = 5
MENTION_SPAM_WINDOW_SECONDS = 10
UNKNOWN_AUDIT_ACTOR = "غير معروف — لا يتوفر سجل تدقيق مطابق"


# ═══════════════════════════════════════
# 💾 المخزن (بدون تغيير)
# ═══════════════════════════════════════
class DiscordLogsStore:
    """Atomically persist logging and spam settings separately per guild."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"Could not load Discord log settings at {self.path}") from error
            self._guilds = self._validate_payload(payload)

    @staticmethod
    def _validate_payload(payload) -> dict[str, dict]:
        if not isinstance(payload, dict) or set(payload) != {"guilds"}:
            raise ValueError("Invalid Discord log settings file.")
        guilds = payload["guilds"]
        if not isinstance(guilds, dict):
            raise ValueError("Invalid Discord log guild settings.")

        validated: dict[str, dict] = {}
        expected = set(DEFAULT_SETTINGS)
        for guild_id, settings in guilds.items():
            if not isinstance(guild_id, str) or not guild_id.isdecimal():
                raise ValueError("Invalid guild ID in Discord log settings.")
            if not isinstance(settings, dict) or set(settings) not in (
                expected,
                _LEGACY_SETTINGS_KEYS,
            ):
                raise ValueError(f"Invalid Discord log settings for guild {guild_id}.")
            for key in ("enabled", "spam_enabled"):
                if type(settings[key]) is not bool:
                    raise ValueError(f"Invalid {key} flag for guild {guild_id}.")
            normalized = deepcopy(DEFAULT_SETTINGS)
            normalized["enabled"] = settings["enabled"]
            normalized["spam_enabled"] = settings["spam_enabled"]
            for key in _ID_KEYS:
                value = settings[key]
                if value is not None and (type(value) is not int or value <= 0):
                    raise ValueError(f"Invalid {key} for guild {guild_id}.")
                normalized[key] = value
            for key in _SPAM_EXEMPTION_KEYS:
                value = settings.get(key, [])
                if (
                    not isinstance(value, list)
                    or any(type(identifier) is not int or identifier <= 0 for identifier in value)
                    or len(set(value)) != len(value)
                ):
                    raise ValueError(f"Invalid {key} for guild {guild_id}.")
                normalized[key] = list(value)
            validated[guild_id] = normalized
        return validated

    @staticmethod
    def _positive_id(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("IDs must be positive integers.")
        return value

    def get(self, guild_id: int) -> dict:
        key = str(self._positive_id(guild_id))
        with self._lock:
            return deepcopy(self._guilds.get(key, DEFAULT_SETTINGS))

    def _update(self, guild_id: int, **changes) -> dict:
        key = str(self._positive_id(guild_id))
        if not changes or set(changes) - set(DEFAULT_SETTINGS):
            raise ValueError("Invalid Discord log settings update.")
        with self._lock:
            updated = deepcopy(self._guilds)
            settings = updated.setdefault(key, deepcopy(DEFAULT_SETTINGS))
            settings.update(changes)
            self._save(updated)
            self._guilds = updated
            return deepcopy(settings)

    def _save(self, guilds: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
                json.dump({"guilds": guilds}, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def set_enabled(self, guild_id: int, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean.")
        return self._update(guild_id, enabled=enabled)

    def set_spam_enabled(self, guild_id: int, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("spam_enabled must be a boolean.")
        return self._update(guild_id, spam_enabled=enabled)

    def set_log_channel(self, guild_id: int, channel_id: int) -> dict:
        return self._update(guild_id, log_channel_id=self._positive_id(channel_id))

    def clear_log_channel(self, guild_id: int) -> dict:
        settings = self.get(guild_id)
        changes = {"enabled": False, "log_channel_id": None}
        if settings["spam_channel_id"] is None:
            changes["spam_enabled"] = False
        return self._update(guild_id, **changes)

    def set_spam_channel(self, guild_id: int, channel_id: int) -> dict:
        return self._update(guild_id, spam_channel_id=self._positive_id(channel_id))

    def clear_spam_channel(self, guild_id: int) -> dict:
        settings = self.get(guild_id)
        changes = {"spam_channel_id": None}
        if settings["log_channel_id"] is None:
            changes["spam_enabled"] = False
        return self._update(guild_id, **changes)

    def set_ship_member(self, guild_id: int, member_id: int) -> dict:
        return self._update(guild_id, ship_member_id=self._positive_id(member_id))

    def clear_ship_member(self, guild_id: int) -> dict:
        return self._update(guild_id, ship_member_id=None)

    def _set_spam_exemption(self, guild_id, key, entity_id, exempt) -> dict:
        guild_key = str(self._positive_id(guild_id))
        entity_id = self._positive_id(entity_id)
        if key not in _SPAM_EXEMPTION_KEYS or type(exempt) is not bool:
            raise ValueError("Invalid mention-spam exemption update.")
        with self._lock:
            updated = deepcopy(self._guilds)
            settings = updated.setdefault(guild_key, deepcopy(DEFAULT_SETTINGS))
            values = settings[key]
            if exempt and entity_id not in values:
                values.append(entity_id)
            elif not exempt and entity_id in values:
                settings[key] = [v for v in values if v != entity_id]
            else:
                return deepcopy(settings)
            self._save(updated)
            self._guilds = updated
            return deepcopy(settings)

    def set_spam_exempt_role(self, guild_id, role_id, exempt) -> dict:
        return self._set_spam_exemption(guild_id, "spam_exempt_role_ids", role_id, exempt)

    def set_spam_exempt_channel(self, guild_id, channel_id, exempt) -> dict:
        return self._set_spam_exemption(guild_id, "spam_exempt_channel_ids", channel_id, exempt)


# ═══════════════════════════════════════
# 🎨 النظام الرئيسي - بتصميم جديد
# ═══════════════════════════════════════
class DiscordLogSystem:
    """Send beautifully formatted audit events."""

    def __init__(self, store, guild_allowed=None):
        self.store = store
        self.guild_allowed = guild_allowed or (lambda _guild_id: True)

    def _allowed(self, guild):
        if guild is None:
            return False
        try:
            return bool(self.guild_allowed(guild.id))
        except Exception:
            logger.exception("Could not check guild access for Discord logs.")
            return False

    def _logging_enabled(self, guild):
        if not self._allowed(guild):
            return False
        return bool(self.store.get(guild.id)["enabled"])

    @staticmethod
    def _channel_label(channel) -> str:
        name = getattr(channel, "name", None)
        if name:
            return f"#{name}"
        return getattr(channel, "mention", None) or "قناة"

    @staticmethod
    def _identity_label(user) -> str:
        if user is None:
            return UNKNOWN_AUDIT_ACTOR
        name = (getattr(user, "display_name", None)
                or getattr(user, "name", None)
                or str(user))
        name = discord.utils.escape_markdown(str(name))
        return f"`{name}`"

    @classmethod
    def _audit_actor_label(cls, entry) -> str:
        return cls._identity_label(getattr(entry, "user", None))

    async def _event_actor_label(self, guild, action, target_id) -> str:
        if not self._logging_enabled(guild):
            return UNKNOWN_AUDIT_ACTOR
        entry = await self._audit_entry(guild, action, target_id)
        return self._audit_actor_label(entry)

    @classmethod
    def _role_list(cls, roles) -> str:
        roles = list(roles)
        if not roles:
            return "*لا توجد*"
        visible = roles[:6]
        lines = [
            f"› {discord.utils.escape_markdown(str(role.name))}"
            for role in visible
        ]
        if len(roles) > len(visible):
            lines.append(f"› *و {len(roles) - len(visible)} رتب أخرى*")
        return "\n".join(lines)

    @staticmethod
    def _clean(value, limit: int = 1024) -> str:
        text = str(value or "—")
        text = discord.utils.escape_mentions(text)
        return text[:limit] or "—"

    def _channel(self, guild, *, spam: bool = False):
        settings = self.store.get(guild.id)
        if spam:
            channel_id = settings["spam_channel_id"] or settings["log_channel_id"]
        else:
            channel_id = settings["log_channel_id"]
        if channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            get_thread = getattr(guild, "get_thread", None)
            if callable(get_thread):
                channel = get_thread(channel_id)
        return channel

    async def _send(self, channel, embed, *, guild_id, context) -> bool:
        if channel is None:
            return False
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.Forbidden:
            logger.warning(
                "Discord logs could not send %s in guild %s.",
                context, guild_id,
            )
        except discord.HTTPException:
            logger.exception("Discord rejected %s in guild %s.", context, guild_id)
        return False

    async def log_event(
        self,
        guild,
        title: str,
        *,
        description: str | None = None,
        fields=(),
        color: int | None = None,
        thumbnail_url: str | None = None,
    ) -> bool:
        if not self._allowed(guild):
            return False
        settings = self.store.get(guild.id)
        if not settings["enabled"]:
            return False
        channel = self._channel(guild)
        if channel is None:
            return False

        # استخدم اللون المخصص للحدث
        if color is None:
            color = LOG_EVENT_COLORS.get(title, LogColors.INFO)
        emoji = LOG_EVENT_EMOJIS.get(title, "📋")

        # ═══ بناء Embed أنيق ═══
        embed = discord.Embed(
            title=f"{emoji} {title}",
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        
        # ═══ الحقول ═══
        for field in fields[:20]:
            if len(field) == 3:
                name, value, inline = field
            else:
                name, value = field
                inline = False
            embed.add_field(
                name=name,
                value=self._clean(value, 1024),
                inline=bool(inline),
            )
        
        # ═══ الصورة ═══
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        
        # ═══ Author ═══
        guild_name = self._clean(getattr(guild, "name", "السيرفر"), 180)
        guild_icon = getattr(getattr(guild, "icon", None), "url", None)
        embed.set_author(
            name=f"📋  {guild_name}",
            icon_url=guild_icon,
        )
        
        # ═══ Footer ═══
        embed.set_footer(
            text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}  •  سجل الأحداث",
            icon_url=guild_icon,
        )
        
        return await self._send(
            channel, embed,
            guild_id=guild.id,
            context="an audit event",
        )

    async def notify_spam(self, guild, member, *, deleted_count, timeout_applied, timeout_error=None) -> bool:
        if not self._allowed(guild):
            return False
        settings = self.store.get(guild.id)
        channel = self._channel(guild, spam=True)
        if channel is None:
            return False

        result = (
            "✅ مهلة 10 دقائق طُبقت"
            if timeout_applied
            else f"⚠️ تعذر تطبيق المهلة: {timeout_error or 'خطأ غير معروف'}"
        )
        
        embed = discord.Embed(
            title="🚨 تنبيه: رصد سبام منشن",
            description=(
                f"رُصدت **{MENTION_SPAM_THRESHOLD}** رسائل منشن خلال "
                f"**{MENTION_SPAM_WINDOW_SECONDS}** ثوانٍ"
            ),
            color=LogColors.DANGER,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="👤 العضو",
            value=f"`{member.display_name}`\n🆔 `{member.id}`",
            inline=False,
        )
        embed.add_field(
            name="📊 الإجراء",
            value=f"🗑️ حُذف `{deleted_count}` رسالة\n{result}",
            inline=False,
        )
        
        ship_id = settings["ship_member_id"]
        if ship_id is not None:
            ship = guild.get_member(ship_id)
            if ship is not None:
                embed.add_field(
                    name="👑 مسؤول المتابعة",
                    value=f"`{ship.display_name}`",
                    inline=False,
                )
        
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        
        guild_icon = getattr(getattr(guild, "icon", None), "url", None)
        embed.set_footer(
            text=f"🛡️ حماية السبام  •  {datetime.now(timezone.utc).strftime('%H:%M:%S')}",
            icon_url=guild_icon,
        )
        
        return await self._send(channel, embed, guild_id=guild.id, context="a spam alert")

    async def _audit_entry(self, guild, action, target_id, *, channel_id=None):
        if action is None or not callable(getattr(guild, "audit_logs", None)):
            return None
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                target = getattr(entry, "target", None)
                created_at = getattr(entry, "created_at", None)
                if getattr(target, "id", None) != target_id or created_at is None:
                    continue
                if channel_id is not None:
                    extra = getattr(entry, "extra", None)
                    extra_channel = getattr(extra, "channel", None)
                    extra_channel_id = getattr(
                        extra_channel, "id",
                        getattr(extra, "channel_id", None),
                    )
                    if extra_channel_id != channel_id:
                        continue
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - created_at > timedelta(seconds=15):
                    continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            pass
        return None

    # ═══════════════════════════════════════
    # 🎉 عضو جديد
    # ═══════════════════════════════════════
    async def on_member_join(self, member: discord.Member) -> None:
        age_days = (datetime.now(timezone.utc) - member.created_at).days
        
        embed_fields = (
            ("👤 العضو", f"{member.mention}\n`{member.name}`", True),
            ("🆔 المعرّف", f"`{member.id}`", True),
            ("📅 عمر الحساب", f"`{age_days}` يوم", True),
            ("👥 عدد الأعضاء", f"`{member.guild.member_count}`", True),
            ("📆 تاريخ الانضمام", f"<t:{int(datetime.now(timezone.utc).timestamp())}:R>", True),
        )
        
        embed = discord.Embed(
            title="🎉  عضو جديد انضم!",
            description=f"**{member.display_name}** انضم إلى **{member.guild.name}**",
            color=LogColors.JOIN,
            timestamp=datetime.now(timezone.utc),
        )
        for name, value, inline in embed_fields:
            embed.add_field(name=name, value=value, inline=inline)
        
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        
        guild_icon = getattr(getattr(member.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {member.guild.name}", icon_url=guild_icon)
        embed.set_footer(
            text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}  •  سجل الأحداث",
            icon_url=guild_icon,
        )
        
        channel = self._channel(member.guild)
        await self._send(channel, embed, guild_id=member.guild.id, context="member join")

    # ═══════════════════════════════════════
    # 👋 عضو غادر
    # ═══════════════════════════════════════
    async def on_member_remove(self, member: discord.Member) -> None:
        ban_action = getattr(discord.AuditLogAction, "ban", None)
        if await self._audit_entry(member.guild, ban_action, member.id) is not None:
            return
        action = getattr(discord.AuditLogAction, "kick", None)
        entry = await self._audit_entry(member.guild, action, member.id)
        
        if entry is not None:
            title = "طرد عضو"
            emoji = "👢"
            color = LogColors.KICK
            actor = getattr(entry, "user", None)
            reason = getattr(entry, "reason", None)
        else:
            title = "عضو غادر السيرفر"
            emoji = "👋"
            color = LogColors.DANGER
            actor = None
            reason = None
        
        fields = [
            ("👤 العضو", f"`{member.name}`\n`{member.id}`", True),
        ]
        if actor is not None:
            fields.append(("🛡️ بواسطة", f"{actor.mention}", True))
        if reason:
            fields.append(("📝 السبب", reason[:500], False))
        
        embed = discord.Embed(
            title=f"{emoji}  {title}",
            description=f"**{member.display_name}** غادر السيرفر",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        
        guild_icon = getattr(getattr(member.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {member.guild.name}", icon_url=guild_icon)
        embed.set_footer(
            text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}  •  سجل الأحداث",
            icon_url=guild_icon,
        )
        
        channel = self._channel(member.guild)
        await self._send(channel, embed, guild_id=member.guild.id, context="member leave")

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        member_update_entry = None
        if before.nick != after.nick or before.timed_out_until != after.timed_out_until:
            if self._logging_enabled(after.guild):
                member_update_entry = await self._audit_entry(
                    after.guild,
                    getattr(discord.AuditLogAction, "member_update", None),
                    after.id,
                )
        
        if before.nick != after.nick:
            embed = discord.Embed(
                title="✏️  تغيير اسم العضو",
                color=LogColors.UPDATE,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="👤 العضو", value=f"{after.mention}\n`{after.id}`", inline=True)
            embed.add_field(name="📛 قبل", value=f"`{before.nick or 'بدون'}`", inline=True)
            embed.add_field(name="📛 بعد", value=f"`{after.nick or 'بدون'}`", inline=True)
            embed.add_field(name="🛡️ بواسطة", value=self._audit_actor_label(member_update_entry), inline=False)
            
            if after.display_avatar:
                embed.set_thumbnail(url=after.display_avatar.url)
            guild_icon = getattr(getattr(after.guild, "icon", None), "url", None)
            embed.set_author(name=f"📋  {after.guild.name}", icon_url=guild_icon)
            embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
            
            channel = self._channel(after.guild)
            await self._send(channel, embed, guild_id=after.guild.id, context="nickname")
        
        # ═══ الرتب ═══
        before_roles = {r.id: r for r in before.roles}
        after_roles = {r.id: r for r in after.roles}
        added = [r for rid, r in after_roles.items() if rid not in before_roles]
        removed = [r for rid, r in before_roles.items() if rid not in after_roles]
        
        if added or removed:
            role_entry = None
            if self._logging_enabled(after.guild):
                role_entry = await self._audit_entry(
                    after.guild,
                    getattr(discord.AuditLogAction, "member_role_update", None),
                    after.id,
                )
            
            color = (LogColors.SUCCESS if added and not removed
                     else LogColors.DANGER if removed and not added
                     else LogColors.ROLE)
            
            embed = discord.Embed(
                title="🎭  تغيير رتب العضو",
                color=color,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="👤 العضو", value=f"{after.mention}\n`{after.id}`", inline=True)
            embed.add_field(name="🛡️ بواسطة", value=self._audit_actor_label(role_entry), inline=True)
            
            if added:
                embed.add_field(name="➕ الرتب المضافة", value=self._role_list(added), inline=False)
            if removed:
                embed.add_field(name="➖ الرتب المحذوفة", value=self._role_list(removed), inline=False)
            
            if after.display_avatar:
                embed.set_thumbnail(url=after.display_avatar.url)
            guild_icon = getattr(getattr(after.guild, "icon", None), "url", None)
            embed.set_author(name=f"📋  {after.guild.name}", icon_url=guild_icon)
            embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
            
            channel = self._channel(after.guild)
            await self._send(channel, embed, guild_id=after.guild.id, context="roles")
        
        # ═══ Timeout ═══
        if before.timed_out_until != after.timed_out_until:
            embed = discord.Embed(
                title="⏳  تغيير المهلة",
                color=LogColors.TIMEOUT,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="👤 العضو", value=f"{after.mention}\n`{after.id}`", inline=True)
            embed.add_field(name="🛡️ بواسطة", value=self._audit_actor_label(member_update_entry), inline=True)
            
            if after.timed_out_until:
                embed.add_field(
                    name="⏰ ينتهي",
                    value=f"<t:{int(after.timed_out_until.timestamp())}:R>",
                    inline=True,
                )
            else:
                embed.add_field(name="✅ الحالة", value="انتهت أو أُلغيت", inline=True)
            
            if after.display_avatar:
                embed.set_thumbnail(url=after.display_avatar.url)
            guild_icon = getattr(getattr(after.guild, "icon", None), "url", None)
            embed.set_author(name=f"📋  {after.guild.name}", icon_url=guild_icon)
            embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
            
            channel = self._channel(after.guild)
            await self._send(channel, embed, guild_id=after.guild.id, context="timeout")

    # ═══════════════════════════════════════
    # 🚫 حظر
    # ═══════════════════════════════════════
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        action = getattr(discord.AuditLogAction, "ban", None)
        entry = await self._audit_entry(guild, action, user.id)
        
        embed = discord.Embed(
            title="🔨  حظر عضو",
            description=f"تم حظر **{user.name}** من السيرفر",
            color=LogColors.BAN,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 العضو", value=f"{user.mention}\n`{user.id}`", inline=True)
        if entry and getattr(entry, "user", None):
            embed.add_field(name="🛡️ بواسطة", value=entry.user.mention, inline=True)
        if entry and getattr(entry, "reason", None):
            embed.add_field(name="📝 السبب", value=entry.reason[:500], inline=False)
        
        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)
        guild_icon = getattr(getattr(guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        channel = self._channel(guild)
        await self._send(channel, embed, guild_id=guild.id, context="ban")

    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        action = getattr(discord.AuditLogAction, "unban", None)
        entry = await self._audit_entry(guild, action, user.id)
        
        embed = discord.Embed(
            title="🔓  إلغاء حظر عضو",
            description=f"تم إلغاء حظر **{user.name}**",
            color=LogColors.UNBAN,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 العضو", value=f"{user.mention}\n`{user.id}`", inline=True)
        if entry and getattr(entry, "user", None):
            embed.add_field(name="🛡️ بواسطة", value=entry.user.mention, inline=True)
        
        if user.display_avatar:
            embed.set_thumbnail(url=user.display_avatar.url)
        guild_icon = getattr(getattr(guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        channel = self._channel(guild)
        await self._send(channel, embed, guild_id=guild.id, context="unban")

    # ═══════════════════════════════════════
    # 📝 الرسائل
    # ═══════════════════════════════════════
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        
        embed = discord.Embed(
            title="📝  تعديل رسالة",
            color=LogColors.WARNING,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 العضو", value=f"{before.author.mention}\n`{before.author.id}`", inline=True)
        embed.add_field(name="📍 القناة", value=before.channel.mention, inline=True)
        embed.add_field(name="🔗 الرابط", value=f"[اذهب للرسالة]({after.jump_url})", inline=True)
        embed.add_field(name="📄 قبل", value=f"```{before.content[:500] or 'رسالة فارغة'}```", inline=False)
        embed.add_field(name="📄 بعد", value=f"```{after.content[:500] or 'رسالة فارغة'}```", inline=False)
        
        if before.author.display_avatar:
            embed.set_thumbnail(url=before.author.display_avatar.url)
        guild_icon = getattr(getattr(before.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {before.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        channel = self._channel(before.guild)
        await self._send(channel, embed, guild_id=before.guild.id, context="edit")

    async def on_message_delete(self, message: discord.Message) -> None:
        author = getattr(message, "author", None)
        if message.guild is None or author is None or getattr(author, "bot", False):
            return
        
        channel_id = getattr(message.channel, "id", None)
        entry = None
        if self._logging_enabled(message.guild):
            entry = await self._audit_entry(
                message.guild,
                getattr(discord.AuditLogAction, "message_delete", None),
                author.id,
                channel_id=channel_id,
            )
        
        embed = discord.Embed(
            title="🗑️  حذف رسالة",
            color=LogColors.DELETE,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 العضو", value=f"{author.mention}\n`{author.id}`", inline=True)
        embed.add_field(name="📍 القناة", value=message.channel.mention, inline=True)
        embed.add_field(name="🛡️ بواسطة", value=self._audit_actor_label(entry), inline=True)
        
        if message.content:
            embed.add_field(name="📄 المحتوى", value=f"```{message.content[:1000]}```", inline=False)
        
        if message.attachments:
            filenames = "\n".join(
                f"📎 `{a.filename}`"
                for a in message.attachments[:10]
            )
            embed.add_field(name="📎 المرفقات", value=filenames, inline=False)
        
        if author.display_avatar:
            embed.set_thumbnail(url=author.display_avatar.url)
        guild_icon = getattr(getattr(message.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {message.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        channel = self._channel(message.guild)
        await self._send(channel, embed, guild_id=message.guild.id, context="delete")

    async def on_bulk_message_delete(self, messages) -> None:
        messages = list(messages)
        if not messages or messages[0].guild is None:
            return
        guild = messages[0].guild
        channel_id = getattr(messages[0].channel, "id", None)
        
        entry = None
        if self._logging_enabled(guild) and channel_id is not None:
            entry = await self._audit_entry(
                guild,
                getattr(discord.AuditLogAction, "message_bulk_delete", None),
                channel_id,
                channel_id=channel_id,
            )
        
        sample = []
        for m in messages[:5]:
            a = getattr(m, "author", None)
            if a is not None and not a.bot:
                sample.append(f"› **{a.display_name}:** {(m.content or 'فارغة')[:80]}")
        
        embed = discord.Embed(
            title="🧹  حذف جماعي للرسائل",
            description=f"تم حذف **{len(messages)}** رسالة من {messages[0].channel.mention}",
            color=LogColors.DELETE,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🛡️ بواسطة", value=self._audit_actor_label(entry), inline=True)
        embed.add_field(name="📍 القناة", value=messages[0].channel.mention, inline=True)
        embed.add_field(name="📊 العدد", value=f"`{len(messages)}`", inline=True)
        
        if sample:
            embed.add_field(name="📄 معاينة", value="\n".join(sample)[:1024], inline=False)
        
        guild_icon = getattr(getattr(guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        channel = self._channel(guild)
        await self._send(channel, embed, guild_id=guild.id, context="bulk delete")

    # ═══════════════════════════════════════
    # 🎤 الحالة الصوتية
    # ═══════════════════════════════════════
    async def on_voice_state_update(self, member, before, after) -> None:
        changes = []
        if before.channel != after.channel:
            if before.channel is None:
                changes.append(f"🟢 دخل {after.channel.mention}")
            elif after.channel is None:
                changes.append(f"🔴 غادر {before.channel.mention}")
            else:
                changes.append(f"🔄 من {before.channel.mention} إلى {after.channel.mention}")
        if before.mute != after.mute:
            changes.append("🔇 كتمه إداريًا" if after.mute else "🔊 أُلغي كتمه الإداري")
        if before.deaf != after.deaf:
            changes.append("🔕 عُطّل صوته" if after.deaf else "🔔 أُلغي تعطيل صوته")
        
        if not changes:
            return
        
        embed = discord.Embed(
            title="🎤  تغيير حالة صوتية",
            color=LogColors.VOICE,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 العضو", value=f"{member.mention}\n`{member.id}`", inline=True)
        embed.add_field(name="🔄 التغيير", value="\n".join(changes), inline=False)
        
        if member.display_avatar:
            embed.set_thumbnail(url=member.display_avatar.url)
        guild_icon = getattr(getattr(member.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {member.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        channel = self._channel(member.guild)
        await self._send(channel, embed, guild_id=member.guild.id, context="voice")

    # ═══════════════════════════════════════
    # 📢 القنوات
    # ═══════════════════════════════════════
    async def on_guild_channel_create(self, channel) -> None:
        actor = await self._event_actor_label(
            channel.guild,
            getattr(discord.AuditLogAction, "channel_create", None),
            channel.id,
        )
        
        embed = discord.Embed(
            title="📢  إنشاء قناة",
            description=f"تم إنشاء {channel.mention}",
            color=LogColors.SUCCESS,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنشئ", value=actor, inline=True)
        embed.add_field(name="📍 القناة", value=f"`#{channel.name}`", inline=True)
        embed.add_field(name="📝 النوع", value=f"`{channel.type}`", inline=True)
        
        guild_icon = getattr(getattr(channel.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {channel.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(channel.guild)
        await self._send(c, embed, guild_id=channel.guild.id, context="channel create")

    async def on_guild_channel_delete(self, channel) -> None:
        actor = await self._event_actor_label(
            channel.guild,
            getattr(discord.AuditLogAction, "channel_delete", None),
            channel.id,
        )
        
        embed = discord.Embed(
            title="🗑️  حذف قناة",
            description=f"تم حذف قناة **#{channel.name}**",
            color=LogColors.DANGER,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنفّذ", value=actor, inline=True)
        embed.add_field(name="📍 القناة", value=f"`{channel.name}`", inline=True)
        embed.add_field(name="🆔 ID", value=f"`{channel.id}`", inline=True)
        
        guild_icon = getattr(getattr(channel.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {channel.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(channel.guild)
        await self._send(c, embed, guild_id=channel.guild.id, context="channel delete")

    async def on_guild_channel_update(self, before, after) -> None:
        changes = []
        for name, label in (("name", "الاسم"), ("topic", "الموضوع"), ("slowmode_delay", "الوضع البطيء")):
            old, new = getattr(before, name, None), getattr(after, name, None)
            if old != new:
                changes.append(f"**{label}:** `{old or '—'}` → `{new or '—'}`")
        
        if not changes:
            return
        
        actor = await self._event_actor_label(
            after.guild,
            getattr(discord.AuditLogAction, "channel_update", None),
            after.id,
        )
        
        embed = discord.Embed(
            title="⚙️  تعديل قناة",
            description=f"تم تعديل {after.mention}",
            color=LogColors.UPDATE,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنفّذ", value=actor, inline=True)
        embed.add_field(name="📍 القناة", value=f"`#{after.name}`", inline=True)
        embed.add_field(name="📝 التغييرات", value="\n".join(changes), inline=False)
        
        guild_icon = getattr(getattr(after.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {after.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(after.guild)
        await self._send(c, embed, guild_id=after.guild.id, context="channel update")

    # ═══════════════════════════════════════
    # 💬 المواضيع
    # ═══════════════════════════════════════
    async def on_thread_create(self, thread) -> None:
        actor = await self._event_actor_label(
            thread.guild,
            getattr(discord.AuditLogAction, "thread_create", None),
            thread.id,
        )
        
        embed = discord.Embed(
            title="💬  إنشاء موضوع",
            description=f"تم إنشاء موضوع **{thread.name}**",
            color=LogColors.SUCCESS,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنشئ", value=actor, inline=True)
        embed.add_field(name="📍 الموضوع", value=f"`{thread.name}`", inline=True)
        embed.add_field(name="🆔 ID", value=f"`{thread.id}`", inline=True)
        
        guild_icon = getattr(getattr(thread.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {thread.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(thread.guild)
        await self._send(c, embed, guild_id=thread.guild.id, context="thread create")

    async def on_thread_delete(self, thread) -> None:
        actor = await self._event_actor_label(
            thread.guild,
            getattr(discord.AuditLogAction, "thread_delete", None),
            thread.id,
        )
        
        embed = discord.Embed(
            title="🗑️  حذف موضوع",
            description=f"تم حذف موضوع **{thread.name}**",
            color=LogColors.DANGER,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنفّذ", value=actor, inline=True)
        embed.add_field(name="📍 الموضوع", value=f"`{thread.name}`", inline=True)
        
        guild_icon = getattr(getattr(thread.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {thread.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(thread.guild)
        await self._send(c, embed, guild_id=thread.guild.id, context="thread delete")

    # ═══════════════════════════════════════
    # 🎨 الرتب
    # ═══════════════════════════════════════
    async def on_guild_role_create(self, role: discord.Role) -> None:
        actor = await self._event_actor_label(
            role.guild,
            getattr(discord.AuditLogAction, "role_create", None),
            role.id,
        )
        
        embed = discord.Embed(
            title="🎨  إنشاء رتبة",
            description=f"تم إنشاء رتبة {role.mention}",
            color=LogColors.SUCCESS,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنشئ", value=actor, inline=True)
        embed.add_field(name="🎭 الرتبة", value=f"`{role.name}`", inline=True)
        embed.add_field(name="🎨 اللون", value=f"`{role.color}`", inline=True)
        
        guild_icon = getattr(getattr(role.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {role.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(role.guild)
        await self._send(c, embed, guild_id=role.guild.id, context="role create")

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        actor = await self._event_actor_label(
            role.guild,
            getattr(discord.AuditLogAction, "role_delete", None),
            role.id,
        )
        
        embed = discord.Embed(
            title="🗑️  حذف رتبة",
            description=f"تم حذف رتبة **{role.name}**",
            color=LogColors.DANGER,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنفّذ", value=actor, inline=True)
        embed.add_field(name="🎭 الرتبة", value=f"`{role.name}`", inline=True)
        
        guild_icon = getattr(getattr(role.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {role.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(role.guild)
        await self._send(c, embed, guild_id=role.guild.id, context="role delete")

    async def on_guild_role_update(self, before, after) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"**الاسم:** `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"**اللون:** `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            changes.append("**الصلاحيات:** تغيّرت")
        
        if not changes:
            return
        
        actor = await self._event_actor_label(
            after.guild,
            getattr(discord.AuditLogAction, "role_update", None),
            after.id,
        )
        
        embed = discord.Embed(
            title="⚙️  تعديل رتبة",
            description=f"تم تعديل رتبة {after.mention}",
            color=LogColors.UPDATE,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 المنفّذ", value=actor, inline=True)
        embed.add_field(name="🎭 الرتبة", value=f"`{after.name}`", inline=True)
        embed.add_field(name="📝 التغييرات", value="\n".join(changes), inline=False)
        
        guild_icon = getattr(getattr(after.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {after.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(after.guild)
        await self._send(c, embed, guild_id=after.guild.id, context="role update")

    # ═══════════════════════════════════════
    # 🔗 الدعوات
    # ═══════════════════════════════════════
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        inviter = getattr(invite, "inviter", None)
        
        embed = discord.Embed(
            title="🔗  إنشاء دعوة",
            color=LogColors.INVITE,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🔑 الرمز", value=f"`{invite.code}`", inline=True)
        if inviter:
            embed.add_field(name="👤 المنشئ", value=f"{inviter.mention}\n`{inviter.id}`", inline=True)
        embed.add_field(
            name="⏰ الانتهاء",
            value=f"<t:{int(invite.expires_at.timestamp())}:R>" if invite.expires_at else "`أبدي`",
            inline=True,
        )
        
        guild_icon = getattr(getattr(invite.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {invite.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(invite.guild)
        await self._send(c, embed, guild_id=invite.guild.id, context="invite create")

    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        
        embed = discord.Embed(
            title="🔗  حذف دعوة",
            description=f"تم حذف الدعوة `{invite.code}`",
            color=LogColors.DANGER,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="🔑 الرمز", value=f"`{invite.code}`", inline=True)
        
        guild_icon = getattr(getattr(invite.guild, "icon", None), "url", None)
        embed.set_author(name=f"📋  {invite.guild.name}", icon_url=guild_icon)
        embed.set_footer(text=f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')}", icon_url=guild_icon)
        
        c = self._channel(invite.guild)
        await self._send(c, embed, guild_id=invite.guild.id, context="invite delete")


# ═══════════════════════════════════════
# 🔧 دالة التحقق من القناة
# ═══════════════════════════════════════
def _channel_can_receive_embeds(guild, channel) -> bool:
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        return False
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    permissions = permissions_for(bot_member)
    return bool(
        getattr(permissions, "send_messages", False)
        and getattr(permissions, "embed_links", False)
    )


# ═══════════════════════════════════════
# 🚀 دالة التسجيل (نفسها)
# ═══════════════════════════════════════
def setup_discord_logs(bot, store, developer_id, *, guild_allowed=None):
    """Register /logs and add listeners."""
    existing = getattr(bot, "_discord_logs_system", None)
    if existing is not None:
        return existing

    if bot.tree.get_command("logs") is not None:
        raise RuntimeError("The /logs command name is already in use.")

    system = DiscordLogSystem(store, guild_allowed)
    group = app_commands.Group(
        name="logs",
        description="📊 إعدادات سجلات الأحداث وحماية السبام",
    )

    async def require_manager(interaction):
        guild = interaction.guild
        user = interaction.user
        perms = getattr(user, "guild_permissions", None)
        allowed = bool(
            guild is not None and (
                user.id == developer_id
                or user.id == guild.owner_id
                or getattr(perms, "administrator", False)
            )
        )
        if not allowed:
            await interaction.response.send_message(
                "🔒 **هذا الأمر لمطور البوت أو لمالك السيرفر وإدارييه فقط.**",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return not allowed

    def configured_channel(interaction, channel_id):
        if channel_id is None or interaction.guild is None:
            return None
        return interaction.guild.get_channel(channel_id)

    @group.command(name="set_channel", description="📍 تحديد قناة سجلات السيرفر")
    @app_commands.guild_only()
    async def set_channel(interaction, channel: discord.TextChannel):
        if await require_manager(interaction):
            return
        if not _channel_can_receive_embeds(interaction.guild, channel):
            await interaction.response.send_message(
                "⚠️ يحتاج البوت صلاحيتَي **إرسال الرسائل** و**إرفاق الروابط**.",
                ephemeral=True,
            )
            return
        try:
            store.set_log_channel(interaction.guild_id, channel.id)
        except (OSError, ValueError):
            logger.exception("Could not save log channel.")
            response = "❌ تعذر حفظ القناة."
        else:
            response = f"✅ حُفظت القناة: {channel.mention}\n💡 فعّل التسجيل بـ `/logs enable`"
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="enable", description="✅ تفعيل سجلات الأحداث")
    @app_commands.guild_only()
    async def enable(interaction):
        if await require_manager(interaction):
            return
        settings = store.get(interaction.guild_id)
        channel = configured_channel(interaction, settings["log_channel_id"])
        if channel is None:
            response = "⚠️ حدّد قناة أولاً بـ `/logs set_channel`"
        elif not _channel_can_receive_embeds(interaction.guild, channel):
            response = "⚠️ البوت يحتاج صلاحيات في القناة."
        else:
            try:
                store.set_enabled(interaction.guild_id, True)
            except (OSError, ValueError):
                response = "❌ تعذر التفعيل."
            else:
                response = f"✅ فُعّلت السجلات في {channel.mention}!"
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="disable", description="⛔ إيقاف سجلات الأحداث")
    @app_commands.guild_only()
    async def disable(interaction):
        if await require_manager(interaction):
            return
        try:
            store.set_enabled(interaction.guild_id, False)
        except (OSError, ValueError):
            response = "❌ تعذر الإيقاف."
        else:
            response = "⛔ أُوقفت السجلات."
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="show", description="👁️ عرض الإعدادات الحالية")
    @app_commands.guild_only()
    async def show(interaction):
        if await require_manager(interaction):
            return
        s = store.get(interaction.guild_id)
        main = f"<#{s['log_channel_id']}>" if s["log_channel_id"] else "`غير محددة`"
        spam = f"<#{s['spam_channel_id']}>" if s["spam_channel_id"] else "`قناة السجلات`"
        ship = f"<@{s['ship_member_id']}>" if s["ship_member_id"] else "`غير محدد`"
        
        embed = discord.Embed(
            title="📊  إعدادات السجلات",
            description=f"الحالة في **{interaction.guild.name}**",
            color=LogColors.INFO,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="📋 السجلات",
            value=f"{'✅ مفعّلة' if s['enabled'] else '⛔ متوقفة'}",
            inline=True,
        )
        embed.add_field(
            name="🛡️ حماية السبام",
            value=f"{'✅ مفعّلة' if s['spam_enabled'] else '⛔ متوقفة'}",
            inline=True,
        )
        embed.add_field(name="📍 قناة السجلات", value=main, inline=True)
        embed.add_field(name="🚨 قناة التنبيهات", value=spam, inline=True)
        embed.add_field(name="👑 مسؤول المتابعة", value=ship, inline=True)
        
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text="استخدم /logs لتعديل الإعدادات")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # احذف الأوامر القديمة (spam_channel, ship, إلخ) لتبسيط الأمر
    bot.tree.add_command(group)

    event_names = (
        "on_member_join", "on_member_remove", "on_member_update",
        "on_member_ban", "on_member_unban",
        "on_message_edit", "on_message_delete", "on_bulk_message_delete",
        "on_voice_state_update",
        "on_guild_channel_create", "on_guild_channel_delete", "on_guild_channel_update",
        "on_thread_create", "on_thread_delete",
        "on_guild_role_create", "on_guild_role_delete", "on_guild_role_update",
        "on_invite_create", "on_invite_delete",
    )
    for name in event_names:
        bot.add_listener(getattr(system, name), name)
    
    bot._discord_logs_system = system
    bot._discord_logs_command_group = group
    return system