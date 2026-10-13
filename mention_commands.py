"""Developer-only slash commands for mention protection settings."""

from __future__ import annotations

import logging

import discord
from discord import app_commands


logger = logging.getLogger(__name__)


def setup_mention_protection_commands(bot, protection, developer_id: int):
    """Register the /anti command once on the bot's application-command tree."""
    existing = getattr(bot, "_mention_protection_command", None)
    if existing is not None:
        return existing

    @bot.tree.command(name="anti", description="إدارة حماية المنشن")
    @app_commands.guild_only()
    @app_commands.describe(
        action="اختر العملية",
        user="العضو المطلوب",
        role="الرتبة المطلوبة",
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="إضافة محمي", value="add"),
            app_commands.Choice(name="إزالة محمي", value="remove"),
            app_commands.Choice(name="قائمة المحميين", value="list"),
            app_commands.Choice(name="مسح تحذيرات عضو", value="clear_warns"),
            app_commands.Choice(name="إضافة عضو لمنع المنشن الجماعي", value="mass_add_member"),
            app_commands.Choice(name="إزالة عضو من منع المنشن الجماعي", value="mass_remove_member"),
            app_commands.Choice(name="إضافة رتبة لمنع المنشن الجماعي", value="mass_add_role"),
            app_commands.Choice(name="إزالة رتبة من منع المنشن الجماعي", value="mass_remove_role"),
            app_commands.Choice(name="قائمة منع المنشن الجماعي", value="mass_list"),
        ]
    )
    async def anti(
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.Member | None = None,
        role: discord.Role | None = None,
    ) -> None:
        if interaction.user.id != developer_id:
            await interaction.response.send_message(
                "هذا الأمر خاص بمطور البوت فقط.",
                ephemeral=True,
            )
            return

        if action.value == "list":
            protected = protection.list_protected()
            lines = [f"• <@{user_id}> (`{user_id}`)" for user_id in protected[:35]]
            if len(protected) > 35:
                lines.append(f"… وهناك {len(protected) - 35} عضو آخر.")
            await interaction.response.send_message(
                "🛡️ المحميون في جميع السيرفرات:\n"
                + ("\n".join(lines) if lines else "لا يوجد محميون."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value == "mass_list":
            targets = protection.list_mass_mention_targets()
            members = [
                f"• عضو: <@{user_id}> (`{user_id}`)"
                for user_id in targets["members"]
            ]
            roles = [
                f"• رتبة: <@&{role_id}> (`{role_id}`)"
                for role_id in targets["roles"]
            ]
            lines = members + roles
            if not lines:
                lines = ["لا توجد أهداف محددة."]
            header = "🚨 أهداف منع المنشن الجماعي في جميع السيرفرات:"
            continuation_header = "🚨 متابعة أهداف منع المنشن الجماعي:"
            pages = []
            current_page = header
            for line in lines:
                if len(current_page) + len(line) + 1 > 1800:
                    pages.append(current_page)
                    current_page = continuation_header
                current_page += f"\n{line}"
            pages.append(current_page)
            await interaction.response.send_message(
                pages[0],
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for page in pages[1:]:
                await interaction.followup.send(
                    page,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        if action.value in {"mass_add_member", "mass_remove_member"}:
            if user is None:
                await interaction.response.send_message(
                    "حدد العضو المطلوب لهذا الإجراء.",
                    ephemeral=True,
                )
                return
            add = action.value == "mass_add_member"
            try:
                changed = (
                    protection.add_mass_mention_member(user.id)
                    if add
                    else protection.remove_mass_mention_member(user.id)
                )
            except OSError:
                logger.exception("Could not save the mass-mention member list.")
                response = "تعذر حفظ قائمة أعضاء منع المنشن الجماعي؛ لم يتغير الإعداد."
            else:
                name = discord.utils.escape_markdown(user.display_name)
                if changed:
                    verb = "أُضيف" if add else "أُزيل"
                    response = f"{verb} العضو {name} {'إلى' if add else 'من'} قائمة منع المنشن الجماعي."
                else:
                    response = (
                        "هذا العضو موجود بالفعل في قائمة منع المنشن الجماعي."
                        if add
                        else "هذا العضو غير موجود في قائمة منع المنشن الجماعي."
                    )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action.value in {"mass_add_role", "mass_remove_role"}:
            if role is None:
                await interaction.response.send_message(
                    "حدد الرتبة المطلوبة لهذا الإجراء.",
                    ephemeral=True,
                )
                return
            add = action.value == "mass_add_role"
            try:
                changed = (
                    protection.add_mass_mention_role(role.id)
                    if add
                    else protection.remove_mass_mention_role(role.id)
                )
            except OSError:
                logger.exception("Could not save the mass-mention role list.")
                response = "تعذر حفظ قائمة رتب منع المنشن الجماعي؛ لم يتغير الإعداد."
            else:
                name = discord.utils.escape_markdown(role.name)
                if changed:
                    verb = "أُضيفت" if add else "أُزيلت"
                    response = f"{verb} الرتبة {name} {'إلى' if add else 'من'} قائمة منع المنشن الجماعي."
                else:
                    response = (
                        "هذه الرتبة موجودة بالفعل في قائمة منع المنشن الجماعي."
                        if add
                        else "هذه الرتبة غير موجودة في قائمة منع المنشن الجماعي."
                    )
            await interaction.response.send_message(
                response,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if user is None:
            await interaction.response.send_message(
                "حدد العضو المطلوب لهذا الإجراء.",
                ephemeral=True,
            )
            return

        if action.value == "add":
            try:
                added = protection.add_protected(user.id)
            except OSError:
                logger.exception("Could not save the protected user list.")
                response = "تعذر حفظ قائمة المحميين؛ لم يتغير الإعداد."
            else:
                response = (
                    f"أُضيف {discord.utils.escape_markdown(user.display_name)} "
                    "إلى قائمة المحميين."
                    if added
                    else "هذا العضو محمي بالفعل."
                )
        elif action.value == "remove":
            if user.id == developer_id:
                response = "لا يمكن إزالة حماية المطور الأساسي."
            else:
                try:
                    removed = protection.remove_protected(user.id)
                except OSError:
                    logger.exception("Could not save the protected user list.")
                    response = "تعذر حفظ قائمة المحميين؛ لم يتغير الإعداد."
                else:
                    response = (
                        f"أُزيل {discord.utils.escape_markdown(user.display_name)} "
                        "من قائمة المحميين."
                        if removed
                        else "هذا العضو غير موجود في قائمة المحميين."
                    )
        elif action.value == "clear_warns":
            try:
                count = protection.clear_warnings(user.id, interaction.guild_id)
            except OSError:
                logger.exception("Could not save the cleared mention warnings.")
                response = "تعذر حفظ مسح التحذيرات."
            else:
                response = (
                    f"مُسحت {count} تحذيرات لهذا العضو في هذا السيرفر."
                    if count
                    else "لا توجد تحذيرات نشطة لهذا العضو."
                )
        else:
            response = "الإجراء غير معروف."

        await interaction.response.send_message(
            response,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    bot._mention_protection_command = anti
    return anti