"""Per-guild anti-link settings, moderation, and admin slash commands."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import discord
from discord import app_commands


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MINUTES = 60
MAX_TIMEOUT_MINUTES = 10_080
DEFAULT_GUILD_SETTINGS = {
    "enabled": False,
    "timeout_minutes": DEFAULT_TIMEOUT_MINUTES,
    "allowed_channels": [],
    "exempt_roles": [],
}
LINK_PATTERN = re.compile(
    r"(?:https?://|www\.)[^\s<>]+"
    r"|(?<![\w@])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>]*)?",
    re.IGNORECASE,
)


def contains_link(text: str | None) -> bool:
    """Return whether text contains a web URL, bare domain, or invite link."""
    return bool(text and LINK_PATTERN.search(text))


class AntiLinkStore:
    """Atomically persist anti-link settings separately for each server."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            self._guilds = self._validate_payload(payload)

    @staticmethod
    def _validate_payload(payload) -> dict[str, dict]:
        if not isinstance(payload, dict) or set(payload) != {"guilds"}:
            raise ValueError("Invalid anti-link settings file.")
        guilds = payload["guilds"]
        if not isinstance(guilds, dict):
            raise ValueError("Invalid anti-link guild settings.")

        validated: dict[str, dict] = {}
        expected_keys = set(DEFAULT_GUILD_SETTINGS)
        for guild_id, settings in guilds.items():
            if not isinstance(guild_id, str) or not guild_id.isdecimal():
                raise ValueError("Invalid guild ID in anti-link settings.")
            if not isinstance(settings, dict) or set(settings) != expected_keys:
                raise ValueError(f"Invalid anti-link settings for guild {guild_id}.")
            if type(settings["enabled"]) is not bool:
                raise ValueError(f"Invalid anti-link enabled flag for guild {guild_id}.")
            minutes = settings["timeout_minutes"]
            if type(minutes) is not int or not 0 <= minutes <= MAX_TIMEOUT_MINUTES:
                raise ValueError(f"Invalid anti-link timeout for guild {guild_id}.")
            for key in ("allowed_channels", "exempt_roles"):
                ids = settings[key]
                if (
                    not isinstance(ids, list)
                    or any(type(value) is not int or value <= 0 for value in ids)
                    or len(ids) != len(set(ids))
                ):
                    raise ValueError(f"Invalid {key} in anti-link settings.")
            validated[guild_id] = {
                "enabled": settings["enabled"],
                "timeout_minutes": minutes,
                "allowed_channels": list(settings["allowed_channels"]),
                "exempt_roles": list(settings["exempt_roles"]),
            }
        return validated

    @staticmethod
    def _positive_id(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("IDs must be positive integers.")
        return value

    def get(self, guild_id: int) -> dict:
        key = str(self._positive_id(guild_id))
        with self._lock:
            settings = self._guilds.get(key, DEFAULT_GUILD_SETTINGS)
            return deepcopy(settings)

    def _update(self, guild_id: int, change) -> dict:
        key = str(self._positive_id(guild_id))
        with self._lock:
            updated = deepcopy(self._guilds)
            settings = updated.setdefault(key, deepcopy(DEFAULT_GUILD_SETTINGS))
            change(settings)
            if updated != self._guilds:
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
        return self._update(guild_id, lambda settings: settings.__setitem__("enabled", enabled))

    def set_timeout(self, guild_id: int, minutes: int) -> dict:
        if type(minutes) is not int or not 0 <= minutes <= MAX_TIMEOUT_MINUTES:
            raise ValueError(
                f"Timeout must be between 0 and {MAX_TIMEOUT_MINUTES} minutes."
            )
        return self._update(
            guild_id,
            lambda settings: settings.__setitem__("timeout_minutes", minutes),
        )

    def set_settings(self, guild_id: int, settings: dict) -> dict:
        """Replace one server's settings in a single atomic write."""
        if not isinstance(settings, dict) or set(settings) != set(DEFAULT_GUILD_SETTINGS):
            raise ValueError("Invalid anti-link settings.")
        if type(settings["enabled"]) is not bool:
            raise ValueError("enabled must be a boolean.")
        minutes = settings["timeout_minutes"]
        if type(minutes) is not int or not 0 <= minutes <= MAX_TIMEOUT_MINUTES:
            raise ValueError(
                f"Timeout must be between 0 and {MAX_TIMEOUT_MINUTES} minutes."
            )

        normalized = {"enabled": settings["enabled"], "timeout_minutes": minutes}
        for key in ("allowed_channels", "exempt_roles"):
            values = settings[key]
            if (
                not isinstance(values, list)
                or any(type(value) is not int or value <= 0 for value in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"Invalid {key} in anti-link settings.")
            normalized[key] = list(values)

        return self._update(guild_id, lambda current: current.update(normalized))

    def add_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        channel_id = self._positive_id(channel_id)
        added = False

        def change(settings):
            nonlocal added
            if channel_id not in settings["allowed_channels"]:
                settings["allowed_channels"].append(channel_id)
                added = True

        self._update(guild_id, change)
        return added

    def remove_allowed_channel(self, guild_id: int, channel_id: int) -> bool:
        channel_id = self._positive_id(channel_id)
        removed = False

        def change(settings):
            nonlocal removed
            if channel_id in settings["allowed_channels"]:
                settings["allowed_channels"].remove(channel_id)
                removed = True

        self._update(guild_id, change)
        return removed

    def add_exempt_role(self, guild_id: int, role_id: int) -> bool:
        role_id = self._positive_id(role_id)
        added = False

        def change(settings):
            nonlocal added
            if role_id not in settings["exempt_roles"]:
                settings["exempt_roles"].append(role_id)
                added = True

        self._update(guild_id, change)
        return added

    def remove_exempt_role(self, guild_id: int, role_id: int) -> bool:
        role_id = self._positive_id(role_id)
        removed = False

        def change(settings):
            nonlocal removed
            if role_id in settings["exempt_roles"]:
                settings["exempt_roles"].remove(role_id)
                removed = True

        self._update(guild_id, change)
        return removed


class AntiLinkModeration:
    """Delete disallowed links and apply the configured timeout."""

    def __init__(self, store: AntiLinkStore, developer_id: int):
        self.store = store
        self.developer_id = developer_id

    async def handle(self, message: discord.Message) -> bool:
        guild = message.guild
        author = message.author
        if guild is None or author.bot:
            return False

        settings = self.store.get(guild.id)
        if not settings["enabled"] or not contains_link(message.content):
            return False
        if author.id == self.developer_id:
            return False

        permissions = getattr(author, "guild_permissions", None)
        if permissions and (
            getattr(permissions, "administrator", False)
            or getattr(permissions, "manage_messages", False)
        ):
            return False
        if any(
            role.id in settings["exempt_roles"]
            for role in getattr(author, "roles", ())
        ):
            return False

        channel_ids = {
            getattr(message.channel, "id", None),
            getattr(message.channel, "parent_id", None),
        }
        if channel_ids.intersection(settings["allowed_channels"]):
            return False

        deleted = False
        try:
            await message.delete()
            deleted = True
        except discord.NotFound:
            # Another moderator or bot already removed the message.
            return True
        except discord.Forbidden:
            logger.warning(
                "Anti-link could not delete a message in guild %s, channel %s; "
                "the bot needs Manage Messages.",
                guild.id,
                message.channel.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected anti-link deletion in guild %s, channel %s.",
                guild.id,
                message.channel.id,
            )

        timeout_minutes = settings["timeout_minutes"]
        timeout_applied = False
        if timeout_minutes:
            try:
                await author.timeout(
                    timedelta(minutes=timeout_minutes),
                    reason="Anti-Link rule: links are not allowed in this server",
                )
                timeout_applied = True
            except discord.Forbidden:
                logger.warning(
                    "Anti-link could not time out member %s in guild %s; "
                    "check Moderate Members and role hierarchy.",
                    author.id,
                    guild.id,
                )
            except discord.HTTPException:
                logger.exception(
                    "Discord rejected anti-link timeout for member %s in guild %s.",
                    author.id,
                    guild.id,
                )

        if deleted:
            warning = "حذفت رسالتك لأنها تحتوي رابطًا غير مسموح في هذا السيرفر."
        else:
            warning = "رصدت رابطًا غير مسموح، لكن تعذر حذف الرسالة. تحقق من صلاحيات البوت."
        if timeout_minutes == 0:
            warning += " الإجراء الحالي حذف الرسالة فقط."
        elif timeout_applied:
            warning += f" طُبقت عليك مهلة لمدة {timeout_minutes} دقيقة."
        else:
            warning += (
                " تعذر تطبيق المهلة؛ تحقق من صلاحية Moderate Members "
                "وترتيب رتبة البوت."
            )

        try:
            await message.channel.send(
                warning,
                delete_after=10,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.warning(
                "Anti-link could not send a warning in guild %s, channel %s.",
                guild.id,
                message.channel.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected an anti-link warning in guild %s, channel %s.",
                guild.id,
                message.channel.id,
            )
        return True


def setup_anti_link_commands(bot, store: AntiLinkStore):
    """Register guild-admin commands for configuring anti-link moderation."""
    existing = getattr(bot, "_anti_link_command_group", None)
    if existing is not None:
        return existing

    group = app_commands.Group(
        name="antilink",
        description="إعدادات منع الروابط في هذا السيرفر",
    )

    async def reject_non_admin(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        if interaction.guild is not None and getattr(
            permissions, "administrator", False
        ):
            return False
        await interaction.response.send_message(
            "هذا الأمر لإداريي السيرفر فقط.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    @group.command(name="enable", description="تفعيل منع الروابط")
    @app_commands.guild_only()
    async def enable(interaction: discord.Interaction) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            store.set_enabled(interaction.guild_id, True)
        except OSError:
            logger.exception("Could not enable anti-link in guild %s.", interaction.guild_id)
            response = "تعذر حفظ الإعداد؛ لم يُفعّل منع الروابط."
        else:
            settings = store.get(interaction.guild_id)
            timeout = (
                "حذف الرسالة فقط"
                if settings["timeout_minutes"] == 0
                else f"حذف الرسالة وإسكات العضو {settings['timeout_minutes']} دقيقة"
            )
            response = f"فُعّل منع الروابط. الإجراء: {timeout}."
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="disable", description="تعطيل منع الروابط")
    @app_commands.guild_only()
    async def disable(interaction: discord.Interaction) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            store.set_enabled(interaction.guild_id, False)
        except OSError:
            logger.exception("Could not disable anti-link in guild %s.", interaction.guild_id)
            response = "تعذر حفظ الإعداد؛ لم يتغير وضع منع الروابط."
        else:
            response = "عُطّل منع الروابط في هذا السيرفر."
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="set_timeout", description="تحديد مدة الإسكات بالدقائق")
    @app_commands.guild_only()
    @app_commands.describe(minutes="من 0 إلى 10080؛ الصفر يعني حذف الرسالة فقط")
    async def set_timeout(
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 0, MAX_TIMEOUT_MINUTES],
    ) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            store.set_timeout(interaction.guild_id, minutes)
        except (OSError, ValueError):
            logger.exception("Could not set anti-link timeout in guild %s.", interaction.guild_id)
            response = "تعذر حفظ مدة الإسكات."
        else:
            response = (
                "سيحذف البوت الرسالة فقط."
                if minutes == 0
                else f"ضُبطت مدة الإسكات على {minutes} دقيقة."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="allow_channel", description="السماح بالروابط في قناة")
    @app_commands.guild_only()
    async def allow_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            added = store.add_allowed_channel(interaction.guild_id, channel.id)
        except OSError:
            logger.exception("Could not allow anti-link channel in guild %s.", interaction.guild_id)
            response = "تعذر حفظ القناة المسموحة."
        else:
            response = (
                f"سُمح بالروابط في {channel.mention}."
                if added
                else f"{channel.mention} مسموح فيها بالروابط بالفعل."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="remove_channel", description="إزالة قناة من قائمة السماح")
    @app_commands.guild_only()
    async def remove_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            removed = store.remove_allowed_channel(interaction.guild_id, channel.id)
        except OSError:
            logger.exception("Could not remove anti-link channel in guild %s.", interaction.guild_id)
            response = "تعذر حفظ إعدادات القنوات."
        else:
            response = (
                f"أُزيلت {channel.mention} من قنوات السماح."
                if removed
                else f"{channel.mention} ليست في قائمة السماح."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="exempt_role", description="إعفاء رتبة من منع الروابط")
    @app_commands.guild_only()
    async def exempt_role(
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            added = store.add_exempt_role(interaction.guild_id, role.id)
        except OSError:
            logger.exception("Could not exempt anti-link role in guild %s.", interaction.guild_id)
            response = "تعذر حفظ الرتبة المعفاة."
        else:
            response = (
                f"أُعفيت رتبة {role.mention} من منع الروابط."
                if added
                else f"رتبة {role.mention} معفاة بالفعل."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="remove_role", description="إزالة إعفاء رتبة")
    @app_commands.guild_only()
    async def remove_role(
        interaction: discord.Interaction,
        role: discord.Role,
    ) -> None:
        if await reject_non_admin(interaction):
            return
        try:
            removed = store.remove_exempt_role(interaction.guild_id, role.id)
        except OSError:
            logger.exception("Could not remove anti-link role in guild %s.", interaction.guild_id)
            response = "تعذر حفظ إعدادات الرتب."
        else:
            response = (
                f"أُزيل إعفاء رتبة {role.mention}."
                if removed
                else f"رتبة {role.mention} ليست معفاة."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="show", description="عرض إعدادات منع الروابط")
    @app_commands.guild_only()
    async def show(interaction: discord.Interaction) -> None:
        if await reject_non_admin(interaction):
            return
        settings = store.get(interaction.guild_id)
        state = "مفعّل" if settings["enabled"] else "معطّل"
        timeout = (
            "حذف الرسالة فقط"
            if settings["timeout_minutes"] == 0
            else f"{settings['timeout_minutes']} دقيقة"
        )
        channels = (
            "، ".join(f"<#{channel_id}>" for channel_id in settings["allowed_channels"][:10])
            or "لا توجد"
        )
        roles = (
            "، ".join(f"<@&{role_id}>" for role_id in settings["exempt_roles"][:10])
            or "لا توجد"
        )
        embed = discord.Embed(
            title="إعدادات منع الروابط",
            description=(
                f"الحالة: **{state}**\n"
                f"الإجراء: **{timeout}**\n"
                f"قنوات السماح: {channels}\n"
                f"الرتب المعفاة: {roles}\n"
                "الإداريون ومَن لديهم صلاحية إدارة الرسائل معفيون دائمًا."
            ),
            color=0x4A90E2,
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot.tree.add_command(group)
    bot._anti_link_command_group = group
    return group