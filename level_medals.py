"""Hourly top-three medal-role management for level activity."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from levels import _leaderboard_member_label, role_reward_block_reason

logger = logging.getLogger(__name__)

MEDAL_CATEGORIES = {
    "messages": ("الرسائل", "messages"),
    "voice": ("وقت الصوت", "voice"),
    "xp": ("XP", "xp"),
}
MEDAL_RANKS = ("gold", "silver", "bronze")
MEDAL_RANK_LABELS = {
    "gold": "ذهبية",
    "silver": "فضية",
    "bronze": "برونزية",
}
MEDAL_ROLE_BLOCK_MESSAGES = {
    "the configured role no longer exists": "الرتبة المحددة لم تعد موجودة.",
    "the bot's server member is unavailable": "تعذر التحقق من رتبة البوت وصلاحياته.",
    "the bot is missing Manage Roles permission": "البوت لا يملك صلاحية إدارة الرتب.",
    "the role is managed by an integration and cannot be assigned": (
        "هذه الرتبة يديرها تكامل خارجي ولا يستطيع البوت منحها."
    ),
    "the server's @everyone role cannot be assigned as a reward": (
        "لا يمكن استخدام رتبة @everyone كميدالية."
    ),
    "the role is not below the bot's highest role": (
        "رتبة الميدالية أعلى من رتبة البوت أو مساوية لها."
    ),
}


class MedalSettingsError(ValueError):
    """A medal setting is incomplete, invalid, or no longer usable."""


def _empty_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "revision": 0,
        "categories": {
            category: {
                "enabled": False,
                "roles": {rank: None for rank in MEDAL_RANKS},
            }
            for category in MEDAL_CATEGORIES
        },
        "assignments": {
            category: {
                rank: {"role_id": None, "member_ids": []}
                for rank in MEDAL_RANKS
            }
            for category in MEDAL_CATEGORIES
        },
        "last_update": None,
    }


def _positive_id(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise MedalSettingsError(f"{field} must be a positive Discord ID.")
    return value


def _normalize_settings(raw: Any) -> dict[str, Any]:
    if raw is None:
        return _empty_settings()
    if not isinstance(raw, Mapping):
        raise MedalSettingsError("Saved medal settings must be an object.")

    result = _empty_settings()
    enabled = raw.get("enabled", False)
    revision = raw.get("revision", 0)
    if not isinstance(enabled, bool):
        raise MedalSettingsError("Saved medal enabled setting must be boolean.")
    if type(revision) is not int or revision < 0:
        raise MedalSettingsError("Saved medal settings revision is invalid.")
    result["enabled"] = enabled
    result["revision"] = revision

    raw_categories = raw.get("categories", {})
    if not isinstance(raw_categories, Mapping):
        raise MedalSettingsError("Saved medal categories must be an object.")
    for category in MEDAL_CATEGORIES:
        raw_category = raw_categories.get(category, {})
        if not isinstance(raw_category, Mapping):
            raise MedalSettingsError(f"Saved {category} medal settings are invalid.")
        category_enabled = raw_category.get("enabled", False)
        if not isinstance(category_enabled, bool):
            raise MedalSettingsError(f"Saved {category} enabled setting must be boolean.")
        raw_roles = raw_category.get("roles", {})
        if not isinstance(raw_roles, Mapping):
            raise MedalSettingsError(f"Saved {category} roles must be an object.")
        result["categories"][category]["enabled"] = category_enabled
        result["categories"][category]["roles"] = {
            rank: _positive_id(
                raw_roles.get(rank),
                f"{category}.{rank}_role_id",
            )
            for rank in MEDAL_RANKS
        }

    raw_assignments = raw.get("assignments", {})
    if not isinstance(raw_assignments, Mapping):
        raise MedalSettingsError("Saved medal assignments must be an object.")
    for category in MEDAL_CATEGORIES:
        raw_category = raw_assignments.get(category, {})
        if not isinstance(raw_category, Mapping):
            raise MedalSettingsError(f"Saved {category} assignments are invalid.")
        for rank in MEDAL_RANKS:
            assignment = raw_category.get(rank, {})
            if not isinstance(assignment, Mapping):
                raise MedalSettingsError(
                    f"Saved {category}.{rank} assignment is invalid."
                )
            role_id = _positive_id(
                assignment.get("role_id"),
                f"{category}.{rank}.assigned_role_id",
            )
            member_ids = assignment.get("member_ids", [])
            if not isinstance(member_ids, list):
                raise MedalSettingsError(
                    f"Saved {category}.{rank} member IDs must be a list."
                )
            checked_ids = []
            for member_id in member_ids:
                parsed_id = _positive_id(
                    member_id,
                    f"{category}.{rank}.member_id",
                )
                if parsed_id not in checked_ids:
                    checked_ids.append(parsed_id)
            result["assignments"][category][rank] = {
                "role_id": role_id,
                "member_ids": checked_ids,
            }

    last_update = raw.get("last_update")
    if last_update is not None and not isinstance(last_update, str):
        raise MedalSettingsError("Saved medal update time is invalid.")
    result["last_update"] = last_update
    return result


def get_medal_settings(system: Any, guild_id: int) -> dict[str, Any]:
    state = system.store.snapshot()
    saved = state.get("level_medals", {}).get(str(guild_id))
    return _normalize_settings(saved)


def save_medal_settings(
    system: Any,
    guild_id: int,
    settings: Mapping[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    normalized = _normalize_settings(settings)
    with system.store.transaction() as state:
        all_settings = state.setdefault("level_medals", {})
        current = _normalize_settings(all_settings.get(str(guild_id)))
        if expected_revision is not None and current["revision"] != expected_revision:
            raise MedalSettingsError(
                "Medal settings changed during this update; reload and try again."
            )
        normalized["revision"] = current["revision"] + 1
        all_settings[str(guild_id)] = deepcopy(normalized)
    return deepcopy(normalized)


def _save_assignments(
    system: Any,
    guild_id: int,
    settings: Mapping[str, Any],
    expected_revision: int,
) -> bool:
    with system.store.transaction() as state:
        all_settings = state.setdefault("level_medals", {})
        current = _normalize_settings(all_settings.get(str(guild_id)))
        if current["revision"] != expected_revision:
            return False
        current["assignments"] = deepcopy(settings["assignments"])
        current["last_update"] = datetime.now(timezone.utc).isoformat()
        all_settings[str(guild_id)] = current
    return True


def clear_medal_settings(system: Any, admin_engine: Any, guild_id: int) -> bool:
    """Back up full game state, audit the deletion, then clear one guild's settings."""
    with system.store.transaction() as state:
        all_settings = state.setdefault("level_medals", {})
        guild_key = str(guild_id)
        if guild_key not in all_settings:
            return False
        logs = state.setdefault("admin_logs", [])
        if not isinstance(logs, list):
            raise MedalSettingsError("Admin audit log is invalid; operation aborted.")
        if admin_engine is None:
            raise MedalSettingsError("Backup service is unavailable; no change was made.")
        backup_path = admin_engine.create_backup(state=state)
        if not backup_path:
            raise MedalSettingsError("Backup failed; no change was made.")
        del all_settings[guild_key]
        logs.append(
            {
                "action": "clear_level_medals_settings",
                "details": f"guild_id={guild_id}; backup={backup_path.name}",
                "time": datetime.now(timezone.utc).isoformat(),
            }
        )
        del logs[:-100]
    return True


