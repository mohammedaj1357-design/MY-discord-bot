"""Consent-based server announcements delivered through Discord DMs."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path

import discord
from discord.ext import commands


logger = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 5
MAX_NEWS_LENGTH = 1800

_MEMBER_STATUSES = {
    "pending",
    "invited",
    "subscribed",
    "declined",
    "unreachable",
    "uncertain",
}
_CAMPAIGN_KINDS = {"opt_in_request", "news"}
_CAMPAIGN_STATES = {"running", "paused", "completed", "cancelled"}
_CAMPAIGN_KEYS = {
    "kind",
    "state",
    "recipients",
    "next_index",
    "delivered",
    "failed",
    "skipped",
    "message",
    "batch_id",
}
_LEGACY_CAMPAIGN_KEYS = _CAMPAIGN_KEYS - {"batch_id"}
_CAMPAIGN_PROGRESS_KEYS = _CAMPAIGN_KEYS - {"batch_id"}


def _stored_id(value, field: str) -> str:
    if not isinstance(value, str) or not value.isdecimal() or int(value) <= 0:
        raise ValueError(f"Invalid {field} in announcement opt-in data")
    return value


def _id_key(value: int, field: str) -> str:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive Discord ID")
    return str(value)


class AnnouncementOptInStore:
    """Persist guild-scoped consent and resumable delivery progress."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._guilds: dict[str, dict] = {}
        if self.path.exists():
            self._guilds = self._load()
            self._pause_interrupted_campaigns()

    def _load(self) -> dict[str, dict]:
        with self.path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "guilds"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or not isinstance(data["guilds"], dict)
        ):
            raise ValueError(f"Invalid announcement opt-in data at {self.path}")

        guilds: dict[str, dict] = {}
        for raw_guild_id, raw_entry in data["guilds"].items():
            guild_id = _stored_id(raw_guild_id, "guild ID")
            if (
                not isinstance(raw_entry, dict)
                or set(raw_entry) != {"members", "campaign"}
                or not isinstance(raw_entry["members"], dict)
            ):
                raise ValueError(
                    f"Invalid announcement data for guild {guild_id}"
                )

            members: dict[str, str] = {}
            for raw_user_id, status in raw_entry["members"].items():
                user_id = _stored_id(raw_user_id, "user ID")
                if not isinstance(status, str) or status not in _MEMBER_STATUSES:
                    raise ValueError(
                        f"Invalid announcement status for user {user_id}"
                    )
                members[user_id] = status

            campaign = raw_entry["campaign"]
            if campaign is not None:
                campaign = self._validate_campaign(campaign, guild_id)
            guilds[guild_id] = {"members": members, "campaign": campaign}
        return guilds

    @staticmethod
    def _validate_campaign(campaign: dict, guild_id: str) -> dict:
        if not isinstance(campaign, dict) or set(campaign) not in (
            _LEGACY_CAMPAIGN_KEYS,
            _CAMPAIGN_KEYS,
        ):
            raise ValueError(f"Invalid campaign data for guild {guild_id}")
        if (
            not isinstance(campaign["kind"], str)
            or campaign["kind"] not in _CAMPAIGN_KINDS
            or not isinstance(campaign["state"], str)
            or campaign["state"] not in _CAMPAIGN_STATES
            or not isinstance(campaign["recipients"], list)
        ):
            raise ValueError(f"Invalid campaign data for guild {guild_id}")

        recipients = [
            _stored_id(user_id, "campaign recipient ID")
            for user_id in campaign["recipients"]
        ]
        if len(recipients) != len(set(recipients)):
            raise ValueError(f"Duplicate campaign recipients for guild {guild_id}")

        next_index = campaign["next_index"]
        counters = (
            campaign["delivered"],
            campaign["failed"],
            campaign["skipped"],
        )
        if (
            type(next_index) is not int
            or next_index < 0
            or next_index > len(recipients)
            or any(type(value) is not int or value < 0 for value in counters)
        ):
            raise ValueError(f"Invalid campaign progress for guild {guild_id}")

        message = campaign["message"]
        if message is not None and not isinstance(message, str):
            raise ValueError(f"Invalid campaign message for guild {guild_id}")
        batch_id = campaign.get("batch_id")
        if batch_id is not None and (
            not isinstance(batch_id, str)
            or len(batch_id) != 32
            or any(char not in "0123456789abcdef" for char in batch_id)
        ):
            raise ValueError(f"Invalid campaign batch ID for guild {guild_id}")
        if campaign["kind"] == "news" and not message:
            raise ValueError(f"Missing campaign message for guild {guild_id}")
        if campaign["kind"] == "opt_in_request" and message is not None:
            raise ValueError(f"Unexpected campaign message for guild {guild_id}")

        return {
            "kind": campaign["kind"],
            "state": campaign["state"],
            "recipients": recipients,
            "next_index": next_index,
            "delivered": counters[0],
            "failed": counters[1],
            "skipped": counters[2],
            "message": message,
            "batch_id": batch_id,
        }

    def _save(self, guilds: dict[str, dict]) -> None:
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
                        "schema_version": 1,
                        "guilds": {
                            guild_id: guilds[guild_id]
                            for guild_id in sorted(guilds, key=int)
                        },
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

    def _pause_interrupted_campaigns(self) -> None:
        updated = copy.deepcopy(self._guilds)
        changed = False
        for entry in updated.values():
            campaign = entry["campaign"]
            if campaign is not None and campaign["state"] == "running":
                campaign["state"] = "paused"
                changed = True
            for user_id, status in entry["members"].items():
                if status == "pending":
                    # The process may have stopped between Discord accepting a
                    # DM and the local status save. Never send that DM twice.
                    entry["members"][user_id] = "uncertain"
                    changed = True
        if changed:
            self._save(updated)
            self._guilds = updated

    @staticmethod
    def _new_entry() -> dict:
        return {"members": {}, "campaign": None}

    def member_status(self, guild_id: int, user_id: int) -> str | None:
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            entry = self._guilds.get(guild_key)
            return entry["members"].get(user_key) if entry else None

    def set_member_status(
        self,
        guild_id: int,
        user_id: int,
        status: str,
        *,
        only_if: str | None = None,
    ) -> bool:
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        if status not in _MEMBER_STATUSES:
            raise ValueError("Invalid announcement member status")
        with self._lock:
            updated = copy.deepcopy(self._guilds)
            entry = updated.setdefault(guild_key, self._new_entry())
            current = entry["members"].get(user_key)
            if only_if is not None and current != only_if:
                return False
            if current == status:
                return False
            entry["members"][user_key] = status
            self._save(updated)
            self._guilds = updated
            return True

    def clear_member(self, guild_id: int, user_id: int) -> bool:
        guild_key = _id_key(guild_id, "guild_id")
        user_key = _id_key(user_id, "user_id")
        with self._lock:
            entry = self._guilds.get(guild_key)
            if not entry or user_key not in entry["members"]:
                return False
            updated = copy.deepcopy(self._guilds)
            del updated[guild_key]["members"][user_key]
            if (
                not updated[guild_key]["members"]
                and updated[guild_key]["campaign"] is None
            ):
                updated.pop(guild_key)
            self._save(updated)
            self._guilds = updated
            return True

    def clear_guild(self, guild_id: int) -> int:
        guild_key = _id_key(guild_id, "guild_id")
        with self._lock:
            entry = self._guilds.get(guild_key)
            if entry is None:
                return 0
            removed = len(entry["members"])
            updated = copy.deepcopy(self._guilds)
            updated.pop(guild_key)
            self._save(updated)
            self._guilds = updated
            return removed

    def subscriber_ids(self, guild_id: int) -> list[int]:
        guild_key = _id_key(guild_id, "guild_id")
        with self._lock:
            entry = self._guilds.get(guild_key)
            if not entry:
                return []
            return sorted(
                int(user_id)
                for user_id, status in entry["members"].items()
                if status == "subscribed"
            )

    def counts(self, guild_id: int) -> dict[str, int]:
        guild_key = _id_key(guild_id, "guild_id")
        with self._lock:
            entry = self._guilds.get(guild_key, self._new_entry())
            result = {status: 0 for status in _MEMBER_STATUSES}
            for status in entry["members"].values():
                result[status] += 1
            return result

    def campaign(self, guild_id: int) -> dict | None:
        guild_key = _id_key(guild_id, "guild_id")
        with self._lock:
            entry = self._guilds.get(guild_key)
            return copy.deepcopy(entry["campaign"]) if entry else None

    def campaigns(self) -> dict[int, dict]:
        with self._lock:
            return {
                int(guild_id): copy.deepcopy(entry["campaign"])
                for guild_id, entry in sorted(
                    self._guilds.items(), key=lambda item: int(item[0])
                )
                if entry["campaign"] is not None
            }

    def campaigns_by_batch(self, batch_id: str) -> dict[int, dict]:
        if (
            not isinstance(batch_id, str)
            or len(batch_id) != 32
            or any(char not in "0123456789abcdef" for char in batch_id)
        ):
            raise ValueError("Invalid campaign batch ID")
        with self._lock:
            return {
                int(guild_id): copy.deepcopy(campaign)
                for guild_id, entry in sorted(
                    self._guilds.items(), key=lambda item: int(item[0])
                )
                if (campaign := entry["campaign"]) is not None
                and campaign["batch_id"] == batch_id
            }

    def start_campaign(
        self,
        guild_id: int,
        kind: str,
        recipient_ids: list[int],
        message: str | None = None,
    ) -> None:
        guild_key = _id_key(guild_id, "guild_id")
        if kind not in _CAMPAIGN_KINDS:
            raise ValueError("Invalid announcement campaign kind")
        if kind == "news" and not message:
            raise ValueError("A news campaign needs a message")
        recipients = [_id_key(user_id, "recipient_id") for user_id in recipient_ids]
        if len(recipients) != len(set(recipients)):
            raise ValueError("Campaign recipients must be unique")
        if not recipients:
            raise ValueError("A campaign needs at least one recipient")

        with self._lock:
            current = self._guilds.get(guild_key)
            if current:
                active = current["campaign"]
                if active and active["state"] in {"running", "paused"}:
                    raise RuntimeError("An announcement campaign is already active")
            updated = copy.deepcopy(self._guilds)
            entry = updated.setdefault(guild_key, self._new_entry())
            entry["campaign"] = {
                "kind": kind,
                "state": "running",
                "recipients": recipients,
                "next_index": 0,
                "delivered": 0,
                "failed": 0,
                "skipped": 0,
                "message": message,
                "batch_id": None,
            }
            self._save(updated)
            self._guilds = updated

    def start_news_batch(
        self,
        recipient_map: dict[int, list[int]],
        message: str,
        batch_id: str,
    ) -> None:
        if (
            not recipient_map
            or not isinstance(message, str)
            or not message
            or len(message) > MAX_NEWS_LENGTH
        ):
            raise ValueError("A news batch needs recipients and a message")
        if (
            not isinstance(batch_id, str)
            or len(batch_id) != 32
            or any(char not in "0123456789abcdef" for char in batch_id)
        ):
            raise ValueError("Invalid campaign batch ID")

        normalized: list[tuple[str, list[str]]] = []
        for guild_id, recipient_ids in recipient_map.items():
            guild_key = _id_key(guild_id, "guild_id")
            recipients = [
                _id_key(user_id, "recipient_id") for user_id in recipient_ids
            ]
            if not recipients or len(recipients) != len(set(recipients)):
                raise ValueError("Each guild needs unique campaign recipients")
            normalized.append((guild_key, recipients))
        normalized.sort(key=lambda item: int(item[0]))

        with self._lock:
            for guild_key, current in self._guilds.items():
                campaign = current["campaign"]
                if campaign and campaign["state"] in {"running", "paused"}:
                    raise RuntimeError(
                        "An announcement campaign is already active for guild "
                        f"{guild_key}"
                    )

            updated = copy.deepcopy(self._guilds)
            for index, (guild_key, recipients) in enumerate(normalized):
                entry = updated.setdefault(guild_key, self._new_entry())
                entry["campaign"] = {
                    "kind": "news",
                    "state": "running" if index == 0 else "paused",
                    "recipients": recipients,
                    "next_index": 0,
                    "delivered": 0,
                    "failed": 0,
                    "skipped": 0,
                    "message": message,
                    "batch_id": batch_id,
                }
            self._save(updated)
            self._guilds = updated

    def update_batch_state(
        self,
        batch_id: str,
        *,
        from_states: set[str],
        state: str,
    ) -> int:
        if state not in _CAMPAIGN_STATES or from_states - _CAMPAIGN_STATES:
            raise ValueError("Invalid campaign batch state update")
        if (
            not isinstance(batch_id, str)
            or len(batch_id) != 32
            or any(char not in "0123456789abcdef" for char in batch_id)
        ):
            raise ValueError("Invalid campaign batch ID")
        changed = 0
        with self._lock:
            updated = copy.deepcopy(self._guilds)
            for entry in updated.values():
                campaign = entry["campaign"]
                if (
                    campaign is None
                    or campaign["batch_id"] != batch_id
                    or campaign["state"] not in from_states
                ):
                    continue
                campaign["state"] = state
                changed += 1
            if changed:
                self._save(updated)
                self._guilds = updated
        return changed

    def update_campaign(self, guild_id: int, **changes) -> dict:
        guild_key = _id_key(guild_id, "guild_id")
        if set(changes) - _CAMPAIGN_PROGRESS_KEYS:
            raise ValueError("Unknown campaign progress field")
        with self._lock:
            current = self._guilds.get(guild_key)
            if not current or current["campaign"] is None:
                raise ValueError("No announcement campaign exists")
            updated = copy.deepcopy(self._guilds)
            campaign = updated[guild_key]["campaign"]
            campaign.update(changes)
            campaign = self._validate_campaign(campaign, guild_key)
            updated[guild_key]["campaign"] = campaign
            self._save(updated)
            self._guilds = updated
            return copy.deepcopy(campaign)


