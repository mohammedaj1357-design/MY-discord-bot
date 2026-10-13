"""Opt-in protection against repeated mention messages in a Discord server."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import timedelta
from typing import Callable

import discord

from discord_logs import (
    MENTION_SPAM_THRESHOLD,
    MENTION_SPAM_WINDOW_SECONDS,
    DiscordLogSystem,
    DiscordLogsStore,
)


logger = logging.getLogger(__name__)
MENTION_SPAM_TIMEOUT = timedelta(minutes=10)
WARNING_SECONDS = 10


class MentionSpamProtection:
    """Delete only the triggering mention messages and time out repeat offenders."""

    def __init__(
        self,
        store: DiscordLogsStore,
        logs: DiscordLogSystem,
        developer_id: int,
        *,
        guild_allowed: Callable[[int], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.store = store
        self.logs = logs
        self.developer_id = int(developer_id)
        self.guild_allowed = guild_allowed or (lambda _guild_id: True)
        self._clock = clock or time.monotonic
        self._recent: dict[tuple[int, int], deque[tuple[float, discord.Message]]] = {}
        self._last_cleanup = self._clock()

    def _prune_expired(self, now: float) -> None:
        if now - self._last_cleanup < MENTION_SPAM_WINDOW_SECONDS:
            return
        self._last_cleanup = now
        for key, messages in list(self._recent.items()):
            while messages and now - messages[0][0] > MENTION_SPAM_WINDOW_SECONDS:
                messages.popleft()
            if not messages:
                self._recent.pop(key, None)

    @staticmethod
    def _is_mention_message(message: discord.Message) -> bool:
        content = (getattr(message, "content", "") or "").casefold()
        return bool(
            getattr(message, "mentions", ())
            or getattr(message, "role_mentions", ())
            or "@everyone" in content
            or "@here" in content
        )

    async def _delete_message(self, message: discord.Message) -> int:
        try:
            await message.delete()
            return 1
        except discord.NotFound:
            return 0
        except discord.Forbidden:
            logger.warning(
                "Could not delete mention-spam message %s in guild %s; "
                "the bot needs Manage Messages.",
                getattr(message, "id", "unknown"),
                getattr(getattr(message, "guild", None), "id", "unknown"),
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected deletion of a mention-spam message."
            )
        except Exception:
            logger.exception("Unexpected failure deleting a mention-spam message.")
        return 0

    async def handle(self, message: discord.Message) -> bool:
        """Moderate after five mention messages from one member within ten seconds."""
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if guild is None or author is None or getattr(author, "bot", False):
            return False
        try:
            allowed_guild = self.guild_allowed(guild.id)
        except Exception:
            logger.exception("Could not check guild access for mention-spam moderation.")
            return False
        if not allowed_guild:
            return False
        if author.id == self.developer_id:
            return False
        permissions = getattr(author, "guild_permissions", None)
        if getattr(permissions, "administrator", False):
            return False
        settings = self.store.get(guild.id)
        if not settings["spam_enabled"]:
            return False
        if not self._is_mention_message(message):
            return False
        exempt_role_ids = set(settings["spam_exempt_role_ids"])
        if any(
            getattr(role, "id", None) in exempt_role_ids
            for role in (getattr(author, "roles", ()) or ())
        ):
            return False
        exempt_channel_ids = set(settings["spam_exempt_channel_ids"])
        channel = getattr(message, "channel", None)
        if (
            getattr(channel, "id", None) in exempt_channel_ids
            or getattr(channel, "parent_id", None) in exempt_channel_ids
        ):
            return False

        now = self._clock()
        self._prune_expired(now)
        key = (guild.id, author.id)
        recent = self._recent.setdefault(key, deque())
        while recent and now - recent[0][0] > MENTION_SPAM_WINDOW_SECONDS:
            recent.popleft()
        recent.append((now, message))
        if len(recent) < MENTION_SPAM_THRESHOLD:
            return False

        violating_messages = [item[1] for item in recent]
        self._recent.pop(key, None)
        deletion_results = await asyncio.gather(
            *(self._delete_message(item) for item in violating_messages)
        )
        deleted_count = sum(deletion_results)

        timeout_applied = False
        timeout_error = None
        try:
            await author.timeout(
                MENTION_SPAM_TIMEOUT,
                reason="Repeated mention spam",
            )
            timeout_applied = True
        except discord.Forbidden:
            timeout_error = "تحقق من صلاحية Moderate Members وترتيب رتبة البوت"
            logger.warning(
                "Could not time out member %s in guild %s; check Moderate Members "
                "and role hierarchy.",
                author.id,
                guild.id,
            )
        except discord.HTTPException:
            timeout_error = "رفض Discord تطبيق المهلة"
            logger.exception(
                "Discord rejected mention-spam timeout for member %s in guild %s.",
                author.id,
                guild.id,
            )

        try:
            await self.logs.notify_spam(
                guild,
                author,
                deleted_count=deleted_count,
                timeout_applied=timeout_applied,
                timeout_error=timeout_error,
            )
        except Exception:
            logger.exception("Could not send mention-spam alert for guild %s.", guild.id)

        outcome = (
            "أُسكت العضو 10 دقائق."
            if timeout_applied
            else f"تعذر تطبيق المهلة ({timeout_error or 'خطأ غير معروف'})."
        )
        try:
            await message.channel.send(
                f"رُصد منشن سبام: حُذف {deleted_count} من الرسائل المرصودة. {outcome}",
                delete_after=WARNING_SECONDS,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            logger.warning(
                "Could not send mention-spam notice in guild %s, channel %s.",
                guild.id,
                getattr(message.channel, "id", "unknown"),
            )
        except discord.HTTPException:
            logger.exception(
                "Discord rejected mention-spam notice in guild %s.",
                guild.id,
            )
        return True