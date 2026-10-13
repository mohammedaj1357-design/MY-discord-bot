"""Owner/developer commands for per-server moderation replies."""

from __future__ import annotations

import logging
import random
from typing import Callable

import discord
from discord import app_commands

from banned_words import GuildBannedWordsStore
from funny_responses import DEFAULT_FUNNY_RESPONSES, MAX_FUNNY_RESPONSES
from profanity_filter import ProfanityTermError


logger = logging.getLogger(__name__)


def setup_funny_commands(
    bot,
    store: GuildBannedWordsStore,
    developer_id: int,
    *,
    guild_allowed: Callable[[int], bool] | None = None,
):
    """Register the per-server /funny command group once."""
    existing = getattr(bot, "_funny_command_group", None)
    if existing is not None:
        return existing

    group = app_commands.Group(
        name="funny",
        description="إدارة ردود فلتر الكلمات المحظورة",
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
            logger.exception("Could not check server access for /funny.")
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
            "إدارة الردود متاحة لمطور البوت أو لصاحب السيرفر فقط.",
            ephemeral=True,
        )
        return False

    @group.command(name="add", description="إضافة رد إلى قائمة هذا السيرفر")
    @app_commands.describe(response="رد قصير يظهر بعد حذف رسالة مخالفة")
    @app_commands.guild_only()
    async def add_response(
        interaction: discord.Interaction,
        response: str,
    ) -> None:
        if not await authorize(interaction):
            return
        try:
            count = store.add_funny_response(interaction.guild.id, response)
        except ProfanityTermError as error:
            content = str(error)
        except OSError:
            logger.exception(
                "Could not save funny responses for guild %s.",
                interaction.guild.id,
            )
            content = "تعذر حفظ الرد؛ لم تتغير القائمة."
        else:
            content = f"أُضيف الرد إلى القائمة. عدد الردود المحفوظة الآن: {count}."
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="remove", description="إزالة رد برقم ترتيبه في القائمة")
    @app_commands.describe(index="رقم الرد الظاهر في /funny list")
    @app_commands.guild_only()
    async def remove_response(
        interaction: discord.Interaction,
        index: app_commands.Range[int, 1, MAX_FUNNY_RESPONSES],
    ) -> None:
        if not await authorize(interaction):
            return
        try:
            removed = store.remove_funny_response(interaction.guild.id, index)
        except ProfanityTermError as error:
            content = str(error)
        except OSError:
            logger.exception(
                "Could not save funny responses for guild %s.",
                interaction.guild.id,
            )
            content = "تعذر حفظ التغيير؛ لم تتغير القائمة."
        else:
            content = (
                f"أُزيل الرد رقم {index}: "
                f"«{discord.utils.escape_markdown(removed)}»."
            )
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="list", description="عرض الردود المستخدمة في هذا السيرفر")
    @app_commands.guild_only()
    async def list_responses(interaction: discord.Interaction) -> None:
        if not await authorize(interaction):
            return
        saved = store.get(interaction.guild.id)["funny_responses"]
        using_defaults = not saved
        responses = saved or DEFAULT_FUNNY_RESPONSES
        lines = []
        for index, response in enumerate(responses[:20], start=1):
            shortened = response[:80] + ("…" if len(response) > 80 else "")
            lines.append(
                f"**{index}.** {discord.utils.escape_markdown(shortened)}"
            )
        if len(responses) > 20:
            lines.append(f"… وهناك {len(responses) - 20} رد إضافي.")
        if using_defaults:
            lines.insert(0, "_تُستخدم الردود الافتراضية حالياً._")
        embed = discord.Embed(
            title="ردود فلتر الكلمات",
            description="\n".join(lines),
            color=0x4A90E2,
        )
        embed.set_footer(
            text=f"{len(responses)} رد • استخدم /funny remove لحذف رد"
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="clear", description="إزالة تخصيصات الردود بعد تأكيد")
    @app_commands.describe(confirm="اختر نعم للعودة إلى الردود الافتراضية")
    @app_commands.guild_only()
    async def clear_responses(
        interaction: discord.Interaction,
        confirm: bool = False,
    ) -> None:
        if not await authorize(interaction):
            return
        if not confirm:
            await interaction.response.send_message(
                "لم يتغير شيء. اختر confirm: نعم لإزالة قائمة الردود المحفوظة "
                "والعودة إلى الردود الافتراضية.",
                ephemeral=True,
            )
            return
        try:
            count = store.clear_funny_responses(interaction.guild.id)
        except OSError:
            logger.exception(
                "Could not clear funny responses for guild %s.",
                interaction.guild.id,
            )
            content = "تعذر حفظ التغيير؛ بقيت الردود كما هي."
        else:
            content = (
                f"مُسحت {count} ردود محفوظة لهذا السيرفر. "
                "سيستخدم البوت الردود الافتراضية."
            )
        await interaction.response.send_message(content, ephemeral=True)

    @group.command(name="random", description="معاينة رد عشوائي دون إرساله للأعضاء")
    @app_commands.guild_only()
    async def random_response(interaction: discord.Interaction) -> None:
        if not await authorize(interaction):
            return
        saved = store.get(interaction.guild.id)["funny_responses"]
        response = random.choice(saved or DEFAULT_FUNNY_RESPONSES)
        await interaction.response.send_message(
            f"معاينة: {response}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(
        name="reset",
        description="استبدال القائمة الحالية بالردود الافتراضية بعد تأكيد",
    )
    @app_commands.describe(confirm="اختر نعم لاستبدال القائمة الحالية")
    @app_commands.guild_only()
    async def reset_responses(
        interaction: discord.Interaction,
        confirm: bool = False,
    ) -> None:
        if not await authorize(interaction):
            return
        if not confirm:
            await interaction.response.send_message(
                "لم يتغير شيء. اختر confirm: نعم لاستبدال القائمة الحالية "
                "بالردود الافتراضية.",
                ephemeral=True,
            )
            return
        try:
            count = store.reset_funny_responses(interaction.guild.id)
        except OSError:
            logger.exception(
                "Could not reset funny responses for guild %s.",
                interaction.guild.id,
            )
            content = "تعذر حفظ التغيير؛ بقيت الردود كما هي."
        else:
            content = f"أُعيدت القائمة إلى {count} رداً افتراضياً."
        await interaction.response.send_message(content, ephemeral=True)

    bot.tree.add_command(group)
    bot._funny_command_group = group
    return group