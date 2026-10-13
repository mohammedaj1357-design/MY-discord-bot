"""Per-server custom blocked terms with explicit, reversible configuration."""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import threading
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands

from profanity_filter import (
    MAX_CONFIGURED_TERMS,
    ProfanityTermError,
    ProfanityTermStore,
    contains_bad_word,
)
from funny_responses import (
    DEFAULT_FUNNY_RESPONSES,
    FUNNY_RESPONSE_DELETE_SECONDS,
    MAX_FUNNY_RESPONSE_LENGTH,
    MAX_FUNNY_RESPONSES,
)


logger = logging.getLogger(__name__)
MAX_TIMEOUT_MINUTES = 24 * 60
MAX_WARNING_LENGTH = 500
ALLOWED_ACTIONS = {"delete", "timeout"}
EXEMPTION_KEYS = {
    "channel": "exempt_channel_ids",
    "role": "exempt_role_ids",
    "user": "exempt_user_ids",
}
DEFAULT_GUILD_SETTINGS = {
    "enabled": False,
    "terms": [],
    "action": "delete",
    "timeout_minutes": 5,
    "warning_message": "🚫 ممنوع استخدام هذه الكلمة أو العبارة.",
    "reply_channel_id": None,
    "funny_responses": list(DEFAULT_FUNNY_RESPONSES),
    "exempt_channel_ids": [],
    "exempt_role_ids": [],
    "exempt_user_ids": [],
}