async def _resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


def _member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(int(role.id) == role_id for role in member.roles)


def _configured_role_ids(settings: Mapping[str, Any]) -> set[int]:
    used: set[int] = set()
    for category in MEDAL_CATEGORIES:
        category_settings = settings["categories"][category]
        for rank, role_id in category_settings["roles"].items():
            if role_id is None:
                continue
            if role_id in used:
                raise MedalSettingsError(
                    "استخدم رتبة مختلفة لكل ميدالية حتى لا تتعارض الفئات."
                )
            used.add(role_id)
    return used


async def update_guild_medals(
    bot: commands.Bot,
    system: Any,
    guild: discord.Guild,
) -> dict[str, int | bool]:
    """Reconcile medal roles using only member-role grants recorded by this feature."""
    settings = get_medal_settings(system, guild.id)
    if not settings["enabled"]:
        return {"active": False, "added": 0, "removed": 0}

    _configured_role_ids(settings)
    bot_member = getattr(guild, "me", None)
    if bot_member is None and bot.user is not None:
        bot_member = guild.get_member(bot.user.id)
    assignments = deepcopy(settings["assignments"])
    role_cache: dict[int, discord.Role] = {}
    plans: list[tuple[str, str, Mapping[str, int | None], dict[str, list[int]]]] = []

    for category, (label, metric) in MEDAL_CATEGORIES.items():
        category_settings = settings["categories"][category]
        if not category_settings["enabled"]:
            continue
        role_ids = category_settings["roles"]
        if any(role_ids[rank] is None for rank in MEDAL_RANKS):
            raise MedalSettingsError(
                f"فئة {label} مفعّلة لكن إحدى رتب الميداليات غير محددة."
            )
        for rank in MEDAL_RANKS:
            role_id = int(role_ids[rank])
            role = guild.get_role(role_id)
            reason = role_reward_block_reason(role, bot_member)
            if reason:
                raise MedalSettingsError(
                    f"تعذر تحديث ميداليات {label}: "
                    f"{MEDAL_ROLE_BLOCK_MESSAGES.get(reason, reason)}"
                )
            role_cache[role_id] = role

        leaders = system.leaderboard(guild.id, limit=3, metric=metric)
        desired_by_rank: dict[str, list[int]] = {rank: [] for rank in MEDAL_RANKS}
        for rank, entry in zip(MEDAL_RANKS, leaders):
            member_id = int(entry["user_id"])
            member = await _resolve_member(guild, member_id)
            if member is not None:
                desired_by_rank[rank].append(member_id)
        plans.append((category, label, role_ids, desired_by_rank))

    added = 0
    removed = 0
    for category, _label, role_ids, desired_by_rank in plans:
        for rank in MEDAL_RANKS:
            role_id = int(role_ids[rank])
            role = role_cache[role_id]
            previous = assignments[category][rank]
            previous_role_id = previous["role_id"]
            previous_member_ids = previous["member_ids"]
            desired_member_ids = desired_by_rank[rank]
            old_role = None
            if previous_role_id is not None:
                old_role = guild.get_role(int(previous_role_id))

            for member_id in previous_member_ids:
                keep_old_role = (
                    int(previous_role_id or 0) == role_id
                    and member_id in desired_member_ids
                )
                if keep_old_role:
                    continue
                member = await _resolve_member(guild, int(member_id))
                if member is None or old_role is None:
                    continue
                if _member_has_role(member, int(previous_role_id)):
                    await member.remove_roles(
                        old_role,
                        reason=f"Hourly level medal update ({category}/{rank})",
                    )
                    removed += 1

            for member_id in desired_member_ids:
                member = await _resolve_member(guild, member_id)
                if member is None or _member_has_role(member, role_id):
                    continue
                await member.add_roles(
                    role,
                    reason=f"Hourly level medal update ({category}/{rank})",
                )
                added += 1

            assignments[category][rank] = {
                "role_id": role_id,
                "member_ids": desired_member_ids,
            }

    settings["assignments"] = assignments
    saved = _save_assignments(
        system,
        guild.id,
        settings,
        expected_revision=int(settings["revision"]),
    )
    if not saved:
        logger.info(
            "Medal assignments changed while updating guild %s; next hourly run will reconcile.",
            guild.id,
        )
    return {
        "active": True,
        "added": added,
        "removed": removed,
    }


