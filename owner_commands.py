"""Developer-only commands for managing per-server assistant owners."""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands

from co_owners import CoOwnerStore


logger = logging.getLogger(__name__)


class CoOwnerResetView(discord.ui.View):
    """Developer-only confirmation for clearing a guild's assistant-owner list."""

    def __init__(
        self,
        developer_id: int,
        store: CoOwnerStore,
        guild_id: int,
        source_interaction: discord.Interaction,
        *,
        timeout: float = 60,
    ):
        super().__init__(timeout=timeout)
        self.developer_id = int(developer_id)
        self.store = store
        self.guild_id = int(guild_id)
        self.source_interaction = source_interaction
        self.deadline = time.monotonic() + timeout
        self.finished = False

    def _disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def _check_developer(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.developer_id:
            await interaction.response.send_message(
                "هذا التأكيد خاص بمطور البوت.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        if self.finished or time.monotonic() >= self.deadline:
            self.finished = True
            self._disable_buttons()
            try:
                await self.source_interaction.edit_original_response(
                    content="انتهت مهلة التأكيد؛ لم تُحذف أي صلاحيات.",
                    embed=None,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.info("Could not update a late assistant-owner reset click.")
            await interaction.response.send_message(
                "انتهت صلاحية هذا التأكيد؛ لم تُحذف أي صلاحيات.",
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
        try:
            await self.source_interaction.edit_original_response(
                content="انتهت مهلة التأكيد؛ لم تُحذف أي صلاحيات.",
                embed=None,
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.info("Could not update an expired assistant-owner reset prompt.")

    @discord.ui.button(label="تأكيد الحذف", style=discord.ButtonStyle.danger)
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
            count, backup_name = self.store.reset_with_backup(self.guild_id)
            result = (
                f"حُذفت صلاحيات {count} أونر مساعد بعد حفظ نسخة خاصة "
                f"(`{backup_name}`)."
                if count
                else "لا توجد أونرز مساعدين؛ لم يتغير شيء."
            )
        except (OSError, ValueError):
            logger.exception(
                "Could not back up and reset assistant owners for guild %s.",
                self.guild_id,
            )
            result = "تعذر حفظ النسخة الاحتياطية؛ لم تُحذف أي صلاحيات."
        await self.source_interaction.edit_original_response(
            content=result,
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
            content="أُلغي الحذف؛ لم تتغير الصلاحيات.",
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def setup_owner_commands(
    bot,
    store: CoOwnerStore,
    developer_id: int,
):
    """Register per-guild assistant-owner management once."""
    existing = getattr(bot, "_co_owner_commands", None)
    if existing is not None:
        return existing

    @bot.tree.command(
        name="owner",
        description="إدارة الأونرز المساعدين في هذا السيرفر؛ للمطور فقط",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        action="العملية المطلوبة",
        user="العضو المطلوب إضافته أو إزالته",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="إضافة أونر مساعد", value="add"),
            app_commands.Choice(name="إزالة أونر مساعد", value="remove"),
            app_commands.Choice(name="عرض القائمة", value="list"),
            app_commands.Choice(name="إعادة ضبط القائمة", value="reset"),
        ]
    )
    async def owner_command(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.Member | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "هذا الأمر يعمل داخل السيرفر فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if interaction.user.id != int(developer_id):
            await interaction.response.send_message(
                "إدارة قائمة الأونرز المساعدين متاحة لمطور البوت فقط.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value in {"add", "remove"}:
            if user is None:
                await interaction.response.send_message(
                    "حدد عضواً من هذا السيرفر.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if action.value == "add" and (
                user.bot
                or user.id in {int(developer_id), guild.owner_id}
            ):
                await interaction.response.send_message(
                    "لا يمكن إضافة المطور أو صاحب السيرفر أو بوت كأونر مساعد.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            try:
                changed = (
                    store.add(guild.id, user.id)
                    if action.value == "add"
                    else store.remove(guild.id, user.id)
                )
            except (OSError, ValueError):
                logger.exception(
                    "Could not update assistant-owner access for guild %s.",
                    guild.id,
                )
                await interaction.response.send_message(
                    "تعذر حفظ قائمة الأونرز المساعدين.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if action.value == "add":
                text = (
                    f"أُضيف {user.mention} كأونر مساعد لهذا السيرفر."
                    if changed
                    else f"{user.mention} موجود في القائمة بالفعل."
                )
            else:
                text = (
                    f"أُزيل {user.mention} من قائمة الأونرز المساعدين."
                    if changed
                    else f"{user.mention} غير موجود في القائمة."
                )
            await interaction.response.send_message(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value == "list":
            members = store.get(guild.id)
            owner = guild.get_member(guild.owner_id)
            owner_label = (
                f"{owner.mention} (`{discord.utils.escape_markdown(owner.display_name)}`)"
                if owner is not None
                else f"<@{guild.owner_id}>"
            )
            lines = []
            for index, user_id in enumerate(members, 1):
                member = guild.get_member(user_id)
                label = (
                    f"{member.mention} (`{discord.utils.escape_markdown(member.display_name)}`)"
                    if member is not None
                    else f"<@{user_id}> (غير موجود حالياً)"
                )
                lines.append(f"**{index}.** {label}")
            embed = discord.Embed(
                title="قائمة الأونرز المساعدين",
                description=f"في **{discord.utils.escape_markdown(guild.name)}**",
                color=0xF1C40F,
            )
            embed.add_field(name="صاحب السيرفر", value=owner_label, inline=False)
            embed.add_field(
                name="مطور البوت",
                value=f"<@{int(developer_id)}>",
                inline=False,
            )
            embed.add_field(
                name=f"الأونرز المساعدون ({len(members)})",
                value="\n".join(lines)[:1024] if lines else "لا يوجد.",
                inline=False,
            )
            embed.set_footer(text="إدارة القائمة للمطور فقط.")
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value == "reset":
            members = store.get(guild.id)
            if not members:
                await interaction.response.send_message(
                    "لا توجد أونرز مساعدين لإزالتهم.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            view = CoOwnerResetView(
                developer_id,
                store,
                guild.id,
                interaction,
            )
            await interaction.response.send_message(
                f"سيُزال جميع الأونرز المساعدين ({len(members)}) من هذا السيرفر. "
                "تُحفظ نسخة احتياطية خاصة قبل أي حذف. هل تريد المتابعة؟",
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    bot._co_owner_commands = owner_command
    return owner_command