class GuildBannedWordsStore:
    """Atomically persist custom blocked terms and policy per Discord guild."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    f"Could not load custom blocked-word settings at {self.path}"
                ) from error
            self._guilds = self._validate_payload(payload)

    @staticmethod
    def _positive_id(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("IDs must be positive integers.")
        return value

    @classmethod
    def _validate_guild_id(cls, guild_id: int) -> str:
        return str(cls._positive_id(guild_id))

    @classmethod
    def _validate_settings(cls, settings: object, guild_id: str) -> dict:
        expected = set(DEFAULT_GUILD_SETTINGS)
        if not isinstance(settings, dict):
            raise ValueError(f"Invalid custom blocked-word settings for guild {guild_id}.")
        existing_keys = set(settings)
        missing_keys = expected - existing_keys
        if (
            not existing_keys <= expected
            or not missing_keys <= {"funny_responses", "reply_channel_id"}
        ):
            raise ValueError(f"Invalid custom blocked-word settings for guild {guild_id}.")
        if missing_keys:
            settings = {
                **settings,
                **{
                    key: deepcopy(DEFAULT_GUILD_SETTINGS[key])
                    for key in missing_keys
                },
            }
        if type(settings["enabled"]) is not bool:
            raise ValueError(f"Invalid enabled flag for guild {guild_id}.")
        if (
            not isinstance(settings["action"], str)
            or settings["action"] not in ALLOWED_ACTIONS
        ):
            raise ValueError(f"Invalid blocked-word action for guild {guild_id}.")
        timeout = settings["timeout_minutes"]
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_MINUTES:
            raise ValueError(f"Invalid timeout duration for guild {guild_id}.")
        warning = settings["warning_message"]
        if (
            not isinstance(warning, str)
            or len(warning) > MAX_WARNING_LENGTH
        ):
            raise ValueError(f"Invalid warning message for guild {guild_id}.")
        reply_channel_id = settings["reply_channel_id"]
        if (
            reply_channel_id is not None
            and (type(reply_channel_id) is not int or reply_channel_id <= 0)
        ):
            raise ValueError(f"Invalid reply channel for guild {guild_id}.")
        responses = settings["funny_responses"]
        if not isinstance(responses, list) or len(responses) > MAX_FUNNY_RESPONSES:
            raise ValueError(f"Invalid funny response list for guild {guild_id}.")
        clean_responses: list[str] = []
        response_keys: set[str] = set()
        for response in responses:
            if (
                not isinstance(response, str)
                or not response.strip()
                or len(response) > MAX_FUNNY_RESPONSE_LENGTH
            ):
                raise ValueError(f"Invalid funny response for guild {guild_id}.")
            cleaned_response = response.strip()
            response_key = cleaned_response.casefold()
            if response_key in response_keys:
                raise ValueError(f"Duplicate funny response for guild {guild_id}.")
            clean_responses.append(cleaned_response)
            response_keys.add(response_key)

        terms = settings["terms"]
        if not isinstance(terms, list) or len(terms) > MAX_CONFIGURED_TERMS:
            raise ValueError(f"Invalid blocked-word list for guild {guild_id}.")
        clean_terms: list[str] = []
        term_keys: set[str] = set()
        for term in terms:
            try:
                cleaned = ProfanityTermStore._clean_term(term)
            except ProfanityTermError as error:
                raise ValueError(
                    f"Invalid blocked-word term for guild {guild_id}."
                ) from error
            key = ProfanityTermStore._key(cleaned)
            if key in term_keys:
                raise ValueError(f"Duplicate blocked-word term for guild {guild_id}.")
            clean_terms.append(cleaned)
            term_keys.add(key)

        normalized = deepcopy(DEFAULT_GUILD_SETTINGS)
        normalized.update(
            {
                "enabled": settings["enabled"],
                "terms": clean_terms,
                "action": settings["action"],
                "timeout_minutes": timeout,
                "warning_message": warning,
                "reply_channel_id": reply_channel_id,
                "funny_responses": clean_responses,
            }
        )
        for key in (
            "exempt_channel_ids",
            "exempt_role_ids",
            "exempt_user_ids",
        ):
            identifiers = settings[key]
            if (
                not isinstance(identifiers, list)
                or any(type(identifier) is not int or identifier <= 0 for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)
            ):
                raise ValueError(f"Invalid {key} for guild {guild_id}.")
            normalized[key] = list(identifiers)
        return normalized

    @classmethod
    def _validate_payload(cls, payload: object) -> dict[str, dict]:
        if not isinstance(payload, dict) or set(payload) != {"guilds"}:
            raise ValueError("Invalid custom blocked-word settings file.")
        guilds = payload["guilds"]
        if not isinstance(guilds, dict):
            raise ValueError("Invalid custom blocked-word guild settings.")
        validated: dict[str, dict] = {}
        for guild_id, settings in guilds.items():
            if (
                not isinstance(guild_id, str)
                or not guild_id.isascii()
                or not guild_id.isdecimal()
                or guild_id.startswith("0")
            ):
                raise ValueError("Invalid guild ID in custom blocked-word settings.")
            validated[guild_id] = cls._validate_settings(settings, guild_id)
        return validated

    def get(self, guild_id: int) -> dict:
        key = self._validate_guild_id(guild_id)
        with self._lock:
            return deepcopy(self._guilds.get(key, DEFAULT_GUILD_SETTINGS))

    def _save(self, guilds: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            os.chmod(temporary_name, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump({"guilds": guilds}, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_name, self.path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _mutate(self, guild_id: int, update: Callable[[dict], object]):
        key = self._validate_guild_id(guild_id)
        with self._lock:
            updated = deepcopy(self._guilds)
            settings = updated.setdefault(key, deepcopy(DEFAULT_GUILD_SETTINGS))
            result = update(settings)
            updated[key] = self._validate_settings(settings, key)
            self._save(updated)
            self._guilds = updated
            return deepcopy(updated[key]), result

    def add_term(self, guild_id: int, term: str) -> str:
        try:
            cleaned = ProfanityTermStore._clean_term(term)
        except ProfanityTermError:
            raise
        key = ProfanityTermStore._key(cleaned)

        def update(settings: dict) -> str:
            if any(
                ProfanityTermStore._key(existing) == key
                for existing in settings["terms"]
            ):
                raise ProfanityTermError("هذه الكلمة أو العبارة موجودة بالفعل.")
            if len(settings["terms"]) >= MAX_CONFIGURED_TERMS:
                raise ProfanityTermError("وصلت قائمة الكلمات إلى الحد الأقصى.")
            settings["terms"].append(cleaned)
            return cleaned

        _, added = self._mutate(guild_id, update)
        return added

    def remove_term(self, guild_id: int, term: str) -> str:
        cleaned = ProfanityTermStore._clean_term(term)
        key = ProfanityTermStore._key(cleaned)

        def update(settings: dict) -> str:
            existing = next(
                (
                    value
                    for value in settings["terms"]
                    if ProfanityTermStore._key(value) == key
                ),
                None,
            )
            if existing is None:
                raise ProfanityTermError("هذه الكلمة أو العبارة غير موجودة.")
            settings["terms"] = [
                value
                for value in settings["terms"]
                if ProfanityTermStore._key(value) != key
            ]
            return existing

        _, removed = self._mutate(guild_id, update)
        return removed

    def clear_terms(self, guild_id: int) -> int:
        def update(settings: dict) -> int:
            count = len(settings["terms"])
            settings["terms"] = []
            settings["enabled"] = False
            return count

        _, count = self._mutate(guild_id, update)
        return count

    def set_enabled(self, guild_id: int, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean.")

        def update(settings: dict) -> None:
            if enabled and not settings["terms"]:
                raise ProfanityTermError(
                    "أضف كلمة واحدة على الأقل قبل تفعيل المرشح."
                )
            settings["enabled"] = enabled

        settings, _ = self._mutate(guild_id, update)
        return settings

    def set_action(self, guild_id: int, action: str) -> dict:
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
            raise ValueError("Unsupported custom blocked-word action.")
        settings, _ = self._mutate(
            guild_id,
            lambda current: current.update(action=action),
        )
        return settings

    def set_timeout(self, guild_id: int, minutes: int) -> dict:
        if type(minutes) is not int or not 1 <= minutes <= MAX_TIMEOUT_MINUTES:
            raise ProfanityTermError(
                f"يجب أن تكون المهلة بين دقيقة واحدة و{MAX_TIMEOUT_MINUTES} دقيقة."
            )
        settings, _ = self._mutate(
            guild_id,
            lambda current: current.update(timeout_minutes=minutes),
        )
        return settings

    def set_warning_message(self, guild_id: int, message: str) -> dict:
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > MAX_WARNING_LENGTH
        ):
            raise ProfanityTermError(
                f"اكتب رسالة بين حرف واحد و{MAX_WARNING_LENGTH} حرفاً."
            )
        settings, _ = self._mutate(
            guild_id,
            lambda current: current.update(warning_message=message.strip()),
        )
        return settings

    def set_reply_channel(self, guild_id: int, channel_id: int | None) -> dict:
        if channel_id is not None:
            channel_id = self._positive_id(channel_id)
        settings, _ = self._mutate(
            guild_id,
            lambda current: current.update(reply_channel_id=channel_id),
        )
        return settings

    def add_funny_response(self, guild_id: int, response: str) -> int:
        if (
            not isinstance(response, str)
            or not response.strip()
            or len(response) > MAX_FUNNY_RESPONSE_LENGTH
        ):
            raise ProfanityTermError(
                f"اكتب رداً من حرف واحد إلى {MAX_FUNNY_RESPONSE_LENGTH} حرفاً."
            )
        cleaned = response.strip()
        response_key = cleaned.casefold()

        def update(settings: dict) -> int:
            if any(
                existing.casefold() == response_key
                for existing in settings["funny_responses"]
            ):
                raise ProfanityTermError("هذا الرد موجود بالفعل.")
            if len(settings["funny_responses"]) >= MAX_FUNNY_RESPONSES:
                raise ProfanityTermError(
                    f"وصلت قائمة الردود إلى الحد الأقصى ({MAX_FUNNY_RESPONSES})."
                )
            settings["funny_responses"].append(cleaned)
            return len(settings["funny_responses"])

        _, count = self._mutate(guild_id, update)
        return count

    def remove_funny_response(self, guild_id: int, index: int) -> str:
        if type(index) is not int or index < 1:
            raise ProfanityTermError("رقم الرد يجب أن يكون 1 أو أكبر.")

        def update(settings: dict) -> str:
            responses = settings["funny_responses"]
            if index > len(responses):
                raise ProfanityTermError(
                    f"رقم الرد يجب أن يكون بين 1 و{len(responses)}."
                )
            return responses.pop(index - 1)

        _, removed = self._mutate(guild_id, update)
        return removed

    def clear_funny_responses(self, guild_id: int) -> int:
        def update(settings: dict) -> int:
            count = len(settings["funny_responses"])
            settings["funny_responses"] = []
            return count

        _, count = self._mutate(guild_id, update)
        return count

    def reset_funny_responses(self, guild_id: int) -> int:
        def update(settings: dict) -> int:
            settings["funny_responses"] = list(DEFAULT_FUNNY_RESPONSES)
            return len(settings["funny_responses"])

        _, count = self._mutate(guild_id, update)
        return count

    def set_exemption(
        self,
        guild_id: int,
        kind: str,
        entity_id: int,
        exempt: bool,
    ) -> dict:
        if kind not in EXEMPTION_KEYS or type(exempt) is not bool:
            raise ValueError("Invalid custom blocked-word exemption.")
        entity_id = self._positive_id(entity_id)
        key = EXEMPTION_KEYS[kind]

        def update(settings: dict) -> None:
            values = settings[key]
            if exempt and entity_id not in values:
                values.append(entity_id)
            elif not exempt and entity_id in values:
                settings[key] = [value for value in values if value != entity_id]

        settings, _ = self._mutate(guild_id, update)
        return settings


class GuildBannedWordsSystem:
    """Apply one guild's custom blocked-word policy to each incoming message."""

    def __init__(
        self,
        store: GuildBannedWordsStore,
        developer_id: int,
        *,
        guild_allowed: Callable[[int], bool] | None = None,
    ):
        self.store = store
        self.developer_id = int(developer_id)
        self.guild_allowed = guild_allowed or (lambda _guild_id: True)

    def _is_exempt(self, message, settings: dict) -> bool:
        guild = message.guild
        author = message.author
        if author.id in settings["exempt_user_ids"]:
            return True
        if author.id == self.developer_id or author.id == getattr(guild, "owner_id", None):
            return True
        permissions = getattr(author, "guild_permissions", None)
        if (
            getattr(permissions, "administrator", False)
            or getattr(permissions, "manage_messages", False)
        ):
            return True
        exempt_roles = set(settings["exempt_role_ids"])
        if any(
            getattr(role, "id", None) in exempt_roles
            for role in getattr(author, "roles", ()) or ()
        ):
            return True
        channel = getattr(message, "channel", None)
        exempt_channels = set(settings["exempt_channel_ids"])
        return bool(
            getattr(channel, "id", None) in exempt_channels
            or getattr(channel, "parent_id", None) in exempt_channels
        )

    async def handle(self, message: discord.Message) -> bool:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if guild is None or author is None or getattr(author, "bot", False):
            return False
        try:
            if not self.guild_allowed(guild.id):
                return False
        except Exception:
            logger.exception(
                "Could not check guild access for custom blocked words."
            )
            return False

        settings = self.store.get(guild.id)
        if not settings["enabled"] or not settings["terms"]:
            return False
        if self._is_exempt(message, settings):
            return False
        matched = contains_bad_word(
            getattr(message, "content", ""),
            settings["terms"],
        )
        if matched is None:
            return False

        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning(
                "Could not delete a custom blocked-word message in guild %s, "
                "channel %s; the bot needs Manage Messages.",
                guild.id,
                getattr(message.channel, "id", "unknown"),
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected custom blocked-word deletion in guild %s.",
                guild.id,
            )

        timeout_error = None
        if settings["action"] == "timeout":
            try:
                await author.timeout(
                    timedelta(minutes=settings["timeout_minutes"]),
                    reason=f"Custom blocked-word filter: {matched}",
                )
            except discord.Forbidden:
                timeout_error = "تعذر تطبيق المهلة؛ تحقق من صلاحيات البوت وترتيب رتبته."
                logger.warning(
                    "Could not time out member %s for a custom blocked word in guild %s.",
                    author.id,
                    guild.id,
                )
            except discord.HTTPException:
                timeout_error = "رفض Discord تطبيق المهلة."
                logger.exception(
                    "Discord rejected a custom blocked-word timeout in guild %s.",
                    guild.id,
                )

        if settings["warning_message"] != DEFAULT_GUILD_SETTINGS["warning_message"]:
            warning = settings["warning_message"]
        else:
            responses = settings["funny_responses"] or DEFAULT_FUNNY_RESPONSES
            warning = random.choice(responses)
        if timeout_error:
            warning = f"{warning}\n{timeout_error}"
        reply_channel = message.channel
        configured_channel_id = settings["reply_channel_id"]
        if configured_channel_id is not None:
            get_channel = getattr(guild, "get_channel", None)
            configured_channel = (
                get_channel(configured_channel_id)
                if callable(get_channel)
                else None
            )
            configured_guild = getattr(configured_channel, "guild", guild)
            if (
                configured_channel is not None
                and getattr(configured_guild, "id", None) == guild.id
            ):
                reply_channel = configured_channel
            else:
                logger.warning(
                    "Configured blocked-word reply channel %s is unavailable in guild %s; "
                    "using the original channel.",
                    configured_channel_id,
                    guild.id,
                )

        try:
            await reply_channel.send(
                f"{author.mention} {warning}",
                delete_after=FUNNY_RESPONSE_DELETE_SECONDS,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.warning(
                "Could not send a custom blocked-word warning in guild %s.",
                guild.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected a custom blocked-word warning in guild %s.",
                guild.id,
            )
        return True


def setup_banned_words_commands(
    bot,
    store: GuildBannedWordsStore,
    developer_id: int,
    *,
    guild_allowed: Callable[[int], bool] | None = None,
):
    """Register owner/developer-only commands for each server's word filter."""
    existing = getattr(bot, "_banned_words_command_group", None)
    if existing is not None:
        return existing

    group = app_commands.Group(
        name="banned",
        description="إدارة كلمات محظورة لهذا السيرفر فقط",
    )

    async def authorize(interaction: discord.Interaction) -> bool:
        guild = getattr(interaction, "guild", None)
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر متاح داخل السيرفر فقط.",
                ephemeral=True,
            )
            return False
        try:
            allowed = (guild_allowed or (lambda _guild_id: True))(guild.id)
        except Exception:
            logger.exception(
                "Could not check guild access for banned-word configuration."
            )
            allowed = False
        if not allowed:
            await interaction.response.send_message(
                "هذا السيرفر غير موجود في قائمة السيرفرات المسموح بها.",
                ephemeral=True,
            )
            return False
        if (
            interaction.user.id == developer_id
            or interaction.user.id == getattr(guild, "owner_id", None)
        ):
            return True
        await interaction.response.send_message(
            "إدارة الكلمات المحظورة متاحة لمطور البوت أو لصاحب السيرفر فقط.",
            ephemeral=True,
        )
        return False

    def guild_id(interaction: discord.Interaction) -> int:
        return interaction.guild.id

    @group.command(name="add", description="إضافة كلمة أو عبارة لهذا السيرفر")
    @app_commands.describe(term="إهانة أو لفظ مسيء واضح؛ يدعم العبارات")
    @app_commands.guild_only()
    async def add_term(interaction: discord.Interaction, term: str) -> None:
        if not await authorize(interaction):
            return
        try:
            added = store.add_term(guild_id(interaction), term)
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception(
                "Could not save custom blocked words for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ القائمة؛ لم تتغير الإعدادات."
        else:
            response = (
                f"أُضيفت «{discord.utils.escape_markdown(added)}» لهذه القائمة. "
                "لن يبدأ الحذف حتى تفعيل المرشح."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="remove", description="إزالة كلمة أو عبارة من هذا السيرفر")
    @app_commands.describe(term="الكلمة أو العبارة المطلوب إزالتها")
    @app_commands.guild_only()
    async def remove_term(interaction: discord.Interaction, term: str) -> None:
        if not await authorize(interaction):
            return
        try:
            removed = store.remove_term(guild_id(interaction), term)
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception(
                "Could not save custom blocked words for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ القائمة؛ لم تتغير الإعدادات."
        else:
            response = f"أُزيلت «{discord.utils.escape_markdown(removed)}»."
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="list", description="عرض قائمة الكلمات الخاصة بهذا السيرفر")
    @app_commands.guild_only()
    async def list_terms(interaction: discord.Interaction) -> None:
        if not await authorize(interaction):
            return
        terms = store.get(guild_id(interaction))["terms"]
        content = "\n".join(
            f"• {discord.utils.escape_markdown(term)}" for term in terms
        )
        await interaction.response.send_message(
            f"قائمة الكلمات المحظورة ({len(terms)}):\n{content or 'لا توجد كلمات.'}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="clear", description="حذف كل الكلمات بعد تأكيد صريح")
    @app_commands.describe(confirm="اختر نعم لحذف القائمة كاملةً")
    @app_commands.guild_only()
    async def clear_terms(
        interaction: discord.Interaction,
        confirm: bool = False,
    ) -> None:
        if not await authorize(interaction):
            return
        if not confirm:
            count = len(store.get(guild_id(interaction))["terms"])
            await interaction.response.send_message(
                f"سيؤدي هذا إلى حذف {count} كلمة وتعطيل المرشح. "
                "لم يتغير شيء؛ أعد الأمر واختر confirm: نعم للمتابعة.",
                ephemeral=True,
            )
            return
        try:
            count = store.clear_terms(guild_id(interaction))
        except OSError:
            logger.exception(
                "Could not clear custom blocked words for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ التغيير؛ بقيت القائمة كما هي."
        else:
            response = f"حُذفت {count} كلمة وعُطّل المرشح لهذا السيرفر."
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="enable", description="تفعيل قائمة هذا السيرفر")
    @app_commands.guild_only()
    async def enable(interaction: discord.Interaction) -> None:
        if not await authorize(interaction):
            return
        try:
            store.set_enabled(guild_id(interaction), True)
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception(
                "Could not enable custom blocked words for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ التغيير؛ لم يتغير الإعداد."
        else:
            response = "تم تفعيل قائمة الكلمات لهذا السيرفر."
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="disable", description="إيقاف قائمة هذا السيرفر")
    @app_commands.guild_only()
    async def disable(interaction: discord.Interaction) -> None:
        if not await authorize(interaction):
            return
        try:
            store.set_enabled(guild_id(interaction), False)
        except OSError:
            logger.exception(
                "Could not disable custom blocked words for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ التغيير؛ لم يتغير الإعداد."
        else:
            response = "تم إيقاف قائمة الكلمات لهذا السيرفر."
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="action", description="اختيار الحذف أو الحذف مع مهلة")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="حذف مع التنبيه", value="delete"),
            app_commands.Choice(name="حذف وتنبيه مع مهلة", value="timeout"),
        ]
    )
    @app_commands.guild_only()
    async def set_action(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ) -> None:
        if not await authorize(interaction):
            return
        try:
            store.set_action(guild_id(interaction), action.value)
        except OSError:
            logger.exception(
                "Could not save custom blocked-word action for guild %s.",
                interaction.guild.id,
            )
            await interaction.response.send_message(
                "تعذر حفظ التغيير؛ لم يتغير الإجراء.",
                ephemeral=True,
            )
            return
        response = (
            "سيُحذف المحتوى المخالف ويُرسل التنبيه."
            if action.value == "delete"
            else "سيُحذف المحتوى المخالف وتُطبّق المهلة مع إرسال التنبيه."
        )
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(name="timeout", description="تحديد مدة المهلة من دقيقة إلى 24 ساعة")
    @app_commands.describe(minutes="المدة بالدقائق؛ من 1 إلى 1440")
    @app_commands.guild_only()
    async def set_timeout(
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
    ) -> None:
        if not await authorize(interaction):
            return
        try:
            store.set_timeout(guild_id(interaction), minutes)
        except (ProfanityTermError, OSError) as error:
            if isinstance(error, OSError):
                logger.exception(
                    "Could not save custom blocked-word timeout for guild %s.",
                    interaction.guild.id,
                )
                response = "تعذر حفظ التغيير؛ لم يتغير الإعداد."
            else:
                response = str(error)
        else:
            response = f"تم ضبط المهلة على {minutes} دقيقة."
        await interaction.response.send_message(response, ephemeral=True)

    @group.command(
        name="reply-channel",
        description="تحديد قناة ردود الكلمات أو العودة إلى القناة الأصلية",
    )
    @app_commands.describe(
        channel="اتركه فارغاً للرد في قناة الرسالة المخالفة نفسها"
    )
    @app_commands.guild_only()
    async def set_reply_channel(
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await authorize(interaction):
            return
        if channel is not None and channel.guild.id != interaction.guild.id:
            await interaction.response.send_message(
                "يجب أن تكون القناة من هذا السيرفر.",
                ephemeral=True,
            )
            return
        try:
            store.set_reply_channel(
                guild_id(interaction),
                channel.id if channel is not None else None,
            )
        except OSError:
            logger.exception(
                "Could not save custom blocked-word reply channel for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ التغيير؛ لم تتغير قناة الردود."
        else:
            response = (
                f"ستُرسل تنبيهات الكلمات المحظورة في {channel.mention}."
                if channel is not None
                else "أُلغي تحديد القناة؛ ستُرسل التنبيهات في قناة الرسالة المخالفة."
            )
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="message", description="تغيير رسالة التنبيه")
    @app_commands.describe(message="رسالة قصيرة تظهر بعد حذف المخالفة")
    @app_commands.guild_only()
    async def set_message(
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if not await authorize(interaction):
            return
        try:
            store.set_warning_message(guild_id(interaction), message)
        except ProfanityTermError as error:
            response = str(error)
        except OSError:
            logger.exception(
                "Could not save custom blocked-word warning for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ التغيير؛ لم تتغير الرسالة."
        else:
            response = "تم تحديث رسالة التنبيه."
        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def configure_exemption(
        interaction: discord.Interaction,
        kind: str,
        operation: app_commands.Choice[str],
        entity,
    ) -> None:
        if not await authorize(interaction):
            return
        entity_guild = getattr(entity, "guild", None)
        if entity_guild is None or entity_guild.id != interaction.guild.id:
            await interaction.response.send_message(
                "يجب أن يكون العنصر من هذا السيرفر.",
                ephemeral=True,
            )
            return
        try:
            store.set_exemption(
                guild_id(interaction),
                kind,
                entity.id,
                operation.value == "add",
            )
        except OSError:
            logger.exception(
                "Could not save custom blocked-word exemption for guild %s.",
                interaction.guild.id,
            )
            response = "تعذر حفظ التغيير؛ لم تتغير الاستثناءات."
        else:
            verb = "أُضيف" if operation.value == "add" else "أُزيل"
            response = f"{verb} الاستثناء من قائمة هذا السيرفر."
        await interaction.response.send_message(response, ephemeral=True)

    operations = [
        app_commands.Choice(name="إضافة إعفاء", value="add"),
        app_commands.Choice(name="إزالة إعفاء", value="remove"),
    ]

    @group.command(name="channel", description="إضافة قناة إلى الاستثناءات أو إزالتها")
    @app_commands.choices(operation=operations.copy())
    @app_commands.guild_only()
    async def configure_channel(
        interaction: discord.Interaction,
        operation: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        await configure_exemption(interaction, "channel", operation, channel)

    @group.command(name="role", description="إضافة رتبة إلى الاستثناءات أو إزالتها")
    @app_commands.choices(operation=operations.copy())
    @app_commands.guild_only()
    async def configure_role(
        interaction: discord.Interaction,
        operation: app_commands.Choice[str],
        role: discord.Role,
    ) -> None:
        await configure_exemption(interaction, "role", operation, role)

    @group.command(name="user", description="إضافة عضو إلى الاستثناءات أو إزالته")
    @app_commands.choices(operation=operations.copy())
    @app_commands.guild_only()
    async def configure_user(
        interaction: discord.Interaction,
        operation: app_commands.Choice[str],
        member: discord.Member,
    ) -> None:
        await configure_exemption(interaction, "user", operation, member)

    @group.command(name="show", description="عرض إعدادات القائمة لهذا السيرفر")
    @app_commands.guild_only()
    async def show(interaction: discord.Interaction) -> None:
        if not await authorize(interaction):
            return
        settings = store.get(guild_id(interaction))
        action = (
            "حذف مع التنبيه"
            if settings["action"] == "delete"
            else f"حذف وتنبيه مع مهلة {settings['timeout_minutes']} دقيقة"
        )
        embed = discord.Embed(
            title="إعدادات الكلمات المحظورة",
            description=f"في {discord.utils.escape_markdown(interaction.guild.name)}",
            color=0xE74C3C,
        )
        embed.add_field(
            name="الحالة",
            value="مفعّلة" if settings["enabled"] else "متوقفة",
            inline=True,
        )
        embed.add_field(name="عدد الكلمات", value=str(len(settings["terms"])), inline=True)
        embed.add_field(name="الإجراء", value=action, inline=False)
        embed.add_field(
            name="الاستثناءات",
            value=(
                f"قنوات: {len(settings['exempt_channel_ids'])} • "
                f"رتب: {len(settings['exempt_role_ids'])} • "
                f"أعضاء: {len(settings['exempt_user_ids'])}"
            ),
            inline=False,
        )
        embed.add_field(
            name="قناة التنبيه",
            value=(
                f"<#{settings['reply_channel_id']}>"
                if settings["reply_channel_id"] is not None
                else "قناة الرسالة المخالفة"
            ),
            inline=False,
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot.tree.add_command(group)
    bot._banned_words_command_group = group
    return group


def setup_banned_words(
    bot,
    store: GuildBannedWordsStore,
    developer_id: int,
    *,
    guild_allowed: Callable[[int], bool] | None = None,
) -> GuildBannedWordsSystem:
    system = getattr(bot, "_guild_banned_words_system", None)
    if system is not None:
        return system
    system = GuildBannedWordsSystem(
        store,
        developer_id,
        guild_allowed=guild_allowed,
    )
    bot._guild_banned_words_system = system
    setup_banned_words_commands(
        bot,
        store,
        developer_id,
        guild_allowed=guild_allowed,
    )
    return system