class NewsPreferenceView(discord.ui.View):
    """Persistent per-guild buttons for explicit opt-in and opt-out."""

    def __init__(
        self,
        bot,
        store: AnnouncementOptInStore,
        guild_id: int,
        *,
        include_opt_in: bool = True,
        include_opt_out: bool = True,
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.store = store
        self.guild_id = _id_key(guild_id, "guild_id")

        if include_opt_in:
            button = discord.ui.Button(
                label="✅ أوافق",
                style=discord.ButtonStyle.success,
                custom_id=f"newsoptin:join:{self.guild_id}",
            )
            button.callback = self._make_choice_callback("subscribed")
            self.add_item(button)

        if include_opt_out:
            button = discord.ui.Button(
                label="❌ إلغاء الاشتراك",
                style=discord.ButtonStyle.danger,
                custom_id=f"newsoptin:leave:{self.guild_id}",
            )
            button.callback = self._make_choice_callback("declined")
            self.add_item(button)

    def _make_choice_callback(self, status: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.bot:
                await interaction.response.send_message(
                    "لا يمكن للبوتات الاشتراك.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if not getattr(self.bot.intents, "members", False):
                await interaction.response.send_message(
                    "تعذر التحقق من عضويتك. يحتاج البوت إلى Server Members "
                    "Intent مفعّل في Discord وإعداد DISCORD_MEMBERS_INTENT=1.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            guild = self.bot.get_guild(int(self.guild_id))
            if guild is None:
                await interaction.response.send_message(
                    "لم يعد هذا السيرفر متاحًا للبوت.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            try:
                await guild.fetch_member(interaction.user.id)
                changed = self.store.set_member_status(
                    int(self.guild_id), interaction.user.id, status
                )
            except discord.NotFound:
                await interaction.response.send_message(
                    "يجب أن تكون عضوًا في السيرفر حتى تغيّر تفضيل الرسائل.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except (discord.HTTPException, OSError, ValueError):
                logger.exception(
                    "Could not update announcement preference for guild %s.",
                    self.guild_id,
                )
                await interaction.response.send_message(
                    "تعذر حفظ اختيارك الآن. حاول مرة أخرى لاحقًا.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if status == "subscribed":
                message = (
                    "تم اشتراكك في أخبار هذا السيرفر. يمكنك إلغاء الاشتراك "
                    "من أي رسالة تصلك."
                    if changed
                    else "أنت مشترك بالفعل في أخبار هذا السيرفر."
                )
            else:
                message = (
                    "تم إلغاء اشتراكك. لن تصلك أخبار أخرى من هذا السيرفر."
                    if changed
                    else "لا يوجد اشتراك نشط لأخبار هذا السيرفر."
                )
            await interaction.response.send_message(
                message,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        return callback


class CampaignConfirmationView(discord.ui.View):
    def __init__(self, requester_id: int, on_confirm):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.on_confirm = on_confirm
        self.confirmed = False

        confirm_button = discord.ui.Button(
            label="تأكيد الإرسال",
            style=discord.ButtonStyle.danger,
            custom_id="news-campaign:confirm",
        )
        confirm_button.callback = self._confirm
        self.add_item(confirm_button)

        cancel_button = discord.ui.Button(
            label="إلغاء",
            style=discord.ButtonStyle.secondary,
            custom_id="news-campaign:cancel",
        )
        cancel_button.callback = self._cancel
        self.add_item(cancel_button)

    async def _check_requester(self, interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "هذا التأكيد متاح لمن بدأ العملية فقط.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await self._check_requester(interaction):
            return
        if self.confirmed:
            await interaction.response.send_message(
                "تم تأكيد هذه العملية بالفعل.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await self.on_confirm(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self._check_requester(interaction):
            return
        await interaction.response.edit_message(
            content="تم إلغاء العملية؛ لم تُرسل أي رسائل.",
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def register_news_preference_view(
    bot,
    store: AnnouncementOptInStore,
    guild_id: int,
) -> None:
    registered = getattr(bot, "_news_preference_views_registered", None)
    if registered is None:
        registered = set()
        bot._news_preference_views_registered = registered
    if guild_id in registered:
        return
    bot.add_view(NewsPreferenceView(bot, store, guild_id))
    registered.add(guild_id)


def register_all_news_preference_views(bot, store: AnnouncementOptInStore) -> None:
    for guild in getattr(bot, "guilds", ()):
        register_news_preference_view(bot, store, guild.id)


def _safe_server_name(value: str) -> str:
    return str(value).replace("@", "@\u200b").replace("\r", " ").replace("\n", " ")


def setup_announcement_optin_commands(
    bot,
    store: AnnouncementOptInStore,
    developer_id: int,
    *,
    delay_seconds: float = REQUEST_DELAY_SECONDS,
    guild_allowed=None,
) -> None:
    if getattr(bot, "_announcement_optin_commands_registered", False):
        return

    campaign_locks: dict[int, asyncio.Lock] = {}
    pause_events: dict[int, asyncio.Event] = {}
    active_tasks: dict[int, asyncio.Task] = {}
    batch_pause_events: dict[str, asyncio.Event] = {}
    batch_tasks: dict[str, asyncio.Task] = {}
    guild_allowed = guild_allowed or (lambda _guild_id: True)

    def active_batch_ids() -> set[str]:
        return {
            campaign["batch_id"]
            for campaign in store.campaigns().values()
            if campaign["batch_id"] is not None
            and campaign["state"] in {"running", "paused"}
        }

    def batch_for_context(ctx) -> str | None:
        batches = active_batch_ids()
        return next(iter(batches)) if len(batches) == 1 else None

    def command_context_error(ctx) -> str | None:
        if ctx.author.id != developer_id:
            return "هذا الأمر متاح لمطور البوت فقط."
        if ctx.guild is None:
            return "استخدم هذا الأمر داخل السيرفر المطلوب."
        return None

    def members_intent_error() -> str | None:
        if getattr(bot.intents, "members", False):
            return None
        return (
            "قبل استخدام هذا الأمر، فعّل Server Members Intent في Discord "
            "Developer Portal، واضبط DISCORD_MEMBERS_INTENT=1 في إعدادات Replit."
        )

    async def update_progress_message(message, campaign: dict) -> None:
        labels = {
            "opt_in_request": "طلبات الموافقة",
            "news": "الأخبار للمشتركين",
        }
        state_labels = {
            "running": "جارٍ",
            "paused": "متوقف مؤقتًا",
            "completed": "اكتمل",
            "cancelled": "أُلغي",
        }
        total = len(campaign["recipients"])
        await message.edit(
            content=(
                f"📨 {labels[campaign['kind']]}: "
                f"{campaign['next_index']}/{total}\n"
                f"وصلت: {campaign['delivered']} • "
                f"تعذر: {campaign['failed']} • "
                f"تخطّي: {campaign['skipped']}\n"
                f"الحالة: {state_labels[campaign['state']]}"
            ),
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_one(guild, user_id: int, campaign: dict) -> bool:
        member = await guild.fetch_member(user_id)
        if (
            campaign["kind"] == "news"
            and store.member_status(guild.id, user_id) != "subscribed"
        ):
            return False
        if campaign["kind"] == "opt_in_request":
            server_name = _safe_server_name(guild.name)
            embed = discord.Embed(
                title="📩 طلب موافقة",
                description=(
                    f"من: **{server_name}**\n\n"
                    "هل ترغب في استقبال أخبار هذا السيرفر عبر الخاص؟\n\n"
                    "الاشتراك اختياري. لن تصلك الأخبار إلا بعد موافقتك، "
                    "ويمكنك إلغاء الاشتراك في أي وقت."
                ),
                color=0x4A90E2,
            )
            icon = getattr(guild, "icon", None)
            if icon:
                embed.set_thumbnail(url=icon.url)
            embed.set_footer(text="اختيارك محفوظ لهذا السيرفر فقط.")
            await member.send(
                embed=embed,
                view=NewsPreferenceView(bot, store, guild.id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        embed = discord.Embed(
            title=f"📢 أخبار {_safe_server_name(guild.name)}",
            description=campaign["message"],
            color=0xFFD700,
        )
        embed.set_footer(
            text="وصلتك الرسالة بسبب اشتراكك الطوعي. استخدم زر إلغاء الاشتراك لإيقافها."
        )
        icon = getattr(guild, "icon", None)
        if icon:
            embed.set_thumbnail(url=icon.url)
        await member.send(
            embed=embed,
            view=NewsPreferenceView(
                bot,
                store,
                guild.id,
                include_opt_in=False,
                include_opt_out=True,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return True

    async def _deliver_campaign(
        guild,
        guild_id: int,
        progress_message,
        pause_event: asyncio.Event,
    ) -> None:
        lock = campaign_locks.setdefault(guild_id, asyncio.Lock())
        try:
            async with lock:
                while True:
                    campaign = store.campaign(guild_id)
                    if campaign is None:
                        return
                    if campaign["state"] == "cancelled":
                        await update_progress_message(progress_message, campaign)
                        return
                    if campaign["state"] != "running":
                        return
                    if pause_event.is_set():
                        campaign = store.update_campaign(
                            guild_id, state="paused"
                        )
                        await update_progress_message(progress_message, campaign)
                        return

                    index = campaign["next_index"]
                    if index >= len(campaign["recipients"]):
                        campaign = store.update_campaign(
                            guild_id, state="completed"
                        )
                        await update_progress_message(progress_message, campaign)
                        return

                    user_id = int(campaign["recipients"][index])
                    current_status = store.member_status(guild_id, user_id)
                    if campaign["kind"] == "news" and current_status != "subscribed":
                        campaign = store.update_campaign(
                            guild_id,
                            next_index=index + 1,
                            skipped=campaign["skipped"] + 1,
                        )
                        await update_progress_message(progress_message, campaign)
                        continue

                    if campaign["kind"] == "opt_in_request":
                        if current_status in {
                            "pending",
                            "invited",
                            "subscribed",
                            "declined",
                            "uncertain",
                        }:
                            campaign = store.update_campaign(
                                guild_id,
                                next_index=index + 1,
                                skipped=campaign["skipped"] + 1,
                            )
                            await update_progress_message(progress_message, campaign)
                            continue
                        store.set_member_status(
                            guild_id, user_id, "pending"
                        )

                    # Advance before the network call. If the process stops
                    # after Discord accepts the DM, resume will not duplicate it.
                    campaign = store.update_campaign(
                        guild_id, next_index=index + 1
                    )
                    outcome = "skipped"
                    delivered = False
                    try:
                        for attempt in range(2):
                            try:
                                delivered = await _send_one(
                                    guild, user_id, campaign
                                )
                                outcome = (
                                    "delivered" if delivered else "skipped"
                                )
                                break
                            except discord.HTTPException as error:
                                if error.status != 429 or attempt == 1:
                                    raise
                                retry_after = getattr(error, "retry_after", 60)
                                try:
                                    wait_seconds = max(5.0, float(retry_after))
                                except (TypeError, ValueError):
                                    wait_seconds = 60.0
                                await asyncio.sleep(wait_seconds)
                    except discord.NotFound:
                        store.clear_member(guild_id, user_id)
                        outcome = "skipped"
                    except discord.Forbidden:
                        if campaign["kind"] == "opt_in_request":
                            store.set_member_status(
                                guild_id,
                                user_id,
                                "unreachable",
                                only_if="pending",
                            )
                        outcome = "failed"
                    except discord.HTTPException as error:
                        logger.warning(
                            "Announcement DM failed in guild %s for user %s "
                            "(HTTP status %s).",
                            guild_id,
                            user_id,
                            error.status,
                        )
                        if campaign["kind"] == "opt_in_request":
                            store.set_member_status(
                                guild_id,
                                user_id,
                                "uncertain",
                                only_if="pending",
                            )
                        outcome = "failed"
                    except Exception:
                        logger.exception(
                            "Unexpected announcement DM failure in guild %s "
                            "for user %s.",
                            guild_id,
                            user_id,
                        )
                        if campaign["kind"] == "opt_in_request":
                            store.set_member_status(
                                guild_id,
                                user_id,
                                "uncertain",
                                only_if="pending",
                            )
                        outcome = "failed"
                    else:
                        if campaign["kind"] == "opt_in_request":
                            store.set_member_status(
                                guild_id,
                                user_id,
                                "invited",
                                only_if="pending",
                            )

                    latest = store.campaign(guild_id)
                    if latest is None:
                        return
                    counter = "delivered" if outcome == "delivered" else outcome
                    changes = {counter: latest[counter] + 1}
                    campaign = store.update_campaign(guild_id, **changes)
                    if (
                        campaign["next_index"] % 25 == 0
                        or campaign["next_index"] == len(campaign["recipients"])
                    ):
                        await update_progress_message(progress_message, campaign)
                    await asyncio.sleep(delay_seconds)
        except Exception:
            logger.exception(
                "Announcement campaign stopped unexpectedly in guild %s.",
                guild_id,
            )
            try:
                campaign = store.campaign(guild_id)
                if campaign and campaign["state"] == "running":
                    campaign = store.update_campaign(
                        guild_id, state="paused"
                    )
                    await update_progress_message(progress_message, campaign)
            except Exception:
                logger.exception(
                    "Could not mark announcement campaign paused for guild %s.",
                    guild_id,
                )
        finally:
            active_tasks.pop(guild_id, None)

    async def begin_campaign(
        interaction,
        guild,
        kind: str,
        recipients: list[int],
        message: str | None = None,
    ) -> None:
        guild_id = guild.id
        if active_batch_ids():
            await interaction.response.edit_message(
                content=(
                    "يوجد إرسال عام نشط أو متوقف. استأنفه أو ألغِه "
                    "قبل بدء إرسال آخر."
                ),
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            store.start_campaign(guild_id, kind, recipients, message)
        except RuntimeError:
            await interaction.response.edit_message(
                content="يوجد إرسال نشط أو متوقف لهذا السيرفر. استأنفه أو أوقفه قبل بدء عملية جديدة.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except (OSError, ValueError):
            logger.exception("Could not create announcement campaign.")
            await interaction.response.edit_message(
                content="تعذر حفظ العملية. لم يبدأ إرسال أي رسائل.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        pause_event = asyncio.Event()
        pause_events[guild_id] = pause_event
        await interaction.response.edit_message(
            content=(
                "بدأ الإرسال. سيتم حفظ التقدم، ولن يعاد إرسال الرسائل "
                "التي بدأ تسليمها عند الاستئناف."
            ),
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        progress_message = interaction.message
        task = asyncio.create_task(
            _deliver_campaign(guild, guild_id, progress_message, pause_event)
        )
        active_tasks[guild_id] = task

    async def update_batch_progress(batch_id: str, message) -> None:
        campaigns = store.campaigns_by_batch(batch_id)
        total = sum(len(campaign["recipients"]) for campaign in campaigns.values())
        processed = sum(campaign["next_index"] for campaign in campaigns.values())
        delivered = sum(campaign["delivered"] for campaign in campaigns.values())
        failed = sum(campaign["failed"] for campaign in campaigns.values())
        skipped = sum(campaign["skipped"] for campaign in campaigns.values())
        states = {campaign["state"] for campaign in campaigns.values()}
        if "running" in states:
            state_label = "جارٍ"
        elif "paused" in states:
            state_label = "متوقف مؤقتًا"
        elif "cancelled" in states:
            state_label = "أُلغي"
        else:
            state_label = "اكتمل"
        try:
            await message.edit(
                content=(
                    f"📨 الرسالة الخاصة للمشتركين: {processed}/{total}\n"
                    f"وصلت: {delivered} • تعذّر: {failed} • تخطّي: {skipped}\n"
                    f"السيرفرات: {len(campaigns)} • الحالة: {state_label}"
                ),
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.info("Could not update global announcement progress message.")

    class BatchProgressMessage:
        def __init__(self, batch_id: str, message):
            self.batch_id = batch_id
            self.message = message

        async def edit(self, **_kwargs):
            await update_batch_progress(self.batch_id, self.message)

    async def run_news_batch(
        batch_id: str,
        progress_message,
        pause_event: asyncio.Event,
    ) -> None:
        progress_proxy = BatchProgressMessage(batch_id, progress_message)
        try:
            while True:
                campaigns = store.campaigns_by_batch(batch_id)
                if not campaigns:
                    return
                if pause_event.is_set():
                    store.update_batch_state(
                        batch_id,
                        from_states={"running"},
                        state="paused",
                    )
                    await update_batch_progress(batch_id, progress_message)
                    return

                pending = next(
                    (
                        (guild_id, campaign)
                        for guild_id, campaign in campaigns.items()
                        if campaign["state"] in {"running", "paused"}
                    ),
                    None,
                )
                if pending is None:
                    await update_batch_progress(batch_id, progress_message)
                    return

                guild_id, campaign = pending
                try:
                    allowed = bool(guild_allowed(guild_id))
                except Exception:
                    logger.exception(
                        "Could not check guild access for global news delivery."
                    )
                    allowed = False
                guild = bot.get_guild(guild_id) if allowed else None
                if guild is None:
                    remaining = (
                        len(campaign["recipients"]) - campaign["next_index"]
                    )
                    store.update_campaign(
                        guild_id,
                        state="cancelled",
                        skipped=campaign["skipped"] + remaining,
                    )
                    await update_batch_progress(batch_id, progress_message)
                    continue

                existing_task = active_tasks.get(guild_id)
                if existing_task is not None and not existing_task.done():
                    store.update_batch_state(
                        batch_id,
                        from_states={"running"},
                        state="paused",
                    )
                    await update_batch_progress(batch_id, progress_message)
                    return

                if campaign["state"] == "paused":
                    store.update_campaign(guild_id, state="running")
                pause_events[guild_id] = pause_event
                task = asyncio.create_task(
                    _deliver_campaign(
                        guild,
                        guild_id,
                        progress_proxy,
                        pause_event,
                    )
                )
                active_tasks[guild_id] = task
                await task

                latest = store.campaign(guild_id)
                if pause_event.is_set() or (
                    latest is not None and latest["state"] == "paused"
                ):
                    store.update_batch_state(
                        batch_id,
                        from_states={"running"},
                        state="paused",
                    )
                    await update_batch_progress(batch_id, progress_message)
                    return
        except Exception:
            logger.exception("Cross-server announcement batch failed.")
            try:
                store.update_batch_state(
                    batch_id,
                    from_states={"running"},
                    state="paused",
                )
                await update_batch_progress(batch_id, progress_message)
            except Exception:
                logger.exception("Could not pause failed cross-server batch.")
        finally:
            if batch_tasks.get(batch_id) is asyncio.current_task():
                batch_tasks.pop(batch_id, None)
            if batch_pause_events.get(batch_id) is pause_event:
                batch_pause_events.pop(batch_id, None)

    async def begin_news_batch(
        interaction,
        recipient_map: dict[int, list[int]],
        message: str,
    ) -> None:
        batch_id = uuid.uuid4().hex
        try:
            store.start_news_batch(recipient_map, message, batch_id)
        except RuntimeError:
            await interaction.response.edit_message(
                content=(
                    "يوجد إرسال نشط أو متوقف في أحد السيرفرات. "
                    "استأنفه أو ألغِه قبل بدء الإرسال العام."
                ),
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except (OSError, ValueError):
            logger.exception("Could not save cross-server announcement batch.")
            await interaction.response.edit_message(
                content="تعذر حفظ العملية. لم يبدأ إرسال أي رسائل.",
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        pause_event = asyncio.Event()
        batch_pause_events[batch_id] = pause_event
        try:
            await interaction.response.edit_message(
                content=(
                    "بدأ الإرسال للمشتركين فقط. حُفظ التقدم ويمكن إيقافه "
                    "أو استئنافه أو إلغاؤه باستخدام أوامر optin."
                ),
                embed=None,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Could not acknowledge the start of a cross-server campaign."
            )
            store.update_batch_state(
                batch_id,
                from_states={"running"},
                state="paused",
            )
            batch_pause_events.pop(batch_id, None)
            return

        task = asyncio.create_task(
            run_news_batch(batch_id, interaction.message, pause_event)
        )
        batch_tasks[batch_id] = task

    async def send_global_news(ctx, message: str) -> None:
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            source_allowed = bool(guild_allowed(ctx.guild.id))
        except Exception:
            logger.exception("Could not check source guild for global news.")
            source_allowed = False
        if not source_allowed:
            await ctx.reply(
                "هذا السيرفر غير معتمد لإرسال رسائل البوت التطويري.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if intent_error := members_intent_error():
            await ctx.reply(
                intent_error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        message = message.strip()
        if not message:
            await ctx.reply(
                "اكتب نص الإعلان بعد `!dm everyone`.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(message) > MAX_NEWS_LENGTH:
            await ctx.reply(
                f"الحد الأقصى لنص الإعلان هو {MAX_NEWS_LENGTH} حرفًا.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if any(
            campaign["state"] in {"running", "paused"}
            for campaign in store.campaigns().values()
        ):
            await ctx.reply(
                "يوجد إرسال نشط أو متوقف في أحد السيرفرات. "
                "استأنفه أو ألغِه قبل بدء إرسال جديد.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        recipient_map: dict[int, list[int]] = {}
        guild_labels: list[str] = []
        for guild in sorted(bot.guilds, key=lambda item: item.id):
            try:
                allowed = bool(guild_allowed(guild.id))
            except Exception:
                logger.exception(
                    "Could not check guild %s for a cross-server announcement.",
                    guild.id,
                )
                continue
            if not allowed:
                continue
            recipients = store.subscriber_ids(guild.id)
            if not recipients:
                continue
            recipient_map[guild.id] = recipients
            name = discord.utils.escape_mentions(
                _safe_server_name(guild.name)
            )[:80]
            guild_labels.append(f"• {name}: {len(recipients)} مشترك")

        total = sum(len(recipients) for recipients in recipient_map.values())
        if total == 0:
            await ctx.reply(
                "لا يوجد أعضاء وافقوا على استقبال أخبار في السيرفرات المتاحة.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        estimate_minutes = max(
            1,
            (total * int(delay_seconds) + 59) // 60,
        )
        server_summary = "\n".join(guild_labels[:12])
        if len(guild_labels) > 12:
            server_summary += f"\n• وسيرفرات أخرى: {len(guild_labels) - 12}"
        embed = discord.Embed(
            title="تأكيد إعلان خاص عبر السيرفرات",
            description=(
                f"سيُرسل النص إلى **{total}** اشتراكًا في "
                f"**{len(recipient_map)}** سيرفر فقط.\n"
                f"الفاصل بين الرسائل {int(delay_seconds)} ثوانٍ تقريبًا "
                f"(نحو {estimate_minutes} دقيقة).\n"
                "إذا اشترك العضو في أكثر من سيرفر، فقد تصله رسالة مستقلة "
                "لكل اشتراك، مع اسم السيرفر وزر إلغاء خاص به.\n\n"
                f"{server_summary}\n\n"
                f"**نص الرسالة:**\n{message}\n\n"
                "لن تصل الرسالة إلى غير المشتركين. هل تريد المتابعة؟"
            ),
            color=0x4A90E2,
        )

        async def on_confirm(interaction):
            await begin_news_batch(interaction, recipient_map, message)

        await ctx.send(
            embed=embed,
            view=CampaignConfirmationView(ctx.author.id, on_confirm),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(
        name="طلب_موافقة",
        aliases=["request_optin", "send_optin"],
    )
    async def request_optin(ctx):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if intent_error := members_intent_error():
            await ctx.reply(
                intent_error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        existing = store.campaign(ctx.guild.id)
        if existing and existing["state"] in {"running", "paused"}:
            await ctx.reply(
                "يوجد إرسال سابق نشط أو متوقف. استخدم `!optin_resume` لمتابعته أو `!optin_cancel` لإلغائه.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            members = [
                member
                async for member in ctx.guild.fetch_members(limit=None)
                if not member.bot and member.id != developer_id
            ]
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "Could not fetch guild members for announcement opt-in."
            )
            await ctx.reply(
                "تعذر جلب أعضاء السيرفر. تحقق من تفعيل Server Members Intent "
                "في Discord ومن إعداد DISCORD_MEMBERS_INTENT=1 في Replit.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        recipients = [
            member.id
            for member in members
            if store.member_status(ctx.guild.id, member.id)
            not in {
                "pending",
                "invited",
                "subscribed",
                "declined",
                "uncertain",
            }
        ]
        if not recipients:
            await ctx.reply(
                "لا يوجد أعضاء جدد لإرسال طلب موافقة إليهم.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        estimate_minutes = max(
            1, (len(recipients) * int(delay_seconds) + 59) // 60
        )

        async def on_confirm(interaction):
            await begin_campaign(
                interaction,
                ctx.guild,
                "opt_in_request",
                recipients,
            )

        embed = discord.Embed(
            title="تأكيد طلبات الاشتراك",
            description=(
                f"سيُرسل طلب موافقة مرة واحدة إلى **{len(recipients)}** عضوًا "
                "لم يسبق إرسال الطلب إليهم.\n"
                f"الفاصل بين الرسائل {int(delay_seconds)} ثوانٍ تقريبًا "
                f"(نحو {estimate_minutes} دقيقة).\n\n"
                "لن تصل الأخبار إلى أي عضو قبل موافقته. هل تريد المتابعة؟"
            ),
            color=0xFFA500,
        )
        await ctx.send(
            embed=embed,
            view=CampaignConfirmationView(ctx.author.id, on_confirm),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(
        name="ارسل_للجميع",
        aliases=["send_all_opted", "dm_opted"],
    )
    async def send_to_opted(ctx, *, message: str):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if intent_error := members_intent_error():
            await ctx.reply(
                intent_error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        message = message.strip()
        if not message:
            await ctx.reply(
                "اكتب نص الخبر بعد الأمر.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(message) > MAX_NEWS_LENGTH:
            await ctx.reply(
                f"الحد الأقصى لنص الخبر هو {MAX_NEWS_LENGTH} حرفًا.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        existing = store.campaign(ctx.guild.id)
        if existing and existing["state"] in {"running", "paused"}:
            await ctx.reply(
                "يوجد إرسال سابق نشط أو متوقف. استأنفه أو ألغِه قبل بدء عملية جديدة.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        recipients = store.subscriber_ids(ctx.guild.id)
        if not recipients:
            await ctx.reply(
                "لا يوجد أعضاء وافقوا على استقبال أخبار هذا السيرفر.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        async def on_confirm(interaction):
            await begin_campaign(
                interaction,
                ctx.guild,
                "news",
                recipients,
                message,
            )

        embed = discord.Embed(
            title="تأكيد إرسال الخبر",
            description=(
                f"سيُرسل هذا النص إلى **{len(recipients)}** مشتركًا فقط، "
                f"بفاصل {int(delay_seconds)} ثوانٍ تقريبًا:\n\n"
                f"{message}"
            ),
            color=0x4A90E2,
        )
        await ctx.send(
            embed=embed,
            view=CampaignConfirmationView(ctx.author.id, on_confirm),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="optin_stats", aliases=["احصائيات_الاشتراك"])
    async def optin_stats(ctx):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        counts = store.counts(ctx.guild.id)
        campaign = store.campaign(ctx.guild.id)
        status = (
            f"\nحالة آخر إرسال: **{campaign['state']}** "
            f"({campaign['next_index']}/{len(campaign['recipients'])})"
            if campaign
            else ""
        )
        await ctx.reply(
            (
                f"مشتركون: **{counts['subscribed']}** • "
                f"طلبات مرسلة: **{counts['invited']}** • "
                f"رفضوا/ألغوا: **{counts['declined']}** • "
                f"تعذر الوصول: **{counts['unreachable']}** • "
                f"حالة غير مؤكدة: **{counts['uncertain']}**"
                f"{status}"
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="المشتركين", aliases=["subscribers", "list_opted"])
    async def list_subscribers(ctx):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        recipients = store.subscriber_ids(ctx.guild.id)
        if not recipients:
            await ctx.reply(
                "لا يوجد مشتركون حاليًا.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        lines = []
        for user_id in recipients[:20]:
            user = bot.get_user(user_id)
            if user is None:
                try:
                    user = await bot.fetch_user(user_id)
                except discord.HTTPException:
                    lines.append(f"ID: {user_id}")
                    continue
            lines.append(f"{user.name} — {user_id}")

        suffix = (
            f"\n... و{len(recipients) - 20} مشتركًا آخر."
            if len(recipients) > 20
            else ""
        )
        try:
            await ctx.author.send(
                "قائمة المشتركين (أول 20):\n"
                + "\n".join(lines)
                + suffix,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await ctx.reply(
                "تعذر إرسال القائمة في الخاص. افتح الرسائل الخاصة وحاول مرة أخرى.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.reply(
            "أرسلت القائمة إليك في الخاص.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="optin_pause", aliases=["إيقاف_الإرسال"])
    async def pause_optin(ctx):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        batch_id = batch_for_context(ctx)
        if batch_id:
            campaigns = store.campaigns_by_batch(batch_id)
            if not any(
                campaign["state"] == "running"
                for campaign in campaigns.values()
            ):
                await ctx.reply(
                    "لا يوجد إرسال عام جارٍ لإيقافه.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            event = batch_pause_events.get(batch_id)
            try:
                if event is not None:
                    event.set()
                else:
                    store.update_batch_state(
                        batch_id,
                        from_states={"running"},
                        state="paused",
                    )
            except (OSError, ValueError):
                logger.exception("Could not pause global announcement batch.")
                await ctx.reply(
                    "تعذر حفظ إيقاف الإرسال العام.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await ctx.reply(
                "سيُوقف الإرسال العام بعد انتهاء محاولة الرسالة الحالية. "
                "استخدم `!optin_resume` لمتابعته لاحقًا.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        campaign = store.campaign(ctx.guild.id)
        event = pause_events.get(ctx.guild.id)
        if not campaign or campaign["state"] != "running" or event is None:
            await ctx.reply(
                "لا يوجد إرسال جارٍ لإيقافه.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        event.set()
        await ctx.reply(
            "سيُوقف الإرسال بعد انتهاء محاولة الرسالة الحالية. استخدم "
            "`!optin_resume` لمتابعته لاحقًا.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.command(name="optin_resume", aliases=["استئناف_الإرسال"])
    async def resume_optin(ctx):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if intent_error := members_intent_error():
            await ctx.reply(
                intent_error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        batch_id = batch_for_context(ctx)
        if batch_id:
            campaigns = store.campaigns_by_batch(batch_id)
            if not any(
                campaign["state"] in {"running", "paused"}
                for campaign in campaigns.values()
            ):
                await ctx.reply(
                    "لا يوجد إرسال عام متوقف يمكن استئنافه.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            task = batch_tasks.get(batch_id)
            if task is not None and not task.done():
                await ctx.reply(
                    "الإرسال العام يعمل بالفعل.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            progress_message = await ctx.send(
                "جارٍ استئناف الإرسال العام المحفوظ. لن يعاد إرسال "
                "العناصر التي بدأ تسليمها.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            pause_event = asyncio.Event()
            batch_pause_events[batch_id] = pause_event
            batch_tasks[batch_id] = asyncio.create_task(
                run_news_batch(batch_id, progress_message, pause_event)
            )
            return
        campaign = store.campaign(ctx.guild.id)
        if not campaign or campaign["state"] != "paused":
            await ctx.reply(
                "لا يوجد إرسال متوقف يمكن استئنافه.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        task = active_tasks.get(ctx.guild.id)
        if task is not None and not task.done():
            await ctx.reply(
                "الإرسال يعمل بالفعل.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        guild = ctx.guild
        store.update_campaign(ctx.guild.id, state="running")
        pause_event = asyncio.Event()
        pause_events[guild.id] = pause_event
        progress_message = await ctx.send(
            "جارٍ استئناف الإرسال المحفوظ. لن يعاد إرسال العناصر التي بدأ تسليمها.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        active_tasks[guild.id] = asyncio.create_task(
            _deliver_campaign(guild, guild.id, progress_message, pause_event)
        )

    @bot.command(name="optin_cancel", aliases=["إلغاء_الإرسال"])
    async def cancel_optin(ctx):
        error = command_context_error(ctx)
        if error:
            await ctx.reply(
                error,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        batch_id = batch_for_context(ctx)
        if batch_id:
            campaigns = store.campaigns_by_batch(batch_id)
            if not any(
                campaign["state"] in {"running", "paused"}
                for campaign in campaigns.values()
            ):
                await ctx.reply(
                    "لا يوجد إرسال عام نشط أو متوقف لإلغائه.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                store.update_batch_state(
                    batch_id,
                    from_states={"running", "paused"},
                    state="cancelled",
                )
            except (OSError, ValueError):
                logger.exception("Could not cancel global announcement batch.")
                await ctx.reply(
                    "تعذر حفظ إلغاء الإرسال العام.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            event = batch_pause_events.get(batch_id)
            if event is not None:
                event.set()
            await ctx.reply(
                "تم إلغاء الإرسال العام. قد تكتمل الرسالة الجاري إرسالها الآن، "
                "ولن يبدأ إرسال إلى عضو آخر.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        campaign = store.campaign(ctx.guild.id)
        if not campaign or campaign["state"] not in {"running", "paused"}:
            await ctx.reply(
                "لا يوجد إرسال نشط أو متوقف لإلغائه.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            store.update_campaign(ctx.guild.id, state="cancelled")
        except (OSError, ValueError):
            logger.exception("Could not cancel announcement campaign.")
            await ctx.reply(
                "تعذر حفظ إلغاء العملية.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        event = pause_events.get(ctx.guild.id)
        if event is not None:
            event.set()
        await ctx.reply(
            "تم إلغاء الإرسال. قد تكتمل الرسالة الجاري إرسالها الآن، ولن يبدأ إرسال إلى عضو آخر.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot._announcement_optin_commands_registered = True
    bot._announcement_optin_pause_events = pause_events
    bot._announcement_optin_campaign_tasks = active_tasks
    bot._announcement_optin_batch_pause_events = batch_pause_events
    bot._announcement_optin_batch_tasks = batch_tasks
    bot._announcement_optin_send_global = send_global_news