class _MedalClearConfirmation(discord.ui.View):
    def __init__(
        self,
        system: Any,
        admin_engine: Any,
        guild_id: int,
        developer_id: int,
    ) -> None:
        super().__init__(timeout=90)
        self.system = system
        self.admin_engine = admin_engine
        self.guild_id = guild_id
        self.developer_id = developer_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id or interaction.guild_id != self.guild_id:
            await interaction.response.send_message(
                "هذا التأكيد متاح لمطور البوت فقط وفي السيرفر نفسه.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(
                content="انتهت مهلة تأكيد حذف إعدادات الميداليات.",
                view=self,
            )
        except discord.HTTPException:
            logger.debug("Could not update expired medal-clear prompt.", exc_info=True)

    @discord.ui.button(
        label="تأكيد الحذف",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        try:
            changed = clear_medal_settings(
                self.system,
                self.admin_engine,
                self.guild_id,
            )
        except Exception:
            logger.exception(
                "Could not clear medal settings for guild %s.",
                self.guild_id,
            )
            await interaction.response.edit_message(
                content="تعذر إنشاء النسخة الاحتياطية أو حذف الإعدادات؛ لم يُحذف شيء.",
                view=None,
            )
            self.stop()
            return
        message = (
            "حُذفت إعدادات الميداليات بعد حفظ نسخة كاملة من البيانات. "
            "الرتب الممنوحة سابقًا لم تُزل وستبقى في السيرفر."
            if changed
            else "لا توجد إعدادات ميداليات محفوظة لحذفها."
        )
        await interaction.response.edit_message(content=message, view=None)
        self.stop()

    @discord.ui.button(
        label="إلغاء",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            content="تم إلغاء حذف إعدادات الميداليات.",
            view=None,
        )
        self.stop()


def setup_medal_commands(
    bot: commands.Bot,
    system: Any,
    developer_id: int,
    *,
    admin_engine: Any = None,
) -> None:
    """Register the owner/developer medal settings command."""
    if bot.tree.get_command("medals") is not None:
        return

    @bot.tree.command(
        name="medals",
        description="إدارة ميداليات أفضل الأعضاء حسب النشاط",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        action="الإجراء المطلوب",
        category="فئة الترتيب التي تريد إعدادها",
        gold="رتبة المركز الأول",
        silver="رتبة المركز الثاني",
        bronze="رتبة المركز الثالث",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="تفعيل التحديث", value="enable"),
            app_commands.Choice(name="إيقاف التحديث", value="disable"),
            app_commands.Choice(name="إعداد فئة", value="setup"),
            app_commands.Choice(name="تحديث الآن", value="update_now"),
            app_commands.Choice(name="عرض الإعدادات", value="show"),
            app_commands.Choice(name="مسح الإعدادات", value="clear"),
        ],
        category=[
            app_commands.Choice(name="الرسائل", value="messages"),
            app_commands.Choice(name="وقت الصوت", value="voice"),
            app_commands.Choice(name="XP", value="xp"),
        ],
    )
    async def medals_command(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        category: app_commands.Choice[str] | None = None,
        gold: discord.Role | None = None,
        silver: discord.Role | None = None,
        bronze: discord.Role | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر يعمل داخل السيرفر فقط.",
                ephemeral=True,
            )
            return
        is_developer = interaction.user.id == developer_id
        is_owner = interaction.user.id == guild.owner_id
        if not (is_developer or is_owner):
            await interaction.response.send_message(
                "إدارة الميداليات متاحة لمطور البوت أو صاحب السيرفر فقط.",
                ephemeral=True,
            )
            return

        selected_action = action.value
        if selected_action == "clear":
            if not is_developer:
                await interaction.response.send_message(
                    "مسح الإعدادات متاح لمطور البوت فقط.",
                    ephemeral=True,
                )
                return
            view = _MedalClearConfirmation(
                system,
                admin_engine,
                guild.id,
                developer_id,
            )
            await interaction.response.send_message(
                "سيُحذف إعداد الميداليات لهذا السيرفر بعد حفظ نسخة كاملة من بيانات اللعبة. "
                "الرتب التي مُنحت سابقًا ستبقى في السيرفر ولن يزيلها هذا الإجراء. "
                "هل تريد المتابعة؟",
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            view.message = await interaction.original_response()
            return

        await interaction.response.defer(ephemeral=True)
        try:
            settings = get_medal_settings(system, guild.id)
            if selected_action == "show":
                lines = [
                    f"التحديث العام: {'مفعّل' if settings['enabled'] else 'متوقف'}"
                ]
                for key, (label, _metric) in MEDAL_CATEGORIES.items():
                    current = settings["categories"][key]
                    state_label = "مفعّلة" if current["enabled"] else "متوقفة"
                    role_labels = []
                    for rank in MEDAL_RANKS:
                        role_id = current["roles"][rank]
                        role = guild.get_role(role_id) if role_id else None
                        role_labels.append(
                            f"{MEDAL_RANK_LABELS[rank]}: "
                            f"{role.mention if role else 'غير محددة'}"
                        )
                    lines.append(
                        f"**{label} — {state_label}**: " + "، ".join(role_labels)
                    )
                lines.append(
                    f"آخر تحديث: {settings['last_update'] or 'لم يُحدّث بعد'}"
                )
                await interaction.followup.send(
                    "\n".join(lines),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if selected_action == "setup":
                if category is None or gold is None or silver is None or bronze is None:
                    await interaction.followup.send(
                        "لإعداد فئة، اختر الفئة وحدد رتب الذهبية والفضية والبرونزية.",
                        ephemeral=True,
                    )
                    return
                if len({gold.id, silver.id, bronze.id}) != 3:
                    await interaction.followup.send(
                        "اختر ثلاث رتب مختلفة للمراكز الثلاثة.",
                        ephemeral=True,
                    )
                    return
                bot_member = getattr(guild, "me", None)
                if bot_member is None and bot.user is not None:
                    bot_member = guild.get_member(bot.user.id)
                for role in (gold, silver, bronze):
                    reason = role_reward_block_reason(role, bot_member)
                    if reason:
                        await interaction.followup.send(
                            MEDAL_ROLE_BLOCK_MESSAGES.get(reason, reason),
                            ephemeral=True,
                        )
                        return

                roles = {
                    "gold": gold.id,
                    "silver": silver.id,
                    "bronze": bronze.id,
                }
                category_key = category.value
                other_role_ids = {
                    role_id
                    for other_category in MEDAL_CATEGORIES
                    if other_category != category_key
                    for role_id in settings["categories"][other_category]["roles"].values()
                    if role_id is not None
                }
                if other_role_ids.intersection(roles.values()):
                    await interaction.followup.send(
                        "استخدم رتبًا مختلفة بين الفئات لتفادي تعارض الميداليات.",
                        ephemeral=True,
                    )
                    return
                settings["categories"][category_key] = {
                    "enabled": True,
                    "roles": roles,
                }
                saved = save_medal_settings(
                    system,
                    guild.id,
                    settings,
                    expected_revision=settings["revision"],
                )
                label = MEDAL_CATEGORIES[category_key][0]
                await interaction.followup.send(
                    f"تم حفظ ميداليات **{label}**. استخدم `/medals enable` "
                    "لتفعيل التحديث التلقائي، أو `/medals update_now` للتحديث فورًا.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if selected_action == "enable":
                if not any(
                    category_settings["enabled"]
                    for category_settings in settings["categories"].values()
                ):
                    await interaction.followup.send(
                        "أعد إعداد فئة واحدة على الأقل قبل تفعيل الميداليات.",
                        ephemeral=True,
                    )
                    return
                settings["enabled"] = True
                save_medal_settings(
                    system,
                    guild.id,
                    settings,
                    expected_revision=settings["revision"],
                )
                await interaction.followup.send(
                    "تم تفعيل تحديث الميداليات كل ساعة. الرتب الحالية لن تُزال عند الإيقاف.",
                    ephemeral=True,
                )
                return

            if selected_action == "disable":
                settings["enabled"] = False
                save_medal_settings(
                    system,
                    guild.id,
                    settings,
                    expected_revision=settings["revision"],
                )
                await interaction.followup.send(
                    "تم إيقاف التحديث التلقائي؛ الرتب الممنوحة حاليًا ستبقى كما هي.",
                    ephemeral=True,
                )
                return

            if selected_action == "update_now":
                if not settings["enabled"]:
                    await interaction.followup.send(
                        "فعّل التحديث أولًا باستخدام `/medals enable`.",
                        ephemeral=True,
                    )
                    return
                result = await update_guild_medals(bot, system, guild)
                await interaction.followup.send(
                    "اكتمل تحديث الميداليات. "
                    f"مُنحت {result['added']} رتبة وأزيلت {result['removed']} رتبة "
                    "من التعيينات السابقة التي يديرها البوت.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                "الإجراء غير معروف.",
                ephemeral=True,
            )
        except MedalSettingsError as error:
            logger.exception(
                "Medal command failed for guild %s (action=%s).",
                guild.id,
                selected_action,
            )
            await interaction.followup.send(
                f"تعذر تنفيذ الإجراء: {error}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception(
                "Medal command failed unexpectedly for guild %s (action=%s).",
                guild.id,
                selected_action,
            )
            await interaction.followup.send(
                "حدث خطأ غير متوقع. راجع سجل البوت ثم أعد المحاولة.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
