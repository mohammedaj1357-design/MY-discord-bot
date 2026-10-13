"""Consent-based invite notifications for Discord server members."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import discord
from discord import app_commands


logger = logging.getLogger(__name__)

_INVITE_CODE = re.compile(r"^[A-Za-z0-9-]{2,32}$")
_INVITE_HOSTS = {
    "discord.gg",
    "www.discord.gg",
    "discord.com",
    "www.discord.com",
    "discordapp.com",
    "www.discordapp.com",
}
_DELIVERY_HISTORY_RETENTION_SECONDS = 365 * 24 * 60 * 60


def normalize_invite_target(value: str) -> str:
    """Accept a Discord invite code or URL and return its canonical URL."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("أدخل رمز دعوة Discord أو رابطها.")

    target = value.strip()
    if "://" not in target and ("/" in target or "." in target):
        target = f"https://{target}"

    if "://" not in target:
        code = target
    else:
        try:
            parsed = urlsplit(target)
            host = (parsed.hostname or "").lower()
            has_invalid_port = parsed.port is not None
        except ValueError as error:
            raise ValueError("رابط الدعوة غير صالح.") from error

        if (
            parsed.scheme != "https"
            or host not in _INVITE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or has_invalid_port
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("استخدم رابط دعوة Discord صالحًا فقط.")

        segments = [segment for segment in parsed.path.split("/") if segment]
        if host.endswith("discord.gg"):
            if len(segments) != 1:
                raise ValueError("رابط دعوة Discord غير صالح.")
            code = segments[0]
        else:
            if len(segments) != 2 or segments[0] != "invite":
                raise ValueError("رابط دعوة Discord غير صالح.")
            code = segments[1]

    if not _INVITE_CODE.fullmatch(code):
        raise ValueError("رمز الدعوة يجب أن يحتوي على 2 إلى 32 حرفًا أو رقمًا.")
    return f"https://discord.gg/{code}"


def _id_key(value: int, field: str) -> str:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive Discord ID")
    return str(value)


def _stored_id(value, field: str) -> str:
    if not isinstance(value, str) or not value.isdecimal() or int(value) <= 0:
        raise ValueError(f"Invalid {field} in invite notification data")
    return value


def _remove_member_subscription(
    store: "CollectSubscriptionStore",
    guild_id: int,
    user_id: int,
) -> bool:
    try:
        removed = store.unsubscribe(guild_id, user_id)
    except (ValueError, OSError):
        logger.exception(
            "Could not remove invite-notification subscription for member "
            "%s in guild %s.",
            user_id,
            guild_id,
        )
        return False
    if removed:
        logger.info(
            "Removed invite-notification subscription for departed member "
            "%s in guild %s.",
            user_id,
            guild_id,
        )
    return removed


def _guild_label(bot, guild_id: int) -> str:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return f"سيرفر غير متاح ({guild_id})"
    name = (
        str(getattr(guild, "name", "") or "سيرفر بلا اسم")
        .replace("@", "@\u200b")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return f"{name[:60]} ({guild_id})"


class CollectResendConfirmationView(discord.ui.View):
    """Require the developer to confirm a repeat invite broadcast."""

    def __init__(self, requester_id: int, recipient_count: int, on_confirm):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.recipient_count = recipient_count
        self.on_confirm = on_confirm
        self.confirmation_message = None
        self._state = "pending"

    async def _check_requester(self, interaction) -> bool:
        if getattr(getattr(interaction, "user", None), "id", None) != self.requester_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح للمطور الذي بدأ العملية فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if self._state != "pending":
            await interaction.response.send_message(
                "انتهى هذا التأكيد بالفعل؛ لم تُرسل رسائل إضافية.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(
        label="تأكيد إعادة الإرسال",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(self, interaction: discord.Interaction, _button) -> None:
        if not await self._check_requester(interaction):
            return
        self._state = "confirmed"
        self._disable_controls()
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"تم تأكيد إعادة الإرسال إلى {self.recipient_count} اشتراكًا. "
                "جارٍ التحقق من الاشتراكات وإرسال الدعوة."
            ),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.on_confirm(interaction)

    @discord.ui.button(
        label="إلغاء",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(self, interaction: discord.Interaction, _button) -> None:
        if not await self._check_requester(interaction):
            return
        self._state = "cancelled"
        self._disable_controls()
        self.stop()
        await interaction.response.edit_message(
            content="تم إلغاء إعادة الإرسال؛ لم تُرسل أي رسائل.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        if self._state != "pending":
            return
        self._state = "expired"
        self._disable_controls()
        if self.confirmation_message is None:
            return
        try:
            await self.confirmation_message.edit(
                content="انتهت مهلة التأكيد؛ لم تُرسل أي رسائل.",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception("Could not mark invite resend confirmation expired.")


class CollectSubscriptionStore:
    """Persist invite subscriptions and recent per-member delivery attempts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, list[str]] = {}
        self._delivery_history: dict[tuple[str, str, str], int] = {}
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if (
            not isinstance(data, dict)
            or set(data)
            not in (
                {"schema_version", "guilds"},
                {"schema_version", "guilds", "delivery_history"},
            )
            or type(data["schema_version"]) is not int
            or data["schema_version"] not in (1, 2)
            or not isinstance(data["guilds"], dict)
            or (
                data["schema_version"] == 1
                and set(data) != {"schema_version", "guilds"}
            )
            or (
                data["schema_version"] == 2
                and set(data)
                != {"schema_version", "guilds", "delivery_history"}
            )
        ):
            raise ValueError(
                f"Invalid invite notification data at {self.path}"
            )

        for raw_guild_id, raw_user_ids in data["guilds"].items():
            guild_id = _stored_id(raw_guild_id, "guild ID")
            if not isinstance(raw_user_ids, list):
                raise ValueError(
                    f"Invalid subscribers for guild {guild_id} at {self.path}"
                )
            user_ids = [
                _stored_id(raw_user_id, "user ID")
                for raw_user_id in raw_user_ids
            ]
            if len(user_ids) != len(set(user_ids)):
                raise ValueError(
                    f"Duplicate subscriber IDs for guild {guild_id}"
                )
            self._guilds[guild_id] = user_ids

        if data["schema_version"] == 2:
            history = data["delivery_history"]
            if not isinstance(history, list):
                raise ValueError(
                    f"Invalid invite delivery history at {self.path}"
                )
            for entry in history:
                if (
                    not isinstance(entry, dict)
                    or set(entry)
                    != {"guild_id", "user_id", "invite_url", "attempted_at"}
                    or type(entry["attempted_at"]) is not int
                    or entry["attempted_at"] < 0
                ):
                    raise ValueError(
                        f"Invalid invite delivery history at {self.path}"
                    )
                guild_id = _stored_id(entry["guild_id"], "guild ID")
                user_id = _stored_id(entry["user_id"], "user ID")
                try:
                    invite_url = normalize_invite_target(entry["invite_url"])
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"Invalid invite delivery history at {self.path}"
                    ) from error
                if invite_url != entry["invite_url"]:
                    raise ValueError(
                        f"Invalid invite delivery history at {self.path}"
                    )
                key = (guild_id, user_id, invite_url)
                if key in self._delivery_history:
                    raise ValueError(
                        f"Duplicate invite delivery history at {self.path}"
                    )
                self._delivery_history[key] = entry["attempted_at"]

    def _save(
        self,
        guilds: dict[str, list[str]],
        delivery_history: dict[tuple[str, str, str], int] | None = None,
    ) -> None:
        history = (
            self._delivery_history
            if delivery_history is None
            else delivery_history
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = file.name
                json.dump(
                    {
                        "schema_version": 2,
                        "guilds": {
                            guild_id: sorted(user_ids, key=int)
                            for guild_id, user_ids in sorted(
                                guilds.items(), key=lambda item: int(item[0])
                            )
                        },
                        "delivery_history": [
                            {
                                "guild_id": guild_id,
                                "user_id": user_id,
                                "invite_url": invite_url,
                                "attempted_at": attempted_at,
                            }
                            for (
                                guild_id,
                                user_id,
                                invite_url,
                            ), attempted_at in sorted(history.items())
                        ],
                    },
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                os.unlink(temporary_path)

    def _prune_delivery_history_locked(self, now: int) -> None:
        cutoff = now - _DELIVERY_HISTORY_RETENTION_SECONDS
        updated = {
            key: attempted_at
            for key, attempted_at in self._delivery_history.items()
            if attempted_at > cutoff
        }
        if len(updated) != len(self._delivery_history):
            self._save(self._guilds, updated)
            self._delivery_history = updated

    def record_invite_delivery_attempt(
        self,
        guild_id: int,
        user_id: int,
        invite_url: str,
        *,
        allow_repeat: bool = False,
    ) -> bool:
        """Persist an attempt before sending; return False for a recent repeat."""
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        invite_key = normalize_invite_target(invite_url)
        key = (guild_key, user_key, invite_key)
        with self._lock:
            now = int(time.time())
            self._prune_delivery_history_locked(now)
            if key in self._delivery_history and not allow_repeat:
                return False
            updated = dict(self._delivery_history)
            updated[key] = now
            self._save(self._guilds, updated)
            self._delivery_history = updated
            return True

    def has_invite_delivery_attempt(
        self,
        guild_id: int,
        user_id: int,
        invite_url: str,
    ) -> bool:
        key = (
            _id_key(guild_id, "guild_id"),
            _id_key(user_id, "user_id"),
            normalize_invite_target(invite_url),
        )
        with self._lock:
            self._prune_delivery_history_locked(int(time.time()))
            return key in self._delivery_history

    def forget_invite_delivery_attempt(
        self,
        guild_id: int,
        user_id: int,
        invite_url: str,
    ) -> bool:
        """Remove a known-undelivered attempt so a later broadcast can retry."""
        key = (
            _id_key(guild_id, "guild_id"),
            _id_key(user_id, "user_id"),
            normalize_invite_target(invite_url),
        )
        with self._lock:
            self._prune_delivery_history_locked(int(time.time()))
            if key not in self._delivery_history:
                return False
            updated = dict(self._delivery_history)
            updated.pop(key)
            self._save(self._guilds, updated)
            self._delivery_history = updated
            return True

    def subscribe(self, guild_id: int, user_id: int) -> bool:
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            current = self._guilds.get(guild_key, [])
            if user_key in current:
                return False
            updated = dict(self._guilds)
            updated[guild_key] = [*current, user_key]
            self._save(updated)
            self._guilds = updated
            return True

    def unsubscribe(self, guild_id: int, user_id: int) -> bool:
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            current = self._guilds.get(guild_key, [])
            if user_key not in current:
                return False
            updated = dict(self._guilds)
            remaining = [saved_id for saved_id in current if saved_id != user_key]
            if remaining:
                updated[guild_key] = remaining
            else:
                updated.pop(guild_key, None)
            self._save(updated)
            self._guilds = updated
            return True

    def unsubscribe_from_all_guilds(self, user_id: int) -> int:
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            updated = {}
            removed = 0
            for guild_key, user_ids in self._guilds.items():
                remaining = [saved_id for saved_id in user_ids if saved_id != user_key]
                removed += len(user_ids) - len(remaining)
                if remaining:
                    updated[guild_key] = remaining
            if removed:
                self._save(updated)
                self._guilds = updated
            return removed

    def subscriber_ids(self, guild_id: int) -> list[int]:
        guild_key = _id_key(guild_id, "guild_id")
        with self._lock:
            return sorted(
                (int(user_id) for user_id in self._guilds.get(guild_key, []))
            )

    def is_subscribed(self, guild_id: int, user_id: int) -> bool:
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            return user_key in self._guilds.get(guild_key, [])

    def guild_ids_for_user(self, user_id: int) -> list[int]:
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            return sorted(
                (
                    int(guild_id)
                    for guild_id, user_ids in self._guilds.items()
                    if user_key in user_ids
                )
            )

    def clear_guild(self, guild_id: int) -> int:
        guild_key = _id_key(guild_id, "guild_id")
        with self._lock:
            current = self._guilds.get(guild_key)
            if current is None:
                return 0
            updated = dict(self._guilds)
            updated.pop(guild_key)
            self._save(updated)
            self._guilds = updated
            return len(current)


def setup_collect_commands(
    bot,
    store: CollectSubscriptionStore,
    developer_id: int,
) -> None:
    if getattr(bot, "_collect_commands_registered", False):
        return
    delivery_lock = asyncio.Lock()

    async def deliver_invite_batch(
        interaction,
        guild,
        guild_id: int,
        invite_url: str,
        *,
        resend: bool,
        recipient_ids: list[int] | None = None,
    ) -> None:
        sent = 0
        failed = 0
        skipped = 0
        async with delivery_lock:
            user_ids = (
                store.subscriber_ids(guild_id)
                if recipient_ids is None
                else list(recipient_ids)
            )
            server_name = (
                str(guild.name)
                .replace("@", "@\u200b")
                .replace("\r", " ")
                .replace("\n", " ")
            )
            message = (
                f"دعوة سيرفر Discord من {server_name}:\n"
                f"{invite_url}\n\n"
                "وصلتك الرسالة لأنك اشتركت طوعًا في رسائل الدعوات. "
                "لإيقافها استخدم `/collect_opt_out`."
            )
            for user_id in user_ids:
                if not store.is_subscribed(guild_id, user_id):
                    continue
                try:
                    had_prior_attempt = store.has_invite_delivery_attempt(
                        guild_id,
                        user_id,
                        invite_url,
                    )
                except (ValueError, OSError):
                    logger.exception(
                        "Could not read invite delivery history for member "
                        "%s in guild %s; skipping delivery.",
                        user_id,
                        guild_id,
                    )
                    failed += 1
                    continue
                if had_prior_attempt and not resend:
                    skipped += 1
                    continue
                try:
                    await guild.fetch_member(user_id)
                except discord.NotFound:
                    _remove_member_subscription(store, guild_id, user_id)
                    continue
                except discord.HTTPException as error:
                    logger.warning(
                        "Could not verify invite subscriber %s in guild %s "
                        "(HTTP status %s); skipping delivery.",
                        user_id,
                        guild_id,
                        error.status,
                    )
                    failed += 1
                    continue
                if not store.is_subscribed(guild_id, user_id):
                    continue
                try:
                    user = bot.get_user(user_id)
                    if user is None:
                        user = await bot.fetch_user(user_id)
                    if not store.is_subscribed(guild_id, user_id):
                        continue
                    try:
                        recorded = store.record_invite_delivery_attempt(
                            guild_id,
                            user_id,
                            invite_url,
                            allow_repeat=resend,
                        )
                    except (ValueError, OSError):
                        logger.exception(
                            "Could not persist invite delivery history for "
                            "member %s in guild %s; skipping delivery.",
                            user_id,
                            guild_id,
                        )
                        failed += 1
                        continue
                    if not recorded:
                        skipped += 1
                        continue
                    await user.send(
                        message,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    sent += 1
                except discord.NotFound:
                    _remove_member_subscription(store, guild_id, user_id)
                    if not had_prior_attempt:
                        try:
                            store.forget_invite_delivery_attempt(
                                guild_id,
                                user_id,
                                invite_url,
                            )
                        except (ValueError, OSError):
                            logger.exception(
                                "Could not clear failed invite delivery "
                                "history for member %s in guild %s.",
                                user_id,
                                guild_id,
                            )
                    failed += 1
                except discord.Forbidden:
                    if not had_prior_attempt:
                        try:
                            store.forget_invite_delivery_attempt(
                                guild_id,
                                user_id,
                                invite_url,
                            )
                        except (ValueError, OSError):
                            logger.exception(
                                "Could not clear failed invite delivery "
                                "history for member %s in guild %s.",
                                user_id,
                                guild_id,
                            )
                    failed += 1
                except discord.HTTPException as error:
                    logger.warning(
                        "Could not deliver a collect invitation from guild %s "
                        "(HTTP status %s).",
                        guild_id,
                        error.status,
                    )
                    failed += 1
                except Exception:
                    logger.exception(
                        "Unexpected error delivering a collect invitation "
                        "from guild %s.",
                        guild_id,
                    )
                    failed += 1

        total = sent + failed
        await interaction.followup.send(
            f"انتهى الإرسال: وصل الرابط إلى {sent} من أصل {total} مشترك. "
            f"تعذر الإرسال إلى {failed}، وتم تخطي {skipped} "
            "لأنهم تلقوا الدعوة نفسها مؤخرًا.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.listen("on_member_remove")
    async def remove_departed_member_subscription(
        member: discord.Member,
    ) -> None:
        _remove_member_subscription(store, member.guild.id, member.id)

    @bot.tree.command(
        name="collect_opt_in",
        description="الاشتراك طوعًا في رسائل دعوات السيرفر عبر الخاص",
    )
    @app_commands.guild_only()
    async def collect_opt_in(interaction: discord.Interaction):
        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            await interaction.response.send_message(
                "استخدم هذا الأمر داخل السيرفر الذي تريد استقبال دعواته.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not getattr(bot.intents, "members", False):
            await interaction.response.send_message(
                "يتطلب الاشتراك تفعيل Server Members Intent في Discord "
                "Developer Portal وضبط DISCORD_MEMBERS_INTENT=1 في إعدادات Replit.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            added = store.subscribe(guild_id, interaction.user.id)
        except (ValueError, OSError):
            logger.exception("Could not save invite-notification opt-in.")
            await interaction.response.send_message(
                "تعذر حفظ الاشتراك الآن. حاول مرة أخرى لاحقًا.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        message = (
            "تم اشتراكك. ستصلك دعوات يرسلها المطور من هذا السيرفر عبر الخاص. "
            "لإيقافها استخدم `/collect_opt_out` داخل السيرفر أو من الخاص."
            if added
            else "أنت مشترك بالفعل في رسائل الدعوات من هذا السيرفر."
        )
        await interaction.response.send_message(
            message,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="collect_subscriptions",
        description="عرض السيرفرات التي اشتركت فيها لرسائل الدعوات",
    )
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    @app_commands.describe(page="رقم الصفحة عند وجود اشتراكات كثيرة")
    async def collect_subscriptions(
        interaction: discord.Interaction,
        page: app_commands.Range[int, 1, 1000] = 1,
    ):
        guild_ids = store.guild_ids_for_user(interaction.user.id)
        if not guild_ids:
            await interaction.response.send_message(
                "ليس لديك اشتراكات نشطة في رسائل الدعوات.",
                ephemeral=getattr(interaction, "guild_id", None) is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        page_size = 20
        page_count = (len(guild_ids) + page_size - 1) // page_size
        start = (page - 1) * page_size
        visible_guild_ids = guild_ids[start : start + page_size]
        if not visible_guild_ids:
            await interaction.response.send_message(
                f"لا توجد اشتراكات في الصفحة {page}. لديك {len(guild_ids)} "
                f"اشتراكًا؛ عدد الصفحات {page_count}.",
                ephemeral=getattr(interaction, "guild_id", None) is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        lines = [
            f"• {_guild_label(bot, guild_id)}"
            for guild_id in visible_guild_ids
        ]
        message = (
            "اشتراكاتك في رسائل الدعوات (تظهر اشتراكاتك أنت فقط):\n"
            + "\n".join(lines)
            + f"\n\nالصفحة {page} من {page_count}. لإلغاء اشتراك واحد، "
            "استخدم `/collect_opt_out` واختر معرّف السيرفر في `server_id`."
        )
        await interaction.response.send_message(
            message,
            ephemeral=getattr(interaction, "guild_id", None) is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(
        name="collect_opt_out",
        description="إيقاف رسائل الدعوات التي اخترت استقبالها",
    )
    @app_commands.allowed_contexts(
        guilds=True,
        dms=True,
        private_channels=True,
    )
    @app_commands.describe(
        server_id="اختر سيرفرًا واحدًا لإلغاء اشتراكه بدلًا من كل السيرفرات",
    )
    async def collect_opt_out(
        interaction: discord.Interaction,
        server_id: int | None = None,
    ):
        guild_id = getattr(interaction, "guild_id", None)
        try:
            if server_id is not None:
                if not store.is_subscribed(server_id, interaction.user.id):
                    removed = False
                else:
                    removed = store.unsubscribe(server_id, interaction.user.id)
                scope = f"السيرفر المحدد ({server_id})"
            elif guild_id is None:
                removed = store.unsubscribe_from_all_guilds(interaction.user.id)
                scope = "كل السيرفرات"
            else:
                removed = int(
                    store.unsubscribe(guild_id, interaction.user.id)
                )
                scope = "هذا السيرفر"
        except (ValueError, OSError):
            logger.exception("Could not save invite-notification opt-out.")
            await interaction.response.send_message(
                "تعذر إلغاء الاشتراك الآن. حاول مرة أخرى لاحقًا.",
                ephemeral=guild_id is not None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        message = (
            f"تم إلغاء اشتراكك في رسائل الدعوات من {scope}."
            if removed
            else f"لا يوجد اشتراك نشط لرسائل الدعوات من {scope}."
        )
        if guild_id is None and server_id is None:
            message += (
                " لعرض اشتراكاتك أو إلغاء اشتراك سيرفر واحد، استخدم "
                "`/collect_subscriptions` ثم اختر `server_id` في "
                "`/collect_opt_out`."
            )
        await interaction.response.send_message(
            message,
            ephemeral=guild_id is not None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @collect_opt_out.autocomplete("server_id")
    async def collect_opt_out_server_autocomplete(
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        choices = []
        query = current.casefold().strip()
        for subscribed_guild_id in store.guild_ids_for_user(
            interaction.user.id
        ):
            label = _guild_label(bot, subscribed_guild_id)
            if (
                query
                and query not in str(subscribed_guild_id)
                and query not in label.casefold()
            ):
                continue
            choices.append(
                app_commands.Choice(
                    name=label[:100],
                    value=subscribed_guild_id,
                )
            )
            if len(choices) == 25:
                break
        return choices

    @bot.tree.command(
        name="collect",
        description="إرسال دعوة Discord للمشتركين طوعًا في هذا السيرفر",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        target="رمز دعوة Discord أو رابطها، مثل valkrossmc",
        resend="إعادة الإرسال عمدًا حتى لمن وصلته هذه الدعوة مؤخرًا",
    )
    async def collect(
        interaction: discord.Interaction,
        target: str,
        resend: bool = False,
    ):
        if interaction.user.id != developer_id:
            await interaction.response.send_message(
                "هذا الأمر متاح لمطور البوت فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        guild = getattr(interaction, "guild", None)
        guild_id = getattr(interaction, "guild_id", None)
        if guild is None or guild_id is None:
            await interaction.response.send_message(
                "استخدم `/collect` داخل السيرفر الذي اشترك أعضاؤه في الرسائل.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not getattr(bot.intents, "members", False):
            await interaction.response.send_message(
                "يتطلب إرسال الدعوات تفعيل Server Members Intent في Discord "
                "Developer Portal وضبط DISCORD_MEMBERS_INTENT=1 في إعدادات Replit. "
                "لم أرسل أي رسائل.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            invite_url = normalize_invite_target(target)
        except ValueError as error:
            await interaction.response.send_message(
                str(error),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if not store.subscriber_ids(guild_id):
            await interaction.response.send_message(
                "لا يوجد أعضاء مشتركون في رسائل الدعوات من هذا السيرفر.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await bot.fetch_invite(invite_url)
        except discord.NotFound:
            await interaction.followup.send(
                "رمز الدعوة غير موجود أو انتهت صلاحيته؛ لم أرسل أي رسائل.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except discord.HTTPException:
            logger.exception("Could not verify a Discord invite before collection.")
            await interaction.followup.send(
                "تعذر التحقق من الدعوة مع Discord؛ لم أرسل أي رسائل.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if resend:
            recipient_ids = store.subscriber_ids(guild_id)
            if not recipient_ids:
                await interaction.followup.send(
                    "لا يوجد أعضاء مشتركون حاليًا؛ لم أرسل أي رسائل.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            async def confirm_resend(confirmation_interaction):
                await deliver_invite_batch(
                    confirmation_interaction,
                    guild,
                    guild_id,
                    invite_url,
                    resend=True,
                    recipient_ids=recipient_ids,
                )

            view = CollectResendConfirmationView(
                interaction.user.id,
                len(recipient_ids),
                confirm_resend,
            )
            view.confirmation_message = await interaction.edit_original_response(
                content=(
                    "سيتم إعادة إرسال هذه الدعوة إلى "
                    f"**{len(recipient_ids)}** عضوًا مشتركًا حاليًا. "
                    "لن أرسل أي رسالة قبل الضغط على زر التأكيد. قد يقل العدد "
                    "الفعلي إذا ألغى أحد اشتراكه أو تعذر التحقق من عضويته أو "
                    "استقبال رسائل الخاص."
                ),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await deliver_invite_batch(
            interaction,
            guild,
            guild_id,
            invite_url,
            resend=False,
        )

    bot._collect_commands_registered = True