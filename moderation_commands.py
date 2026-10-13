"""Slash commands for member moderation, roles, autoroles, and keyword ACLs."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Awaitable, Callable

import discord
from discord import app_commands

from moderation import (
    KEYWORD_ACTIONS,
    MAX_PURGE_COUNT,
    MAX_SLOWMODE_SECONDS,
    MAX_TIMEOUT_SECONDS,
    ModerationError,
    ModerationService,
    format_duration,
    parse_duration,
    shortcut_help_embed,
)


logger = logging.getLogger(__name__)
_STORE_FAILED = object()


class ConfirmActionView(discord.ui.View):
    """Short-lived owner-only confirmation with visible timeout handling."""

    def __init__(
        self,
        owner_id: int,
        source_interaction: discord.Interaction,
        action: Callable[[], Awaitable[str]],
        *,
        timeout: float = 30,
    ):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.source_interaction = source_interaction
        self.action = action
        self.finished = False

    def _disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_timeout(self) -> None:
        if self.finished:
            return
        self.finished = True
        self._disable_buttons()
        try:
            await self.source_interaction.edit_original_response(
                content="انتهت مهلة التأكيد؛ لم يُنفّذ الإجراء.",
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.info("Could not update an expired moderation confirmation.")

    async def _check_actor(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "هذا التأكيد ظاهر لمن طلبه فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if self.finished:
            await interaction.response.send_message(
                "انتهت صلاحية هذا التأكيد.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    @discord.ui.button(label="تأكيد", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if not await self._check_actor(interaction):
            return
        self.finished = True
        self._disable_buttons()
        await interaction.response.defer()
        try:
            result = await self.action()
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Confirmed moderation action failed in Discord.")
            result = "رفض Discord الإجراء أو تعذر تنفيذه؛ تحقق من الصلاحيات وترتيب الرتب."
        except OSError:
            logger.exception("Could not save a confirmed moderation change.")
            result = "تعذر حفظ التغيير؛ لم يكتمل الإجراء."
        await self.source_interaction.edit_original_response(
            content=result,
            embed=None,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="إلغاء", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if not await self._check_actor(interaction):
            return
        self.finished = True
        self._disable_buttons()
        await interaction.response.edit_message(
            content="أُلغي الإجراء.",
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def _send_ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    kwargs = {
        "content": content,
        "embed": embed,
        "view": view,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def _persist_settings(interaction, operation, failure_message: str):
    try:
        return operation()
    except OSError:
        logger.exception("Could not persist moderation settings.")
        await _send_ephemeral(interaction, failure_message)
        return _STORE_FAILED


def _is_config_manager(service: ModerationService, interaction) -> bool:
    return service.is_config_manager(interaction.user, interaction.guild)


def _role_is_assignable(service: ModerationService, guild, role: discord.Role) -> str | None:
    if role.guild.id != guild.id:
        return "اختر رتبة من هذا السيرفر."
    if role.is_default() or role.managed:
        return "لا يمكن إدارة رتبة @everyone أو الرتب المُدارة من تكامل."
    bot_member = service._bot_member(guild)
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        return "يحتاج البوت إلى صلاحية Manage Roles."
    if bot_member.top_role <= role:
        return "رتبة البوت يجب أن تكون أعلى من الرتبة المحددة."
    return None


def _roles_to_assign(service: ModerationService, guild, member: discord.Member):
    bot_member = service._bot_member(guild)
    if bot_member is None:
        return [], list(member.roles)
    removable = [
        role
        for role in member.roles
        if not role.is_default()
        and not role.managed
        and bot_member.top_role > role
    ]
    blocked = [role for role in member.roles if not role.is_default() and role not in removable]
    return removable, blocked


def _short_list(values: list[str], empty: str = "لا يوجد") -> str:
    if not values:
        return empty
    text = "\n".join(values)
    if len(text) > 1000:
        text = text[:997] + "..."
    return text


def setup_moderation_commands(
    bot,
    service: ModerationService,
    developer_id: int,
):
    """Register the full moderation command set exactly once."""
    existing = getattr(bot, "_moderation_command_setup", None)
    if existing is not None:
        return existing

    @bot.tree.command(name="role", description="إدارة رتب الأعضاء")
    @app_commands.guild_only()
    @app_commands.describe(
        action="العملية المطلوبة",
        user="العضو المستهدف",
        role="الرتبة المطلوبة",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="إضافة رتبة", value="add"),
            app_commands.Choice(name="إزالة رتبة", value="remove"),
            app_commands.Choice(name="عرض الرتب", value="show"),
            app_commands.Choice(name="تبديل رتبة", value="toggle"),
            app_commands.Choice(name="إزالة كل الرتب", value="clear"),
        ]
    )
    async def role_command(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.Member | None = None,
        role: discord.Role | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not _is_config_manager(service, interaction):
            await _send_ephemeral(
                interaction,
                "هذا الأمر لصاحب السيرفر أو للمطور فقط.",
            )
            return
        target = user or interaction.user
        if action.value == "show":
            roles = sorted(
                (candidate for candidate in target.roles if not candidate.is_default()),
                key=lambda candidate: candidate.position,
                reverse=True,
            )
            embed = discord.Embed(
                title=f"رتب {target.display_name}",
                color=target.color if target.color.value else discord.Color.blurple(),
            )
            embed.set_thumbnail(url=target.display_avatar.url)
            embed.add_field(name="العضو", value=f"{target.mention}\n`{target.id}`", inline=True)
            embed.add_field(name="العدد", value=str(len(roles)), inline=True)
            embed.add_field(
                name="الرتب",
                value=_short_list([candidate.mention for candidate in roles]),
                inline=False,
            )
            await _send_ephemeral(interaction, embed=embed)
            return

        if action.value == "clear":
            if user is None:
                await _send_ephemeral(interaction, "حدد العضو الذي تريد إزالة رتبه.")
                return
            if target.id == guild.owner_id:
                await _send_ephemeral(interaction, "لا يمكن إزالة رتب صاحب السيرفر.")
                return
            removable, blocked = _roles_to_assign(service, guild, target)
            if not removable:
                await _send_ephemeral(interaction, "لا توجد رتب يستطيع البوت إزالتها.")
                return
            blocked_note = (
                f"\nستبقى {len(blocked)} رتبة أعلى من رتبة البوت أو مُدارة."
                if blocked
                else ""
            )

            async def clear_roles() -> str:
                current, _ = _roles_to_assign(service, guild, target)
                if not current:
                    return "لم تعد هناك رتب يستطيع البوت إزالتها."
                await target.remove_roles(
                    *current,
                    reason=f"إزالة رتب بطلب {interaction.user}",
                )
                return f"أُزيلت {len(current)} رتبة من {target.mention}."

            view = ConfirmActionView(interaction.user.id, interaction, clear_roles)
            await _send_ephemeral(
                interaction,
                f"هل تريد إزالة {len(removable)} رتبة من {target.mention}؟"
                f"{blocked_note}\nلا يمكن التراجع عن هذا التغيير.",
                view=view,
            )
            return

        if user is None or role is None:
            await _send_ephemeral(interaction, "حدد العضو والرتبة.")
            return
        if target.id == guild.owner_id:
            await _send_ephemeral(interaction, "لا يمكن تغيير رتب صاحب السيرفر.")
            return
        role_error = _role_is_assignable(service, guild, role)
        if role_error:
            await _send_ephemeral(interaction, role_error)
            return

        removing = action.value == "remove" or (
            action.value == "toggle" and role in target.roles
        )
        if action.value not in {"add", "remove", "toggle"}:
            await _send_ephemeral(interaction, "العملية غير معروفة.")
            return
        if removing and role not in target.roles:
            await _send_ephemeral(interaction, f"{target.mention} لا يملك هذه الرتبة.")
            return
        if not removing and role in target.roles:
            await _send_ephemeral(interaction, f"{target.mention} يملك هذه الرتبة بالفعل.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if removing:
                await target.remove_roles(role, reason=f"بواسطة {interaction.user}")
                text = f"أُزيلت رتبة {role.mention} من {target.mention}."
                title = "تمت إزالة رتبة"
            else:
                await target.add_roles(role, reason=f"بواسطة {interaction.user}")
                text = f"أُضيفت رتبة {role.mention} إلى {target.mention}."
                title = "تمت إضافة رتبة"
            try:
                await target.send(
                    embed=discord.Embed(
                        title=title,
                        description=f"في سيرفر **{guild.name}**\nالرتبة: {role.name}",
                        color=0x3498DB,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.info("Could not send role-change DM to member %s.", target.id)
            await interaction.followup.send(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "رفض Discord تغيير الرتبة؛ تحقق من ترتيب رتب البوت وصلاحيته.",
                ephemeral=True,
            )
        except discord.HTTPException:
            logger.exception("Role change failed in guild %s.", guild.id)
            await interaction.followup.send(
                "تعذر تغيير الرتبة بسبب خطأ من Discord.",
                ephemeral=True,
            )

    @bot.tree.command(name="autorole", description="إعداد الرتب والترحيب للأعضاء الجدد")
    @app_commands.guild_only()
    @app_commands.describe(
        action="العملية المطلوبة",
        role="الرتبة",
        channel="قناة الترحيب",
        message="نص الترحيب؛ المتغيرات: {user} و{name} و{server} و{count}",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="تفعيل", value="enable"),
            app_commands.Choice(name="تعطيل", value="disable"),
            app_commands.Choice(name="إضافة رتبة للأعضاء", value="add_role"),
            app_commands.Choice(name="إزالة رتبة للأعضاء", value="remove_role"),
            app_commands.Choice(name="عرض الرتب", value="list"),
            app_commands.Choice(name="إضافة رتبة للبوتات", value="bots"),
            app_commands.Choice(name="إزالة رتبة للبوتات", value="remove_bots_role"),
            app_commands.Choice(name="ضبط الترحيب", value="welcome"),
            app_commands.Choice(name="حذف الترحيب", value="clear_welcome"),
            app_commands.Choice(name="إعادة ضبط الإعدادات", value="reset"),
            app_commands.Choice(name="عرض الإعدادات", value="show"),
        ]
    )
    async def autorole_command(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        role: discord.Role | None = None,
        channel: discord.TextChannel | None = None,
        message: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or not _is_config_manager(service, interaction):
            await _send_ephemeral(
                interaction,
                "هذا الأمر لصاحب السيرفر أو للمطور فقط.",
            )
            return
        store = service.autorole_store
        settings = store.get(guild.id)
        if action.value == "enable":
            if not bot.intents.members:
                await _send_ephemeral(
                    interaction,
                    "فعّل Server Members Intent في بوابة Discord واضبط "
                    "`DISCORD_MEMBERS_INTENT=1` في Replit قبل تشغيل AutoRole.",
                )
                return
            if not (
                settings["roles"]
                or settings["bots_roles"]
                or settings["welcome_message"]
            ):
                await _send_ephemeral(
                    interaction,
                    "أضف رتبة للأعضاء أو للبوتات، أو اضبط رسالة ترحيب أولاً.",
                )
                return
            saved = await _persist_settings(
                interaction,
                lambda: store.set_enabled(guild.id, True),
                "تعذر حفظ إعداد AutoRole.",
            )
            if saved is _STORE_FAILED:
                return
            await _send_ephemeral(interaction, "تم تفعيل AutoRole والترحيب.")
            return
        if action.value == "disable":
            saved = await _persist_settings(
                interaction,
                lambda: store.set_enabled(guild.id, False),
                "تعذر حفظ إعداد AutoRole.",
            )
            if saved is _STORE_FAILED:
                return
            await _send_ephemeral(interaction, "تم تعطيل AutoRole والترحيب.")
            return
        if action.value == "show" or action.value == "list":
            def roles_text(ids):
                return [
                    (
                        guild.get_role(role_id).mention
                        if guild.get_role(role_id)
                        else f"رتبة محذوفة (`{role_id}`)"
                    )
                    for role_id in ids
                ]

            embed = discord.Embed(
                title="إعدادات AutoRole",
                description="✅ مفعّل" if settings["enabled"] else "❌ معطّل",
                color=0x3498DB,
            )
            embed.add_field(
                name=f"رتب الأعضاء ({len(settings['roles'])})",
                value=_short_list(roles_text(settings["roles"])),
                inline=False,
            )
            embed.add_field(
                name=f"رتب البوتات ({len(settings['bots_roles'])})",
                value=_short_list(roles_text(settings["bots_roles"])),
                inline=False,
            )
            if settings["welcome_message"]:
                channel_obj = guild.get_channel(settings["welcome_channel"])
                embed.add_field(
                    name="رسالة الترحيب",
                    value=(
                        f"القناة: {channel_obj.mention if channel_obj else 'غير موجودة'}\n"
                        f"{settings['welcome_message'][:800]}"
                    ),
                    inline=False,
                )
            await _send_ephemeral(interaction, embed=embed)
            return
        if action.value in {"add_role", "remove_role", "bots", "remove_bots_role"}:
            if role is None:
                await _send_ephemeral(interaction, "حدد الرتبة.")
                return
            role_error = _role_is_assignable(service, guild, role)
            if role_error:
                await _send_ephemeral(interaction, role_error)
                return
            if action.value == "add_role":
                operation = lambda: store.add_role(guild.id, role.id)
                message_text = f"{role.mention} أُضيفت لرتب الأعضاء الجدد."
            elif action.value == "remove_role":
                operation = lambda: store.remove_role(guild.id, role.id)
                message_text = f"{role.mention} أُزيلت من رتب الأعضاء الجدد."
            elif action.value == "bots":
                operation = lambda: store.add_bot_role(guild.id, role.id)
                message_text = f"{role.mention} أُضيفت لرتب البوتات الجديدة."
            else:
                operation = lambda: store.remove_bot_role(guild.id, role.id)
                message_text = f"{role.mention} أُزيلت من رتب البوتات الجديدة."
            changed = await _persist_settings(
                interaction,
                operation,
                "تعذر حفظ إعداد AutoRole.",
            )
            if changed is _STORE_FAILED:
                return
            await _send_ephemeral(
                interaction,
                message_text if changed else "هذه الرتبة موجودة في الإعداد كما هو بالفعل.",
            )
            return
        if action.value == "welcome":
            if channel is None or message is None:
                await _send_ephemeral(
                    interaction,
                    "حدد قناة ونصاً. المتغيرات المتاحة: `{user}` و`{name}` و"
                    "`{server}` و`{count}`.",
                )
                return
            if channel.guild.id != guild.id:
                await _send_ephemeral(interaction, "اختر قناة من هذا السيرفر.")
                return
            try:
                store.set_welcome(guild.id, channel.id, message)
            except (ValueError, OSError):
                logger.exception("Could not save autorole welcome settings.")
                await _send_ephemeral(interaction, "تعذر حفظ رسالة الترحيب.")
                return
            preview = (
                message.replace("{user}", interaction.user.mention)
                .replace("{name}", interaction.user.display_name)
                .replace("{server}", guild.name)
                .replace("{count}", str(guild.member_count or "غير متاح"))
            )
            await _send_ephemeral(
                interaction,
                f"حُفظ الترحيب في {channel.mention}.\nالمعاينة:\n{preview[:1500]}",
            )
            return
        if action.value == "clear_welcome":
            saved = await _persist_settings(
                interaction,
                lambda: store.clear_welcome(guild.id),
                "تعذر حفظ إعداد AutoRole.",
            )
            if saved is _STORE_FAILED:
                return
            await _send_ephemeral(interaction, "حُذفت رسالة الترحيب.")
            return
        if action.value == "reset":
            async def reset_autorole() -> str:
                store.reset(guild.id)
                return "أُعيدت إعدادات AutoRole لهذا السيرفر إلى الوضع الافتراضي."

            view = ConfirmActionView(
                interaction.user.id,
                interaction,
                reset_autorole,
            )
            await _send_ephemeral(
                interaction,
                "سيُحذف إعداد AutoRole والترحيب المحفوظان لهذا السيرفر. تأكيد؟",
                view=view,
            )

    @bot.tree.command(name="keyword", description="إدارة صلاحيات اختصارات الإشراف")
    @app_commands.guild_only()
    @app_commands.describe(
        action="العملية المطلوبة",
        keyword="الكلمة كما تُكتب في الرسالة",
        role="الرتبة التي يسمح لها باستخدام الكلمة",
        user="عضو يُسمح له باستخدام كل الكلمات",
        channel="القناة",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="السماح لرتبة بكلمة", value="add_role_keyword"),
            app_commands.Choice(name="إزالة رتبة من كلمة", value="remove_role_keyword"),
            app_commands.Choice(name="حذف رتب كلمة", value="clear_keyword"),
            app_commands.Choice(name="السماح لعضو بكل الكلمات", value="add_user"),
            app_commands.Choice(name="إزالة عضو مسموح", value="remove_user"),
            app_commands.Choice(name="السماح بقناة", value="add_channel"),
            app_commands.Choice(name="إزالة قناة مسموحة", value="remove_channel"),
            app_commands.Choice(name="منع قناة", value="block_channel"),
            app_commands.Choice(name="إلغاء منع قناة", value="unblock_channel"),
            app_commands.Choice(name="عرض الإعدادات", value="show"),
            app_commands.Choice(name="عرض الكلمات المسموحة لي", value="my_keywords"),
            app_commands.Choice(name="إعادة ضبط الصلاحيات", value="reset"),
        ]
    )
    async def keyword_command(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        keyword: str | None = None,
        role: discord.Role | None = None,
        user: discord.Member | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        store = service.keyword_store
        settings = store.get(guild.id) if guild else None
        if guild is None:
            await _send_ephemeral(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
            return
        if action.value == "my_keywords":
            roles = {role.id for role in interaction.user.roles}
            is_all = (
                service.is_developer_or_owner(interaction.user, guild)
                or interaction.user.guild_permissions.administrator
                or interaction.user.id in settings["allowed_users"]
            )
            allowed = [
                f"`{word}` → {KEYWORD_ACTIONS[word]}"
                for word, role_ids in settings["keyword_roles"].items()
                if roles.intersection(role_ids)
            ]
            embed = discord.Embed(
                title="اختصاراتك المسموحة",
                description=(
                    "يمكنك استخدام كل الاختصارات."
                    if is_all
                    else "\n".join(allowed) if allowed else "ليس لديك صلاحية مخصصة."
                ),
                color=0x2ECC71,
            )
            await _send_ephemeral(interaction, embed=embed)
            return
        if not _is_config_manager(service, interaction):
            await _send_ephemeral(
                interaction,
                "إعداد هذه الصلاحيات لصاحب السيرفر أو للمطور فقط.",
            )
            return

        if action.value in {
            "add_role_keyword",
            "remove_role_keyword",
            "clear_keyword",
        }:
            if not keyword or keyword not in KEYWORD_ACTIONS:
                sample = "، ".join(f"`{word}`" for word in list(KEYWORD_ACTIONS)[:12])
                await _send_ephemeral(
                    interaction,
                    f"اكتب كلمة مسجلة كما هي. أمثلة: {sample}",
                )
                return
            try:
                if action.value == "clear_keyword":
                    changed = store.clear_keyword_roles(guild.id, keyword)
                    result = f"حُذفت الرتب المخصصة للكلمة `{keyword}`."
                else:
                    if role is None:
                        await _send_ephemeral(interaction, "حدد الرتبة.")
                        return
                    if role.is_default() or role.managed:
                        await _send_ephemeral(
                            interaction,
                            "لا يمكن استخدام @everyone أو رتبة مُدارة لهذا الإعداد.",
                        )
                        return
                    if action.value == "add_role_keyword":
                        changed = store.add_keyword_role(guild.id, keyword, role.id)
                        result = f"أصبحت {role.mention} مسموحة لاستخدام `{keyword}`."
                    else:
                        changed = store.remove_keyword_role(
                            guild.id, keyword, role.id
                        )
                        result = f"أُزيل السماح لـ {role.mention} باستخدام `{keyword}`."
            except (ValueError, OSError):
                logger.exception("Could not update keyword role permissions.")
                await _send_ephemeral(interaction, "تعذر حفظ صلاحيات الكلمة.")
                return
            await _send_ephemeral(
                interaction,
                result if changed else "لم يتغير شيء؛ الإعداد موجود أو غير موجود بالفعل.",
            )
            return

        if action.value in {"add_user", "remove_user"}:
            if user is None:
                await _send_ephemeral(interaction, "حدد العضو.")
                return
            changed = await _persist_settings(
                interaction,
                lambda: (
                    store.add_user(guild.id, user.id)
                    if action.value == "add_user"
                    else store.remove_user(guild.id, user.id)
                ),
                "تعذر حفظ صلاحيات الاختصارات.",
            )
            if changed is _STORE_FAILED:
                return
            result = (
                f"سُمح لـ {user.mention} باستخدام كل الاختصارات."
                if action.value == "add_user"
                else f"أُزيل السماح المخصص لـ {user.mention}."
            )
            await _send_ephemeral(
                interaction,
                result if changed else "لم يتغير شيء؛ الإعداد موجود أو غير موجود بالفعل.",
            )
            return

        if action.value in {
            "add_channel",
            "remove_channel",
            "block_channel",
            "unblock_channel",
        }:
            if channel is None:
                await _send_ephemeral(interaction, "حدد القناة.")
                return
            if channel.guild.id != guild.id:
                await _send_ephemeral(interaction, "اختر قناة من هذا السيرفر.")
                return
            store_action = {
                "add_channel": store.add_allowed_channel,
                "remove_channel": store.remove_allowed_channel,
                "block_channel": store.block_channel,
                "unblock_channel": store.unblock_channel,
            }[action.value]
            changed = await _persist_settings(
                interaction,
                lambda: store_action(guild.id, channel.id),
                "تعذر حفظ صلاحيات الاختصارات.",
            )
            if changed is _STORE_FAILED:
                return
            descriptions = {
                "add_channel": f"السماح بالاختصارات في {channel.mention}.",
                "remove_channel": f"إزالة {channel.mention} من قائمة القنوات المسموحة.",
                "block_channel": f"منع الاختصارات في {channel.mention}.",
                "unblock_channel": f"إلغاء منع الاختصارات في {channel.mention}.",
            }
            await _send_ephemeral(
                interaction,
                descriptions[action.value]
                if changed
                else "لم يتغير شيء؛ الإعداد موجود أو غير موجود بالفعل.",
            )
            return

        if action.value == "show":
            roles = [
                f"`{word}` ({KEYWORD_ACTIONS[word]}): "
                + " ".join(f"<@&{role_id}>" for role_id in role_ids)
                for word, role_ids in settings["keyword_roles"].items()
            ]
            users = [f"<@{user_id}>" for user_id in settings["allowed_users"]]
            embed = discord.Embed(
                title="صلاحيات اختصارات الإشراف",
                color=0x3498DB,
            )
            embed.add_field(
                name="الرتب لكل كلمة",
                value=_short_list(roles),
                inline=False,
            )
            embed.add_field(
                name="أعضاء مسموحون بكل الكلمات",
                value=_short_list(users),
                inline=False,
            )
            embed.add_field(
                name="قنوات مسموحة",
                value=_short_list(
                    [f"<#{channel_id}>" for channel_id in settings["whitelist_channels"]]
                ),
                inline=False,
            )
            embed.add_field(
                name="قنوات ممنوعة",
                value=_short_list(
                    [f"<#{channel_id}>" for channel_id in settings["blacklist_channels"]]
                ),
                inline=False,
            )
            await _send_ephemeral(interaction, embed=embed)
            return

        if action.value == "reset":
            async def reset_keywords() -> str:
                store.reset(guild.id)
                return "أُعيدت صلاحيات الاختصارات لهذا السيرفر إلى الوضع الافتراضي."

            view = ConfirmActionView(
                interaction.user.id,
                interaction,
                reset_keywords,
            )
            await _send_ephemeral(
                interaction,
                "سيُحذف إعداد الرتب والأعضاء والقنوات المخصصة للاختصارات. تأكيد؟",
                view=view,
            )

    @bot.tree.command(
        name="shortcuts",
        description="عرض قائمة اختصارات الإشراف وطريقة استخدامها",
    )
    @app_commands.guild_only()
    async def shortcuts_command(interaction: discord.Interaction) -> None:
        await _send_ephemeral(interaction, embed=shortcut_help_embed())

    async def run_action(
        interaction: discord.Interaction,
        action: str,
        *,
        target: discord.Member | None = None,
        duration: timedelta | None = None,
        reason: str = "بدون سبب",
        count: int | None = None,
        channel=None,
        slowmode_seconds: int | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await _send_ephemeral(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
            return
        if not service.actor_can_use(action, interaction.user, guild):
            needed = service._ACTOR_PERMISSIONS.get(action)
            await _send_ephemeral(
                interaction,
                f"تحتاج صلاحية `{needed or 'إدارة السيرفر'}` لاستخدام هذا الأمر.",
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            text, embed = await service.execute(
                action,
                guild,
                interaction.user,
                target=target,
                duration=duration,
                reason=reason,
                count=count,
                channel=channel or interaction.channel,
                slowmode_seconds=slowmode_seconds,
            )
        except ModerationError as error:
            text, embed = str(error), None
        except discord.Forbidden:
            text, embed = "رفض Discord الإجراء؛ تحقق من صلاحيات البوت وترتيب الرتب.", None
        except discord.NotFound:
            text, embed = "لم يعد العضو أو العنصر المحدد موجوداً.", None
        except OSError:
            logger.exception("Could not persist temporary moderation state.")
            text, embed = "تعذر حفظ الإجراء المؤقت؛ لم يكتمل الإجراء.", None
        except discord.HTTPException:
            logger.exception(
                "Slash moderation action %s failed in guild %s.",
                action,
                guild.id,
            )
            text, embed = "تعذر تنفيذ الإجراء بسبب خطأ مؤقت من Discord.", None
        await interaction.followup.send(
            text or None,
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @bot.tree.command(name="ban", description="حظر عضو؛ المدة اختيارية للحظر المؤقت")
    @app_commands.guild_only()
    @app_commands.describe(
        member="العضو المراد حظره",
        duration="مدة اختيارية مثل 1h أو 7d؛ اتركها فارغة للحظر الدائم",
        reason="سبب الحظر",
    )
    async def ban_command(
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str | None = None,
        reason: str | None = None,
    ) -> None:
        parsed = None
        if duration:
            try:
                parsed = parse_duration(duration)
            except ModerationError as error:
                await _send_ephemeral(interaction, str(error))
                return
            if parsed is None:
                await _send_ephemeral(
                    interaction,
                    "صيغة المدة غير صحيحة. استخدم مثلاً `1h` أو `2d`.",
                )
                return
        await run_action(
            interaction,
            "ban",
            target=member,
            duration=parsed,
            reason=reason or "بدون سبب",
        )

    @bot.tree.command(name="kick", description="طرد عضو من السيرفر")
    @app_commands.guild_only()
    @app_commands.describe(member="العضو المراد طرده", reason="سبب الطرد")
    async def kick_command(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        await run_action(
            interaction,
            "kick",
            target=member,
            reason=reason or "بدون سبب",
        )

    @bot.tree.command(name="timeout", description="إسكات عضو لمدة محددة")
    @app_commands.guild_only()
    @app_commands.describe(
        member="العضو المراد إسكاتُه",
        duration="مدة من ثانية إلى 28 يوماً، مثل 10m أو 2h",
        reason="سبب الإسكات",
    )
    async def timeout_command(
        interaction: discord.Interaction,
        member: discord.Member,
        duration: str,
        reason: str | None = None,
    ) -> None:
        try:
            parsed = parse_duration(duration)
        except ModerationError as error:
            await _send_ephemeral(interaction, str(error))
            return
        if parsed is None or parsed.total_seconds() > MAX_TIMEOUT_SECONDS:
            await _send_ephemeral(
                interaction,
                "حدد مدة صحيحة من ثانية إلى 28 يوماً، مثل `10m` أو `2h`.",
            )
            return
        await run_action(
            interaction,
            "mute",
            target=member,
            duration=parsed,
            reason=reason or "بدون سبب",
        )

    @bot.tree.command(name="unmute", description="إزالة مهلة الإسكات عن عضو")
    @app_commands.guild_only()
    @app_commands.describe(member="العضو", reason="سبب فك الإسكات")
    async def unmute_command(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        await run_action(
            interaction,
            "unmute",
            target=member,
            reason=reason or "بدون سبب",
        )

    @bot.tree.command(name="warn", description="إرسال تحذير خاص إلى عضو")
    @app_commands.guild_only()
    @app_commands.describe(member="العضو", reason="سبب التحذير")
    async def warn_command(
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        await run_action(
            interaction,
            "warn",
            target=member,
            reason=reason or "بدون سبب",
        )

    @bot.tree.command(name="memberinfo", description="عرض معلومات عضو في السيرفر")
    @app_commands.guild_only()
    @app_commands.describe(member="العضو؛ افتراضياً أنت")
    async def memberinfo_command(
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await run_action(interaction, "info", target=member or interaction.user)

    @bot.tree.command(name="purge", description="حذف عدد من الرسائل الأخيرة")
    @app_commands.guild_only()
    @app_commands.describe(
        count=f"عدد الرسائل من 1 إلى {MAX_PURGE_COUNT}",
        member="اختياري: حذف رسائل عضو محدد فقط",
    )
    async def purge_command(
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, MAX_PURGE_COUNT],
        member: discord.Member | None = None,
    ) -> None:
        await run_action(
            interaction,
            "purge",
            target=member,
            count=count,
            channel=interaction.channel,
        )

    @bot.tree.command(name="slowmode", description="ضبط الوضع البطيء للقناة الحالية")
    @app_commands.guild_only()
    @app_commands.describe(
        seconds=f"0 لإيقافه، أو مدة من 1 إلى {MAX_SLOWMODE_SECONDS} ثانية"
    )
    async def slowmode_command(
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, MAX_SLOWMODE_SECONDS],
    ) -> None:
        await run_action(
            interaction,
            "slowmode",
            channel=interaction.channel,
            slowmode_seconds=seconds,
        )

    @bot.tree.command(
        name="send_weekly_stats",
        description="إرسال ملخص الإشراف الأسبوعي للمطور وصاحب السيرفر",
    )
    @app_commands.guild_only()
    async def send_weekly_stats_command(interaction: discord.Interaction) -> None:
        if interaction.user.id != developer_id:
            await _send_ephemeral(interaction, "هذا الأمر متاح للمطور فقط.")
            return
        if interaction.guild is None:
            await _send_ephemeral(interaction, "هذا الأمر يعمل داخل السيرفر فقط.")
            return
        await interaction.response.defer(ephemeral=True)
        status = await service.send_weekly_stats(interaction.guild)
        messages = {
            "sent": "أُرسل الملخص الأسبوعي إلى المطور وصاحب السيرفر على الخاص.",
            "no_activity": "لا توجد إجراءات إشرافية مسجلة خلال آخر 7 أيام.",
            "recipient_unavailable": "تعذر العثور على المطور أو صاحب السيرفر لإرسال الملخص.",
            "delivery_failed": "تعذر إرسال الملخص إلى الخاص للمطور أو صاحب السيرفر.",
            "partial_delivery": "أُرسل الملخص لبعض المستلمين وتعذر إرساله لآخرين؛ راجع سجل التشغيل.",
            "guild_not_allowed": "هذا السيرفر غير متاح في بيئة التشغيل الحالية.",
            "unavailable": "إحصاءات الإشراف غير مفعّلة.",
        }
        await interaction.followup.send(
            messages.get(status, "تعذر إرسال الملخص الأسبوعي."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot.add_listener(service.on_member_join, "on_member_join")
    bot.add_listener(service.on_ready, "on_ready")
    bot._moderation_command_setup = service
    bot._moderation_developer_id = developer_id
    return service
